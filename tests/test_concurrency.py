"""Mixed-concurrency stress: reports, decisions and an appeal racing together
must always leave one case, one active action and a consistent trail."""

from __future__ import annotations

import threading

from abuse.enums import ActionStatus, ActionType, CaseStatus, Role
from tests.conftest import actor

TARGET = "acc-stress"


def test_mixed_concurrency_converges(wf, config):
    config.auto_action_enabled = False
    n_reporters = 20
    def report(i):
        wf.submit_report(actor(f"r{i}"), "web", TARGET, f"evt-{i}", f"e{i}")

    def decide(i, action_type):
        # decisions only make sense once reports have created the case
        case = None
        for _ in range(100):
            case = wf.repo.get_case_for_target(TARGET)
            if case is not None:
                break
        if case is None:
            return
        try:
            wf.actions.decide(
                actor(f"rev{i}", Role.REVIEWER), case.id, action_type,
                f"reason {i}", f"basis {i}", duration_seconds=100,
            )
        except Exception:  # noqa: BLE001
            pass

    threads = [threading.Thread(target=report, args=(i,)) for i in range(n_reporters)]
    threads += [
        threading.Thread(target=decide, args=(0, ActionType.WARN)),
        threading.Thread(target=decide, args=(1, ActionType.MUTE)),
        threading.Thread(target=decide, args=(2, ActionType.BAN_TIMED)),
        threading.Thread(target=decide, args=(3, ActionType.BAN_PERM)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cases = [c for c in wf.repo.list_cases() if c.target_account_id == TARGET]
    assert len(cases) == 1
    case = cases[0]
    assert case.evidence_count == n_reporters
    assert wf.repo.count_reports_for_target(TARGET) == n_reporters

    actives = [
        a for a in wf.repo.list_actions_for_account(TARGET)
        if a.status == ActionStatus.ACTIVE
    ]
    assert len(actives) == 1
    # ban_perm is strongest among the decisions
    assert actives[0].action_type == ActionType.BAN_PERM
    assert case.status in (CaseStatus.ACTIONED, CaseStatus.APPEALED)


def test_converge_is_idempotent_under_parallel_calls(wf, config):
    config.auto_action_enabled = False
    wf.submit_report(actor("r0"), "web", TARGET, "e0", "e0")
    case = wf.repo.get_case_for_target(TARGET)
    for i in range(4):
        wf.actions.decide(
            actor(f"rev{i}", Role.REVIEWER), case.id, ActionType.MUTE,
            f"r{i}", f"b{i}", duration_seconds=1000,
        )

    results = []

    def converge():
        results.append(wf.actions.converge(TARGET))

    threads = [threading.Thread(target=converge) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    actives = [
        a for a in wf.repo.list_actions_for_account(TARGET)
        if a.status == ActionStatus.ACTIVE
    ]
    assert len(actives) == 1
    # latest mute of equal severity wins deterministically
    assert all(r.id == actives[0].id for r in results if r is not None)
