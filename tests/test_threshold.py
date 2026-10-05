"""Requirement 6: reporter rate limiting and distinct-reporter auto
escalation without flooding-driven false bans; manual review has priority."""

from __future__ import annotations

from abuse.enums import ActionType, CaseStatus, Priority, Role
from abuse.errors import RateLimitedError
from tests.conftest import actor


def test_single_reporter_flood_is_rate_limited(wf):
    # config default: 5 new reports / 60s
    for i in range(5):
        wf.submit_report(actor("flooder"), "web", "victim", f"evt-{i}", "spammy")
    try:
        wf.submit_report(actor("flooder"), "web", "victim", "evt-5", "spammy")
        raise AssertionError("expected RateLimitedError")
    except RateLimitedError:
        pass

    # rejected submissions must not exist as evidence
    case = wf.repo.get_case_for_target("victim")
    assert case.evidence_count == 5
    # and the rejected attempt is audited
    assert any(
        not e.allowed and e.action == "report.rate_limited"
        for e in wf.audit_trail("reporter", "flooder")
    )


def test_rate_limit_window_slides(wf, clock):
    for i in range(5):
        wf.submit_report(actor("flooder"), "web", "victim", f"evt-{i}", "x")
    try:
        wf.submit_report(actor("flooder"), "web", "victim", "evt-x", "x")
        raise AssertionError("expected rate limit")
    except RateLimitedError:
        pass
    clock.advance(61)
    # after the window, reports flow again
    result = wf.submit_report(actor("flooder"), "web", "victim", "evt-after", "x")
    assert result["is_new"] is True


def test_one_flooding_reporter_cannot_trigger_auto_ban(wf):
    # A single reporter hammers one target with many *distinct events*.
    # Rate limiting blocks them first; even if it were raised, distinct
    # reporter count stays 1 so the ban threshold is never met.
    for i in range(20):
        try:
            wf.submit_report(actor("flooder"), "web", "victim", f"evt-{i}", "x")
        except RateLimitedError:
            pass
    assert wf.repo.get_active_action("victim") is None
    case = wf.repo.get_case_for_target("victim")
    assert case.status == CaseStatus.OPEN


def test_distinct_reporters_escalate_then_auto_ban(wf):
    # 3 distinct reporters -> auto escalation to in_review/auto
    for i in range(3):
        wf.submit_report(actor(f"r{i}"), "web", "victim", f"evt-{i}", "x")
    case = wf.repo.get_case_for_target("victim")
    assert case.status == CaseStatus.IN_REVIEW
    assert case.priority == Priority.AUTO

    # 10 distinct reporters -> automatic timed ban
    for i in range(3, 10):
        wf.submit_report(actor(f"r{i}"), "web", "victim", f"evt-{i}", "x")
    active = wf.repo.get_active_action("victim")
    assert active is not None
    assert active.action_type == ActionType.BAN_TIMED
    assert active.auto is True
    assert active.decided_by.startswith("system:")
    case = wf.repo.get_case_for_target("victim")
    assert case.status == CaseStatus.ACTIONED


def test_manual_review_blocks_automatic_ban(wf):
    # enough distinct reporters to escalate to review
    for i in range(3):
        wf.submit_report(actor(f"r{i}"), "web", "victim", f"evt-{i}", "x")
    # human takes ownership
    case = wf.repo.get_case_for_target("victim")
    wf.begin_review(actor("rev-1", Role.REVIEWER), case.id)
    assert wf.cases.get(case.id).priority == Priority.MANUAL

    # threshold continues to be crossed by more reporters...
    for i in range(3, 12):
        wf.submit_report(actor(f"r{i}"), "web", "victim", f"evt-{i}", "x")
    # ...but no automatic ban is issued while a human owns it
    assert wf.repo.get_active_action("victim") is None
    case = wf.cases.get(case.id)
    assert case.priority == Priority.MANUAL


def test_automatic_ban_uses_distinct_reporters_not_raw_report_count(wf, clock, config):
    # 2 distinct reporters, each retries many times: retries merge and never
    # inflate the count.
    for dup in range(8):
        wf.submit_report(actor("r0"), "web", "victim", "event-A", "x")
        wf.submit_report(actor("r1"), "web", "victim", "event-B", "x")
    assert wf.repo.get_active_action("victim") is None
    case = wf.repo.get_case_for_target("victim")
    # below distinct-reporter threshold -> stays open
    assert case.evidence_count == 2
    assert case.status == CaseStatus.OPEN
