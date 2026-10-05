"""Report ingestion, case aggregation and the case state machine.

State machine::

    open -> in_review | actioned | closed
    in_review -> open | actioned | appealed | closed
    actioned -> appealed | closed
    appealed -> actioned | closed
    closed -> open

Any other transition is rejected and audited. Compare-and-set updates make
sure two concurrent reviewers can never roll a case backwards or mix fields.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from .clock import Clock
from .enums import CaseStatus, Priority
from .errors import (
    ConflictRetryError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from .idempotency import report_dedup_key
from .models import Actor, Case, Report
from .permissions import REVIEW_CASE, SUBMIT_REPORT, can, require
from .repository import Repository

ALLOWED_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.OPEN: frozenset(
        {CaseStatus.IN_REVIEW, CaseStatus.ACTIONED, CaseStatus.CLOSED}
    ),
    CaseStatus.IN_REVIEW: frozenset(
        {CaseStatus.OPEN, CaseStatus.ACTIONED, CaseStatus.APPEALED, CaseStatus.CLOSED}
    ),
    CaseStatus.ACTIONED: frozenset(
        {CaseStatus.OPEN, CaseStatus.APPEALED, CaseStatus.CLOSED}
    ),
    CaseStatus.APPEALED: frozenset(
        {CaseStatus.OPEN, CaseStatus.ACTIONED, CaseStatus.CLOSED}
    ),
    CaseStatus.CLOSED: frozenset({CaseStatus.OPEN}),
}


class CaseService:
    def __init__(
        self,
        repo: Repository,
        auditor,
        clock: Clock | None = None,
        auto_evaluator: Callable[[Case], None] | None = None,
    ) -> None:
        self.repo = repo
        self.auditor = auditor
        self.clock = clock or Clock()
        self.auto_evaluator = auto_evaluator

    # -- ingestion -----------------------------------------------------
    def submit_report(
        self,
        actor: Actor,
        source: str,
        target_account_id: str,
        event_key: str,
        evidence: str,
    ) -> tuple[Report, Case, bool, int]:
        """Submit (or retry) a report.

        Returns ``(report, case, is_new, duplicate_count)``. Identical
        submissions are merged: they never double-count evidence nor trigger
        follow-up work twice.
        """
        if not source or not target_account_id or not event_key:
            raise ValidationError("source, target_account_id and event_key are required")
        require(actor, SUBMIT_REPORT)

        dedup_key = report_dedup_key(source, actor.id, event_key)
        existing = self.repo.get_report(dedup_key)
        if existing is not None:
            # Idempotent replay: merge into the counter, run no side effects.
            placeholder = Report(
                id=existing.id,
                dedup_key=dedup_key,
                source=source,
                reporter_id=actor.id,
                target_account_id=target_account_id,
                event_key=event_key,
                case_id=existing.case_id,
                evidence=evidence,
                created_at=self.clock.now(),
            )
            stored, _is_new, count = self.repo.upsert_report(placeholder)
            case = self.repo.get_case(stored.case_id)
            assert case is not None
            self.auditor.record(
                actor,
                "report.submit.duplicate",
                "report",
                stored.id,
                True,
                f"merged duplicate #{count} for event {event_key}",
            )
            return stored, case, False, count

        case = self._get_or_create_case(actor, target_account_id)
        now = self.clock.now()
        # Between get/create and insert another thread may have committed the
        # authoritative case for this target. Re-read and, if ours lost the
        # race, attach this report to the winning case so evidence can never
        # land on an orphaned case.
        canonical = self.repo.get_case_for_target(target_account_id)
        if canonical is not None and canonical.id != case.id:
            case = canonical
        report = Report(
            id=f"rpt_{uuid.uuid4().hex[:16]}",
            dedup_key=dedup_key,
            source=source,
            reporter_id=actor.id,
            target_account_id=target_account_id,
            event_key=event_key,
            case_id=case.id,
            evidence=evidence,
            created_at=now,
        )
        stored, is_new, count = self.repo.upsert_report(report)
        assert is_new
        self._add_evidence(case)
        case = self.repo.get_case(case.id)
        assert case is not None
        self.auditor.record(
            actor,
            "report.submit",
            "report",
            stored.id,
            True,
            f"aggregated into case {case.id}, evidence={case.evidence_count}",
        )
        if self.auto_evaluator is not None:
            self.auto_evaluator(case)
        return stored, case, True, 1

    def _get_or_create_case(self, actor: Actor, target_account_id: str) -> Case:
        existing = self.repo.get_case_for_target(target_account_id)
        if existing is not None:
            return existing
        now = self.clock.now()
        case = Case(
            id=f"case_{uuid.uuid4().hex[:16]}",
            target_account_id=target_account_id,
            status=CaseStatus.OPEN,
            priority=Priority.MANUAL,
            evidence_count=0,
            created_at=now,
            updated_at=now,
            version=0,
        )
        # UNIQUE(target_account_id) makes concurrent first-reports converge.
        try:
            return self.repo.insert_case(case)
        except Exception:
            found = self.repo.get_case_for_target(target_account_id)
            if found is not None:
                return found
            raise

    def _add_evidence(self, case: Case) -> None:
        """Increment evidence with CAS retry under a contended case."""
        current = case
        for _ in range(100):
            updated = self.repo.cas_case(
                current.id,
                current.version,
                evidence_delta=1,
                now=self.clock.now(),
            )
            if updated is not None:
                return
            refreshed = self.repo.get_case(current.id)
            if refreshed is None:
                raise NotFoundError(f"case {current.id} vanished")
            current = refreshed
        raise ConflictRetryError("case evidence update contended too many times")

    # -- state machine -------------------------------------------------
    def transition(
        self,
        actor: Actor,
        case_id: str,
        new_status: CaseStatus,
        expected_version: int | None = None,
    ) -> Case:
        case = self.repo.get_case(case_id)
        if case is None:
            raise NotFoundError(f"case {case_id} not found")
        if not can(actor, REVIEW_CASE):
            self.auditor.record(
                actor,
                "case.transition.denied",
                "case",
                case_id,
                False,
                f"role '{actor.role.value}' may not perform 'review_case'",
            )
        require(actor, REVIEW_CASE)
        # A caller acting on a stale snapshot is rejected up front; CAS below
        # still closes the race if two commits interleave cross-process.
        if expected_version is not None and case.version != expected_version:
            raise ConflictRetryError(
                f"case {case_id} moved (v{expected_version} -> v{case.version}); retry"
            )
        allowed = new_status in ALLOWED_TRANSITIONS[case.status]
        if not allowed:
            self.auditor.record(
                actor,
                "case.transition.denied",
                "case",
                case_id,
                False,
                f"{case.status.value} -> {new_status.value} is not allowed",
            )
            raise InvalidStateTransitionError(
                f"cannot move case {case_id} from {case.status.value} to {new_status.value}"
            )
        updated = self.repo.cas_case(
            case_id, case.version, status=new_status, now=self.clock.now()
        )
        if updated is None:
            # Another writer committed first; never silently overwrite.
            raise ConflictRetryError(
                f"case {case_id} changed concurrently, retry the transition"
            )
        self.auditor.record(
            actor,
            "case.transition",
            "case",
            case_id,
            True,
            f"{case.status.value} -> {new_status.value}",
        )
        return updated

    def set_priority(self, actor: Actor, case_id: str, priority: Priority) -> Case:
        case = self.repo.get_case(case_id)
        if case is None:
            raise NotFoundError(f"case {case_id} not found")
        updated = self.repo.cas_case(
            case_id, case.version, priority=priority, now=self.clock.now()
        )
        if updated is None:
            raise ConflictRetryError(f"case {case_id} changed concurrently")
        self.auditor.record(
            actor,
            "case.priority",
            "case",
            case_id,
            True,
            f"priority -> {priority.value}",
        )
        return updated

    def get(self, case_id: str) -> Case:
        case = self.repo.get_case(case_id)
        if case is None:
            raise NotFoundError(f"case {case_id} not found")
        return case
