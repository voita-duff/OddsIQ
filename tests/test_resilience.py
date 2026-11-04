from __future__ import annotations

from datetime import datetime, timezone

import pytest

from oddsiq.db.models import Game
from oddsiq.pipeline.schedule_service import ScheduleService


class _FakeSource:
    async def fetch_games(self, sport_key):
        return [Game(
            external_id=f"e-{sport_key}", home_team="Boston Celtics",
            away_team="Los Angeles Lakers",
            scheduled_at=datetime(2026, 6, 9, tzinfo=timezone.utc), sport="nba",
        )]

    async def close(self):
        ...


class _FakeStore:
    def __init__(self):
        self._n = 0

    async def get_handled_live_game_ids(self):
        return set()

    async def upsert_game(self, game):
        self._n += 1
        return self._n


class _FakeSettlement:
    async def recover_finalized_game(self, game):
        ...


@pytest.mark.asyncio
async def test_refresh_prunes_handled_live_to_slate():
    svc = ScheduleService(
        schedule_source=_FakeSource(), store=_FakeStore(), settlement=_FakeSettlement(),
    )
    svc._handled_live = {1, 2, 999}  # 999 is not in the new slate
    await svc.refresh()
    assert 999 not in svc._handled_live
    assert svc._handled_live <= {1, 2, 3}


import logging


@pytest.mark.asyncio
async def test_recover_at_startup_swallows_errors(caplog):
    from oddsiq.pipeline.scheduler import Scheduler

    class _S:
        async def load_handled_live(self):
            raise RuntimeError("db blip")

    class _Set:
        async def recover_historical_bets(self):
            raise RuntimeError("recovery blip")

    sch = Scheduler.__new__(Scheduler)  # bypass full __init__
    sch._schedule = _S()
    sch._settlement = _Set()
    with caplog.at_level(logging.ERROR):
        await sch._recover_at_startup()  # must NOT raise
    assert "recovery" in caplog.text.lower()


@pytest.mark.asyncio
async def test_maybe_alert_budget_logs_once_each(caplog):
    """Budget exhaustion and max-throttle each log a WARNING once (the
    2026-08-10 rebrand replaced these Telegram alerts with logs — the phone
    only carries real-money placed/won/lost)."""
    from oddsiq.pipeline.scheduler import Scheduler

    class _Gov:
        def snapshot(self):
            return (20000, 0, 20000)  # remaining == 0

        def throttle_level(self, now):
            return 3

    sch = Scheduler.__new__(Scheduler)
    sch._budget_alert_exhausted_sent = False
    sch._budget_alert_throttle3_sent = False
    sch._budget_governor = _Gov()
    now = datetime(2026, 6, 9, tzinfo=timezone.utc)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="oddsiq.pipeline.scheduler"):
        await sch._maybe_alert_budget(now)
        await sch._maybe_alert_budget(now)  # second call must not re-log
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("EXHAUSTED" in m for m in msgs) == 1
    assert sum("throttle" in m.lower() for m in msgs) == 1
