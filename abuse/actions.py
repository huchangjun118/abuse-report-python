"""Account enforcement decisions, convergence and automatic release.

Decisions
=========
* ``warn``        - no expiry, informational;
* ``mute``        - time-boxed, defaults from config;
* ``ban_timed``   - time-boxed, defaults from config;
* ``ban_perm``    - permanent, no expiry.

Every decision carries ``effective_at`` and ``expires_at``, the deciding
operator, a human reason and the evidence basis.

Convergence
===========
An account has **at most one ACTIVE action** (enforced by a partial unique
index). When concurrent decisions exist they converge deterministically:

1. manual decisions always beat automatic ones (human review has priority);
2. higher severity wins (warn < mute < ban_timed < ban_perm);
3. on equal severity, the later decision wins (and the later creation
   timestamp, tie-broken by id, is the same across retries).

A decision whose effective time is in the future stays PENDING until its
effective time; the sweeper activates it, so a crash cannot lose or replay
the "apply" side effect.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from .clock import Clock
from .enums import (
    ActionStatus,
    ActionType,
    CaseStatus,
)
from .errors import (
    ConflictRetryError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from .models import Action, Actor
from .permissions import CHANGE_ACTION, DECIDE_ACTION, can, require
from .repository import Repository

SEVERITY: dict[ActionType, int] = {
    ActionType.WARN: 1,
    ActionType.MUTE: 2,
    ActionType.BAN_TIMED: 3,
    ActionType.BAN_PERM: 4,
}

# Cases on which a decision may be recorded/changed.
DECIDABLE_STATUSES = frozenset(
    {CaseStatus.OPEN, CaseStatus.IN_REVIEW, CaseStatus.ACTIONED, CaseStatus.APPEALED}
)


class ActionService:
    def __init__(self, repo: Repository, config, auditor, clock: Clock | None = None):
        self.repo = repo
        self.config = config
        self.auditor = auditor
        self.clock = clock or Clock()

    # -- decisions -----------------------------------------------------
    def decide(
        self,
        actor: Actor,
        case_id: str,
        action_type: ActionType,
        reason: str,
        basis: str,
        effective_at: datetime | None = None,
        duration_seconds: int | None = None,
        auto: bool = False,
    ) -> Action:
        if not auto:
            case = self.repo.get_case(case_id)
            if case is None:
                raise NotFoundError(f"case {case_id} not found")
            if not can(actor, DECIDE_ACTION):
                self.auditor.record(
                    actor, "action.decide.denied", "case", case_id, False,
                    f"role '{actor.role.value}' may not decide enforcement",
                )
            require(actor, DECIDE_ACTION)
        else:
            case = self.repo.get_case(case_id)
            if case is None:
                raise NotFoundError(f"case {case_id} not found")
        # Enforcement decisions are recorded independently of the exact case
        # status (reports and decisions race). Only an explicitly closed case
        # refuses a new decision; the case is aligned to 'actioned' best-effort
        # when the decision becomes active.
        if case.status == CaseStatus.CLOSED:
            raise InvalidStateTransitionError(
                "cannot record a new enforcement decision on a closed case"
            )
        if not reason or not basis:
            raise ValidationError("reason and evidence basis are required")

        now = self.clock.now()
        effective_at = effective_at or now
        expires_at = self._compute_expiry(action_type, effective_at, duration_seconds)

        action = Action(
            id=f"act_{uuid.uuid4().hex[:16]}",
            case_id=case_id,
            account_id=case.target_account_id,
            action_type=action_type,
            status=ActionStatus.PENDING,
            reason=reason,
            basis=basis,
            effective_at=effective_at,
            expires_at=expires_at,
            decided_by=actor.id,
            created_at=now,
            auto=auto,
        )
        self.repo.insert_action(action)
        self.auditor.record(
            actor,
            "action.decide",
            "action",
            action.id,
            True,
            f"{'auto ' if auto else ''}{action_type.value} on {case.target_account_id} "
            f"effective {effective_at.isoformat()} "
            f"expires {expires_at.isoformat() if expires_at else 'never'} "
            f"reason={reason!r} basis={basis!r}",
        )
        # Activate immediately if due, then converge all decisions on the account.
        if effective_at <= now:
            self._activate(action.id, now, actor)
        self.converge(case.target_account_id, now, actor)
        stored = self.repo.get_action(action.id)
        assert stored is not None
        return stored

    def _compute_expiry(
        self,
        action_type: ActionType,
        effective_at: datetime,
        duration_seconds: int | None,
    ) -> datetime | None:
        if action_type in (ActionType.WARN, ActionType.BAN_PERM):
            return None
        if duration_seconds is None:
            duration_seconds = self.config.action_durations[action_type.value]
        if duration_seconds is None:
            return None
        if duration_seconds <= 0:
            raise ValidationError("duration must be positive")
        return effective_at + timedelta(seconds=duration_seconds)

    # -- convergence ---------------------------------------------------
    def _rank(self, action: Action) -> tuple[int, int, int, str]:
        # Higher tuple wins. Manual beats auto, then severity, then recency.
        return (
            0 if action.auto else 1,
            SEVERITY[action.action_type],
            int(action.created_at.timestamp() * 1_000_000),
            action.id,
        )

    def converge(
        self, account_id: str, now: datetime | None = None, actor: Actor | None = None
    ) -> Action | None:
        """Make exactly one action ACTIVE per account.

        Idempotent and crash-safe: repeatedly converges to the same result.
        """
        now = now or self.clock.now()
        with self.repo.lock:
            current_active = self.repo.get_active_action(account_id)
            # Build candidate set from a fresh snapshot; always include the
            # currently ACTIVE action so a just-committed stronger decision can
            # supersede it but it can never vanish into the empty branch.
            rows = self.repo.list_actions_for_account(account_id)
            candidates = [
                a
                for a in rows
                if a.status
                in (ActionStatus.PENDING, ActionStatus.ACTIVE, ActionStatus.SUPERSEDED)
                and a.effective_at <= now
                and (a.expires_at is None or a.expires_at > now)
            ]
            if current_active is not None and (
                current_active.expires_at is None
                or current_active.expires_at > now
            ):
                if current_active not in candidates:
                    candidates.append(current_active)
            if not candidates:
                active = self.repo.get_active_action(account_id)
                if active is not None and (
                    active.expires_at is not None and active.expires_at <= now
                ):
                    self._expire(active, now, actor)
                return self.repo.get_active_action(account_id)

            winner = max(candidates, key=self._rank)
            active = self.repo.get_active_action(account_id)

            if active is not None and active.id != winner.id:
                if self._rank(winner) > self._rank(active):
                    self.repo.cas_action_status(
                        active.id, ActionStatus.ACTIVE, ActionStatus.SUPERSEDED, now
                    )
                    self._audit_convergence(actor, active, "superseded", winner)
                    active = None
                else:
                    winner = active
            if active is None:
                self._activate(winner.id, now, actor)
            # Every other due candidate must be SUPERSEDED (or kept pending if
            # scheduled later). Repeat until stable.
            for other in self.repo.list_actions_for_account(account_id):
                if other.id == winner.id:
                    continue
                if other.status == ActionStatus.ACTIVE:
                    continue
                if other.status == ActionStatus.PENDING and other.effective_at <= now:
                    if self._rank(winner) >= self._rank(other):
                        self.repo.cas_action_status(
                            other.id, ActionStatus.PENDING, ActionStatus.SUPERSEDED, now
                        )
            final_active = self.repo.get_active_action(account_id)
            if final_active is not None:
                # Unified closing step: whichever action ends ACTIVE, its case
                # must reflect that. Idempotent CAS makes this race-free.
                self._advance_case(final_active, now, actor)
            return final_active

    def _activate(self, action_id: str, now: datetime, actor: Actor | None) -> None:
        """Activate while honouring the single-active-action guarantee."""
        action = self.repo.get_action(action_id)
        if action is None or action.status != ActionStatus.PENDING:
            return
        active = self.repo.get_active_action(action.account_id)
        if active is not None and active.id != action_id:
            if self._rank(action) > self._rank(active):
                self.repo.cas_action_status(
                    active.id, ActionStatus.ACTIVE, ActionStatus.SUPERSEDED, now
                )
                self._audit_convergence(actor, active, "superseded", action)
            else:
                self.repo.cas_action_status(
                    action_id, ActionStatus.PENDING, ActionStatus.SUPERSEDED, now
                )
                self._audit_convergence(actor, action, "superseded", active)
                return
        ok = self.repo.cas_action_status(
            action_id, ActionStatus.PENDING, ActionStatus.ACTIVE, now
        )
        if ok:
            self._advance_case(action, now, actor)
            self._audit_convergence(actor, action, "activated", None)

    def _advance_case(self, action: Action, now: datetime, actor: Actor | None) -> None:
        """Idempotently align the action's case to 'actioned' whenever an
        enforcement action becomes active. Safe under concurrent activation:
        uses CAS and tolerates any legal intermediate status."""
        case = self.repo.get_case(action.case_id)
        if case is None:
            return
        if case.status in (CaseStatus.OPEN, CaseStatus.IN_REVIEW, CaseStatus.APPEALED):
            updated = self.repo.cas_case(
                case.id, case.version, status=CaseStatus.ACTIONED, now=now
            )
            if updated is not None:
                self.auditor.record(
                    actor or Actor(id=action.decided_by, role=_decider_role(action)),
                    "case.transition",
                    "case",
                    case.id,
                    True,
                    f"{case.status.value} -> actioned (action {action.id} active)",
                )

    def _expire(self, action: Action, now: datetime, actor: Actor | None) -> bool:
        ok = self.repo.cas_action_status(
            action.id, ActionStatus.ACTIVE, ActionStatus.EXPIRED, now
        )
        if ok:
            self._audit_convergence(actor, action, "expired", None)
            # A timed sanction ending naturally leaves the case without
            # enforcement: align actioned -> open (legal, audited edge).
            case = self.repo.get_case(action.case_id)
            if (
                case is not None
                and case.status == CaseStatus.ACTIONED
                and self.repo.get_active_action(action.account_id) is None
            ):
                updated = self.repo.cas_case(
                    case.id, case.version, status=CaseStatus.OPEN, now=now
                )
                if updated is not None:
                    self.auditor.record(
                        actor or Actor(id=action.decided_by, role=_decider_role(action)),
                        "case.transition",
                        "case",
                        case.id,
                        True,
                        f"actioned -> open (action {action.id} expired at "
                        f"{action.expires_at.isoformat() if action.expires_at else ''})",
                    )
        return ok

    def release_expired(self, now: datetime | None = None) -> list[str]:
        """Auto-release every due timed action. Returns released action ids."""
        now = now or self.clock.now()
        released: list[str] = []
        with self.repo.lock:
            for action in self.repo.list_due_actions(now):
                if self._expire(action, now, None):
                    released.append(action.id)
        return released

    def activate_pending(self, now: datetime | None = None) -> list[str]:
        """Activate scheduled decisions whose effective time has arrived."""
        now = now or self.clock.now()
        activated: list[str] = []
        with self.repo.lock:
            for action in self.repo.list_actions_by_status(ActionStatus.PENDING):
                if action.effective_at <= now:
                    self._activate(action.id, now, None)
                    after = self.repo.get_action(action.id)
                    if after is not None and after.status == ActionStatus.ACTIVE:
                        activated.append(action.id)
            accounts = {
                a.account_id
                for a in self.repo.list_actions_by_status(ActionStatus.ACTIVE)
            }
            for account_id in accounts:
                self.converge(account_id, now, None)
        return activated

    def revoke(
        self, actor: Actor, action_id: str, reason: str
    ) -> Action:
        """Manually change/revoke an existing enforcement decision."""
        action = self.repo.get_action(action_id)
        if action is None:
            raise NotFoundError(f"action {action_id} not found")
        if not can(actor, CHANGE_ACTION):
            self.auditor.record(
                actor, "action.revoke.denied", "action", action_id, False,
                f"role '{actor.role.value}' may not change enforcement",
            )
        require(actor, CHANGE_ACTION)
        now = self.clock.now()
        if action.status in (ActionStatus.EXPIRED, ActionStatus.REVOKED):
            raise InvalidStateTransitionError(
                f"action already {action.status.value}"
            )
        ok = self.repo.cas_action_status(
            action_id, action.status, ActionStatus.REVOKED, now
        )
        if not ok:
            raise ConflictRetryError(f"action {action_id} changed concurrently")
        self.auditor.record(
            actor,
            "action.revoke",
            "action",
            action_id,
            True,
            f"revoked {action.action_type.value}: {reason}",
        )
        self.converge(action.account_id, now, actor)
        stored = self.repo.get_action(action_id)
        assert stored is not None
        return stored

    def explain(self, account_id: str) -> dict:
        """Human-readable explanation of the current enforcement state."""
        with self.repo.lock:
            actions = self.repo.list_actions_for_account(account_id)
            active = self.repo.get_active_action(account_id)
        history = [
            {
                "id": a.id,
                "case_id": a.case_id,
                "type": a.action_type.value,
                "status": a.status.value,
                "auto": a.auto,
                "decided_by": a.decided_by,
                "effective_at": a.effective_at.isoformat(),
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
                "applied_at": a.applied_at.isoformat() if a.applied_at else None,
                "released_at": a.released_at.isoformat() if a.released_at else None,
                "reason": a.reason,
                "basis": a.basis,
            }
            for a in actions
        ]
        result = {
            "account_id": account_id,
            "active": None,
            "history": history,
        }
        if active is not None:
            result["active"] = {
                "id": active.id,
                "type": active.action_type.value,
                "effective_at": active.effective_at.isoformat(),
                "expires_at": active.expires_at.isoformat() if active.expires_at else None,
                "explanation": (
                    f"{active.action_type.value} decided by {active.decided_by} "
                    f"based on {active.basis}; "
                    + (
                        f"automatically released at {active.expires_at.isoformat()}"
                        if active.expires_at
                        else "no automatic release"
                    )
                ),
            }
        else:
            result["active"] = None
            result["explanation"] = "no active enforcement"
        return result

    def _audit_convergence(
        self,
        actor: Actor | None,
        action: Action,
        outcome: str,
        winner: Action | None,
    ) -> None:
        who = actor or Actor(
            id=action.decided_by, role=_decider_role(action)
        )
        detail = f"action {action.id} {action.action_type.value} {outcome}"
        if winner is not None:
            detail += f" by {winner.id}"
        self.auditor.record(
            who, "action.converge", "action", action.id, True, detail
        )


def _decider_role(action: Action):
    from .enums import Role

    return Role.SYSTEM if action.auto else Role.REVIEWER
