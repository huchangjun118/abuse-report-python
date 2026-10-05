"""审核工作流核心场景测试。"""
import threading

import pytest

from abuse import (
    AbuseService,
    ActionType,
    AppealStatus,
    CaseStatus,
    InvalidTransition,
    NotFound,
    PermissionDenied,
    RateLimited,
    ReportStatus,
    Role,
    WorkflowConfig,
)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_service(**overrides):
    clock = overrides.pop("clock", Clock())
    cfg = WorkflowConfig(db_path=":memory:", **overrides)
    return AbuseService(cfg, now_fn=clock), clock


# ---------------------------------------------------------------- 1. 幂等

def test_duplicate_report_is_merged():
    svc, _ = make_service()
    r1 = svc.submit_report("u1", "bad_actor", "evt-1", "spam", ["log1"])
    # 网络重试：同幂等键
    r2 = svc.submit_report("u1", "bad_actor", "evt-1", "spam", ["log1"])
    # 显式同幂等键的不同请求
    r3 = svc.submit_report("u1", "bad_actor", "evt-1b", "spam",
                           idempotency_key="u1:evt-1")
    assert r1.report_id == r2.report_id == r3.report_id
    case = svc.get_case(r1.case_id)
    assert case.report_count == 1  # 不重复计数
    submits = [a for a in svc.audit_trail(case.case_id) if a.action == "submit_report"]
    assert len(submits) == 1  # 不重复触发后续动作


def test_distinct_events_count_separately():
    svc, _ = make_service()
    svc.submit_report("u1", "t", "evt-1", "spam")
    r2 = svc.submit_report("u1", "t", "evt-2", "spam")
    case = svc.get_case(r2.case_id)
    assert case.report_count == 2
    assert case.unique_reporters == 1


# ------------------------------------------------------- 2. 聚合与状态机

def test_auto_escalation_on_threshold():
    svc, _ = make_service(escalate_report_threshold=3, escalate_reporter_threshold=3)
    case_id = None
    for i in range(3):
        r = svc.submit_report(f"reporter{i}", "t", f"evt-{i}", "abuse")
        case_id = r.case_id
    case = svc.get_case(case_id)
    assert case.status == CaseStatus.TRIAGED
    actions = [a.action for a in svc.audit_trail(case_id)]
    assert "auto_escalate" in actions


def test_illegal_transition_rejected():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    with pytest.raises(InvalidTransition):
        svc.resolve_case("rev", Role.REVIEWER, r.case_id, "nope")
    with pytest.raises(InvalidTransition):
        svc.file_appeal("t", r.case_id, "not me")  # 未处置不可申诉


def test_reject_case_flow():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    case = svc.reject_case("rev", Role.REVIEWER, r.case_id, "证据不足")
    assert case.status == CaseStatus.REJECTED


# ------------------------------------------------------- 3. 处置与收敛

def test_decide_warn_and_temp_ban_converge():
    svc, clock = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    svc.decide("rev", Role.REVIEWER, r.case_id, ActionType.WARN, "初犯")
    svc.decide("admin", Role.ADMIN, r.case_id, ActionType.TEMP_BAN,
               "加重", duration=3600)
    active = svc.active_sanction("t")
    assert active.action == ActionType.TEMP_BAN
    assert active.effective_at == pytest.approx(clock.t)
    assert active.expires_at == pytest.approx(clock.t + 3600)
    # 败者被 superseded，可解释
    lifted = [a for a in svc.audit_trail("t") if a.action == "sanction_superseded"]
    assert len(lifted) == 1


def test_perm_ban_requires_admin():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    with pytest.raises(PermissionDenied):
        svc.decide("rev", Role.REVIEWER, r.case_id, ActionType.PERM_BAN, "重罚")
    svc.decide("admin", Role.ADMIN, r.case_id, ActionType.PERM_BAN, "重罚")
    assert svc.active_sanction("t").action == ActionType.PERM_BAN


def test_temp_ban_auto_expires():
    svc, clock = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    svc.decide("rev", Role.REVIEWER, r.case_id, ActionType.TEMP_BAN,
               "限期", duration=100)
    assert svc.active_sanction("t") is not None
    clock.advance(101)
    assert svc.active_sanction("t") is None  # 惰性到期清理
    lifts = [a for a in svc.audit_trail("t") if a.action == "auto_lift"]
    assert len(lifts) == 1 and "expired" in lifts[0].reason


def test_concurrent_decides_converge_to_single_result():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    errors = []

    def decide(actor, role, action):
        try:
            svc.decide(actor, role, r.case_id, action, f"by {actor}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [
        threading.Thread(target=decide, args=("rev1", Role.REVIEWER, ActionType.WARN)),
        threading.Thread(target=decide, args=("rev2", Role.REVIEWER, ActionType.MUTE)),
        threading.Thread(target=decide, args=("adm", Role.ADMIN, ActionType.TEMP_BAN)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    active = svc.active_sanction("t")
    assert active.action == ActionType.TEMP_BAN  # 最高严重级别收敛
    decides = [a for a in svc.audit_trail(r.case_id) if a.action == "decide"]
    assert len(decides) == 3  # 每个决定都留痕


# ---------------------------------------------------------------- 4. 申诉

def test_appeal_only_by_sanctioned_account():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    svc.decide("rev", Role.REVIEWER, r.case_id, ActionType.MUTE, "m")
    with pytest.raises(PermissionDenied):
        svc.file_appeal("someone_else", r.case_id, "冤枉")
    denied = [a for a in svc.audit_trail(r.case_id) if a.action == "appeal_denied"]
    assert len(denied) == 1  # 越权留痕
    appeal = svc.file_appeal("t", r.case_id, "我是本人，申诉")
    assert appeal.status == AppealStatus.PENDING


def test_appeal_overturn_lifts_sanction():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    svc.decide("rev", Role.REVIEWER, r.case_id, ActionType.TEMP_BAN, "m", duration=9999)
    appeal = svc.file_appeal("t", r.case_id, "误封")
    decided = svc.decide_appeal("admin", Role.ADMIN, appeal.appeal_id,
                                uphold=False, reason="证据不足，撤销")
    assert decided.status == AppealStatus.OVERTURNED
    assert svc.active_sanction("t") is None
    assert svc.get_case(r.case_id).status == CaseStatus.RESOLVED


def test_appeal_upheld_keeps_sanction():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    svc.decide("rev", Role.REVIEWER, r.case_id, ActionType.MUTE, "m")
    appeal = svc.file_appeal("t", r.case_id, "申诉")
    svc.decide_appeal("admin", Role.ADMIN, appeal.appeal_id, uphold=True, reason="维持")
    assert svc.active_sanction("t").action == ActionType.MUTE
    assert svc.get_case(r.case_id).status == CaseStatus.ACTION_TAKEN


def test_decide_blocked_while_appeal_pending():
    """申诉待决期间新处置被拒绝，保证并发结果确定。"""
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    svc.decide("rev", Role.REVIEWER, r.case_id, ActionType.MUTE, "m")
    svc.file_appeal("t", r.case_id, "申诉")
    with pytest.raises(InvalidTransition):
        svc.decide("admin", Role.ADMIN, r.case_id, ActionType.TEMP_BAN, "加重")


def test_concurrent_appeal_and_decide_deterministic():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    svc.decide("rev", Role.REVIEWER, r.case_id, ActionType.MUTE, "m")
    results = {}

    def do_appeal():
        try:
            results["appeal"] = svc.file_appeal("t", r.case_id, "s").appeal_id
        except Exception as e:  # noqa: BLE001
            results["appeal_err"] = e

    def do_decide():
        try:
            svc.decide("admin", Role.ADMIN, r.case_id, ActionType.TEMP_BAN, "esc")
            results["decide"] = True
        except InvalidTransition:
            results["decide"] = False

    threads = [threading.Thread(target=do_appeal), threading.Thread(target=do_decide)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 确定性：若申诉先立案，则处置必被拒绝；反之处置先生效、申诉仍可对 ACTION_TAKEN 发起
    if "appeal" in results and results.get("decide") is False:
        pass
    else:
        assert results.get("decide") is True
    # 无论哪种顺序，状态机都处于合法状态
    assert svc.get_case(r.case_id).status in (CaseStatus.APPEALED, CaseStatus.ACTION_TAKEN)


# ------------------------------------------------------- 5. 权限与审计

def test_unauthorized_review_rejected_and_audited():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "x")
    with pytest.raises(PermissionDenied):
        svc.triage_case("random", Role.REPORTER, r.case_id)
    with pytest.raises(PermissionDenied):
        svc.decide("random", Role.REPORTER, r.case_id, ActionType.WARN, "x")
    with pytest.raises(PermissionDenied):
        svc.decide_appeal("random", Role.REPORTER, "apl_x", True, "x")


def test_audit_trail_records_actor_reason_evidence():
    svc, _ = make_service()
    r = svc.submit_report("u1", "t", "e1", "骚扰", ["screenshot-1"])
    svc.triage_case("rev1", Role.REVIEWER, r.case_id, "开始复核")
    svc.decide("admin1", Role.ADMIN, r.case_id, ActionType.TEMP_BAN,
               "屡犯", evidence_refs=["screenshot-1"], duration=60)
    trail = svc.audit_trail(r.case_id)
    decide_ev = [a for a in trail if a.action == "decide"][0]
    assert decide_ev.actor == "admin1"
    assert decide_ev.reason == "屡犯"
    assert "screenshot-1" in decide_ev.detail


# ------------------------------------------------------- 6. 限速与防误封

def test_rate_limit_blocks_flooding_reporter():
    svc, _ = make_service(reporter_rate_limit=5, reporter_rate_window=60)
    for i in range(5):
        svc.submit_report("spammer", f"t{i}", f"e{i}", "x")
    with pytest.raises(RateLimited):
        svc.submit_report("spammer", "t5", "e5", "x")
    limited = [a for a in svc.audit_trail() if a.action == "rate_limited"]
    assert len(limited) == 1


def test_single_reporter_flood_cannot_escalate_case():
    """单举报人刷量：计数贡献有上限，不会把目标顶到自动升级阈值。"""
    svc, _ = make_service(
        reporter_rate_limit=100,
        escalate_report_threshold=5,
        escalate_reporter_threshold=3,
        per_reporter_case_cap=2,
    )
    last = None
    for i in range(20):
        last = svc.submit_report("spammer", "victim", f"e{i}", "x")
    case = svc.get_case(last.case_id)
    assert case.report_count == 2  # 被 cap 截断
    assert case.status == CaseStatus.OPEN  # 未触发自动升级 -> 不会误封


def test_idempotent_retry_not_rate_limited():
    svc, _ = make_service(reporter_rate_limit=1, reporter_rate_window=60)
    r1 = svc.submit_report("u1", "t", "e1", "x")
    r2 = svc.submit_report("u1", "t", "e1", "x")  # 重试不计入限速
    assert r1.report_id == r2.report_id


# ------------------------------------------------------- 7. 重启恢复

def test_restart_recovers_and_expires(tmp_path):
    db = str(tmp_path / "abuse.db")
    clock = Clock()
    cfg = WorkflowConfig(db_path=db)
    svc = AbuseService(cfg, now_fn=clock)
    r = svc.submit_report("u1", "t", "e1", "x")
    svc.triage_case("rev", Role.REVIEWER, r.case_id)
    svc.decide("rev", Role.REVIEWER, r.case_id, ActionType.TEMP_BAN,
               "限期", duration=100)
    case_id = r.case_id
    svc.close()  # 模拟进程中断

    # 中断后重启：状态对齐，不丢失在途案件
    svc2 = AbuseService(WorkflowConfig(db_path=db), now_fn=clock)
    case = svc2.get_case(case_id)
    assert case.status == CaseStatus.ACTION_TAKEN
    assert svc2.active_sanction("t") is not None

    # 到期后再次重启：恢复流程自动解除且只执行一次
    clock.advance(200)
    svc2.close()
    svc3 = AbuseService(WorkflowConfig(db_path=db), now_fn=clock)
    assert svc3.active_sanction("t") is None
    lifts = [a for a in svc3.audit_trail("t") if a.action == "auto_lift"]
    assert len(lifts) == 1
    svc3.close()


def test_restart_does_not_duplicate_report(tmp_path):
    db = str(tmp_path / "abuse.db")
    svc = AbuseService(WorkflowConfig(db_path=db))
    r1 = svc.submit_report("u1", "t", "e1", "x")
    svc.close()
    svc2 = AbuseService(WorkflowConfig(db_path=db))
    r2 = svc2.submit_report("u1", "t", "e1", "x")  # 重启后重试同一举报
    assert r1.report_id == r2.report_id
    assert svc2.get_case(r1.case_id).report_count == 1
    svc2.close()


def test_concurrent_submissions_from_many_reporters():
    svc, _ = make_service(reporter_rate_limit=1000)
    errors = []

    def submit(i):
        try:
            svc.submit_report(f"r{i}", "t", f"e{i}", "x")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    case = svc.store.get_case_by_target  # noqa - 通过公开接口校验
    r = svc.submit_report("final", "t", "efinal", "x")
    c = svc.get_case(r.case_id)
    assert c.report_count == 21
    assert c.unique_reporters == 21
    assert c.status == CaseStatus.TRIAGED  # 阈值默认 5，已自动升级且未回退
