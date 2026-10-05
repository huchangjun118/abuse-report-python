"""Requirement 3: action types, deterministic convergence, auto release."""

from __future__ import annotations

import threading

import pytest

from abuse.enums import ActionStatus, ActionType, CaseStatus, Role
from abuse.errors import (
    PermissionDeniedError,
    ValidationError,
)
from tests.conftest import actor

REV = lambda: actor("rev-1", Role.REVIEWER)


@pytest.fixture
def case_id(wf, config):
    config.auto_action_enabled = False
    result = wf.submit_report(actor("u1"), "web", "acc-1", "evt-1", "spam screenshot")
    return result["case"].id


def test_decisions_require_reason_and_basis(wf, case_id):
    with pytest.raises(ValidationError):
        wf.actions.decide(REV(), case_id, ActionType.WARN, reason="", basis="b")


def test_warn_is_active_without_expiry(wf, case_id):
    action = wf.actions.decide(REV(), case_id, ActionType.WARN, "first offense", "report#1")
    assert action.status == ActionStatus.ACTIVE
    assert action.effective_at is not None
    assert action.expires_at is None
    active = wf.repo.get_active_action("acc-1")
    assert active.action_type == ActionType.WARN


def test_timed_action_carries_effective_and_expiry(wf, case_id, config):
    action = wf.actions.decide(
        REV(), case_id, ActionType.BAN_TIMED, "spam wave", "3 reports",
        duration_seconds=config.action_durations["ban_timed"],
    )
    assert action.effective_at is not None
    assert action.expires_at is not None
    delta = action.expires_at - action.effective_at
    assert delta.total_seconds() == config.action_durations["ban_timed"]
    assert action.status == ActionStatus.ACTIVE


def test_permanent_ban_never_expires_and_survives_sweep(wf, case_id):
    wf.actions.decide(REV(), case_id, ActionType.BAN_PERM, "irreversible abuse", "many reports")
    wf.clock.advance(3600 * 24 * 365 * 10)
    released = wf.actions.release_expired()
    assert released == []
    assert wf.repo.get_active_action("acc-1").action_type == ActionType.BAN_PERM


def test_timed_ban_auto_releases_at_expiry_and_is_explainable(wf, case_id):
    action = wf.actions.decide(
        REV(), case_id, ActionType.BAN_TIMED, "spam", "basis", duration_seconds=3600
    )
    assert wf.repo.get_active_action("acc-1") is not None

    # one second early: still active
    wf.clock.advance(3599)
    assert wf.actions.release_expired() == []
    assert wf.repo.get_active_action("acc-1") is not None

    # exactly due: released exactly once
    wf.clock.advance(1)
    first = wf.actions.release_expired()
    second = wf.actions.release_expired()
    assert first == [action.id]
    assert second == []
    stored = wf.repo.get_action(action.id)
    assert stored.status == ActionStatus.EXPIRED
    assert stored.released_at is not None
    assert wf.repo.get_active_action("acc-1") is None

    explanation = wf.actions.explain("acc-1")
    assert explanation["active"] is None
    entry = next(h for h in explanation["history"] if h["id"] == action.id)
    assert entry["status"] == "expired"
    assert entry["released_at"] is not None


def test_concurrent_decisions_converge_to_unique_strongest(wf, case_id):
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    types = [ActionType.WARN, ActionType.MUTE, ActionType.BAN_PERM]
    ids: list[str] = []

    def decide(i: int, action_type: ActionType):
        barrier.wait()
        a = wf.actions.decide(
            actor(f"rev-{i}", Role.REVIEWER), case_id, action_type,
            f"reason {i}", f"basis {i}", duration_seconds=100,
        )
        with lock:
            ids.append(a.id)

    threads = [
        threading.Thread(target=decide, args=(i, t))
        for i, t in enumerate(types)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    active = wf.repo.get_active_action("acc-1")
    assert active is not None
    assert active.action_type == ActionType.BAN_PERM
    # exactly one ACTIVE row (storage-level guarantee)
    actives = [
        a for a in wf.repo.list_actions_for_account("acc-1")
        if a.status == ActionStatus.ACTIVE
    ]
    assert len(actives) == 1
    weaker = [
        a for a in wf.repo.list_actions_for_account("acc-1")
        if a.action_type in (ActionType.WARN, ActionType.MUTE)
    ]
    assert all(a.status == ActionStatus.SUPERSEDED for a in weaker)


def test_manual_decision_beats_concurrent_auto_decision(wf, case_id):
    # automatic timed ban first
    from abuse.enums import Role as R
    system = actor("system:auto", R.SYSTEM)
    auto_action = wf.actions.decide(
        system, case_id, ActionType.BAN_TIMED, "auto", "distinct=10",
        duration_seconds=999, auto=True,
    )
    assert wf.repo.get_active_action("acc-1").id == auto_action.id

    # human reviewer overrides with a warning: manual always wins
    human = wf.actions.decide(REV(), case_id, ActionType.WARN, "reviewed: not severe", "manual review")
    active = wf.repo.get_active_action("acc-1")
    assert active.id == human.id
    assert wf.repo.get_action(auto_action.id).status == ActionStatus.SUPERSEDED

    # a later automatic decision must not replace the human's action
    auto_again = wf.actions.decide(
        system, case_id, ActionType.BAN_PERM, "auto again", "threshold", auto=True
    )
    active = wf.repo.get_active_action("acc-1")
    assert active.id == human.id
    assert wf.repo.get_action(auto_again.id).status == ActionStatus.SUPERSEDED


def test_case_moves_to_actioned_when_decision_activates(wf, case_id):
    assert wf.cases.get(case_id).status == CaseStatus.OPEN
    wf.actions.decide(REV(), case_id, ActionType.MUTE, "spam", "b", duration_seconds=10)
    assert wf.cases.get(case_id).status == CaseStatus.ACTIONED


def test_every_decision_audited_with_operator_reason_basis(wf, case_id):
    wf.actions.decide(REV(), case_id, ActionType.MUTE, "because spam", "report evidence #7")
    entries = [e for e in wf.audit_trail() if e.action == "action.decide"]
    assert entries and entries[0].actor_id == "rev-1"
    assert "because spam" in entries[0].detail
    assert "report evidence #7" in entries[0].detail


def test_user_cannot_decide_action(wf, case_id):
    with pytest.raises(PermissionDeniedError):
        wf.actions.decide(actor("plain-user"), case_id, ActionType.WARN, "r", "b")
