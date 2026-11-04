"""Integration tests: full game lifecycle through the real Scheduler + Store.

Each test uses a real in-memory SQLite DB (schema applied via migrations.run),
real Scheduler / Store APIs, and mock scrapers injected via sch._scrapers —
the same pattern used throughout test_scheduler.py.

Tests:
  1. test_moneyline_full_lifecycle       — hot-tier edge detection → closing mark
                                           → CLV + result on final game.
  2. test_total_market_resolution_*      — over/under/push total bets resolve
                                           correctly end-to-end via _settlement.resolve_bets.
  3. test_scraper_error_isolation        — one scraper raising must not abort the
                                           poll cycle; the other scraper's odds
                                           are still persisted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from oddsiq.db import migrations
from oddsiq.db.models import Game, OddsSnapshot
from oddsiq.db.store import Store
from oddsiq.pipeline.scheduler import Scheduler
from oddsiq.sports.profiles import NBA


# ---------------------------------------------------------------------------
# Autouse fixtures (mirrors test_scheduler.py hermetic setup)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence_telegram(monkeypatch):
    """Suppress outbound Telegram for every test in this file."""
    async def _noop(_text: str) -> None:
        return None

    monkeypatch.setattr("oddsiq.pipeline.scheduler.telegram.send_telegram", _noop)


@pytest.fixture(autouse=True)
def _hermetic_bookmaker_flags(monkeypatch):
    """Pin bookmaker flags to production defaults so local .env doesn't bleed in."""
    monkeypatch.setattr("oddsiq.scrapers.registry.config.BOOKMAKER_TIPSPORT_ENABLED", True)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path):
    db_path = tmp_path / "test.db"
    migrations.run(db_path)
    s = Store(db_path)
    await s.initialize()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Snapshot builders (copied from test_scheduler.py style)
# ---------------------------------------------------------------------------


def _pin_pair(
    home: str,
    away: str,
    *,
    home_odds: float = 1.70,
    away_odds: float = 2.20,
) -> list[OddsSnapshot]:
    """Pinnacle moneyline pair. Default odds produce a vig-removed fair of
    roughly 1.636 / 2.139 — Tipsport home @ 2.05 exceeds that by >3%."""
    return [
        OddsSnapshot(
            game_id=0, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=home_odds,
            _home_team=home, _away_team=away,
        ),
        OddsSnapshot(
            game_id=0, bookmaker="pinnacle", market_type="moneyline",
            selection="away", odds=away_odds,
            _home_team=home, _away_team=away,
        ),
    ]


def _tip_snap(
    home: str,
    away: str,
    *,
    selection: str = "home",
    odds: float = 2.05,
    market_type: str = "moneyline",
    line: float | None = None,
) -> OddsSnapshot:
    return OddsSnapshot(
        game_id=0, bookmaker="tipsport", market_type=market_type,
        selection=selection, odds=odds, line=line,
        _home_team=home, _away_team=away,
        _commence_time=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


def _pin_total_pair(
    home: str, away: str, *, line: float, over_odds: float = 1.91, under_odds: float = 1.91
) -> list[OddsSnapshot]:
    return [
        OddsSnapshot(
            game_id=0, bookmaker="pinnacle", market_type="total",
            selection="over", odds=over_odds, line=line,
            _home_team=home, _away_team=away,
        ),
        OddsSnapshot(
            game_id=0, bookmaker="pinnacle", market_type="total",
            selection="under", odds=under_odds, line=line,
            _home_team=home, _away_team=away,
        ),
    ]


# ---------------------------------------------------------------------------
# Helper: seed a hot-tier game (N seconds to tipoff)
# ---------------------------------------------------------------------------


async def _make_hot_game(
    store: Store,
    *,
    external_id: str,
    seconds_to_tipoff: float = 600,
    home_team: str = "Boston Celtics",
    away_team: str = "Los Angeles Lakers",
) -> Game:
    now = datetime.now(timezone.utc)
    game = Game(
        external_id=external_id,
        home_team=home_team,
        away_team=away_team,
        scheduled_at=now + timedelta(seconds=seconds_to_tipoff),
        status="scheduled",
    )
    game.id = await store.upsert_game(game)
    return game


# ---------------------------------------------------------------------------
# Task 1: full moneyline lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moneyline_full_lifecycle(store: Store):
    """End-to-end: hot-tier poll detects edge → closing mark → CLV + result.

    Phase 1 — poll:
      A mock Pinnacle returns a mispriced moneyline pair; mock Tipsport
      returns a home price (2.05) above the vig-removed fair. After one
      poll() call a value_bet row must exist with the expected
      selection and positive edge_pct.

    Phase 2 — closing mark:
      We plant pre-tipoff snapshots and call mark_closing_snapshots.
      The snapshots must be flagged is_closing=1.

    Phase 3 — CLV + result:
      After updating the game to final (home wins 112-105) we call
      _settlement.calculate_clv_for_pending_bets and _settlement.resolve_bets. Both clv_pct
      and result ('win' for home) must be set on the value_bet row.
    """
    sch = Scheduler(store)
    sch._schedule._schedule_source.fetch_games = AsyncMock(return_value=[])
    sch._running = True

    game = await _make_hot_game(store, external_id="lifecycle_ml_001")
    sch._schedule._games = [game]

    # Inject mock scrapers — mirrors _stub_scrapers pattern in test_scheduler.py.
    pin = next(s for s in sch._scrapers if s.name == "pinnacle")
    tip = next(s for s in sch._scrapers if s.name == "tipsport")
    pin.fetch_odds = AsyncMock(return_value=_pin_pair(game.home_team, game.away_team))
    tip.fetch_odds = AsyncMock(return_value=[
        _tip_snap(game.home_team, game.away_team, selection="home", odds=2.05),
    ])

    # --- Phase 1: poll detects edge ---
    await sch._poll_coordinator.poll(NBA, [game], sch._schedule.games, datetime.now(timezone.utc))

    cursor = await store._db.execute(
        "SELECT bookmaker, market_type, selection, entry_odds, edge_pct, tier "
        "FROM value_bets WHERE game_id=?",
        (game.id,),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1, f"expected one value_bet, got {[dict(r) for r in rows]}"
    vb_row = rows[0]
    assert vb_row["bookmaker"] == "tipsport"
    assert vb_row["market_type"] == "moneyline"
    assert vb_row["selection"] == "home"
    assert vb_row["entry_odds"] == 2.05
    assert vb_row["edge_pct"] > 0.0, "edge_pct must be positive"
    assert vb_row["tier"] == "hot"

    # --- Phase 2: closing mark ---
    # Inject fresh pre-tipoff snapshots so mark_closing_snapshots has something
    # to mark. We use a captured_at 5 min before the scheduled tipoff — well
    # within the default CLOSING_FRESHNESS_MINUTES=30 window.
    tipoff = game.scheduled_at
    captured_at = tipoff - timedelta(minutes=5)
    await store.insert_odds_snapshots_batch([
        OddsSnapshot(
            game_id=game.id, bookmaker="tipsport", market_type="moneyline",
            selection="home", odds=2.05, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.70, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="away", odds=2.20, captured_at=captured_at,
        ),
    ])
    await store.mark_closing_snapshots(game.id, freshness_minutes=30)

    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE game_id=? AND is_closing=1",
        (game.id,),
    )
    closing_count = (await cursor.fetchone())[0]
    assert closing_count >= 1, "at least one closing snapshot must be marked"

    # --- Phase 3: CLV + result on final game ---
    game.home_score = 112
    game.away_score = 105
    await store.update_game_status(game.id, "final", 112, 105)

    await sch._settlement.calculate_clv_for_pending_bets(game)
    await sch._settlement.resolve_bets(game)

    cursor = await store._db.execute(
        "SELECT clv_pct, result FROM value_bets WHERE game_id=?",
        (game.id,),
    )
    final_row = await cursor.fetchone()
    assert final_row["clv_pct"] is not None, "CLV must be set after resolution"
    assert final_row["result"] == "win", (
        "home selection should win when home_score(112) > away_score(105)"
    )

    await sch._schedule._schedule_source.close()


# ---------------------------------------------------------------------------
# Task 2: total-market resolution (over / under / push) end-to-end
# ---------------------------------------------------------------------------

# We exercise this through poll() (edge detection → value_bet insert)
# followed by mark_closing_snapshots + _settlement.calculate_clv_for_pending_bets +
# _settlement.resolve_bets — the same path the scheduler takes on a real game-night.
#
# Line: 220.5 total points.
# The Tipsport "over" price (2.00) vs Pinnacle balanced pair (1.91/1.91) gives a
# vig-removed fair of ~2.0 — to guarantee the test doesn't flip with rounding we
# use a slightly inflated over_odds (2.05) so the edge is robustly above 3%.


async def _setup_total_bet(
    store: Store,
    *,
    external_id: str,
    tip_selection: str = "over",
    tip_odds: float = 2.10,
    line: float = 220.5,
) -> tuple["Scheduler", Game]:
    """Build a scheduler + hot-tier game with a Tipsport total bet detected."""
    sch = Scheduler(store)
    sch._schedule._schedule_source.fetch_games = AsyncMock(return_value=[])
    sch._running = True

    home, away = "Miami Heat", "Toronto Raptors"
    game = await _make_hot_game(
        store, external_id=external_id, home_team=home, away_team=away
    )
    sch._schedule._games = [game]

    pin = next(s for s in sch._scrapers if s.name == "pinnacle")
    tip = next(s for s in sch._scrapers if s.name == "tipsport")
    # Pinnacle total pair — balanced (~2.0 vig-removed fair on each side).
    pin.fetch_odds = AsyncMock(
        return_value=_pin_total_pair(home, away, line=line, over_odds=1.91, under_odds=1.91)
    )
    # Tipsport mispriced on the selected side.
    tip.fetch_odds = AsyncMock(
        return_value=[_tip_snap(home, away, selection=tip_selection, odds=tip_odds,
                                market_type="total", line=line)]
    )

    await sch._poll_coordinator.poll(NBA, [game], sch._schedule.games, datetime.now(timezone.utc))

    # Verify the value bet was created.
    cursor = await store._db.execute(
        "SELECT id, selection, line FROM value_bets WHERE game_id=?", (game.id,)
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1, (
        f"expected 1 total value_bet, got {[dict(r) for r in rows]}"
    )
    assert rows[0]["selection"] == tip_selection
    assert rows[0]["line"] == line

    # Plant closing snapshots pre-tipoff.
    tipoff = game.scheduled_at
    captured_at = tipoff - timedelta(minutes=5)
    # Use the actual tip_odds that were fetched (and produced the edge).
    await store.insert_odds_snapshots_batch([
        OddsSnapshot(
            game_id=game.id, bookmaker="tipsport", market_type="total",
            selection=tip_selection, odds=tip_odds, line=line,
            captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="total",
            selection="over", odds=1.91, line=line, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="total",
            selection="under", odds=1.91, line=line, captured_at=captured_at,
        ),
    ])
    await store.mark_closing_snapshots(game.id, freshness_minutes=30)

    return sch, game


@pytest.mark.asyncio
async def test_total_over_win(store: Store):
    """Tipsport 'over 220.5' bet wins when total points (218+5=223) exceed line."""
    sch, game = await _setup_total_bet(
        store, external_id="total_over_win", tip_selection="over", line=220.5
    )
    game.home_score = 118
    game.away_score = 105  # total 223 > 220.5 → over wins
    await store.update_game_status(game.id, "final", 118, 105)

    await sch._settlement.calculate_clv_for_pending_bets(game)
    await sch._settlement.resolve_bets(game)

    cursor = await store._db.execute(
        "SELECT clv_pct, result FROM value_bets WHERE game_id=?", (game.id,)
    )
    row = await cursor.fetchone()
    assert row["result"] == "win", f"over 220.5 should win with 223 total; got {row['result']}"
    assert row["clv_pct"] is not None, "CLV must be computed for a total bet"
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_total_over_loss(store: Store):
    """Tipsport 'over 220.5' bet loses when total points (113+106=219) < line."""
    sch, game = await _setup_total_bet(
        store, external_id="total_over_loss", tip_selection="over", line=220.5
    )
    game.home_score = 113
    game.away_score = 106  # total 219 < 220.5 → over loses
    await store.update_game_status(game.id, "final", 113, 106)

    await sch._settlement.calculate_clv_for_pending_bets(game)
    await sch._settlement.resolve_bets(game)

    cursor = await store._db.execute(
        "SELECT result FROM value_bets WHERE game_id=?", (game.id,)
    )
    row = await cursor.fetchone()
    assert row["result"] == "loss", f"over 220.5 should lose with 219 total; got {row['result']}"
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_total_over_push(store: Store):
    """Tipsport 'over 220.0' bet pushes on exact total."""
    sch, game = await _setup_total_bet(
        store, external_id="total_over_push", tip_selection="over", line=220.0
    )
    game.home_score = 110
    game.away_score = 110  # total 220 == 220.0 line → push
    await store.update_game_status(game.id, "final", 110, 110)

    await sch._settlement.calculate_clv_for_pending_bets(game)
    await sch._settlement.resolve_bets(game)

    cursor = await store._db.execute(
        "SELECT result FROM value_bets WHERE game_id=?", (game.id,)
    )
    row = await cursor.fetchone()
    assert row["result"] == "push", f"over 220.0 should push with 220 total; got {row['result']}"
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_total_under_win(store: Store):
    """Tipsport 'under 220.5' bet wins when total points (105+112=217) < line."""
    sch, game = await _setup_total_bet(
        store, external_id="total_under_win", tip_selection="under",
        tip_odds=2.10, line=220.5
    )
    game.home_score = 112
    game.away_score = 105  # total 217 < 220.5 → under wins
    await store.update_game_status(game.id, "final", 112, 105)

    await sch._settlement.calculate_clv_for_pending_bets(game)
    await sch._settlement.resolve_bets(game)

    cursor = await store._db.execute(
        "SELECT clv_pct, result FROM value_bets WHERE game_id=?", (game.id,)
    )
    row = await cursor.fetchone()
    assert row["result"] == "win", f"under 220.5 should win with 217 total; got {row['result']}"
    assert row["clv_pct"] is not None, "CLV must be computed for under bet"
    await sch._schedule._schedule_source.close()


# ---------------------------------------------------------------------------
# Task 3: scraper-error isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scraper_error_does_not_abort_cycle(store: Store):
    """When one scraper raises during fetch, the other scraper's odds are still
    persisted. The cycle must not raise and the healthy scraper's snapshot must
    appear in odds_snapshots.

    This exercises the asyncio.gather + try/except path in _fetch_scraper_odds:
    if an exception escapes from one scraper's coroutine, the others must still
    run and their data reach the DB.
    """
    sch = Scheduler(store)
    sch._schedule._schedule_source.fetch_games = AsyncMock(return_value=[])
    sch._running = True

    home, away = "Denver Nuggets", "Phoenix Suns"
    game = await _make_hot_game(
        store, external_id="scraper_isolation_001",
        home_team=home, away_team=away,
    )
    sch._schedule._games = [game]

    # Pinnacle raises; Tipsport returns valid data.
    pin = next(s for s in sch._scrapers if s.name == "pinnacle")
    tip = next(s for s in sch._scrapers if s.name == "tipsport")
    pin.fetch_odds = AsyncMock(side_effect=RuntimeError("simulated pinnacle outage"))
    tip.fetch_odds = AsyncMock(return_value=[
        _tip_snap(home, away, selection="home", odds=2.05),
    ])

    # Must not raise.
    await sch._poll_coordinator.poll(NBA, [game], sch._schedule.games, datetime.now(timezone.utc))

    # Pinnacle was attempted.
    assert pin.fetch_odds.await_count == 1

    # Tipsport snapshot was persisted even though Pinnacle failed.
    tip_snap = await store.get_latest_odds(game.id, "tipsport", "moneyline", "home")
    assert tip_snap is not None, (
        "Tipsport snapshot must be written despite Pinnacle raising"
    )
    assert tip_snap.odds == 2.05

    # No Pinnacle snapshot was written (it failed).
    pin_snap = await store.get_latest_odds(game.id, "pinnacle", "moneyline", "home")
    assert pin_snap is None, "No Pinnacle snapshot should exist after a fetch error"

    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_tipsport_error_does_not_abort_cycle(store: Store):
    """Mirror of the above: Tipsport raising must not prevent Pinnacle data
    from being stored. Ensures the isolation property is symmetric."""
    sch = Scheduler(store)
    sch._schedule._schedule_source.fetch_games = AsyncMock(return_value=[])
    sch._running = True

    home, away = "Boston Celtics", "Miami Heat"
    game = await _make_hot_game(
        store, external_id="scraper_isolation_002",
        home_team=home, away_team=away,
    )
    sch._schedule._games = [game]

    pin = next(s for s in sch._scrapers if s.name == "pinnacle")
    tip = next(s for s in sch._scrapers if s.name == "tipsport")
    tip.fetch_odds = AsyncMock(side_effect=RuntimeError("simulated tipsport outage"))
    pin.fetch_odds = AsyncMock(
        return_value=_pin_pair(home, away, home_odds=1.70, away_odds=2.20)
    )

    # Must not raise.
    await sch._poll_coordinator.poll(NBA, [game], sch._schedule.games, datetime.now(timezone.utc))

    assert tip.fetch_odds.await_count == 1

    # Pinnacle snapshots were stored.
    pin_home = await store.get_latest_odds(game.id, "pinnacle", "moneyline", "home")
    assert pin_home is not None, (
        "Pinnacle snapshot must be written despite Tipsport raising"
    )

    # Tipsport snapshot not written.
    tip_snap = await store.get_latest_odds(game.id, "tipsport", "moneyline", "home")
    assert tip_snap is None

    await sch._schedule._schedule_source.close()
