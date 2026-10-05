"""Requirement 2: aggregation, evidence accumulation and legal transitions."""

from __future__ import annotations

import threading

import pytest

from abuse.enums import CaseStatus, Role
from abuse.errors import InvalidStateTransitionError
from tests.conftest import actor


def _report(wf, reporter, target="target-1", event=None):
    return wf.submit_report(
        actor(reporter), "web", target, event or f"evt-{reporter}", "ev"
    )


def test_reports_against_same_target_share_case(wf, config):
    config.auto_action_enabled = False
    r1 = _report(wf, "u1", event="e1")
    r2 = _report(wf, "u2", event="e2")
    r3 = _report(wf, "u3", event="e3")
    assert r1["case"].id == r2["case"].id == r3["case"].id
    case = wf.cases.get(r1["case"].id)
    assert case.evidence_count == 3
    assert case.status == CaseStatus.OPEN


def test_different_targets_get_different_cases(wf, config):
    config.auto_action_enabled = False
    a = _report(wf, "u1", target="A", event="e1")
    b = _report(wf, "u2", target="B", event="e2")
    assert a["case"].id != b["case"].id


def test_legal_and_illegal_transitions(wf):
    reviewer = actor("rev-1", Role.REVIEWER)
    res = _report(wf, "u1")
    case_id = res["case"].id

    assert wf.cases.transition(reviewer, case_id, CaseStatus.IN_REVIEW).status == CaseStatus.IN_REVIEW
    assert wf.cases.transition(reviewer, case_id, CaseStatus.ACTIONED).status == CaseStatus.ACTIONED
    assert wf.cases.transition(reviewer, case_id, CaseStatus.APPEALED).status == CaseStatus.APPEALED
    assert wf.cases.transition(reviewer, case_id, CaseStatus.CLOSED).status == CaseStatus.CLOSED
    assert wf.cases.transition(reviewer, case_id, CaseStatus.OPEN).status == CaseStatus.OPEN


def test_illegal_transition_rejected_without_state_change(wf, config):
    config.auto_action_enabled = False
    reviewer = actor("rev-1", Role.REVIEWER)
    case_id = _report(wf, "u1")["case"].id
    with pytest.raises(InvalidStateTransitionError):
        # open -> appealed is illegal
        wf.cases.transition(reviewer, case_id, CaseStatus.APPEALED)
    case = wf.cases.get(case_id)
    assert case.status == CaseStatus.OPEN
    denied = [
        e for e in wf.audit_trail("case", case_id)
        if e.action == "case.transition.denied" and not e.allowed
    ]
    assert len(denied) == 1


def test_concurrent_reports_never_lose_evidence_or_mix_fields(wf, config):
    config.auto_action_enabled = False
    target = "target-c"
    n = 30
    barrier = threading.Barrier(n)

    def submit(i):
        barrier.wait()
        wf.submit_report(actor(f"r{i}"), "web", target, f"event-{i}", f"e{i}")

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cases = wf.repo.list_cases()
    matching = [c for c in cases if c.target_account_id == target]
    assert len(matching) == 1
    case = matching[0]
    assert case.evidence_count == n
    # no field-level mix: status stays a valid enum
    assert isinstance(case.status, CaseStatus)
    assert wf.repo.count_reports_for_target(target) == n


def test_concurrent_conflicting_transitions_cannot_roll_back(wf, config):
    config.auto_action_enabled = False
    case_id = _report(wf, "u1")["case"].id
    rev_a = actor("ra", Role.REVIEWER)
    rev_b = actor("rb", Role.REVIEWER)

    # both reviewers snapshot the same version before deciding
    snapshotted_version = wf.cases.get(case_id).version
    outcomes = []

    def t_review():
        try:
            wf.cases.transition(
                rev_a, case_id, CaseStatus.IN_REVIEW,
                expected_version=snapshotted_version,
            )
            outcomes.append("ok-in_review")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"fail-{type(exc).__name__}")

    def t_close():
        try:
            wf.cases.transition(
                rev_b, case_id, CaseStatus.CLOSED,
                expected_version=snapshotted_version,
            )
            outcomes.append("ok-closed")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"fail-{type(exc).__name__}")

    t1 = threading.Thread(target=t_review)
    t2 = threading.Thread(target=t_close)
    t1.start(); t2.start(); t1.join(); t2.join()

    final = wf.cases.get(case_id)
    ok = [o for o in outcomes if o.startswith("ok")]
    assert len(ok) == 1
    # the loser must retry on fresh version; status never silently rolled back
    assert final.status in (CaseStatus.IN_REVIEW, CaseStatus.CLOSED)
    assert final.version == snapshotted_version + 1
