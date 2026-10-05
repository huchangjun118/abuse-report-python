"""Requirement 4: owner-only appeals, deterministic concurrent outcomes,
and full traceability of who decided what based on which evidence."""

from __future__ import annotations

import threading

import pytest

from abuse.enums import (
    ActionStatus,
    ActionType,
    AppealStatus,
    CaseStatus,
    Role,
)
from abuse.errors import (
    ConflictRetryError,
    DuplicateError,
    PermissionDeniedError,
)
from tests.conftest import actor

TARGET = "acc-appeal"


@pytest.fixture
def actioned(wf, config):
    config.auto_action_enabled = False
    wf.submit_report(actor("reporter-1"), "web", TARGET, "evt", "evidence")
    case = wf.cases.get(wf.repo.get_case_for_target(TARGET).id)
    wf.actions.decide(
        actor("rev-1", Role.REVIEWER), case.id, ActionType.BAN_TIMED,
        "spam", "screenshot-1", duration_seconds=999,
    )
    return case


def test_only_sanctioned_owner_may_appeal(wf, actioned):
    # a different user is rejected and the rejection is audited
    with pytest.raises(PermissionDeniedError):
        wf.appeals.file(actor("someone-else"), actioned.id, "unfair")
    denied = [
        e for e in wf.audit_trail("case", actioned.id)
        if e.action == "appeal.file.denied" and not e.allowed
    ]
    assert len(denied) == 1

    # a reviewer principal cannot impersonate the owner either
    with pytest.raises(PermissionDeniedError):
        wf.appeals.file(actor(TARGET, Role.REVIEWER), actioned.id, "unfair")


def test_owner_files_appeal_case_becomes_appealed(wf, actioned):
    appeal = wf.appeals.file(actor(TARGET), actioned.id, "false positive, logs attached")
    assert appeal.status == AppealStatus.PENDING
    assert wf.cases.get(actioned.id).status == CaseStatus.APPEALED
    entry = next(e for e in wf.audit_trail("appeal", appeal.id) if e.action == "appeal.file")
    assert entry.actor_id == TARGET


def test_duplicate_appeal_rejected(wf, actioned):
    wf.appeals.file(actor(TARGET), actioned.id, "first")
    with pytest.raises(DuplicateError):
        wf.appeals.file(actor(TARGET), actioned.id, "second")


def test_granted_appeal_lifts_enforcement_and_reopens(wf, actioned):
    appeal = wf.appeals.file(actor(TARGET), actioned.id, "not me")
    result = wf.appeals.decide_appeal(
        actor("rev-2", Role.REVIEWER), appeal.id, True, "logs exonerate account"
    )
    assert result.status == AppealStatus.GRANTED
    assert wf.repo.get_active_action(TARGET) is None
    actions = wf.repo.list_actions_for_account(TARGET)
    assert all(a.status != ActionStatus.ACTIVE for a in actions)
    assert actions[0].status == ActionStatus.REVOKED
    assert wf.cases.get(actioned.id).status == CaseStatus.OPEN


def test_denied_appeal_keeps_enforcement(wf, actioned):
    appeal = wf.appeals.file(actor(TARGET), actioned.id, "not me")
    wf.appeals.decide_appeal(
        actor("rev-2", Role.REVIEWER), appeal.id, False, "evidence confirmed"
    )
    assert wf.repo.get_active_action(TARGET) is not None
    assert wf.cases.get(actioned.id).status == CaseStatus.ACTIONED


def test_appeal_decision_racing_second_reviewer_is_deterministic(wf, actioned):
    appeal = wf.appeals.file(actor(TARGET), actioned.id, "not me")
    outcomes = []
    barrier = threading.Barrier(2)

    def decide(grant: bool, reviewer: str):
        barrier.wait()
        try:
            wf.appeals.decide_appeal(
                actor(reviewer, Role.REVIEWER), appeal.id, grant, f"verdict {grant}"
            )
            outcomes.append(("ok", grant))
        except ConflictRetryError:
            outcomes.append(("conflict", grant))

    t1 = threading.Thread(target=decide, args=(True, "rev-2"))
    t2 = threading.Thread(target=decide, args=(False, "rev-3"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert [o for o in outcomes if o[0] == "ok"] == [("ok", True)] or \
           [o for o in outcomes if o[0] == "ok"] == [("ok", False)]
    assert len([o for o in outcomes if o[0] == "ok"]) == 1
    final = wf.repo.get_appeal_by_id(appeal.id)
    active = wf.repo.get_active_action(TARGET)
    if final.status == AppealStatus.GRANTED:
        assert active is None
    else:
        assert active is not None


def test_appeal_racing_new_report_keeps_consistent_state(wf, actioned):
    """A new report arriving while the appeal is decided must not corrupt the
    outcome: appeal verdict and enforcement stay aligned."""
    appeal = wf.appeals.file(actor(TARGET), actioned.id, "not me")

    def file_report():
        wf.submit_report(actor("reporter-2"), "web", TARGET, "evt-2", "more evidence")

    def grant():
        wf.appeals.decide_appeal(
            actor("rev-9", Role.REVIEWER), appeal.id, True, "exonerated"
        )

    t1 = threading.Thread(target=file_report)
    t2 = threading.Thread(target=grant)
    t1.start(); t2.start(); t1.join(); t2.join()

    # granted appeal always leaves no active action, regardless of ordering
    assert wf.repo.get_active_action(TARGET) is None
    assert wf.repo.get_appeal_by_id(appeal.id).status == AppealStatus.GRANTED
    # evidence from the new report is still retained for the reopened case
    case = wf.cases.get(actioned.id)
    assert case.evidence_count == 2


def test_full_trail_reconstructs_who_what_when_why(wf, actioned):
    appeal = wf.appeals.file(actor(TARGET), actioned.id, "misunderstanding")
    wf.appeals.decide_appeal(
        actor("boss", Role.ADMIN), appeal.id, True, "reviewed logs, no abuse"
    )
    actions_seen = {e.action for e in wf.audit_trail()}
    assert "report.submit" in actions_seen
    assert "action.decide" in actions_seen
    assert "appeal.file" in actions_seen
    assert "appeal.grant" in actions_seen
    assert "action.revoke" in actions_seen

    grant_entries = [e for e in wf.audit_trail("appeal", appeal.id) if e.action == "appeal.grant"]
    assert grant_entries[0].actor_id == "boss"
    assert "reviewed logs" in grant_entries[0].detail
    assert grant_entries[0].ts is not None
