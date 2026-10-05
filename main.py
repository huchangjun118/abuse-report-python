"""举报受理与账号处置审核工作流 HTTP 接口。

身份与角色通过请求头传入（示例部署中由网关注入）：
- X-Actor: 操作人账号
- X-Role:  reporter / reviewer / admin
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from abuse import (
    AbuseService,
    ActionType,
    InvalidTransition,
    NotFound,
    PermissionDenied,
    RateLimited,
    Role,
    WorkflowConfig,
)

service: AbuseService


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    service = AbuseService(
        WorkflowConfig(db_path=os.environ.get("ABUSE_DB_PATH", "abuse.db"))
    )
    yield
    service.close()


app = FastAPI(title="Abuse Report Workflow", lifespan=lifespan)


@app.exception_handler(PermissionDenied)
async def _forbidden(_, exc: PermissionDenied):
    raise HTTPException(status_code=403, detail=str(exc))


@app.exception_handler(NotFound)
async def _not_found(_, exc: NotFound):
    raise HTTPException(status_code=404, detail=str(exc))


@app.exception_handler(InvalidTransition)
async def _conflict(_, exc: InvalidTransition):
    raise HTTPException(status_code=409, detail=str(exc))


@app.exception_handler(RateLimited)
async def _rate_limited(_, exc: RateLimited):
    raise HTTPException(status_code=429, detail=str(exc))


def _role(x_role: str) -> Role:
    try:
        return Role(x_role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown role {x_role}")


class ReportIn(BaseModel):
    reporter_id: str
    target_account: str
    event_id: str
    reason: str
    evidence: list[str] = Field(default_factory=list)
    idempotency_key: Optional[str] = None


class DecideIn(BaseModel):
    action: ActionType
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    duration: Optional[float] = None


class ReasonIn(BaseModel):
    reason: str = ""


class AppealDecisionIn(BaseModel):
    uphold: bool
    reason: str


@app.post("/reports", status_code=201)
def submit_report(body: ReportIn):
    r = service.submit_report(
        body.reporter_id, body.target_account, body.event_id,
        body.reason, body.evidence, body.idempotency_key,
    )
    return {"report_id": r.report_id, "case_id": r.case_id, "status": r.status.value}


@app.post("/cases/{case_id}/triage")
def triage(case_id: str, body: ReasonIn,
           x_actor: str = Header(...), x_role: str = Header(...)):
    case = service.triage_case(x_actor, _role(x_role), case_id, body.reason)
    return {"case_id": case.case_id, "status": case.status.value}


@app.post("/cases/{case_id}/reject")
def reject(case_id: str, body: ReasonIn,
           x_actor: str = Header(...), x_role: str = Header(...)):
    case = service.reject_case(x_actor, _role(x_role), case_id, body.reason)
    return {"case_id": case.case_id, "status": case.status.value}


@app.post("/cases/{case_id}/decide")
def decide(case_id: str, body: DecideIn,
           x_actor: str = Header(...), x_role: str = Header(...)):
    s = service.decide(x_actor, _role(x_role), case_id, body.action,
                       body.reason, body.evidence_refs, body.duration)
    return {"sanction_id": s.sanction_id, "action": s.action.value,
            "effective_at": s.effective_at, "expires_at": s.expires_at}


@app.post("/cases/{case_id}/appeals", status_code=201)
def file_appeal(case_id: str, body: ReasonIn, x_actor: str = Header(...)):
    a = service.file_appeal(x_actor, case_id, body.reason)
    return {"appeal_id": a.appeal_id, "status": a.status.value}


@app.post("/appeals/{appeal_id}/decide")
def decide_appeal(appeal_id: str, body: AppealDecisionIn,
                  x_actor: str = Header(...), x_role: str = Header(...)):
    a = service.decide_appeal(x_actor, _role(x_role), appeal_id,
                              body.uphold, body.reason)
    return {"appeal_id": a.appeal_id, "status": a.status.value}


@app.post("/cases/{case_id}/resolve")
def resolve(case_id: str, body: ReasonIn,
            x_actor: str = Header(...), x_role: str = Header(...)):
    case = service.resolve_case(x_actor, _role(x_role), case_id, body.reason)
    return {"case_id": case.case_id, "status": case.status.value}


@app.get("/cases/{case_id}")
def get_case(case_id: str):
    c = service.get_case(case_id)
    return {
        "case_id": c.case_id, "target_account": c.target_account,
        "status": c.status.value, "report_count": c.report_count,
        "unique_reporters": c.unique_reporters, "version": c.version,
    }


@app.get("/accounts/{account}/sanction")
def get_sanction(account: str):
    s = service.active_sanction(account)
    if s is None:
        return {"active": False}
    return {"active": True, "action": s.action.value, "reason": s.reason,
            "effective_at": s.effective_at, "expires_at": s.expires_at}


@app.get("/audit")
def audit(target: Optional[str] = None):
    return [
        {"ts": e.ts, "actor": e.actor, "role": e.role, "action": e.action,
         "target": e.target, "reason": e.reason, "detail": e.detail}
        for e in service.audit_trail(target)
    ]


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
