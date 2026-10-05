"""Audit trail.

Every state-changing operation *and* every rejected / out-of-state attempt
produces an immutable audit row: who, when, what, on which entity, whether it
was allowed, and why. This is what makes the whole chain reconstructable.
"""

from __future__ import annotations

from .clock import Clock
from .models import Actor, AuditEntry
from .repository import Repository


class Auditor:
    def __init__(self, repo: Repository, clock: Clock | None = None) -> None:
        self.repo = repo
        self.clock = clock or Clock()

    def record(
        self,
        actor: Actor,
        action: str,
        entity_type: str,
        entity_id: str,
        allowed: bool,
        detail: str = "",
    ) -> AuditEntry:
        entry = AuditEntry(
            id=None,
            ts=self.clock.now(),
            actor_id=actor.id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            allowed=allowed,
            detail=detail,
        )
        return self.repo.insert_audit(entry)

    def trail(
        self, entity_type: str | None = None, entity_id: str | None = None
    ) -> list[AuditEntry]:
        return self.repo.list_audit(entity_type, entity_id)
