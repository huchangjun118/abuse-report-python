"""Rate limiting and configurable automatic escalation.

Two independent protections
============================
1. *Reporter rate limit*: a single reporter may only create ``N`` reports in a
   sliding window. Flooding is rejected (and audited) so retries or a runaway
   client cannot spam the pipeline. Crucially rate-limit rejections happen
   *before* evidence is aggregated, so one flooding reporter can never push a
   target over an escalation threshold.
2. *Distinct-reporter escalation*: thresholds count **distinct reporter ids**
   against a target within a window. Reaching ``auto_escalate_distinct_reporters``
   puts the case into review with AUTO priority; reaching
   ``auto_ban_distinct_reporters`` issues an automatic timed ban.

Manual review always wins: a case a human has already touched (priority
MANUAL while IN_REVIEW/ACTIONED) is never auto-banned, and any converged
manual action beats an automatic one.
"""

from __future__ import annotations

from datetime import timedelta

from .clock import Clock
from .enums import (
    ActionType,
    CaseStatus,
    Priority,
)
from .errors import RateLimitedError
from .models import Actor, Case
from .repository import Repository

ESCALATE_MIN_DISTINCT = 3


class ThresholdService:
    def __init__(self, repo: Repository, config, auditor, actions, clock: Clock | None = None):
        self.repo = repo
        self.config = config
        self.auditor = auditor
        self.actions = actions
        self.clock = clock or Clock()

    def check_reporter_rate(self, actor: Actor) -> None:
        now = self.clock.now()
        window_start = now - timedelta(seconds=self.config.rate_limit_window_seconds)
        # prune + count in one serialised step
        current = self.repo.prune_and_count_hits(actor.id, window_start)
        if current >= self.config.rate_limit_max_per_reporter:
            self.auditor.record(
                actor,
                "report.rate_limited",
                "reporter",
                actor.id,
                False,
                f">{self.config.rate_limit_max_per_reporter} reports within "
                f"{self.config.rate_limit_window_seconds}s",
            )
            raise RateLimitedError(
                f"reporter {actor.id} exceeded "
                f"{self.config.rate_limit_max_per_reporter} reports / "
                f"{self.config.rate_limit_window_seconds}s"
            )
        self.repo.add_reporter_hit(actor.id, now)

    def evaluate(self, actor: Actor, case: Case) -> Case | None:
        """Run automatic escalation after a new (non-duplicate) report.

        Returns the updated case if an escalation happened, else None.
        """
        if not self.config.auto_action_enabled:
            return None
        now = self.clock.now()
        since = now - timedelta(seconds=self.config.auto_escalate_window_seconds)
        distinct = self.repo.count_distinct_reporters(case.target_account_id, since)

        # Auto-ban threshold: enough *different* people reported, and no human
        # has already taken ownership of this case.
        if distinct >= self.config.auto_ban_distinct_reporters:
            if self._human_owns(case):
                self.auditor.record(
                    actor,
                    "escalate.auto_ban.skipped_manual",
                    "case",
                    case.id,
                    True,
                    f"{distinct} distinct reporters but case under manual handling",
                )
                return None
            self._escalate_priority(case, Priority.AUTO, actor)
            existing_active = self.repo.get_active_action(case.target_account_id)
            if existing_active is None or existing_active.auto is False:
                # A human's active action must never be replaced by the system.
                if existing_active is not None:
                    return None
                system = Actor(id="system:auto-escalation", role=_system_role())
                self.actions.decide(
                    system,
                    case.id,
                    ActionType.BAN_TIMED,
                    reason=(
                        f"automatic escalation: {distinct} distinct reporters "
                        f"within {self.config.auto_escalate_window_seconds}s"
                    ),
                    basis=f"reports(distinct_reporters={distinct})",
                    auto=True,
                )
                refreshed = self.repo.get_case(case.id)
                return refreshed
            return None

        if distinct >= self.config.auto_escalate_distinct_reporters:
            return self._escalate_priority(case, Priority.AUTO, actor)
        return None

    def _human_owns(self, case: Case) -> bool:
        """True once a human has taken over: manual priority while the case is
        being reviewed/acted on."""
        case = self.repo.get_case(case.id) or case
        if case.priority == Priority.MANUAL and case.status in (
            CaseStatus.IN_REVIEW,
            CaseStatus.ACTIONED,
            CaseStatus.APPEALED,
        ):
            return True
        active = self.repo.get_active_action(case.target_account_id)
        return active is not None and not active.auto

    def _escalate_priority(self, case: Case, priority: Priority, actor: Actor) -> Case | None:
        current = self.repo.get_case(case.id)
        if current is None:
            return None
        # Do not demote a case a human is handling.
        if current.priority == Priority.MANUAL and current.status != CaseStatus.OPEN:
            return None
        status = current.status
        if status == CaseStatus.OPEN:
            status = CaseStatus.IN_REVIEW
        updated = self.repo.cas_case(
            current.id,
            current.version,
            status=status,
            priority=priority,
            now=self.clock.now(),
        )
        if updated is None:
            return None
        self.auditor.record(
            actor,
            "escalate.auto",
            "case",
            case.id,
            True,
            f"auto escalation to {status.value}/{priority.value}",
        )
        return updated


def _system_role():
    from .enums import Role

    return Role.SYSTEM
