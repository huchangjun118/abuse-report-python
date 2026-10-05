"""Crash recovery.

Because every state change is committed transactionally before its side
effect is considered done, restart recovery only has to realign derived
state with the durable records:

1. PENDING actions whose ``effective_at`` has passed are activated;
2. ACTIVE timed actions whose ``expires_at`` has passed are released;
3. every account with decisions is re-converged to the unique winner;
4. cases are aligned: an ACTIVE non-revoked action implies ``actioned``;
   an UNDECIDED PENDING appeal leaves the case ``appealed``; a granted
   appeal with no active action means ``open``.

The procedure is idempotent: interrupting and rerunning it converges to the
same state, and enforcement actions are never executed twice (CAS on
status prevents double activation/release).
"""

from __future__ import annotations

from .clock import Clock
from .enums import ActionStatus, AppealStatus, CaseStatus
from .models import Actor
from .repository import Repository


class RecoveryService:
    def __init__(self, repo: Repository, config, actions, clock: Clock | None = None):
        self.repo = repo
        self.config = config
        self.actions = actions
        self.clock = clock or Clock()

    def recover(self) -> dict:
        now = self.clock.now()
        report = {
            "activated": [],
            "released": [],
            "converged_accounts": [],
            "aligned_cases": [],
        }
        with self.repo.lock:
            # 1 & 2: finish time-driven action side effects (idempotent CAS).
            report["activated"] = self.actions.activate_pending(now)
            report["released"] = self.actions.release_expired(now)

            # 3: reconverge every account that ever had a decision.
            accounts = {
                a.account_id
                for a in self.repo.list_actions_by_status(ActionStatus.ACTIVE)
            }
            accounts |= {
                a.account_id
                for a in self.repo.list_actions_by_status(ActionStatus.PENDING)
            }
            for account_id in sorted(accounts):
                self.actions.converge(account_id, now, None)
                report["converged_accounts"].append(account_id)

            # 4: align case status with durable decisions/appeals.
            for case in self.repo.list_cases():
                aligned = self._align_case(case, now)
                if aligned is not None:
                    report["aligned_cases"].append(aligned)
        return report

    def _align_case(self, case, now) -> str | None:
        active = self.repo.get_active_action(case.target_account_id)
        appeal = self.repo.get_appeal(case.id, case.target_account_id)

        target_status = case.status
        if appeal is not None and appeal.status == AppealStatus.PENDING:
            target_status = CaseStatus.APPEALED
        elif active is not None:
            target_status = CaseStatus.ACTIONED
        elif appeal is not None and appeal.status == AppealStatus.GRANTED:
            target_status = CaseStatus.OPEN
        elif (
            appeal is not None
            and appeal.status == AppealStatus.DENIED
            and active is None
        ):
            # denied appeal but the action expired naturally: no enforcement
            target_status = CaseStatus.OPEN
        elif case.status == CaseStatus.ACTIONED and active is None:
            # the only enforcement expired or was revoked while we were down
            target_status = CaseStatus.OPEN

        if target_status != case.status and target_status in _recovery_allowed(case.status):
            updated = self.repo.cas_case(
                case.id,
                case.version,
                status=target_status,
                now=now,
            )
            if updated is not None:
                system = Actor(id="system:recovery", role=_system_role())
                self.actions.auditor.record(
                    system,
                    "case.recover",
                    "case",
                    case.id,
                    True,
                    f"aligned {case.status.value} -> {target_status.value} on startup",
                )
                return case.id
        return None


def _recovery_allowed(status: CaseStatus) -> frozenset[CaseStatus]:
    # Recovery may only heal obvious mismatches; it respects the machine and
    # never forces CLOSED open unless the target relationship itself demands it.
    from .cases import ALLOWED_TRANSITIONS

    return ALLOWED_TRANSITIONS.get(status, frozenset())


def _system_role():
    from .enums import Role

    return Role.SYSTEM
