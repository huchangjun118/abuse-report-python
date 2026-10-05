"""Appeal workflow.

Rules
=====
* only the sanctioned account owner (``actor.id`` equals the case target and
  the actor is a plain user principal) may file an appeal;
* one appeal per case; filing again while it is pending is rejected, while
  re-appealing after a denial is also rejected to keep the outcome final;
* reviewers/admins decide appeals. The decision is compare-and-set on
  ``status = pending``, so an appeal racing a new report or a second reviewer
  resolves to exactly one outcome;
* a granted appeal revokes the active action and reopens the case; a denied
  appeal leaves enforcement in place. Every step is audited.
"""

from __future__ import annotations

import uuid

from .clock import Clock
from .enums import (
    ActionStatus,
    AppealStatus,
    CaseStatus,
    Role,
)
from .errors import (
    ConflictRetryError,
    DuplicateError,
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from .models import Actor, Appeal
from .permissions import DECIDE_APPEAL, FILE_APPEAL, can, require
from .repository import Repository

FILEABLE_STATUSES = frozenset({CaseStatus.ACTIONED, CaseStatus.APPEALED})


class AppealService:
    def __init__(self, repo: Repository, config, auditor, clock: Clock | None = None):
        self.repo = repo
        self.config = config
        self.auditor = auditor
        self.clock = clock or Clock()

    def file(self, actor: Actor, case_id: str, reason: str) -> Appeal:
        require(actor, FILE_APPEAL)
        case = self.repo.get_case(case_id)
        if case is None:
            raise NotFoundError(f"case {case_id} not found")
        # Identity check: only the sanctioned account owner may appeal.
        if actor.id != case.target_account_id or actor.role != Role.USER:
            self.auditor.record(
                actor,
                "appeal.file.denied",
                "case",
                case_id,
                False,
                f"actor {actor.id} is not the sanctioned owner {case.target_account_id}",
            )
            raise PermissionDeniedError(
                "only the sanctioned account owner may file an appeal"
            )
        if case.status not in FILEABLE_STATUSES:
            raise InvalidStateTransitionError(
                f"cannot appeal a case in status {case.status.value}"
            )
        if not reason:
            raise ValidationError("appeal reason is required")

        existing = self.repo.get_appeal(case_id, actor.id)
        if existing is not None:
            raise DuplicateError(
                f"appeal {existing.id} for case {case_id} already exists "
                f"(status {existing.status.value})"
            )

        appeal = Appeal(
            id=f"apl_{uuid.uuid4().hex[:16]}",
            case_id=case_id,
            account_id=actor.id,
            status=AppealStatus.PENDING,
            reason=reason,
            created_by=actor.id,
            created_at=self.clock.now(),
        )
        try:
            self.repo.insert_appeal(appeal)
        except Exception:
            raise DuplicateError(f"appeal for case {case_id} already exists")

        # Mark the case appealed (actioned -> appealed is the legal edge).
        if case.status == CaseStatus.ACTIONED:
            updated = self.repo.cas_case(
                case_id, case.version, status=CaseStatus.APPEALED, now=self.clock.now()
            )
            if updated is None:
                # A concurrent transition happened; the appeal row still stands,
                # surface a retryable conflict rather than a half-done state.
                raise ConflictRetryError("case changed while filing appeal; retry")
        self.auditor.record(
            actor,
            "appeal.file",
            "appeal",
            appeal.id,
            True,
            f"owner {actor.id} appealed case {case_id}: {reason!r}",
        )
        return appeal

    def decide_appeal(
        self, actor: Actor, appeal_id: str, grant: bool, reason: str
    ) -> Appeal:
        appeal = self.repo.get_appeal_by_id(appeal_id)
        if appeal is None:
            raise NotFoundError(f"appeal {appeal_id} not found")
        if not can(actor, DECIDE_APPEAL):
            self.auditor.record(
                actor,
                "appeal.decide.denied",
                "appeal",
                appeal_id,
                False,
                f"role '{actor.role.value}' may not decide appeals",
            )
        require(actor, DECIDE_APPEAL)
        if not reason:
            raise ValidationError("decision reason is required")

        new_status = AppealStatus.GRANTED if grant else AppealStatus.DENIED
        ok = self.repo.cas_appeal_decision(
            appeal_id,
            new_status,
            decided_by=actor.id,
            reason=reason,
            now=self.clock.now(),
        )
        if not ok:
            current = self.repo.get_appeal_by_id(appeal_id)
            assert current is not None
            raise ConflictRetryError(
                f"appeal already decided as {current.status.value}"
            )

        case = self.repo.get_case(appeal.case_id)
        assert case is not None
        if grant:
            self._grant(appeal, case, actor, reason)
        else:
            self._deny(appeal, case, actor, reason)
        stored = self.repo.get_appeal_by_id(appeal_id)
        assert stored is not None
        return stored

    def _grant(self, appeal: Appeal, case, actor: Actor, reason: str) -> None:
        now = self.clock.now()
        active = self.repo.get_active_action(case.target_account_id)
        if active is not None:
            self.repo.cas_action_status(
                active.id, ActionStatus.ACTIVE, ActionStatus.REVOKED, now
            )
            self.auditor.record(
                actor,
                "action.revoke",
                "action",
                active.id,
                True,
                f"revoked by granted appeal {appeal.id}: {reason}",
            )
        # appealed -> actioned would mean still sanctioned; granted means the
        # case is reopened for handling with no active enforcement.
        if case.status == CaseStatus.APPEALED:
            self.repo.cas_case(
                case.id, case.version, status=CaseStatus.OPEN, now=now
            )
        self.auditor.record(
            actor,
            "appeal.grant",
            "appeal",
            appeal.id,
            True,
            f"granted by {actor.id}: {reason!r}; enforcement lifted",
        )

    def _deny(self, appeal: Appeal, case, actor: Actor, reason: str) -> None:
        now = self.clock.now()
        if case.status == CaseStatus.APPEALED:
            self.repo.cas_case(
                case.id, case.version, status=CaseStatus.ACTIONED, now=now
            )
        self.auditor.record(
            actor,
            "appeal.deny",
            "appeal",
            appeal.id,
            True,
            f"denied by {actor.id}: {reason!r}; enforcement stands",
        )
