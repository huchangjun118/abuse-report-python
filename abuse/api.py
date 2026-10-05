"""HTTP API exposing the workflow.

Authentication is a lightweight principal passed via headers so the service
is self-contained:

* ``X-Actor-Id``   - the acting user/operator id;
* ``X-Actor-Role`` - ``user`` | ``reviewer`` | ``admin`` (default ``user``).

Endpoints
=========
* ``POST /reports``                 submit/dedup a report;
* ``GET  /cases`` / ``/cases/{id}`` inspect cases;
* ``POST /cases/{id}/review``       reviewer takes ownership (manual priority);
* ``POST /cases/{id}/transition``   legal status transition;
* ``POST /cases/{id}/actions``      decide an enforcement action;
* ``GET  /accounts/{id}/enforcement`` explain current state;
* ``POST /actions/{id}/revoke``     change/revoke an action;
* ``POST /cases/{id}/appeals``      owner files an appeal;
* ``POST /appeals/{id}/decision``   reviewer grants/denies;
* ``GET  /audit``                   query the audit trail;
* ``POST /internal/sweep``          run a maintenance tick.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import errors
from .config import Config
from .enums import (
    ActionType,
    CaseStatus,
    Role,
)
from .models import Actor
from .workflow import Workflow


# -- request / response schemas ------------------------------------------
class ReportIn(BaseModel):
    source: str
    target_account_id: str
    event_key: str
    evidence: str = ""


class TransitionIn(BaseModel):
    status: CaseStatus


class ActionIn(BaseModel):
    action_type: ActionType
    reason: str
    basis: str
    effective_at: datetime | None = None
    duration_seconds: int | None = None


class RevokeIn(BaseModel):
    reason: str


class AppealIn(BaseModel):
    reason: str


class AppealDecisionIn(BaseModel):
    grant: bool
    reason: str


def _case_out(case) -> dict:
    return {
        "id": case.id,
        "target_account_id": case.target_account_id,
        "status": case.status.value,
        "priority": case.priority.value,
        "evidence_count": case.evidence_count,
        "version": case.version,
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
    }


def _report_out(report) -> dict:
    return {
        "id": report.id,
        "dedup_key": report.dedup_key,
        "source": report.source,
        "reporter_id": report.reporter_id,
        "target_account_id": report.target_account_id,
        "event_key": report.event_key,
        "case_id": report.case_id,
        "evidence": report.evidence,
        "created_at": report.created_at.isoformat(),
    }


def create_app(
    db_path: str = ":memory:",
    config: Config | None = None,
    start_sweeper: bool = True,
) -> FastAPI:
    config = config or Config.default()
    workflow = Workflow(db_path, config=config, start_sweeper=start_sweeper).start()
    app = FastAPI(title="Abuse Report & Account Action Workflow", version="0.1.0")
    app.state.workflow = workflow

    def actor(
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
        role: str = Header(default="user", alias="X-Actor-Role"),
    ) -> Actor:
        if not actor_id:
            raise HTTPException(status_code=401, detail="X-Actor-Id header required")
        try:
            return Actor(id=actor_id, role=Role(role))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown role {role!r}")

    def _raise(exc: Exception) -> None:
        status = {
            errors.NotFoundError: 404,
            errors.DuplicateError: 409,
            errors.ConflictError: 409,
            errors.ConflictRetryError: 409,
            errors.InvalidStateTransitionError: 409,
            errors.PermissionDeniedError: 403,
            errors.RateLimitedError: 429,
            errors.ValidationError: 422,
        }.get(type(exc), 500)
        raise HTTPException(status_code=status, detail=str(exc))

    @app.on_event("shutdown")
    def _shutdown() -> None:  # pragma: no cover
        workflow.close()

    @app.get("/")
    def root() -> dict:
        return {"service": "abuse-workflow", "status": "ok"}

    @app.post("/reports")
    def submit_report(body: ReportIn, principal: Actor = actor_dep()):
        try:
            result = workflow.submit_report(
                principal,
                body.source,
                body.target_account_id,
                body.event_key,
                body.evidence,
            )
        except errors.AbuseError as exc:  # type: ignore[misc]
            _raise(exc)
        return {
            "report": _report_out(result["report"]),
            "case": _case_out(result["case"]),
            "is_new": result["is_new"],
            "duplicate_count": result["duplicate_count"],
        }

    @app.get("/cases")
    def list_cases(principal: Actor = actor_dep()):
        return {"cases": [_case_out(c) for c in workflow.repo.list_cases()]}

    @app.get("/cases/{case_id}")
    def get_case(case_id: str, principal: Actor = actor_dep()):
        try:
            return _case_out(workflow.cases.get(case_id))
        except errors.AbuseError as exc:  # type: ignore[misc]
            _raise(exc)

    @app.post("/cases/{case_id}/review")
    def review_case(case_id: str, principal: Actor = actor_dep()):
        try:
            return _case_out(workflow.begin_review(principal, case_id))
        except errors.AbuseError as exc:  # type: ignore[misc]
            _raise(exc)

    @app.post("/cases/{case_id}/transition")
    def transition_case(case_id: str, body: TransitionIn, principal: Actor = actor_dep()):
        try:
            return _case_out(
                workflow.cases.transition(principal, case_id, body.status)
            )
        except errors.AbuseError as exc:  # type: ignore[misc]
            _raise(exc)

    @app.post("/cases/{case_id}/actions")
    def decide_action(case_id: str, body: ActionIn, principal: Actor = actor_dep()):
        try:
            action = workflow.actions.decide(
                principal,
                case_id,
                body.action_type,
                body.reason,
                body.basis,
                effective_at=body.effective_at,
                duration_seconds=body.duration_seconds,
            )
        except errors.AbuseError as exc:  # type: ignore[misc]
            _raise(exc)
        return _action_out(action)

    @app.post("/actions/{action_id}/revoke")
    def revoke_action(action_id: str, body: RevokeIn, principal: Actor = actor_dep()):
        try:
            return _action_out(
                workflow.actions.revoke(principal, action_id, body.reason)
            )
        except errors.AbuseError as exc:  # type: ignore[misc]
            _raise(exc)

    @app.get("/accounts/{account_id}/enforcement")
    def enforcement(account_id: str, principal: Actor = actor_dep()):
        return workflow.actions.explain(account_id)

    @app.post("/cases/{case_id}/appeals")
    def file_appeal(case_id: str, body: AppealIn, principal: Actor = actor_dep()):
        try:
            appeal = workflow.appeals.file(principal, case_id, body.reason)
        except errors.AbuseError as exc:  # type: ignore[misc]
            _raise(exc)
        return _appeal_out(appeal)

    @app.post("/appeals/{appeal_id}/decision")
    def decide_appeal(
        appeal_id: str, body: AppealDecisionIn, principal: Actor = actor_dep()
    ):
        try:
            appeal = workflow.appeals.decide_appeal(
                principal, appeal_id, body.grant, body.reason
            )
        except errors.AbuseError as exc:  # type: ignore[misc]
            _raise(exc)
        return _appeal_out(appeal)

    @app.get("/audit")
    def audit(
        entity_type: str | None = None,
        entity_id: str | None = None,
        principal: Actor = actor_dep(),
    ):
        return {
            "entries": [
                {
                    "id": e.id,
                    "ts": e.ts.isoformat(),
                    "actor_id": e.actor_id,
                    "action": e.action,
                    "entity_type": e.entity_type,
                    "entity_id": e.entity_id,
                    "allowed": e.allowed,
                    "detail": e.detail,
                }
                for e in workflow.audit_trail(entity_type, entity_id)
            ]
        }

    @app.post("/internal/sweep")
    def sweep(principal: Actor = actor_dep()):
        return workflow.sweep()

    return app


def actor_dep():
    """FastAPI dependency wrapper (kept as a function to avoid header default
    sharing issues in tests)."""
    from fastapi import Depends

    def _dep(
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
        role: str = Header(default="user", alias="X-Actor-Role"),
    ) -> Actor:
        if not actor_id:
            raise HTTPException(status_code=401, detail="X-Actor-Id header required")
        try:
            return Actor(id=actor_id, role=Role(role))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown role {role!r}")

    return Depends(_dep)


def _action_out(action) -> dict:
    return {
        "id": action.id,
        "case_id": action.case_id,
        "account_id": action.account_id,
        "action_type": action.action_type.value,
        "status": action.status.value,
        "auto": action.auto,
        "reason": action.reason,
        "basis": action.basis,
        "effective_at": action.effective_at.isoformat(),
        "expires_at": action.expires_at.isoformat() if action.expires_at else None,
        "decided_by": action.decided_by,
        "created_at": action.created_at.isoformat(),
        "applied_at": action.applied_at.isoformat() if action.applied_at else None,
        "released_at": action.released_at.isoformat() if action.released_at else None,
    }


def _appeal_out(appeal) -> dict:
    return {
        "id": appeal.id,
        "case_id": appeal.case_id,
        "account_id": appeal.account_id,
        "status": appeal.status.value,
        "reason": appeal.reason,
        "created_by": appeal.created_by,
        "created_at": appeal.created_at.isoformat(),
        "decided_by": appeal.decided_by,
        "decided_at": appeal.decided_at.isoformat() if appeal.decided_at else None,
        "decision_reason": appeal.decision_reason,
    }
