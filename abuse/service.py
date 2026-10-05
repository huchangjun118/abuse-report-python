"""审核工作流服务。

一致性设计：
- 每个公开方法 = 一个 ``Store.transact`` 事务，中断只会留下完整前态或完整后态。
- 举报按 (reporter, event) 幂等键去重，重试/重复提交返回原举报，零副作用。
- 案件更新走乐观锁整行替换，状态机校验在事务内完成，并发下不会回退或字段混合。
- 处置收敛规则：同时生效的多个决定取严重级别最高者，同级取到期最晚者，
  其余活跃处置以 superseded 原因解除，全程留痕可解释。
- 限速在事务内计数，单个举报人刷量被限速且对案件的计数贡献有上限，
  不会因刷量把目标账号顶到自动升级阈值（防批量误封）。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable, Optional

from .config import WorkflowConfig
from .errors import InvalidTransition, NotFound, PermissionDenied, RateLimited
from .models import (
    ACTION_SEVERITY,
    CASE_TRANSITIONS,
    ActionType,
    Appeal,
    AppealStatus,
    AuditEvent,
    Case,
    CaseStatus,
    Report,
    ReportStatus,
    Role,
    Sanction,
)
from .storage import Store

# 角色权限矩阵
_REVIEW_ROLES = {Role.REVIEWER, Role.ADMIN}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class AbuseService:
    def __init__(
        self,
        config: WorkflowConfig | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or WorkflowConfig()
        self.store = Store(self.config.db_path)
        self._now = now_fn or time.time
        self.recover()

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    def recover(self) -> None:
        """启动恢复：解除已到期的限期封禁（幂等），其余状态因事务性写入天然一致。"""
        self.expire_sanctions()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _audit(
        self,
        conn,
        actor: str,
        role: Role,
        action: str,
        target: str,
        reason: str,
        detail: dict | None = None,
    ) -> None:
        self.store.append_audit(
            conn,
            AuditEvent(
                event_id=_new_id("aud"),
                ts=self._now(),
                actor=actor,
                role=role.value,
                action=action,
                target=target,
                reason=reason,
                detail=json.dumps(detail or {}, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _require(role: Role, allowed: set[Role], action: str) -> None:
        if role not in allowed:
            raise PermissionDenied(f"role {role.value} not allowed to {action}")

    def _transition(self, case: Case, new_status: CaseStatus) -> None:
        allowed = CASE_TRANSITIONS[case.status]
        if new_status not in allowed:
            raise InvalidTransition(
                f"case {case.case_id}: {case.status.value} -> {new_status.value} not allowed"
            )
        case.status = new_status

    # ------------------------------------------------------------------
    # 1. 举报提交（幂等 + 限速 + 聚合 + 自动升级）
    # ------------------------------------------------------------------

    def submit_report(
        self,
        reporter_id: str,
        target_account: str,
        event_id: str,
        reason: str,
        evidence: Optional[list[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Report:
        """提交举报。相同 (reporter, event) 的重复提交/网络重试归并为同一条。"""
        key = idempotency_key or f"{reporter_id}:{event_id}"
        now = self._now()

        # 快速路径：已存在的幂等键直接返回原举报，不受限速影响（重试安全）
        existing = self.store.get_report_by_key(key)
        if existing is not None:
            return existing

        try:
            with self.store.transact() as conn:
                # 事务内复查：并发下另一事务可能已抢先写入同幂等键
                existing = self.store.get_report_by_key(key)
                if existing is not None:
                    return existing

                # 限速：窗口内提交次数（同事务计数，并发安全）
                window = self.config.reporter_rate_window
                bucket = now - (now % window)
                count = self.store.hit_rate_bucket(conn, reporter_id, bucket)
                if count > self.config.reporter_rate_limit:
                    raise _RateLimitedSignal()

                report = Report(
                    report_id=_new_id("rpt"),
                    idempotency_key=key,
                    reporter_id=reporter_id,
                    target_account=target_account,
                    event_id=event_id,
                    reason=reason,
                    evidence=list(evidence or []),
                    created_at=now,
                )
                if not self.store.insert_report(conn, report):
                    raise Conflict(f"duplicate idempotency key {key}")

                case = self.store.get_case_by_target(conn, target_account)
                if case is None:
                    case = Case(
                        case_id=_new_id("case"),
                        target_account=target_account,
                        created_at=now,
                        updated_at=now,
                    )
                    self.store.insert_case(conn, case)

                # 单举报人对单案件的计数贡献有上限，防刷量顶到阈值造成误封
                contributed = case.reporter_counts.get(reporter_id, 0)
                if contributed < self.config.per_reporter_case_cap:
                    case.reporter_counts[reporter_id] = contributed + 1
                    case.report_count += 1
                    case.unique_reporters = len(case.reporter_counts)
                    case.evidence.extend(report.evidence)
                    report.status = ReportStatus.AGGREGATED
                    report.case_id = case.case_id
                else:
                    report.status = ReportStatus.DUPLICATE
                    report.case_id = case.case_id
                    self._audit(
                        conn, reporter_id, Role.REPORTER, "reporter_cap_exceeded",
                        case.case_id, "per-reporter contribution capped, not counted",
                    )

                # 自动升级：仅在 OPEN 时推进到 TRIAGED，绝不覆盖人工更靠后的状态
                escalated = False
                if case.status == CaseStatus.OPEN and (
                    case.report_count >= self.config.escalate_report_threshold
                    or case.unique_reporters >= self.config.escalate_reporter_threshold
                ):
                    self._transition(case, CaseStatus.TRIAGED)
                    escalated = True

                case.updated_at = now
                self.store.update_case(conn, case)
                self.store.update_report_status(conn, report.report_id, report.status, report.case_id)

                self._audit(
                    conn, reporter_id, Role.REPORTER, "submit_report",
                    case.case_id, reason,
                    {"report_id": report.report_id, "event_id": event_id,
                     "counted": report.status == ReportStatus.AGGREGATED},
                )
                if escalated:
                    self._audit(
                        conn, "system", Role.SYSTEM, "auto_escalate",
                        case.case_id,
                        f"threshold reached: reports={case.report_count}, "
                        f"reporters={case.unique_reporters}",
                    )
                return report
        except _RateLimitedSignal:
            # 拒绝也要留痕：在独立事务中记录，避免随业务回滚丢失
            with self.store.transact() as audit_conn:
                self._audit(
                    audit_conn, reporter_id, Role.REPORTER, "rate_limited",
                    target_account,
                    f"exceeded {self.config.reporter_rate_limit}/"
                    f"{self.config.reporter_rate_window}s",
                )
            raise RateLimited(f"reporter {reporter_id} rate limited")

    # ------------------------------------------------------------------
    # 2. 案件复核
    # ------------------------------------------------------------------

    def triage_case(self, actor: str, role: Role, case_id: str, note: str = "") -> Case:
        """人工开始复核：OPEN -> TRIAGED。"""
        self._require(role, _REVIEW_ROLES, "triage")
        with self.store.transact() as conn:
            case = self._must_case(conn, case_id)
            self._transition(case, CaseStatus.TRIAGED)
            case.updated_at = self._now()
            self.store.update_case(conn, case)
            self._audit(conn, actor, role, "triage", case_id, note or "manual triage")
            return case

    def reject_case(self, actor: str, role: Role, case_id: str, reason: str) -> Case:
        """复核不成立：OPEN/TRIAGED -> REJECTED。"""
        self._require(role, _REVIEW_ROLES, "reject")
        with self.store.transact() as conn:
            case = self._must_case(conn, case_id)
            self._transition(case, CaseStatus.REJECTED)
            case.updated_at = self._now()
            self.store.update_case(conn, case)
            self._audit(conn, actor, role, "reject_case", case_id, reason)
            return case

    def _must_case(self, conn, case_id: str) -> Case:
        case = self.store.get_case(case_id)
        if case is None:
            raise NotFound(f"case {case_id} not found")
        return case

    # ------------------------------------------------------------------
    # 3. 处置决定（并发收敛为唯一结果）
    # ------------------------------------------------------------------

    def decide(
        self,
        actor: str,
        role: Role,
        case_id: str,
        action: ActionType,
        reason: str,
        evidence_refs: Optional[list[str]] = None,
        duration: Optional[float] = None,
    ) -> Sanction:
        """对案件作出处置。永久封禁需 ADMIN，其余 REVIEWER 即可。"""
        if action == ActionType.PERM_BAN:
            self._require(role, {Role.ADMIN}, "perm_ban")
        else:
            self._require(role, _REVIEW_ROLES, "decide")

        now = self._now()
        with self.store.transact() as conn:
            case = self._must_case(conn, case_id)
            # 申诉待决期间禁止新处置，保证申诉与处置并发时结果确定
            if self.store.get_pending_appeal(conn, case.case_id) is not None:
                raise InvalidTransition("appeal pending; resolve appeal before new decision")
            if case.status == CaseStatus.OPEN:
                # 人工直接处置视为已复核（人工优先于自动升级路径）
                self._transition(case, CaseStatus.TRIAGED)
            if case.status != CaseStatus.ACTION_TAKEN:
                # ACTION_TAKEN 上的新决定是加重/变更，由收敛规则处理
                self._transition(case, CaseStatus.ACTION_TAKEN)

            if action == ActionType.TEMP_BAN:
                dur = duration if duration is not None else self.config.temp_ban_duration
                expires = now + dur
            elif action in (ActionType.WARN, ActionType.MUTE):
                expires = now + (duration or 0) if duration else None
            else:
                expires = None  # 永久

            sanction = Sanction(
                sanction_id=_new_id("snc"),
                case_id=case.case_id,
                target_account=case.target_account,
                action=action,
                reason=reason,
                evidence_refs=list(evidence_refs or case.evidence),
                operator=actor,
                effective_at=now,
                expires_at=expires,
            )
            self.store.insert_sanction(conn, sanction)

            # 收敛：新决定与现存活跃决定按严重级别取唯一生效结果
            winner = self._converge(conn, case.target_account, sanction)

            case.updated_at = now
            self.store.update_case(conn, case)
            self._audit(
                conn, actor, role, "decide", case.case_id, reason,
                {"action": action.value, "sanction_id": sanction.sanction_id,
                 "effective_at": now, "expires_at": expires,
                 "evidence_refs": sanction.evidence_refs,
                 "effective_winner": winner.sanction_id},
            )
            return sanction

    def _converge(self, conn, target: str, new_sanction: Sanction) -> Sanction:
        """严重级别高者生效；同级到期晚者生效；败者以 superseded 解除。"""
        actives = self.store.list_sanctions(conn, target, active_only=True)
        winner = max(
            actives,
            key=lambda s: (
                ACTION_SEVERITY[s.action],
                s.expires_at if s.expires_at is not None else float("inf"),
                s.effective_at,
            ),
        )
        for s in actives:
            if s.sanction_id != winner.sanction_id:
                self.store.lift_sanction(conn, s.sanction_id, f"superseded_by:{winner.sanction_id}")
                self._audit(
                    conn, "system", Role.SYSTEM, "sanction_superseded",
                    target, f"superseded by {winner.sanction_id}",
                    {"loser": s.sanction_id, "winner": winner.sanction_id},
                )
        return winner

    # ------------------------------------------------------------------
    # 限期封禁到期自动解除
    # ------------------------------------------------------------------

    def expire_sanctions(self) -> list[Sanction]:
        """解除所有已到期的活跃处置（幂等，可反复调用）。"""
        now = self._now()
        expired = self.store.list_expired_active_sanctions(now)
        lifted: list[Sanction] = []
        for s in expired:
            with self.store.transact() as conn:
                self.store.lift_sanction(conn, s.sanction_id, "temp_ban_expired")
                self._audit(
                    conn, "system", Role.SYSTEM, "auto_lift",
                    s.target_account,
                    f"sanction {s.sanction_id} expired at {s.expires_at}",
                    {"sanction_id": s.sanction_id, "action": s.action.value,
                     "effective_at": s.effective_at, "expires_at": s.expires_at},
                )
                s.active = False
                s.lifted_reason = "temp_ban_expired"
                lifted.append(s)
        return lifted

    def active_sanction(self, target_account: str) -> Optional[Sanction]:
        """查询账号当前生效处置（先惰性到期清理，保证读到的状态是收敛后的）。"""
        self.expire_sanctions()
        with self.store.transact() as conn:
            actives = self.store.list_sanctions(conn, target_account, active_only=True)
        if not actives:
            return None
        return max(
            actives,
            key=lambda s: (
                ACTION_SEVERITY[s.action],
                s.expires_at if s.expires_at is not None else float("inf"),
            ),
        )

    # ------------------------------------------------------------------
    # 4. 申诉
    # ------------------------------------------------------------------

    def file_appeal(self, appellant_id: str, case_id: str, reason: str) -> Appeal:
        """仅被处置方本人可发起；同一案件同时只允许一个待决申诉。"""
        now = self._now()
        # 越权尝试也要留痕：先查案件，再在独立事务中记录拒绝
        target_case = self.store.get_case(case_id)
        if target_case is not None and appellant_id != target_case.target_account:
            with self.store.transact() as audit_conn:
                self._audit(
                    audit_conn, appellant_id, Role.REPORTER, "appeal_denied",
                    target_case.case_id, "only the sanctioned account may appeal",
                )
            raise PermissionDenied("only the sanctioned account may appeal")
        with self.store.transact() as conn:
            case = self._must_case(conn, case_id)
            if appellant_id != case.target_account:
                raise PermissionDenied("only the sanctioned account may appeal")
            if case.status != CaseStatus.ACTION_TAKEN:
                raise InvalidTransition(f"cannot appeal case in {case.status.value}")
            if self.store.get_pending_appeal(conn, case.case_id) is not None:
                raise InvalidTransition("an appeal is already pending for this case")

            appeal = Appeal(
                appeal_id=_new_id("apl"),
                case_id=case.case_id,
                appellant_id=appellant_id,
                reason=reason,
                created_at=now,
            )
            self.store.insert_appeal(conn, appeal)
            self._transition(case, CaseStatus.APPEALED)
            case.updated_at = now
            self.store.update_case(conn, case)
            self._audit(
                conn, appellant_id, Role.REPORTER, "file_appeal",
                case.case_id, reason, {"appeal_id": appeal.appeal_id},
            )
            return appeal

    def decide_appeal(
        self,
        actor: str,
        role: Role,
        appeal_id: str,
        uphold: bool,
        reason: str,
    ) -> Appeal:
        """复核申诉：维持（uphold）或撤销处置（overturn）。"""
        self._require(role, _REVIEW_ROLES, "decide_appeal")
        now = self._now()
        with self.store.transact() as conn:
            appeal = self.store.get_appeal(appeal_id)
            if appeal is None:
                raise NotFound(f"appeal {appeal_id} not found")
            if appeal.status != AppealStatus.PENDING:
                raise InvalidTransition("appeal already decided")
            case = self._must_case(conn, appeal.case_id)

            appeal.decided_by = actor
            appeal.decision_reason = reason
            appeal.decided_at = now
            if uphold:
                appeal.status = AppealStatus.UPHELD
                self._transition(case, CaseStatus.ACTION_TAKEN)
            else:
                appeal.status = AppealStatus.OVERTURNED
                for s in self.store.list_sanctions(conn, case.target_account, active_only=True):
                    self.store.lift_sanction(conn, s.sanction_id, f"appeal_overturned:{appeal_id}")
                self._transition(case, CaseStatus.RESOLVED)
            self.store.update_appeal(conn, appeal)
            case.updated_at = now
            self.store.update_case(conn, case)
            self._audit(
                conn, actor, role,
                "appeal_upheld" if uphold else "appeal_overturned",
                case.case_id, reason,
                {"appeal_id": appeal_id, "appellant": appeal.appellant_id},
            )
            return appeal

    def resolve_case(self, actor: str, role: Role, case_id: str, reason: str) -> Case:
        """人工结案：ACTION_TAKEN -> RESOLVED（处置保持生效直至到期/申诉）。"""
        self._require(role, _REVIEW_ROLES, "resolve")
        with self.store.transact() as conn:
            case = self._must_case(conn, case_id)
            self._transition(case, CaseStatus.RESOLVED)
            case.updated_at = self._now()
            self.store.update_case(conn, case)
            self._audit(conn, actor, role, "resolve_case", case_id, reason)
            return case

    # ------------------------------------------------------------------
    # 5. 审计查询
    # ------------------------------------------------------------------

    def audit_trail(self, target: Optional[str] = None) -> list[AuditEvent]:
        return self.store.list_audit(target)

    def get_case(self, case_id: str) -> Case:
        case = self.store.get_case(case_id)
        if case is None:
            raise NotFound(f"case {case_id} not found")
        return case


class _RateLimitedSignal(Exception):
    """内部信号：触发限速，需回滚业务写入后在独立事务中留痕。"""
