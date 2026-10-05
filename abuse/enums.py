"""Enumerations for the abuse review workflow."""

from __future__ import annotations

import enum


class CaseStatus(str, enum.Enum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    ACTIONED = "actioned"
    APPEALED = "appealed"
    CLOSED = "closed"


class ActionType(str, enum.Enum):
    WARN = "warn"
    MUTE = "mute"
    BAN_TIMED = "ban_timed"
    BAN_PERM = "ban_perm"


class ActionStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    REVOKED = "revoked"


class AppealStatus(str, enum.Enum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"


class Role(str, enum.Enum):
    USER = "user"
    REVIEWER = "reviewer"
    ADMIN = "admin"
    # Pseudo-actor for automatic/system-generated decisions in the audit trail.
    SYSTEM = "system"


class Priority(str, enum.Enum):
    MANUAL = "manual"
    AUTO = "auto"
