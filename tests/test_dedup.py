"""Requirement 1: identical retried submissions merge into one report."""

from __future__ import annotations

from abuse.idempotency import report_dedup_key
from tests.conftest import actor


def test_dedup_key_stable():
    assert report_dedup_key("web", "u1", "evt") == report_dedup_key("web", "u1", "evt")
    assert report_dedup_key("web", "u1", "evt") != report_dedup_key("api", "u1", "evt")
    assert report_dedup_key("web", "u1", "evt") != report_dedup_key("web", "u2", "evt")
    assert report_dedup_key("web", "u1", "a") != report_dedup_key("web", "u1", "b")


def test_repeated_submission_is_idempotent(wf):
    user = actor("reporter-1")
    first = wf.submit_report(user, "web", "target-1", "event-1", "evidence A")
    assert first["is_new"] is True
    assert first["duplicate_count"] == 1
    assert first["case"].evidence_count == 1
    report_id = first["report"].id
    case_id = first["case"].id

    # network retry / double click: same everything
    second = wf.submit_report(user, "web", "target-1", "event-1", "evidence A again")
    assert second["is_new"] is False
    assert second["duplicate_count"] == 2
    assert second["report"].id == report_id
    assert second["case"].id == case_id
    # evidence must not be double-counted
    assert second["case"].evidence_count == 1

    # many retries
    for _ in range(8):
        again = wf.submit_report(user, "web", "target-1", "event-1", "retry")
    assert again["is_new"] is False
    assert again["duplicate_count"] == 10
    assert again["case"].evidence_count == 1

    # only one physical report row exists
    assert wf.repo.count_reports_for_target("target-1") == 1

    # duplicates are audited as merged, original submit audited once
    trail = wf.audit_trail("report", report_id)
    submit_events = [e for e in trail if e.action == "report.submit"]
    merge_events = [e for e in trail if e.action == "report.submit.duplicate"]
    assert len(submit_events) == 1
    assert len(merge_events) == 9


def test_different_events_or_sources_are_separate(wf):
    user = actor("reporter-1")
    a = wf.submit_report(user, "web", "target-1", "event-1", "a")
    b = wf.submit_report(user, "web", "target-1", "event-2", "b")
    c = wf.submit_report(user, "api", "target-1", "event-1", "c")
    assert a["report"].id != b["report"].id != c["report"].id
    # same target -> aggregated into the same case, evidence accumulates
    assert a["case"].id == b["case"].id == c["case"].id
    assert wf.cases.get(a["case"].id).evidence_count == 3
