"""High level facade wiring persistence, services, recovery and the sweeper."""

from __future__ import annotations

import threading

from .actions import ActionService
from .appeals import AppealService
from .audit import Auditor
from .cases import CaseService
from .clock import Clock
from .config import Config
from .enums import Role
from .models import Actor
from .recovery import RecoveryService
from .repository import Repository
from .threshold import ThresholdService

SYSTEM_ACTOR = Actor(id="system", role=Role.SYSTEM)


class Workflow:
    def __init__(
        self,
        db_path: str = ":memory:",
        config: Config | None = None,
        clock: Clock | None = None,
        start_sweeper: bool = True,
    ) -> None:
        self.db_path = db_path
        self.config = config or Config.default()
        self.clock = clock or Clock()
        self.repo = Repository(db_path)
        self.auditor = Auditor(self.repo, self.clock)
        self.actions = ActionService(self.repo, self.config, self.auditor, self.clock)
        self.appeals = AppealService(self.repo, self.config, self.auditor, self.clock)
        self.thresholds = ThresholdService(
            self.repo, self.config, self.auditor, self.actions, self.clock
        )
        self.cases = CaseService(
            self.repo,
            self.auditor,
            self.clock,
            auto_evaluator=lambda case: self.thresholds.evaluate(SYSTEM_ACTOR, case),
        )
        self.recovery = RecoveryService(
            self.repo, self.config, self.actions, self.clock
        )
        self._sweeper: threading.Thread | None = None
        self._stopping = threading.Event()
        self._start_sweeper = start_sweeper and db_path != ":memory:"

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "Workflow":
        self.repo.connect()
        self.recovery.recover()
        if self._start_sweeper:
            self._sweeper = threading.Thread(
                target=self._sweep_loop, name="abuse-sweeper", daemon=True
            )
            self._sweeper.start()
        return self

    def close(self) -> None:
        self._stopping.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=2)
        self.repo.close()

    def __enter__(self) -> "Workflow":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def _sweep_loop(self) -> None:
        while not self._stopping.wait(self.config.sweep_interval_seconds):
            try:
                self.sweep()
            except Exception:  # pragma: no cover - sweeper must never die
                pass

    def sweep(self) -> dict:
        """One maintenance tick: activate due decisions, release expired ones,
        and converge affected accounts."""
        activated = self.actions.activate_pending()
        released = self.actions.release_expired()
        return {"activated": activated, "released": released}

    # -- ingress -------------------------------------------------------
    def submit_report(
        self,
        actor: Actor,
        source: str,
        target_account_id: str,
        event_key: str,
        evidence: str,
    ) -> dict:
        # Rate limit runs before a genuinely new report is persisted/aggregated,
        # so a flooding reporter can neither inflate evidence nor trigger auto
        # escalation. A retry of an already-seen event (same dedup key) is an
        # idempotent replay and must not consume the reporter's quota.
        from .idempotency import report_dedup_key

        dedup_key = report_dedup_key(source, actor.id, event_key)
        if self.repo.get_report(dedup_key) is None:
            self.thresholds.check_reporter_rate(actor)
        report, case, is_new, duplicate_count = self.cases.submit_report(
            actor, source, target_account_id, event_key, evidence
        )
        return {
            "report": report,
            "case": case,
            "is_new": is_new,
            "duplicate_count": duplicate_count,
        }

    def begin_review(self, actor: Actor, case_id: str):
        """Human takes ownership: manual priority wins over automation."""
        from .enums import CaseStatus, Priority

        case = self.cases.get(case_id)
        if case.status == CaseStatus.OPEN:
            case = self.cases.transition(actor, case_id, CaseStatus.IN_REVIEW)
        return self.cases.set_priority(actor, case_id, Priority.MANUAL)

    def audit_trail(self, entity_type: str | None = None, entity_id: str | None = None):
        return self.auditor.trail(entity_type, entity_id)
