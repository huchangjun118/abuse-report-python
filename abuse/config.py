"""Configuration for thresholds and decision convergence."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Config:
    # action type -> default duration in seconds (None = permanent / no expiry)
    action_durations: dict[str, int | None] = field(default_factory=dict)
    # auto escalation thresholds
    rate_limit_window_seconds: int = 60
    rate_limit_max_per_reporter: int = 5
    auto_escalate_distinct_reporters: int = 3
    auto_escalate_window_seconds: int = 3600
    auto_ban_distinct_reporters: int = 10
    auto_action_enabled: bool = True
    # background sweeper tick
    sweep_interval_seconds: float = 1.0

    @classmethod
    def default(cls) -> "Config":
        return cls(
            action_durations={
                "warn": None,
                "mute": 3600,
                "ban_timed": 86400,
                "ban_perm": None,
            },
        )
