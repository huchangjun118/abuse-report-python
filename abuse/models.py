"""Domain model dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .enums import (
    ActionStatus,
    ActionType,
    AppealStatus,
    CaseStatus,
    Priority,
    Role,
)


@dataclass
class Actor:
    id: str
    role: Role


@dataclass
class Report:
    id: str
    dedup_key: str
    source: str
    reporter_id: str
    target_account_id: str
    event_key: str
    case_id: str
    evidence: str
    created_at: datetime


@dataclass
class Case:
    id: str
    target_account_id: str
    status: CaseStatus
    priority: Priority
    evidence_count: int
    created_at: datetime
    updated_at: datetime
    version: int


@dataclass
class Action:
    id: str
    case_id: str
    account_id: str
    action_type: ActionType
    status: ActionStatus
    reason: str
    basis: str
    effective_at: datetime
    expires_at: datetime | None
    decided_by: str
    created_at: datetime
    auto: bool = False
    applied_at: datetime | None = None
    released_at: datetime | None = None


@dataclass
class Appeal:
    id: str
    case_id: str
    account_id: str
    status: AppealStatus
    reason: str
    created_by: str
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None


@dataclass
class AuditEntry:
    id: int | None
    ts: datetime
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    allowed: bool
    detail: str


@dataclass
class DedupCounter:
    key: str
    count: int
    first_seen: datetime
