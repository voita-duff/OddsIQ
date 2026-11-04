import logging
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from oddsiq import backup, config
from oddsiq.db import migrations
from oddsiq.db.models import Game, ValueBet
from oddsiq.db.store import Store
from oddsiq.pipeline.scheduler import Scheduler


@pytest.fixture(autouse=True)
def _silence_real_telegram(monkeypatch):
    """Default every scheduler test to a no-op send_telegram.

    Without this, tests that exercise poll-driven paths with realistic
    edge-producing odds fire real Telegram alerts to whatever bot/chat .env
    points at. Tests that need to assert on outbound messages override this
    via _capture_telegram(monkeypatch) — the later setattr wins.
    """

    async def _noop(_text: str) -> None:
        return None

    monkeypatch.setattr("oddsiq.pipeline.scheduler.telegram.send_telegram", _noop)


@pytest.fixture(autouse=True)
def _hermetic_bookmaker_flags(monkeypatch):
    """Force a deterministic Czech-scraper list for every Scheduler test.

    Scheduler instantiation calls ``build_czech_scrapers()`` which reads
    ``config.BOOKMAKER_TIPSPORT_ENABLED`` from the developer's ``.env`` at runtime.
    Without this fixture, a local `.env` override could change the scraper list
    in tests and break coverage-gap assertions.

    The fixture pins the flag to its production default.
    """
    monkeypatch.setattr("oddsiq.scrapers.registry.config.BOOKMAKER_TIPSPORT_ENABLED", True)


@pytest.fixture(autouse=True)
def _hermetic_betting_flag(monkeypatch):
    """Pin the auto-betting engine OFF so a local .env BETTING_ENABLED=1
    doesn't construct the bridge/service in unrelated scheduler tests.
    Betting-specific tests re-enable it explicitly."""
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BETTING_ENABLED", False)


@pytest.fixture(autouse=True)
def _hermetic_enabled_sports(monkeypatch):
    """Pin the full 4-sport list so scheduler tests exercise per-sport gating
    regardless of the production enablement (MLB-only since 2026-08-08, the
    off-season quota strategy). Scheduler reads the list at call time via
    enabled_profiles(), so patching the profiles module is sufficient."""
    monkeypatch.setattr(
        "oddsiq.sports.profiles.ENABLED_SPORTS", ["nba", "nhl", "mlb", "wnba"]
    )


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
async def store(tmp_path):
    db_path = tmp_path / "test.db"
    # Schema creation now lives in the migration runner, not Store.initialize.
    migrations.run(db_path)
    s = Store(db_path)
    await s.initialize()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_console_value_and_result_lines_include_timestamp_and_game_id(
    store: Store, monkeypatch, capsys
):
    sch = Scheduler(store)
    game = await _make_game(store, external_id="nba_console_ids", seconds_to_tipoff=600)
    game.away_score = 114
    game.home_score = 127
    vb = ValueBet(
        game_id=game.id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="away",
        entry_odds=2.53,
        fair_odds=2.45,
        edge_pct=3.1,
        clv_pct=3.6,
        tier="hot",
    )
    monkeypatch.setattr(
        "oddsiq.pipeline.edge_service.console_timestamp",
        lambda: "2026-05-27 02:18:49 CEST",
    )
    monkeypatch.setattr(
        "oddsiq.pipeline.settlement_service.console_timestamp",
        lambda: "2026-05-27 02:18:49 CEST",
    )

    sch._edge_service._print_value_bet(game, vb)
    sch._settlement._print_result(game, vb, "loss")

    out = capsys.readouterr().out
    assert "[2026-05-27 02:18:49 CEST] VALUE BET:" in out
    assert f"game_id={game.id}" in out
    assert "[2026-05-27 02:18:49 CEST] RESULT:" in out
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_print_summary_includes_scraper_session_stats(store: Store, capsys):
    sch = Scheduler(store)
    game = await _make_game(store, external_id="nba_summary_scrapers", seconds_to_tipoff=600)
    sch._schedule._games = [game]
    seen = datetime(2026, 5, 27, 0, 39, 37, tzinfo=timezone.utc)
    sch._coverage._last_game_coverage_success[(game.id, "tipsport")] = seen
    sch._stats.record_scraper_poll(
        "tipsport",
        duration_ms=100,
        zero_odds=False,
        error_count=0,
    )
    sch._stats.record_scraper_poll(
        "tipsport",
        duration_ms=300,
        zero_odds=True,
        error_count=2,
    )

    sch.print_summary()

    out = capsys.readouterr().out
    assert "Scrapers:" in out
    assert "tipsport: polls=2 zero_odds=1 errors=2 avg_ms=200 max_ms=300" in out
    assert "last_coverage=" in out
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_heartbeat_emitted_at_cadence(store: Store, caplog, monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_HEARTBEAT_SECONDS", 1)
    sch = Scheduler(store)
    sch._scrapers = []
    sch._schedule._games = []
    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.scheduler")
    now = datetime(2026, 5, 28, 1, 0, 0, tzinfo=timezone.utc)
    sch._maybe_emit_heartbeat(now)
    sch._maybe_emit_heartbeat(now + timedelta(milliseconds=500))  # within cadence — suppressed
    sch._maybe_emit_heartbeat(now + timedelta(seconds=2))
    msgs = [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("heartbeat:")]
    assert len(msgs) == 2  # middle call suppressed
    assert "state=" in msgs[0]
    assert "games_tracked=" in msgs[0]
    assert "budget_throttle=" in msgs[0]  # dashboard-facing heartbeat field


@pytest.mark.asyncio
async def test_heartbeat_state_idle_no_games(store: Store):
    sch = Scheduler(store)
    sch._scrapers = []
    sch._schedule._games = []
    assert sch._derive_pipeline_state(datetime.now(timezone.utc)) == "idle-no-games"


@pytest.mark.asyncio
async def test_heartbeat_state_polling_after_recent_poll(store: Store):
    sch = Scheduler(store)
    sch._scrapers = []
    sch._schedule._games = []
    fixed = datetime.now(timezone.utc)
    sch._last_poll_time = {"nba": fixed}
    assert sch._derive_pipeline_state(fixed) == "polling"


@pytest.mark.asyncio
async def test_throttle_level_widens_effective_min_interval(store: Store):
    # A warm-tier game (90 min out): base warm cadence 300; at throttle level 2
    # the effective interval widens to 900 (so the sport is not-yet-due sooner).
    sch = Scheduler(store)
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    warm = await _make_game(store, external_id="nba_throttle", seconds_to_tipoff=90 * 60)
    # Anchor scheduled_at to the fixed `now` so time_to_tipoff is deterministic.
    warm.scheduled_at = now + timedelta(seconds=90 * 60)
    sch._schedule._games = [warm]

    sch._budget_governor.throttle_level = lambda _now: 0
    _pollable, sport_min_interval = sch._compute_pollable(now)
    assert sport_min_interval["nba"] == config.POLL_TIER_WARM == 300

    sch._budget_governor.throttle_level = lambda _now: 2
    _pollable, sport_min_interval = sch._compute_pollable(now)
    assert sport_min_interval["nba"] == 900

    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_poll_gating_is_per_sport(store: Store):
    """A hot game in one sport (90s cadence) must NOT drag a warm-only slate in
    another sport into 90s Pinnacle polling — each sport gates on its OWN
    interval and its OWN last-poll time (was a global min_interval bug that cost
    5-15x quota during CET evening overlaps)."""
    from unittest.mock import AsyncMock

    sch = Scheduler(store)
    sch._scrapers = []
    sch._poll_coordinator.poll = AsyncMock()
    sch._budget_governor.throttle_level = lambda _n: 0
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    nba = Game(external_id="nba_hot", home_team="Boston Celtics",
               away_team="Miami Heat",
               scheduled_at=now + timedelta(minutes=30), status="scheduled", sport="nba")
    nba.id = 1
    nhl = Game(external_id="nhl_warm", home_team="Bruins", away_team="Rangers",
               scheduled_at=now + timedelta(minutes=90), status="scheduled", sport="nhl")
    nhl.id = 2
    sch._schedule._games = [nba, nhl]

    pollable, smi = sch._compute_pollable(now)
    assert smi["nba"] == config.POLL_TIER_HOT    # 90
    assert smi["nhl"] == config.POLL_TIER_WARM   # 300

    # First cycle: neither has polled yet → both poll.
    assert await sch._poll_due_sports(now, now, pollable, smi) is True
    polled = {c.args[0].key for c in sch._poll_coordinator.poll.await_args_list}
    assert polled == {"nba", "nhl"}

    # 250s later: NBA (live 240s) is due again, NHL (warm 300s) is NOT — skipped.
    sch._poll_coordinator.poll.reset_mock()
    later = now + timedelta(seconds=250)
    pollable2, smi2 = sch._compute_pollable(later)
    assert await sch._poll_due_sports(later, later, pollable2, smi2) is True
    polled2 = {c.args[0].key for c in sch._poll_coordinator.poll.await_args_list}
    assert polled2 == {"nba"}

    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_run_loop_polls_due_sport_then_stops_promptly(store: Store, monkeypatch):
    """Drive Scheduler.run end-to-end (bounded): it polls a due sport at least
    once and exits promptly on stop() — the stop-wake path, not the 30s sleep."""
    import asyncio
    from unittest.mock import AsyncMock

    sch = Scheduler(store)
    sch._scrapers = []
    # Stub heavy collaborators so the loop is fast and deterministic.
    sch._recover_at_startup = AsyncMock()
    sch._check_browser_preflight = AsyncMock()
    sch._schedule.health_check = AsyncMock(return_value=True)
    sch._schedule.refresh = AsyncMock()
    sch._schedule.maybe_refresh = AsyncMock()
    sch._schedule.process_live_transitions = AsyncMock()
    sch._schedule.check_completed_games = AsyncMock()
    sch._budget_governor.probe = AsyncMock()
    sch._budget_governor.maybe_reprobe = AsyncMock()
    sch._budget_governor.persist = AsyncMock()
    sch._coverage.check_gaps = AsyncMock()
    poll = AsyncMock()
    sch._poll_coordinator.poll = poll

    now = datetime.now(timezone.utc)
    hot = Game(external_id="run_nba", home_team="Boston Celtics", away_team="Miami Heat",
               scheduled_at=now + timedelta(minutes=30), status="scheduled", sport="nba")
    hot.id = 1
    sch._schedule._games = [hot]

    task = asyncio.create_task(sch.run())
    for _ in range(100):  # wait for the first poll, then stop
        if poll.await_count >= 1:
            break
        await asyncio.sleep(0.01)
    sch.stop()
    await asyncio.wait_for(task, timeout=5)  # must not hang on the 30s sleep

    assert poll.await_count >= 1
    assert poll.await_args_list[0].args[0].key == "nba"


@pytest.mark.asyncio
async def test_heartbeat_state_between_game_windows(store: Store):
    sch = Scheduler(store)
    sch._scrapers = []
    g = Game(
        external_id="ext1",
        home_team="Boston Celtics",
        away_team="Miami Heat",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=12),
        status="scheduled",
    )
    g.id = 1
    sch._schedule._games = [g]
    assert sch._derive_pipeline_state(datetime.now(timezone.utc)) == "between-game-windows"



@pytest.mark.asyncio
async def test_poll_failure_in_one_sport_does_not_block_others(store: Store, caplog):
    """A deterministic error in one sport's post-fetch path must not starve
    the sports after it in enabled_profiles() order — the fetch fan-out is
    already exception-guarded, so the ordered poll must be too."""
    from unittest.mock import AsyncMock

    sch = Scheduler(store)
    sch._scrapers = []
    sch._budget_governor.throttle_level = lambda _n: 0
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    nba = Game(external_id="nba_boom", home_team="Boston Celtics",
               away_team="Miami Heat",
               scheduled_at=now + timedelta(minutes=30), status="scheduled", sport="nba")
    nba.id = 1
    mlb = Game(external_id="mlb_ok", home_team="New York Yankees",
               away_team="Boston Red Sox",
               scheduled_at=now + timedelta(minutes=90), status="scheduled", sport="mlb")
    mlb.id = 2
    sch._schedule._games = [nba, mlb]

    async def _poll(profile, *args, **kwargs):
        if profile.key == "nba":
            raise RuntimeError("boom")

    sch._poll_coordinator.poll = AsyncMock(side_effect=_poll)

    pollable, smi = sch._compute_pollable(now)
    caplog.clear()  # caplog leaks across files via cli.setup_logging
    caplog.set_level(logging.ERROR, logger="oddsiq.pipeline.scheduler")
    assert await sch._poll_due_sports(now, now, pollable, smi) is True
    polled = {c.args[0].key for c in sch._poll_coordinator.poll.await_args_list}
    assert polled == {"nba", "mlb"}, "mlb must still poll after nba raised"
    # The failed sport still advances its cadence clock — otherwise it would
    # re-poll with zero interval gating every loop iteration.
    assert "nba" in sch._last_poll_time and "mlb" in sch._last_poll_time
    assert any("nba" in r.getMessage() for r in caplog.records)
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_schedule_health_check_failure_logs_warning(store: Store, caplog):
    """A failed startup health check (bad/expired ODDS_API_KEY) silently zeroes
    every Pinnacle snapshot the project exists to collect — it logs a WARNING
    (the 2026-08-10 rebrand dropped the Telegram page; the phone only carries
    real-money placed/won/lost, and liveness lives on the dead-man ping)."""
    from unittest.mock import AsyncMock

    sch = Scheduler(store)
    sch._schedule.health_check = AsyncMock(return_value=False)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="oddsiq.pipeline.scheduler"):
        await sch._check_schedule_health()
    assert any("health check failed" in r.getMessage() for r in caplog.records)

    # Healthy probe -> nothing logged.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="oddsiq.pipeline.scheduler"):
        sch._schedule.health_check = AsyncMock(return_value=True)
        await sch._check_schedule_health()
    assert not any("health check failed" in r.getMessage() for r in caplog.records)
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_heartbeat_ping_fired_when_url_configured(store, monkeypatch):
    """Dead-man ping: with HEARTBEAT_PING_URL set, the scheduler GETs it on
    the heartbeat cadence (healthchecks.io-style — missing pings page the
    operator). Skipped entirely when unset."""
    from unittest.mock import AsyncMock
    ping = AsyncMock()
    monkeypatch.setattr("oddsiq.pipeline.scheduler._send_heartbeat_ping", ping)
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.HEARTBEAT_PING_URL",
                        "https://hc-ping.com/abc")
    sch = Scheduler(store)
    now = datetime.now(timezone.utc)
    await sch._maybe_ping_heartbeat_url(now)
    ping.assert_awaited_once_with("https://hc-ping.com/abc")
    # Within cadence — suppressed.
    await sch._maybe_ping_heartbeat_url(now + timedelta(seconds=2))
    assert ping.await_count == 1
    # Past cadence — pings again.
    await sch._maybe_ping_heartbeat_url(now + timedelta(seconds=61))
    assert ping.await_count == 2
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_heartbeat_ping_skipped_when_unset_and_failure_swallowed(store, monkeypatch):
    from unittest.mock import AsyncMock
    ping = AsyncMock()
    monkeypatch.setattr("oddsiq.pipeline.scheduler._send_heartbeat_ping", ping)
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.HEARTBEAT_PING_URL", "")
    sch = Scheduler(store)
    await sch._maybe_ping_heartbeat_url(datetime.now(timezone.utc))
    ping.assert_not_awaited()

    # A failing ping must never propagate into the poll loop.
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.HEARTBEAT_PING_URL",
                        "https://hc-ping.com/abc")
    ping.side_effect = RuntimeError("network down")
    await sch._maybe_ping_heartbeat_url(datetime.now(timezone.utc))  # must not raise
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_periodic_backup_fires_after_interval(store, monkeypatch):
    """BACKUP_INTERVAL_HOURS closes the 'container never restarts → never
    backs up' hole (Jul 22→Aug 8 ran 17 days with zero backups). First call
    only arms the clock — the CLI's startup backup just ran."""
    ran = []
    monkeypatch.setattr(
        "oddsiq.pipeline.scheduler.backup.backup_database",
        lambda *a, **k: ran.append(a) or None,
    )
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BACKUP_ENABLED", True)
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BACKUP_INTERVAL_HOURS", 24.0)
    sch = Scheduler(store)
    now = datetime.now(timezone.utc)
    await sch._maybe_periodic_backup(now)              # arms the clock
    assert ran == []
    await sch._maybe_periodic_backup(now + timedelta(hours=1))
    assert ran == []                                    # within interval
    await sch._maybe_periodic_backup(now + timedelta(hours=25))
    assert len(ran) == 1                                # past interval — backs up
    # Pin the moment literal to the set backup.py actually accepts: a drifting
    # spelling here is invisible until a real periodic tick raises ValueError.
    assert ran[0][2] == "periodic"
    assert ran[0][2] in backup._VALID_MOMENTS
    await sch._maybe_periodic_backup(now + timedelta(hours=26))
    assert len(ran) == 1                                # clock reset — suppressed again
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_periodic_backup_failure_never_costs_a_poll_cycle(store, monkeypatch, caplog):
    """A raising backup used to escape into the main loop's blanket handler,
    which logs, sleeps 10 s and SKIPS the REST of that iteration — budget alert,
    schedule refresh, polling, finals. On an armed NFL Sunday a lost cycle can
    cost a bet, so the call is isolated like its `_maybe_ping_heartbeat_url`
    sibling. Arming stays BEFORE the call: a persistent disk error must remain
    once-per-interval, not a retry on every 30 s loop pass."""
    calls = []

    def _boom(*a, **k):
        calls.append(a)
        raise RuntimeError("disk full")

    monkeypatch.setattr("oddsiq.pipeline.scheduler.backup.backup_database", _boom)
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BACKUP_ENABLED", True)
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BACKUP_INTERVAL_HOURS", 24.0)
    sch = Scheduler(store)
    now = datetime.now(timezone.utc)
    await sch._maybe_periodic_backup(now)              # arms the clock
    armed_at = sch._last_periodic_backup
    assert calls == []

    due = now + timedelta(hours=25)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        await sch._maybe_periodic_backup(due)          # must NOT raise
    assert len(calls) == 1
    assert "periodic_backup raised" in caplog.text

    # The arming timestamp advanced despite the failure, so the next loop pass
    # a minute later is suppressed rather than re-attempting the dead disk.
    assert sch._last_periodic_backup == due
    assert sch._last_periodic_backup != armed_at
    await sch._maybe_periodic_backup(due + timedelta(minutes=1))
    assert len(calls) == 1
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_periodic_backup_disabled(store, monkeypatch):
    ran = []
    monkeypatch.setattr(
        "oddsiq.pipeline.scheduler.backup.backup_database",
        lambda *a, **k: ran.append(a) or None,
    )
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BACKUP_ENABLED", True)
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BACKUP_INTERVAL_HOURS", 0)
    sch = Scheduler(store)
    now = datetime.now(timezone.utc)
    await sch._maybe_periodic_backup(now)
    await sch._maybe_periodic_backup(now + timedelta(hours=48))
    assert ran == []
    await sch._schedule._schedule_source.close()


@pytest.mark.asyncio
async def test_heartbeat_reports_betting_off_when_disabled(store: Store, caplog, monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_HEARTBEAT_SECONDS", 1)
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BETTING_ENABLED", False)
    sch = Scheduler(store)
    sch._scrapers = []
    sch._schedule._games = []
    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.scheduler")
    sch._maybe_emit_heartbeat(datetime.now(timezone.utc))
    msgs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("heartbeat:")]
    assert msgs and "betting=off" in msgs[0]


@pytest.mark.asyncio
async def test_betting_enabled_without_token_refuses_to_start(store: Store, monkeypatch):
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BETTING_ENABLED", True)
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BETTING_BRIDGE_TOKEN", "")
    with pytest.raises(ValueError, match="BETTING_BRIDGE_TOKEN"):
        Scheduler(store)


@pytest.mark.asyncio
async def test_betting_enabled_constructs_engine_and_reports_state(store: Store, monkeypatch):
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BETTING_ENABLED", True)
    monkeypatch.setattr("oddsiq.pipeline.scheduler.config.BETTING_BRIDGE_TOKEN", "t0k3n")
    sch = Scheduler(store)
    assert sch._betting is not None and sch._betting_bridge is not None
    # No extension connected -> state 'disconnected' regardless of armed flag.
    await store.set_meta("betting_armed", "1")
    assert await sch._betting_heartbeat_state() == "disconnected"
    await sch._schedule._schedule_source.close()
