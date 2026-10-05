"""Role based permission checks.

Roles:
* ``user``     - may submit reports and appeal actions against own account;
* ``reviewer`` - may review cases and decide/change enforcement actions;
* ``admin``    - all reviewer powers plus config-level overrides.

Every denied check is expected to be written into the audit trail by the
calling service.
"""

from __future__ import annotations

from .enums import Role
from .errors import PermissionDeniedError
from .models import Actor

REVIEW_CASE = "review_case"
DECIDE_ACTION = "decide_action"
CHANGE_ACTION = "change_action"
DECIDE_APPEAL = "decide_appeal"
MANUAL_OVERRIDE_AUTO = "manual_override_auto"
SUBMIT_REPORT = "submit_report"
FILE_APPEAL = "file_appeal"

PERMISSIONS: dict[str, frozenset[Role]] = {
    SUBMIT_REPORT: frozenset({Role.USER, Role.REVIEWER, Role.ADMIN}),
    FILE_APPEAL: frozenset({Role.USER, Role.REVIEWER, Role.ADMIN}),
    REVIEW_CASE: frozenset({Role.REVIEWER, Role.ADMIN}),
    DECIDE_ACTION: frozenset({Role.REVIEWER, Role.ADMIN}),
    CHANGE_ACTION: frozenset({Role.REVIEWER, Role.ADMIN}),
    DECIDE_APPEAL: frozenset({Role.REVIEWER, Role.ADMIN}),
    MANUAL_OVERRIDE_AUTO: frozenset({Role.REVIEWER, Role.ADMIN}),
}


def can(actor: Actor, operation: str) -> bool:
    allowed = PERMISSIONS.get(operation, frozenset())
    return actor.role in allowed


def require(actor: Actor, operation: str) -> None:
    if not can(actor, operation):
        raise PermissionDeniedError(
            f"role '{actor.role.value}' may not perform '{operation}'"
        )
