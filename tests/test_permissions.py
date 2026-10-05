"""Requirement 5: only authorised roles may review/decide, and every
unauthorised attempt is rejected and audited."""

from __future__ import annotations

import pytest

from abuse.enums import (
    ActionType,
    AppealStatus,
    CaseStatus,
    Role,
)
from abuse.errors import PermissionDeniedError
from tests.conftest import actor


@pytest.fixture
def case_id(wf, config):
    config.auto_action_enabled = False
    result = wf.submit_report(actor("reporter-1"), "web", "acc-p", "evt", "ev")
    return result["case"].id


def test_user_cannot_review_case(wf, case_id):
    with pytest.raises(PermissionDeniedError):
        wf.begin_review(actor("plain-user"), case_id)


def test_user_cannot_transition_case(wf, case_id):
    with pytest.raises(PermissionDeniedError):
        wf.cases.transition(actor("plain-user"), case_id, CaseStatus.IN_REVIEW)


def test_user_cannot_decide_or_revoke_action(wf, case_id):
    with pytest.raises(PermissionDeniedError):
        wf.actions.decide(actor("plain-user"), case_id, ActionType.WARN, "r", "b")
    action = wf.actions.decide(
        actor("rev", Role.REVIEWER), case_id, ActionType.WARN, "r", "b"
    )
    with pytest.raises(PermissionDeniedError):
        wf.actions.revoke(actor("plain-user"), action.id, "stop")


def test_user_cannot_decide_appeal(wf, case_id):
    wf.actions.decide(actor("rev", Role.REVIEWER), case_id, ActionType.WARN, "r", "b")
    appeal = wf.appeals.file(actor("acc-p"), case_id, "unfair")
    with pytest.raises(PermissionDeniedError):
        wf.appeals.decide_appeal(actor("acc-p"), appeal.id, True, "self grant")
    # stays pending, no enforcement changed
    assert wf.repo.get_appeal_by_id(appeal.id).status == AppealStatus.PENDING


def test_reviewer_and_admin_can_review_and_decide(wf, case_id):
    wf.begin_review(actor("rev", Role.REVIEWER), case_id)
    wf.actions.decide(
        actor("admin", Role.ADMIN), case_id, ActionType.MUTE, "ok", "basis",
        duration_seconds=5,
    )
    assert wf.repo.get_active_action("acc-p").action_type == ActionType.MUTE


def test_denied_attempts_are_audited(wf, case_id):
    for _ in range(2):
        try:
            wf.cases.transition(actor("mallory"), case_id, CaseStatus.CLOSED)
        except PermissionDeniedError:
            pass
    denied = [
        e for e in wf.audit_trail("case", case_id)
        if not e.allowed and e.actor_id == "mallory"
    ]
    assert len(denied) == 2
    assert all("may not perform" in e.detail for e in denied)
