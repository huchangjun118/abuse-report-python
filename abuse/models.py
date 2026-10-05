"""领域模型与枚举定义。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class Role(str, enum.Enum):
    """操作者角色。"""

    REPORTER = "reporter"      # 普通举报人
    REVIEWER = "reviewer"      # 复核员：可复核案件
    ADMIN = "admin"            # 管理员：可变更处置
    SYSTEM = "system"          # 系统自动动作


class ReportStatus(str, enum.Enum):
    RECEIVED = "received"
    DUPLICATE = "duplicate"    # 幂等归并后的重复提交
    AGGREGATED = "aggregated"  # 已并入案件


class CaseStatus(str, enum.Enum):
    OPEN = "open"
    TRIAGED = "triaged"        # 达到阈值自动升级 / 人工开始复核
    ACTION_TAKEN = "action_taken"
    APPEALED = "appealed"
    RESOLVED = "resolved"
    REJECTED = "rejected"      # 复核认为不成立


# 允许的案件状态转换（单向推进，禁止回退）
CASE_TRANSITIONS: dict[CaseStatus, set[CaseStatus]] = {
    CaseStatus.OPEN: {CaseStatus.TRIAGED, CaseStatus.REJECTED},
    CaseStatus.TRIAGED: {CaseStatus.ACTION_TAKEN, CaseStatus.REJECTED},
    CaseStatus.ACTION_TAKEN: {CaseStatus.APPEALED, CaseStatus.RESOLVED},
    CaseStatus.APPEALED: {CaseStatus.RESOLVED, CaseStatus.ACTION_TAKEN},
    CaseStatus.RESOLVED: set(),
    CaseStatus.REJECTED: set(),
}


class ActionType(str, enum.Enum):
    WARN = "warn"
    MUTE = "mute"
    TEMP_BAN = "temp_ban"
    PERM_BAN = "perm_ban"


# 处置严重级别，用于并发决定收敛
ACTION_SEVERITY: dict[ActionType, int] = {
    ActionType.WARN: 1,
    ActionType.MUTE: 2,
    ActionType.TEMP_BAN: 3,
    ActionType.PERM_BAN: 4,
}


class AppealStatus(str, enum.Enum):
    PENDING = "pending"
    UPHELD = "upheld"      # 维持原处置
    OVERTURNED = "overturned"  # 撤销处置


@dataclass
class Report:
    report_id: str
    idempotency_key: str       # 来源 + 事件 的幂等键
    reporter_id: str
    target_account: str
    event_id: str              # 同一次事件标识
    reason: str
    evidence: list[str] = field(default_factory=list)
    status: ReportStatus = ReportStatus.RECEIVED
    case_id: Optional[str] = None
    created_at: float = 0.0


@dataclass
class Case:
    case_id: str
    target_account: str
    status: CaseStatus = CaseStatus.OPEN
    report_count: int = 0
    unique_reporters: int = 0
    evidence: list[str] = field(default_factory=list)
    reporter_counts: dict[str, int] = field(default_factory=dict)  # 举报人 -> 计入次数
    version: int = 0           # 乐观锁版本号
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class Sanction:
    sanction_id: str
    case_id: str
    target_account: str
    action: ActionType
    reason: str
    evidence_refs: list[str] = field(default_factory=list)
    operator: str = ""
    effective_at: float = 0.0
    expires_at: Optional[float] = None   # None 表示永久
    active: bool = True
    lifted_reason: Optional[str] = None  # 解除原因（到期/申诉成立），可解释


@dataclass
class Appeal:
    appeal_id: str
    case_id: str
    appellant_id: str          # 必须等于被处置账号
    reason: str
    status: AppealStatus = AppealStatus.PENDING
    decided_by: Optional[str] = None
    decision_reason: Optional[str] = None
    created_at: float = 0.0
    decided_at: Optional[float] = None


@dataclass
class AuditEvent:
    event_id: str
    ts: float
    actor: str
    role: str
    action: str                # 如 submit_report / decide / appeal / resolve ...
    target: str                # 案件/账号/举报 id
    reason: str
    detail: str = ""           # JSON 依据快照
