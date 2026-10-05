from __future__ import annotations

from datetime import datetime, timezone

import pytest

from abuse.clock import Clock
from abuse.config import Config
from abuse.enums import Role
from abuse.models import Actor
from abuse.workflow import Workflow


class ManualClock(Clock):
    """Deterministic clock advanced explicitly by tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> datetime:
        from datetime import timedelta

        self.t = self.t + timedelta(seconds=seconds)
        return self.t


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def config() -> Config:
    return Config.default()


@pytest.fixture
def wf(clock, config) -> Workflow:
    workflow = Workflow(":memory:", config=config, clock=clock, start_sweeper=False)
    workflow.start()
    yield workflow
    workflow.close()


def actor(id_: str, role: Role = Role.USER) -> Actor:
    return Actor(id=id_, role=role)
