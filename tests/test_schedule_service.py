import logging
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from oddsiq import config
from oddsiq.db import migrations
from oddsiq.db.models import Game, OddsSnapshot, ValueBet
from oddsiq.db.store import Store
from oddsiq.pipeline.schedule_service import ScheduleService
from oddsiq.pipeline.settlement_service import SettlementService
from oddsiq.pipeline.stats import SessionStats
from oddsiq.scrapers.odds_api_schedule import ScheduleFetchError


@pytest.fixture(autouse=True)
def _silence_real_telegram(monkeypatch):
    """Default every test to a no-op send_telegram."""
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", AsyncMock())


@pytest.fixture(autouse=True)
def _hermetic_enabled_sports(monkeypatch):
    """Pin ENABLED_SPORTS to NBA-only so adding NHL/MLB to the global default
    doesn't change the fetch_fresh_games call count in schedule_service tests."""
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", ["nba"])


@pytest_asyncio.fixture
async def store(tmp_path):
    db_path = tmp_path / "test.db"
    migrations.run(db_path)
    s = Store(db_path)
    await s.initialize()
    yield s
    await s.close()


async def _make_game(
    store: Store,
    *,
    external_id: str,
    seconds_to_tipoff: float,
    commence_seconds_to_tipoff: float | None = None,
) -> Game:
    """Build and persist a game with tipoff `seconds_to_tipoff` seconds from now."""
    now = datetime.now(timezone.utc)
    game = Game(
        external_id=external_id,
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=now + timedelta(seconds=seconds_to_tipoff),
        commence_time=(
            now + timedelta(seconds=commence_seconds_to_tipoff)
            if commence_seconds_to_tipoff is not None
            else None
        ),
        status="scheduled",
    )
    game.id = await store.upsert_game(game)
    return game


@pytest_asyncio.fixture
async def schedule_service(store):
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    live_game = Game(
        external_id="nba_live_1",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime.now(timezone.utc) - timedelta(hours=1),
        status="live",
    )
    live_game.id = await store.upsert_game(live_game)
    svc._games = [live_game]
    yield svc


@pytest.mark.asyncio
async def test_check_completed_games_does_not_cross_sports(store, monkeypatch):
    """A final game in one sport must NOT mark a same-external_id game in
    another sport as final. Odds API ids are global, but the completion match
    loop must scope by sport (defensive parity with the rest of the pipeline)."""
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", AsyncMock())
    src = AsyncMock()
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    nba_live = Game(
        external_id="collision",
        sport="nba",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime.now(timezone.utc) - timedelta(hours=1),
        status="live",
    )
    nba_live.id = await store.upsert_game(nba_live)
    svc._games = [nba_live]
    # The fresh fetch returns a DIFFERENT sport's final game sharing the id.
    mlb_final = Game(
        external_id="collision",
        sport="mlb",
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_at=datetime.now(timezone.utc) - timedelta(hours=3),
        status="final",
        home_score=5,
        away_score=2,
    )
    src.fetch_games = AsyncMock(return_value=[mlb_final])
    svc._last_completion_check = None  # bypass the 5-min throttle

    await svc.check_completed_games()

    assert nba_live.status == "live"  # not cross-marked final by the mlb collision


@pytest.mark.asyncio
async def test_check_completed_games_throttled_within_window(
    schedule_service: ScheduleService,
):
    """Repeated main-loop invocations must not re-fetch the Odds API within
    the completion-check window."""
    for _ in range(10):
        await schedule_service.check_completed_games()

    assert schedule_service._schedule_source.fetch_games.call_count == 1


@pytest.mark.asyncio
async def test_check_completed_games_refetches_after_window(
    schedule_service: ScheduleService,
):
    """Once the completion-check throttle window elapses, the next invocation
    fetches again."""
    await schedule_service.check_completed_games()
    assert schedule_service._schedule_source.fetch_games.call_count == 1

    schedule_service._last_completion_check = datetime.now(timezone.utc) - timedelta(
        seconds=config.SCHEDULE_COMPLETION_CHECK_INTERVAL + 1
    )
    await schedule_service.check_completed_games()
    assert schedule_service._schedule_source.fetch_games.call_count == 2


@pytest.mark.asyncio
async def test_check_completed_games_window_is_config_driven(
    schedule_service: ScheduleService, monkeypatch
):
    """The finals-check throttle reads config.SCHEDULE_COMPLETION_CHECK_INTERVAL
    (was a hardcoded 300s until 2026-08-08 — the finals path was the largest
    single Odds API burn at 2 credits per live sport per check)."""
    monkeypatch.setattr(config, "SCHEDULE_COMPLETION_CHECK_INTERVAL", 60)

    await schedule_service.check_completed_games()
    assert schedule_service._schedule_source.fetch_games.call_count == 1

    # Inside the 60s window: throttled.
    schedule_service._last_completion_check = datetime.now(timezone.utc) - timedelta(
        seconds=31
    )
    await schedule_service.check_completed_games()
    assert schedule_service._schedule_source.fetch_games.call_count == 1

    # Past the 60s window: refetches (would still be throttled under a
    # hardcoded 300s).
    schedule_service._last_completion_check = datetime.now(timezone.utc) - timedelta(
        seconds=61
    )
    await schedule_service.check_completed_games()
    assert schedule_service._schedule_source.fetch_games.call_count == 2


@pytest.mark.asyncio
async def test_refresh_schedule_satisfies_completion_check_window(
    schedule_service: ScheduleService,
):
    """A fresh schedule refresh should prevent an immediate duplicate fetch."""
    live_game = schedule_service._games[0]
    schedule_service._schedule_source.fetch_games.return_value = [live_game]

    await schedule_service.refresh()
    assert schedule_service._schedule_source.fetch_games.call_count == 1
    assert (
        schedule_service._last_completion_check
        == schedule_service._last_schedule_refresh
    )

    await schedule_service.check_completed_games()
    assert schedule_service._schedule_source.fetch_games.call_count == 1


@pytest.mark.asyncio
async def test_check_completed_games_noop_without_live_games(
    schedule_service: ScheduleService,
):
    """Guard: skip entirely when no game is in live status, so throttle stays unset."""
    schedule_service._games[0].status = "scheduled"
    await schedule_service.check_completed_games()
    assert schedule_service._schedule_source.fetch_games.call_count == 0
    assert schedule_service._last_completion_check is None


@pytest.mark.asyncio
async def test_final_transition_resolves_each_pending_bet(
    schedule_service: ScheduleService, store: Store, monkeypatch
):
    """When a live game transitions to final, each pending bet is resolved in
    the DB. Paper bets (no placed_bets row) never notify — the phone only
    carries real placed/won/lost (2026-08-10 rebrand)."""
    game = schedule_service._games[0]
    for sel, odds in (("home", 1.95), ("away", 2.10)):
        await store.insert_value_bet(
            ValueBet(
                game_id=game.id,
                bookmaker="tipsport",
                market_type="moneyline",
                selection=sel,
                entry_odds=odds,
                fair_odds=1.90,
                edge_pct=4.2,
            )
        )

    final_game = Game(
        external_id=game.external_id,
        home_team=game.home_team,
        away_team=game.away_team,
        scheduled_at=game.scheduled_at,
        status="final",
        home_score=112,
        away_score=105,
    )
    schedule_service._schedule_source.fetch_games.return_value = [final_game]

    sent: list[str] = []

    async def fake_send(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", fake_send)

    await schedule_service.check_completed_games()

    cursor = await store._db.execute(
        "SELECT result FROM value_bets WHERE game_id=?", (game.id,)
    )
    results = [r["result"] for r in await cursor.fetchall()]
    assert len(results) == 2
    assert all(r in ("win", "loss", "push", "draw") for r in results)
    assert sent == []  # paper bets resolve silently — no phone alert


@pytest.mark.asyncio
async def test_process_live_transitions_marks_game_handled_on_success(
    store: Store, monkeypatch
):
    """Happy path: a game whose tipoff just passed is handled exactly once."""
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )

    game = await _make_game(store, external_id="nba_live_ok", seconds_to_tipoff=-30)
    svc._games = [game]

    handled: list[int] = []

    async def fake_handler(g: Game) -> None:
        handled.append(g.id)

    monkeypatch.setattr(svc._settlement, "handle_game_live", fake_handler)

    await svc.process_live_transitions(datetime.now(timezone.utc))

    assert handled == [game.id], "handler should run once for a live game"
    assert game.id in svc._handled_live, (
        "game.id should be marked handled after success"
    )

    await svc.process_live_transitions(datetime.now(timezone.utc))
    assert handled == [game.id], "second pass must not re-handle the same game"


@pytest.mark.asyncio
async def test_process_live_transitions_skips_handled_set_on_handler_failure(
    store: Store, monkeypatch
):
    """If handle_game_live raises, the in-memory dedup set must NOT be updated."""
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )

    game = await _make_game(store, external_id="nba_live_fail", seconds_to_tipoff=-30)
    svc._games = [game]

    calls = {"n": 0}

    async def flaky_handler(_g: Game) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB write failure")

    monkeypatch.setattr(svc._settlement, "handle_game_live", flaky_handler)

    await svc.process_live_transitions(datetime.now(timezone.utc))

    assert game.id not in svc._handled_live, (
        "failed handler must not mark the game as handled — next iteration "
        "needs to retry"
    )

    await svc.process_live_transitions(datetime.now(timezone.utc))
    assert calls["n"] == 2, "second iteration should re-invoke the handler"
    assert game.id in svc._handled_live, "successful retry must mark the game handled"


@pytest.mark.asyncio
async def test_process_live_transitions_isolates_failing_game(
    store: Store, monkeypatch
):
    """A failure handling one live game must not skip the other live games."""
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )

    bad = await _make_game(store, external_id="nba_live_bad", seconds_to_tipoff=-30)
    good = await _make_game(store, external_id="nba_live_good", seconds_to_tipoff=-20)
    svc._games = [bad, good]

    handled: list[int] = []

    async def handler(g: Game) -> None:
        if g.id == bad.id:
            raise RuntimeError("transient failure on first game")
        handled.append(g.id)

    monkeypatch.setattr(svc._settlement, "handle_game_live", handler)

    await svc.process_live_transitions(datetime.now(timezone.utc))

    assert handled == [good.id], "later game must be handled despite earlier failure"
    assert bad.id not in svc._handled_live, "failed game retries next cycle"
    assert good.id in svc._handled_live


@pytest.mark.asyncio
async def test_refresh_schedule_keeps_existing_games_on_fetch_failure(store: Store):
    """Transient Odds API schedule failure must not erase the in-memory slate."""
    src = AsyncMock()
    src.fetch_games = AsyncMock(side_effect=ScheduleFetchError("boom"))
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    hot_game = await _make_game(store, external_id="nba_hot", seconds_to_tipoff=600)
    svc._games = [hot_game]

    await svc.refresh()

    assert svc._games == [hot_game]
    assert svc._last_schedule_refresh is None
    assert svc._last_completion_check is None


def _capture_telegram(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake_send(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", fake_send)
    return sent


@pytest.mark.asyncio
async def test_refresh_schedule_recovers_already_final_pending_bet_after_restart(
    store: Store, monkeypatch
):
    """Startup schedule refresh must resolve bets on games already final."""
    src = AsyncMock()
    tipoff = datetime.now(timezone.utc) - timedelta(hours=3)
    game = Game(
        external_id="nba_restart_final",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
        status="live",
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(
        ValueBet(
            game_id=game.id,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=1.95,
            fair_odds=1.90,
            edge_pct=2.6,
        )
    )
    final_game = Game(
        external_id=game.external_id,
        home_team=game.home_team,
        away_team=game.away_team,
        scheduled_at=tipoff,
        status="final",
        home_score=112,
        away_score=105,
    )
    src.fetch_games = AsyncMock(return_value=[final_game])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    sent = _capture_telegram(monkeypatch)

    await svc.refresh()

    assert await store.get_pending_value_bets(game.id) == []
    cursor = await store._db.execute(
        "SELECT result FROM value_bets WHERE game_id=?", (game.id,)
    )
    row = await cursor.fetchone()
    assert row["result"] == "win"
    assert (
        sent == []
    )  # paper bet resolves silently — no phone alert (2026-08-10 rebrand)


@pytest.mark.asyncio
async def test_refresh_schedule_recovers_final_game_clv_before_result(
    store: Store, monkeypatch
):
    """Final recovery marks fresh closings and computes CLV before resolving."""
    src = AsyncMock()
    tipoff = datetime.now(timezone.utc) - timedelta(hours=1)
    game = Game(
        external_id="nba_restart_clv",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
        status="live",
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(
        ValueBet(
            game_id=game.id,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=2.05,
            fair_odds=1.95,
            edge_pct=5.1,
        )
    )
    captured_at = tipoff - timedelta(minutes=5)
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game.id,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="home",
                odds=2.05,
                captured_at=captured_at,
            ),
            OddsSnapshot(
                game_id=game.id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.95,
                captured_at=captured_at,
            ),
            OddsSnapshot(
                game_id=game.id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="away",
                odds=2.05,
                captured_at=captured_at,
            ),
        ]
    )
    final_game = Game(
        external_id=game.external_id,
        home_team=game.home_team,
        away_team=game.away_team,
        scheduled_at=tipoff,
        status="final",
        home_score=112,
        away_score=105,
    )
    src.fetch_games = AsyncMock(return_value=[final_game])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    sent = _capture_telegram(monkeypatch)

    await svc.refresh()

    cursor = await store._db.execute(
        """SELECT closing_odds, pinnacle_closing, clv_pct, result
           FROM value_bets WHERE game_id=?""",
        (game.id,),
    )
    row = await cursor.fetchone()
    assert row["closing_odds"] == 2.05
    assert row["pinnacle_closing"] is not None
    assert row["clv_pct"] is not None
    assert row["result"] == "win"
    # CLV is computed + stored but never messaged, and a paper bet's result is
    # not phoned either (2026-08-10 rebrand — only real placed/won/lost notify).
    assert sent == []


@pytest.mark.asyncio
async def test_refresh_schedule_final_recovery_does_not_duplicate_result_alerts(
    store: Store, monkeypatch
):
    """Repeated final recovery is quiet once result IS NULL no longer matches."""
    src = AsyncMock()
    tipoff = datetime.now(timezone.utc) - timedelta(hours=2)
    game = Game(
        external_id="nba_restart_idempotent",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
        status="live",
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(
        ValueBet(
            game_id=game.id,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="away",
            entry_odds=2.10,
            fair_odds=2.00,
            edge_pct=5.0,
        )
    )
    final_game = Game(
        external_id=game.external_id,
        home_team=game.home_team,
        away_team=game.away_team,
        scheduled_at=tipoff,
        status="final",
        home_score=112,
        away_score=105,
    )
    src.fetch_games = AsyncMock(return_value=[final_game])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    sent = _capture_telegram(monkeypatch)

    await svc.refresh()
    await svc.refresh()

    # Paper bet: resolves once, silently. The regression guard is that the
    # second refresh neither errors nor re-resolves (result already set).
    cursor = await store._db.execute(
        "SELECT result FROM value_bets WHERE game_id=?", (game.id,)
    )
    assert (await cursor.fetchone())["result"] == "loss"
    assert sent == []  # no phone alert for a paper bet (2026-08-10 rebrand)


@pytest.mark.asyncio
async def test_fetch_fresh_games_filters_to_requested_sports(store, monkeypatch):
    """fetch_fresh_games(sports=...) issues a /scores call only for the requested
    sports — idle sports cost no Odds API quota."""
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", ["nba", "mlb", "nhl"])
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )

    await svc.fetch_fresh_games(sports={"mlb"})

    called_keys = [c.args[0] for c in src.fetch_games.call_args_list]
    assert called_keys == ["baseball_mlb"], (
        "only the requested sport's /scores is fetched"
    )


@pytest.mark.asyncio
async def test_fetch_fresh_games_default_fetches_all_enabled(store, monkeypatch):
    """No sports arg = full sweep across every enabled sport (unchanged default)."""
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", ["nba", "mlb"])
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )

    await svc.fetch_fresh_games()

    called_keys = sorted(c.args[0] for c in src.fetch_games.call_args_list)
    assert called_keys == ["baseball_mlb", "basketball_nba"]


@pytest.mark.asyncio
async def test_partial_refresh_merges_keeping_other_sports(store, monkeypatch):
    """A partial refresh (subset of sports) replaces ONLY those sports' games and
    RETAINS the slate entries of other sports — a just-final game in an unfetched
    sport must not vanish before settlement."""
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", ["nba", "mlb"])
    src = AsyncMock()
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    nba_game = Game(
        external_id="nba_keep",
        sport="nba",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=5),
        status="scheduled",
    )
    nba_game.id = await store.upsert_game(nba_game)
    svc._games = [nba_game]
    mlb_game = Game(
        external_id="mlb_new",
        sport="mlb",
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
        status="scheduled",
    )
    src.fetch_games = AsyncMock(return_value=[mlb_game])

    await svc.refresh(sports={"mlb"})

    slate = {(g.sport, g.external_id) for g in svc._games}
    assert ("nba", "nba_keep") in slate, "unfetched sport's game must be retained"
    assert ("mlb", "mlb_new") in slate, "fetched sport's game must be present"


@pytest.mark.asyncio
async def test_maybe_refresh_fetches_only_active_sports_between_full_sweeps(
    store, monkeypatch
):
    """During an active window, between hourly full sweeps, maybe_refresh fetches
    /scores only for sports with a pollable game; idle sports are skipped."""
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", ["nba", "mlb"])
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    now = datetime.now(timezone.utc)
    mlb_hot = Game(
        external_id="mlb_hot",
        sport="mlb",
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_at=now + timedelta(minutes=30),
        commence_time=now + timedelta(minutes=30),
        status="scheduled",
    )
    mlb_hot.id = await store.upsert_game(mlb_hot)
    nba_idle = Game(
        external_id="nba_idle",
        sport="nba",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=now + timedelta(hours=8),
        commence_time=now + timedelta(hours=8),
        status="scheduled",
    )
    nba_idle.id = await store.upsert_game(nba_idle)
    svc._games = [mlb_hot, nba_idle]
    # A full sweep happened recently (within the hour) so the next refresh is partial.
    svc._last_schedule_refresh = now - timedelta(seconds=1000)  # > 900s active interval
    svc._last_full_sweep = now - timedelta(seconds=1000)  # < 3600s -> no full sweep due
    src.fetch_games.reset_mock()

    await svc.maybe_refresh(now)

    called_keys = [c.args[0] for c in src.fetch_games.call_args_list]
    assert called_keys == ["baseball_mlb"], (
        "only the active sport (MLB hot) is fetched; idle NBA (8h out) is skipped"
    )


@pytest.mark.asyncio
async def test_maybe_refresh_does_full_sweep_when_interval_elapsed(store, monkeypatch):
    """Even during an active window, a full sweep (all enabled sports) runs at
    least every SCHEDULE_REFRESH_INTERVAL so new games in idle sports are found."""
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", ["nba", "mlb"])
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    now = datetime.now(timezone.utc)
    mlb_hot = Game(
        external_id="mlb_hot2",
        sport="mlb",
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_at=now + timedelta(minutes=30),
        commence_time=now + timedelta(minutes=30),
        status="scheduled",
    )
    mlb_hot.id = await store.upsert_game(mlb_hot)
    svc._games = [mlb_hot]
    svc._last_schedule_refresh = now - timedelta(seconds=1000)  # > 900s active interval
    svc._last_full_sweep = now - timedelta(
        seconds=config.SCHEDULE_REFRESH_INTERVAL + 100
    )  # past the sweep interval -> full sweep due
    src.fetch_games.reset_mock()

    await svc.maybe_refresh(now)

    called_keys = sorted(c.args[0] for c in src.fetch_games.call_args_list)
    assert called_keys == ["baseball_mlb", "basketball_nba"], (
        "full sweep hits all sports"
    )


@pytest.mark.asyncio
async def test_check_completed_games_fetches_only_live_sports(store, monkeypatch):
    """check_completed_games only needs finals for sports that currently have a
    live game — it must not fetch /scores for idle sports."""
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", ["nba", "mlb"])
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", AsyncMock())
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[])
    svc = ScheduleService(
        schedule_source=src,
        store=store,
        settlement=SettlementService(store=store, stats=SessionStats()),
    )
    now = datetime.now(timezone.utc)
    mlb_live = Game(
        external_id="mlb_live",
        sport="mlb",
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_at=now - timedelta(hours=1),
        status="live",
    )
    mlb_live.id = await store.upsert_game(mlb_live)
    nba_sched = Game(
        external_id="nba_sched",
        sport="nba",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=now + timedelta(hours=2),
        status="scheduled",
    )
    nba_sched.id = await store.upsert_game(nba_sched)
    svc._games = [mlb_live, nba_sched]
    svc._last_completion_check = None  # bypass throttle

    await svc.check_completed_games()

    called_keys = [c.args[0] for c in src.fetch_games.call_args_list]
    assert called_keys == ["baseball_mlb"], "only the live sport's finals are checked"


@pytest.mark.asyncio
async def test_game_final_emits_log(schedule_service: ScheduleService, caplog):
    """check_completed_games must emit exactly one game_final: log line with scores."""
    game = schedule_service._games[0]
    final_game = Game(
        external_id=game.external_id,
        home_team=game.home_team,
        away_team=game.away_team,
        scheduled_at=game.scheduled_at,
        status="final",
        home_score=112,
        away_score=105,
    )
    schedule_service._schedule_source.fetch_games.return_value = [final_game]
    # Reset throttle guard so the call is not skipped.
    schedule_service._last_completion_check = None

    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.schedule_service")
    await schedule_service.check_completed_games()

    msgs = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("game_final:")
    ]
    assert len(msgs) == 1
    line = msgs[0]
    assert f"game_id={game.id}" in line
    assert "away_score=105" in line
    assert "home_score=112" in line


@pytest.mark.asyncio
async def test_refresh_isolates_per_game_recovery_failure(store: Store):
    """One final game whose recovery raises must not abort the whole refresh:
    later games still recover and the in-memory slate still updates (an
    unguarded raise here left the slate stale until the error cleared)."""
    now = datetime.now(timezone.utc)
    g1 = Game(
        external_id="nba_f1",
        home_team="A",
        away_team="B",
        scheduled_at=now - timedelta(hours=4),
        status="final",
        home_score=100,
        away_score=90,
    )
    g2 = Game(
        external_id="nba_f2",
        home_team="C",
        away_team="D",
        scheduled_at=now - timedelta(hours=4),
        status="final",
        home_score=80,
        away_score=95,
    )
    src = AsyncMock()
    src.fetch_games = AsyncMock(return_value=[g1, g2])
    settlement = AsyncMock()
    settlement.recover_finalized_game = AsyncMock(
        side_effect=[RuntimeError("boom"), None]
    )
    svc = ScheduleService(schedule_source=src, store=store, settlement=settlement)

    await svc.refresh()

    assert settlement.recover_finalized_game.await_count == 2, (
        "second game's recovery must still run after the first raised"
    )
    assert {g.external_id for g in svc.games} == {"nba_f1", "nba_f2"}, (
        "slate must still be assigned after a per-game recovery failure"
    )
