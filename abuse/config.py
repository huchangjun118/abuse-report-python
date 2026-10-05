"""工作流配置项。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WorkflowConfig:
    # 自动升级阈值：案件累计举报数达到该值时自动 TRIAGED
    escalate_report_threshold: int = 5
    # 自动升级阈值：独立举报人数达到该值时自动 TRIAGED
    escalate_reporter_threshold: int = 3
    # 举报人限速：窗口内最多提交次数
    reporter_rate_limit: int = 10
    # 限速窗口（秒）
    reporter_rate_window: float = 60.0
    # 限期封禁默认时长（秒）
    temp_ban_duration: float = 7 * 24 * 3600.0
    # 同一举报人针对同一目标的刷量，在窗口内最多计入案件的次数
    per_reporter_case_cap: int = 3
    # 数据文件路径（":memory:" 用于测试）
    db_path: str = ":memory:"
