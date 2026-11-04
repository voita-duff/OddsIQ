import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# cli.py transitively imports playwright via the scraper chain; skip the whole
# module cleanly where playwright is unavailable.
pytest.importorskip("playwright")

from oddsiq import cli, config
import logging


@pytest.fixture(autouse=True)
def _hermetic_log_file(tmp_path, monkeypatch):
    """Pin ``LOG_FILE`` to a per-test tmp path so ``setup_logging()`` never
    writes to the project's real ``./logs/oddsiq.log`` during a test run.

    A2 introduced a file-writing side effect on ``setup_logging()``. Tests
    that don't explicitly mock ``config.LOG_FILE`` would otherwise create
    real files under the project root. Mirrors the
    ``_hermetic_bookmaker_flags`` pattern in ``tests/test_scheduler.py``.
    """
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "oddsiq.log")


def _clear_root_handlers() -> None:
    """Detach AND close every handler on the root logger.

    Tests that call ``cli.setup_logging()`` directly may end up with
    ``RotatingFileHandler`` instances pointing at tmp files. Leaking the
    open file descriptor between tests (or test sessions) is wrong. Call
    this at the start of any test that needs a clean handler state.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)


@pytest.mark.asyncio
async def test_check_budget_prints_used_remaining_total(monkeypatch, capsys):
    """--check-budget must print 'used / remaining / total' and return 0."""
    monkeypatch.setattr("oddsiq.cli.config.ODDS_API_KEY", "fake_key")

    async def fake_probe(self):
        self._budget_used = 42
        self._budget_remaining = 458
        return {"used": 42, "remaining": 458}

    monkeypatch.setattr(
        "oddsiq.cli.PinnacleScraper.probe_budget", fake_probe
    )
    monkeypatch.setattr(
        "oddsiq.cli.PinnacleScraper.close", AsyncMock()
    )

    code = await cli.check_budget()
    out = capsys.readouterr().out

    assert code == 0
    assert "42 used" in out
    assert "458 remaining" in out
    assert "500 total" in out


@pytest.mark.asyncio
async def test_check_budget_probe_failure_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr("oddsiq.cli.config.ODDS_API_KEY", "fake_key")
    monkeypatch.setattr(
        "oddsiq.cli.PinnacleScraper.probe_budget",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "oddsiq.cli.PinnacleScraper.close", AsyncMock()
    )

    code = await cli.check_budget()
    err = capsys.readouterr().err

    assert code == 1
    assert "failed" in err.lower()


@pytest.mark.asyncio
async def test_check_budget_missing_api_key(monkeypatch, capsys):
    """ODDS_API_KEY not set → fail with a clear message."""
    monkeypatch.setattr("oddsiq.cli.config.ODDS_API_KEY", "")
    code = await cli.check_budget()
    err = capsys.readouterr().err
    assert code == 1
    assert "ODDS_API_KEY" in err


def test_parse_args_check_budget_flag():
    args = cli._parse_args(["--check-budget"])
    assert args.check_budget is True


def test_parse_args_default():
    args = cli._parse_args([])
    assert args.check_budget is False


@pytest.mark.asyncio
async def test_backup_runs_before_init_and_after_close(monkeypatch, tmp_path):
    """Startup backup runs BEFORE store.initialize(); shutdown backup runs AFTER store.close()."""
    monkeypatch.setattr("oddsiq.cli.config.ODDS_API_KEY", "fake")
    monkeypatch.setattr("oddsiq.cli.config.BACKUP_ENABLED", True)
    monkeypatch.setattr("oddsiq.cli.config.BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("oddsiq.cli.config.BACKUP_KEEP_DAILY", 3)
    monkeypatch.setattr("oddsiq.cli.config.BACKUP_KEEP_MONTHLY", 3)
    monkeypatch.setattr("oddsiq.cli.config.RAW_DATA_DIR", tmp_path / "raw")

    call_order: list[str] = []

    def fake_backup(db_path, backup_dir, moment, **kwargs):
        call_order.append(f"backup:{moment}")
        return tmp_path / f"fake-{moment}.db"

    class FakeStore:
        meta: dict[str, str] = {}

        def __init__(self, *a, **kw):
            pass
        async def initialize(self):
            call_order.append("initialize")
        async def close(self):
            call_order.append("close")
        async def get_final_games_without_snapshots(self):
            return []
        async def get_meta(self, key):
            return self.meta.get(key)
        async def set_meta(self, key, value):
            self.meta[key] = value

    class FakeScheduler:
        def __init__(self, *a, **kw):
            pass
        async def run(self):
            call_order.append("run")
        def stop(self):
            pass

    monkeypatch.setattr("oddsiq.cli.Store", FakeStore)
    monkeypatch.setattr("oddsiq.cli.Scheduler", FakeScheduler)
    monkeypatch.setattr("oddsiq.cli.backup.backup_database", fake_backup)

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **kw: None)

    await cli.async_main()

    assert call_order == [
        "backup:startup",
        "initialize",
        "run",
        "close",
        "backup:shutdown",
    ]


def _stub_async_main_dependencies(monkeypatch, tmp_path, *, db_path):
    """Common monkeypatching for async_main wiring tests."""
    monkeypatch.setattr("oddsiq.cli.config.ODDS_API_KEY", "fake")
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    monkeypatch.setattr("oddsiq.cli.config.BACKUP_ENABLED", True)
    monkeypatch.setattr("oddsiq.cli.config.BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("oddsiq.cli.config.BACKUP_KEEP_DAILY", 3)
    monkeypatch.setattr("oddsiq.cli.config.BACKUP_KEEP_MONTHLY", 3)
    monkeypatch.setattr("oddsiq.cli.config.RAW_DATA_DIR", tmp_path / "raw")
    monkeypatch.setattr(
        "oddsiq.cli.config.RAW_RESPONSE_RETENTION_ENABLED", False
    )
    # Bypass the migration runner so first-install scenarios stay first-install.
    monkeypatch.setattr("oddsiq.cli.migrations.run", lambda path: None)

    class FakeStore:
        meta: dict[str, str] = {}

        def __init__(self, *a, **kw):
            pass

        async def initialize(self):
            pass

        async def close(self):
            pass

        async def get_final_games_without_snapshots(self):
            return []

        async def get_meta(self, key):
            return self.meta.get(key)

        async def set_meta(self, key, value):
            self.meta[key] = value

    class FakeScheduler:
        def __init__(self, *a, **kw):
            pass

        async def run(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr("oddsiq.cli.Store", FakeStore)
    monkeypatch.setattr("oddsiq.cli.Scheduler", FakeScheduler)

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **kw: None)


@pytest.mark.asyncio
async def test_backup_failure_logs_warning_when_db_existed(monkeypatch, tmp_path, caplog):
    """Backup returning None with an existing source DB logs a WARNING at both
    startup and shutdown (the 2026-08-10 rebrand replaced the Telegram alert
    with a log — the phone only carries real-money placed/won/lost)."""
    db_path = tmp_path / "oddsiq.db"
    db_path.touch()  # simulate prior session DB

    _stub_async_main_dependencies(monkeypatch, tmp_path, db_path=db_path)

    monkeypatch.setattr(
        "oddsiq.cli.backup.backup_database", lambda *a, **kw: None
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="oddsiq"):
        await cli.async_main()

    msgs = [r.getMessage() for r in caplog.records]
    assert any("Startup backup failed" in m for m in msgs)
    assert any("Shutdown backup failed" in m for m in msgs)


@pytest.mark.asyncio
async def test_backup_failure_silent_at_startup_when_db_did_not_exist(
    monkeypatch, tmp_path
):
    """First-install: startup backup returning None with no source DB → no startup alert."""
    db_path = tmp_path / "fresh.db"  # intentionally not created

    _stub_async_main_dependencies(monkeypatch, tmp_path, db_path=db_path)

    monkeypatch.setattr(
        "oddsiq.cli.backup.backup_database", lambda *a, **kw: None
    )
    sent: list[str] = []

    async def fake_send(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr("oddsiq.cli.telegram.send_telegram", fake_send)

    await cli.async_main()

    assert all("Startup backup failed" not in s for s in sent)


@pytest.mark.asyncio
async def test_backup_failure_alert_swallows_telegram_errors(monkeypatch, tmp_path):
    """An exception during the Telegram alert must not break async_main."""
    db_path = tmp_path / "oddsiq.db"
    db_path.touch()

    _stub_async_main_dependencies(monkeypatch, tmp_path, db_path=db_path)

    monkeypatch.setattr(
        "oddsiq.cli.backup.backup_database", lambda *a, **kw: None
    )

    async def raising_send(text: str) -> None:
        raise RuntimeError("telegram offline")

    monkeypatch.setattr("oddsiq.cli.telegram.send_telegram", raising_send)

    # Should complete cleanly despite Telegram raising twice.
    await cli.async_main()


@pytest.mark.asyncio
async def test_raw_response_retention_toggle_off_skips_prune(monkeypatch, tmp_path):
    """RAW_RESPONSE_RETENTION_ENABLED=False skips the pruner at both phases."""
    db_path = tmp_path / "oddsiq.db"
    db_path.touch()

    _stub_async_main_dependencies(monkeypatch, tmp_path, db_path=db_path)
    monkeypatch.setattr(
        "oddsiq.cli.backup.backup_database",
        lambda *a, **kw: tmp_path / f"fake-{a[2]}.db",
    )

    prune_calls: list[tuple] = []

    def fake_prune(root, keep_days):
        prune_calls.append((root, keep_days))

    monkeypatch.setattr(
        "oddsiq.cli.scraper_base.prune_raw_responses", fake_prune
    )

    await cli.async_main()

    assert prune_calls == []


@pytest.mark.asyncio
async def test_raw_response_retention_toggle_on_runs_prune_at_both_phases(
    monkeypatch, tmp_path
):
    """RAW_RESPONSE_RETENTION_ENABLED=True calls prune_raw_responses at startup and shutdown."""
    db_path = tmp_path / "oddsiq.db"
    db_path.touch()

    _stub_async_main_dependencies(monkeypatch, tmp_path, db_path=db_path)
    monkeypatch.setattr(
        "oddsiq.cli.config.RAW_RESPONSE_RETENTION_ENABLED", True
    )
    monkeypatch.setattr("oddsiq.cli.config.RAW_RESPONSE_KEEP_DAYS", 7)
    monkeypatch.setattr(
        "oddsiq.cli.backup.backup_database",
        lambda *a, **kw: tmp_path / f"fake-{a[2]}.db",
    )

    prune_calls: list[tuple] = []

    def fake_prune(root, keep_days):
        prune_calls.append((root, keep_days))

    monkeypatch.setattr(
        "oddsiq.cli.scraper_base.prune_raw_responses", fake_prune
    )

    await cli.async_main()

    assert len(prune_calls) == 2
    assert all(call[1] == 7 for call in prune_calls)


@pytest.mark.asyncio
async def test_audit_missed_gamedays_baselines_existing_misses_and_alerts_new_ones(
    tmp_path, caplog, monkeypatch
):
    """Pipeline downtime leaves final games with zero snapshots — there's
    no closing line, no CLV, no value bets from that night. Baseline
    existing misses once, then only alert when a newly missed game appears.
    """
    from datetime import datetime, timezone
    from oddsiq import cli
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, OddsSnapshot
    from oddsiq.db.store import Store

    db_path = tmp_path / "audit.db"
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    try:
        monkeypatch.setattr("oddsiq.cli.config.TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setattr("oddsiq.cli.config.TELEGRAM_CHAT_ID", "chat")
        send_telegram = AsyncMock()
        monkeypatch.setattr("oddsiq.cli.telegram.send_telegram", send_telegram)

        # Final game with zero snapshots — should be reported.
        missed = Game(
            external_id="nba_missed",
            home_team="Boston Celtics",
            away_team="Los Angeles Lakers",
            scheduled_at=datetime(2026, 4, 20, 23, 0, tzinfo=timezone.utc),
            status="final",
        )
        await store.upsert_game(missed)
        # Final game WITH snapshots — should not be reported.
        observed = Game(
            external_id="nba_observed",
            home_team="Miami Heat",
            away_team="Toronto Raptors",
            scheduled_at=datetime(2026, 4, 21, 23, 0, tzinfo=timezone.utc),
            status="final",
        )
        observed_id = await store.upsert_game(observed)
        await store.insert_odds_snapshot(OddsSnapshot(
            game_id=observed_id, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.95,
        ))

        with caplog.at_level("WARNING", logger="oddsiq"):
            await cli.audit_missed_gamedays(store)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) >= 1, "missed gameday should produce a WARNING"
        msg = " ".join(r.getMessage() for r in warnings)
        assert "nba_missed" in msg or "Boston Celtics" in msg, (
            f"warning must identify the missed game; got: {msg!r}"
        )
        assert "Telegram suppressed" in msg
        assert "nba_observed" not in msg and "Miami Heat" not in msg, (
            f"observed game must not be reported as missed; got: {msg!r}"
        )
        send_telegram.assert_not_awaited()

        caplog.clear()
        with caplog.at_level("INFO", logger="oddsiq"):
            await cli.audit_missed_gamedays(store)

        repeat_warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert repeat_warnings == [], "known misses should not warn repeatedly"
        assert "Telegram suppressed" in " ".join(
            r.getMessage() for r in caplog.records
        )
        send_telegram.assert_not_awaited()

        newly_missed = Game(
            external_id="nba_missed_later",
            home_team="New York Knicks",
            away_team="Atlanta Hawks",
            scheduled_at=datetime(2026, 4, 22, 23, 0, tzinfo=timezone.utc),
            status="final",
        )
        await store.upsert_game(newly_missed)

        caplog.clear()
        with caplog.at_level("WARNING", logger="oddsiq"):
            await cli.audit_missed_gamedays(store)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        msg = " ".join(r.getMessage() for r in warnings)
        assert "nba_missed_later" in msg
        assert "nba_missed (" not in msg, (
            f"known baseline miss should not be repeated; got: {msg!r}"
        )
        # Rebrand (2026-08-10): a newly missed game is logged, never phoned.
        send_telegram.assert_not_awaited()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cleanup_closing_flags_resets_stale_keeps_fresh(tmp_path, monkeypatch):
    """Cleanup must clear is_closing=1 from stale rows (under the new
    30-min gate) and re-mark fresh ones. Simulates pre-Fix-3 state where
    every latest row was indiscriminately marked is_closing=1."""
    from datetime import datetime, timedelta, timezone
    from oddsiq import cli
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, OddsSnapshot
    from oddsiq.db.store import Store

    db_path = tmp_path / "cleanup.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    try:
        tipoff = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
        game = Game(
            external_id="nba_stale_check",
            home_team="Cleveland Cavaliers",
            away_team="Toronto Raptors",
            scheduled_at=tipoff,
            status="final",
        )
        gid = await store.upsert_game(game)
        # One stale row (6h pre-tipoff) marked is_closing=1 — pre-Fix-3 fake.
        stale = OddsSnapshot(
            game_id=gid, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.95,
            captured_at=tipoff - timedelta(hours=6),
            is_closing=True,
        )
        # One fresh row (5 min pre-tipoff) marked is_closing=1 — should stay marked.
        fresh = OddsSnapshot(
            game_id=gid, bookmaker="pinnacle", market_type="moneyline",
            selection="away", odds=2.10,
            captured_at=tipoff - timedelta(minutes=5),
            is_closing=True,
        )
        await store.insert_odds_snapshot(stale)
        await store.insert_odds_snapshot(fresh)
    finally:
        await store.close()

    # Run cleanup (real, not dry-run)
    code = await cli.cleanup_closing_flags(dry_run=False)
    assert code == 0

    # Verify post-cleanup state
    store = Store(db_path)
    await store.initialize()
    try:
        latest_home = await store.get_latest_odds(gid, "pinnacle", "moneyline", "home")
        latest_away = await store.get_latest_odds(gid, "pinnacle", "moneyline", "away")
        assert latest_home.is_closing is False, "stale (6h pre-tipoff) row must be cleared"
        assert latest_away.is_closing is True, "fresh (5min pre-tipoff) row must remain marked"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cleanup_closing_flags_dry_run_does_not_write(tmp_path, monkeypatch):
    """--dry-run must not modify any rows."""
    from datetime import datetime, timedelta, timezone
    from oddsiq import cli
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, OddsSnapshot
    from oddsiq.db.store import Store

    db_path = tmp_path / "cleanup_dryrun.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    try:
        tipoff = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
        game = Game(
            external_id="nba_dryrun_check",
            home_team="Cleveland Cavaliers",
            away_team="Toronto Raptors",
            scheduled_at=tipoff,
            status="final",
        )
        gid = await store.upsert_game(game)
        stale = OddsSnapshot(
            game_id=gid, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.95,
            captured_at=tipoff - timedelta(hours=6),
            is_closing=True,
        )
        await store.insert_odds_snapshot(stale)
    finally:
        await store.close()

    code = await cli.cleanup_closing_flags(dry_run=True)
    assert code == 0

    store = Store(db_path)
    await store.initialize()
    try:
        latest = await store.get_latest_odds(gid, "pinnacle", "moneyline", "home")
        assert latest.is_closing is True, (
            "dry-run must not modify rows; stale flag should still be set"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cleanup_dry_run_count_matches_real_mark_with_post_tipoff_latest(tmp_path, monkeypatch):
    """Regression: --dry-run undercounted when a selection's LATEST snapshot was
    post-tipoff (a dedup 'last seen' bump / late live poll). The would-mark count
    must equal what mark_closing_snapshots actually marks — both must filter the
    pre-tipoff window INSIDE the ROW_NUMBER() input, not in an outer WHERE."""
    from datetime import datetime, timedelta, timezone
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, OddsSnapshot
    from oddsiq.db.store import Store

    db_path = tmp_path / "drycount.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    try:
        tipoff = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
        game = Game(external_id="nba_dedup_bump", home_team="Cleveland Cavaliers",
                    away_team="Toronto Raptors", scheduled_at=tipoff, status="final")
        gid = await store.upsert_game(game)
        # Valid closing candidate: fresh PRE-tipoff snapshot.
        await store.insert_odds_snapshot(OddsSnapshot(
            game_id=gid, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.95, captured_at=tipoff - timedelta(minutes=10)))
        # Later POST-tipoff snapshot (dedup bump / live poll) — must NOT win
        # ROW_NUMBER()=1 and orphan the pre-tipoff candidate.
        await store.insert_odds_snapshot(OddsSnapshot(
            game_id=gid, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.95, captured_at=tipoff + timedelta(minutes=5)))

        would_mark = await store.count_markable_closing_snapshots(gid, 30)
        await store.mark_closing_snapshots(gid, 30)
        actually_marked = await store.count_closing_flags(gid)

        assert would_mark == 1
        assert would_mark == actually_marked
    finally:
        await store.close()


def test_format_missed_games_html_escapes_team_names():
    """The missed-gameday summary is sent via an HTML-parse-mode Telegram
    message, so feed-sourced team names containing &, <, or > must be escaped
    or Telegram rejects the alert."""
    from datetime import datetime, timezone
    from oddsiq.db.models import Game

    g = Game(
        external_id="x1",
        home_team="A & B",
        away_team="C <D>",
        scheduled_at=datetime(2026, 4, 20, 23, 0, tzinfo=timezone.utc),
        status="final",
    )
    out = cli._format_missed_games([g])
    assert "&amp;" in out
    assert "&lt;D&gt;" in out


@pytest.mark.asyncio
async def test_audit_missed_gamedays_silent_when_no_misses(tmp_path, caplog):
    """No final-game-without-snapshots → no warning."""
    from oddsiq import cli
    from oddsiq.db import migrations
    from oddsiq.db.store import Store

    db_path = tmp_path / "audit.db"
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    try:
        with caplog.at_level("WARNING", logger="oddsiq"):
            await cli.audit_missed_gamedays(store)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == [], f"empty DB should produce no warnings; got: {warnings}"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_backup_skipped_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr("oddsiq.cli.config.ODDS_API_KEY", "fake")
    monkeypatch.setattr("oddsiq.cli.config.BACKUP_ENABLED", False)
    monkeypatch.setattr("oddsiq.cli.config.RAW_DATA_DIR", tmp_path / "raw")

    calls: list[str] = []

    def fake_backup(*a, **kw):
        calls.append("backup")
        return None

    class FakeStore:
        meta: dict[str, str] = {}

        def __init__(self, *a, **kw): pass
        async def initialize(self): pass
        async def close(self): pass
        async def get_final_games_without_snapshots(self): return []
        async def get_meta(self, key): return self.meta.get(key)
        async def set_meta(self, key, value): self.meta[key] = value

    class FakeScheduler:
        def __init__(self, *a, **kw): pass
        async def run(self): pass
        def stop(self): pass

    monkeypatch.setattr("oddsiq.cli.Store", FakeStore)
    monkeypatch.setattr("oddsiq.cli.Scheduler", FakeScheduler)
    monkeypatch.setattr("oddsiq.cli.backup.backup_database", fake_backup)
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **kw: None)

    await cli.async_main()

    assert calls == []


@pytest.mark.asyncio
async def test_audit_bookmakers_runs_and_prints_section_headers(monkeypatch, tmp_path, capsys):
    """`oddsiq audit-bookmakers` opens the store, runs the audit, prints the
    text report, and exits 0."""
    from oddsiq.db import migrations

    db_path = tmp_path / "audit.db"
    migrations.run(db_path)

    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)

    code = await cli.audit_bookmakers(bookmaker=None, since=None, as_json=False)
    out = capsys.readouterr().out

    assert code == 0
    assert "Section 1" in out
    assert "Section 5" in out
    assert "Section 6" not in out


@pytest.mark.asyncio
async def test_audit_bookmakers_json_flag_produces_parseable_json(monkeypatch, tmp_path, capsys):
    """`--json` emits valid JSON with the five section keys."""
    import json
    from oddsiq.db import migrations

    db_path = tmp_path / "audit.db"
    migrations.run(db_path)
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)

    code = await cli.audit_bookmakers(bookmaker=None, since=None, as_json=True)
    out = capsys.readouterr().out

    assert code == 0
    parsed = json.loads(out)
    assert set(parsed.keys()) == {
        "snapshot_volumes", "value_bets", "clv_by_bucket",
        "closing_quality", "coverage_gap_proxy",
    }


def test_parse_args_audit_bookmakers_with_filters():
    args = cli._parse_args(["audit-bookmakers", "--bookmaker", "tipsport", "--since", "2026-05-15", "--json"])
    assert args.command == "audit-bookmakers"
    assert args.bookmaker == "tipsport"
    assert args.since == "2026-05-15"
    assert args.as_json is True


@pytest.mark.asyncio
async def test_audit_bookmakers_filters_round_trip_to_audit_module(monkeypatch, tmp_path):
    """`audit_bookmakers(bookmaker=..., since=...)` must thread BOTH filters
    into `run_audit`. Parse-args alone doesn't prove the CLI wrapper forwards
    them — this test verifies the round trip end-to-end."""
    from oddsiq.db import migrations

    db_path = tmp_path / "audit.db"
    migrations.run(db_path)
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)

    captured: dict[str, object] = {}

    async def fake_run_audit(store, *, bookmaker=None, since=None):
        captured["bookmaker"] = bookmaker
        captured["since"] = since
        return {
            "snapshot_volumes": [], "value_bets": [], "clv_by_bucket": [],
            "closing_quality": [], "coverage_gap_proxy": [],
        }

    monkeypatch.setattr("oddsiq.audit.bookmakers.run_audit", fake_run_audit)

    code = await cli.audit_bookmakers(bookmaker="tipsport", since="2026-05-15", as_json=True)

    assert code == 0
    # A bare date is normalized to a full UTC instant before it reaches the SQL
    # layer (where it is compared lexicographically against +00:00 timestamps).
    assert captured == {"bookmaker": "tipsport", "since": "2026-05-15T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_audit_bookmakers_normalizes_offset_since_to_utc(monkeypatch, tmp_path):
    """A --since with an explicit non-UTC offset must be converted to its UTC
    instant before the lexicographic captured_at/scheduled_at comparison —
    otherwise a +02:00 string mis-sorts against stored +00:00 timestamps and
    silently drops in-window rows (parity with live-scout-report)."""
    from oddsiq.db import migrations

    db_path = tmp_path / "audit.db"
    migrations.run(db_path)
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)

    captured: dict[str, object] = {}

    async def fake_run_audit(store, *, bookmaker=None, since=None):
        captured["since"] = since
        return {
            "snapshot_volumes": [], "value_bets": [], "clv_by_bucket": [],
            "closing_quality": [], "coverage_gap_proxy": [],
        }

    monkeypatch.setattr("oddsiq.audit.bookmakers.run_audit", fake_run_audit)

    code = await cli.audit_bookmakers(
        bookmaker=None, since="2026-06-01T12:00:00+02:00", as_json=True)

    assert code == 0
    assert captured["since"] == "2026-06-01T10:00:00+00:00"


@pytest.mark.asyncio
async def test_audit_bookmakers_rejects_malformed_since(tmp_path, monkeypatch, capsys):
    """A malformed --since must fail fast (exit 1 + stderr hint), not silently
    feed garbage into a lexicographic flagged_at comparison."""
    db_path = tmp_path / "audit.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    code = await cli.audit_bookmakers(bookmaker=None, since="june first", as_json=False)
    assert code == 1
    assert "since" in capsys.readouterr().err.lower()


def test_parse_args_tipsport_smoke():
    args = cli._parse_args(["tipsport-smoke"])
    assert args.command == "tipsport-smoke"


def test_parse_args_sport_flag():
    from oddsiq.cli import _parse_args
    assert _parse_args(["tipsport-smoke", "--sport", "mlb"]).sport == "mlb"
    assert _parse_args(["tipsport-smoke"]).sport == "nba"


def _smoke_snap(
    odds: float = 1.95,
    selection: str = "home",
    home: str = "A",
    away: str = "B",
    bookmaker: str = "tipsport",
    market_type: str = "moneyline",
    line: float | None = None,
):
    """Build a minimal OddsSnapshot the smoke command will accept."""
    from datetime import datetime, timezone
    from oddsiq.db.models import OddsSnapshot
    return OddsSnapshot(
        game_id=0, bookmaker=bookmaker, market_type=market_type,
        selection=selection, odds=odds, line=line,
        _home_team=home, _away_team=away,
        captured_at=datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_tipsport_smoke_healthy_exits_0(monkeypatch, capsys):
    from oddsiq.cli import tipsport_smoke

    async def fake_fetch(self, game_date, profile):
        self.last_fetch_error_count = 0
        return [
            _smoke_snap(
                bookmaker="tipsport", market_type="moneyline",
                selection="home", odds=1.80,
            ),
            _smoke_snap(
                bookmaker="tipsport", market_type="moneyline",
                selection="away", odds=2.05,
            ),
            _smoke_snap(
                bookmaker="tipsport", market_type="spread",
                selection="home", odds=1.91, line=-3.5,
            ),
        ]

    monkeypatch.setattr("oddsiq.scrapers.tipsport.TipsportScraper.fetch_odds", fake_fetch)

    code = await tipsport_smoke()
    out = capsys.readouterr().out

    assert code == 0
    assert "Tipsport smoke test" in out
    assert "Market counts" in out


@pytest.mark.asyncio
async def test_tipsport_smoke_prints_two_way_overround(monkeypatch, capsys):
    # The hold measurement used before arming a new sport (2026-08-25, NFL):
    # 1/1.80 + 1/2.05 = 1.043 — bounds the max co-temporal edge.
    from oddsiq.cli import tipsport_smoke

    async def fake_fetch(self, game_date, profile):
        self.last_fetch_error_count = 0
        return [
            _smoke_snap(bookmaker="tipsport", market_type="moneyline",
                        selection="home", odds=1.80),
            _smoke_snap(bookmaker="tipsport", market_type="moneyline",
                        selection="away", odds=2.05),
        ]

    monkeypatch.setattr("oddsiq.scrapers.tipsport.TipsportScraper.fetch_odds", fake_fetch)

    code = await tipsport_smoke()
    out = capsys.readouterr().out
    assert code == 0
    assert "Two-way overround" in out
    assert "avg 1.043" in out


@pytest.mark.asyncio
async def test_tipsport_smoke_fetch_error_exits_1(monkeypatch, capsys):
    from oddsiq.cli import tipsport_smoke

    async def fake_fetch(self, game_date, profile):
        self.last_fetch_error_count = 1
        return [
            _smoke_snap(bookmaker="tipsport", selection="home", odds=1.80),
            _smoke_snap(bookmaker="tipsport", selection="away", odds=2.05),
        ]

    monkeypatch.setattr("oddsiq.scrapers.tipsport.TipsportScraper.fetch_odds", fake_fetch)

    code = await tipsport_smoke()
    err = capsys.readouterr().err

    assert code == 1
    assert "internal fetch error" in err.lower()


@pytest.mark.asyncio
async def test_tipsport_smoke_zero_moneyline_exits_1(monkeypatch, capsys):
    from oddsiq.cli import tipsport_smoke

    async def fake_fetch(self, game_date, profile):
        self.last_fetch_error_count = 0
        return [
            _smoke_snap(
                bookmaker="tipsport", market_type="spread",
                selection="home", odds=1.91, line=-3.5,
            ),
            _smoke_snap(
                bookmaker="tipsport", market_type="spread",
                selection="away", odds=1.91, line=3.5,
            ),
        ]

    monkeypatch.setattr("oddsiq.scrapers.tipsport.TipsportScraper.fetch_odds", fake_fetch)

    code = await tipsport_smoke()
    err = capsys.readouterr().err

    assert code == 1
    assert "zero moneyline" in err.lower()


# ---------------------------------------------------------------------------
# Logging hygiene — no API secrets in terminal output
# ---------------------------------------------------------------------------


def test_setup_logging_silences_httpx():
    """httpx and friends must not log URLs at INFO — those URLs leak the
    Odds API ``?apiKey=...`` and Telegram bot tokens into the terminal."""
    import logging

    cli.setup_logging()
    for name in ("httpx", "httpcore", "urllib3"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, (
            f"logger {name!r} would still print INFO-level URL requests"
        )


def test_redacting_filter_removes_odds_api_key():
    """The Odds API embeds the secret in a ``?apiKey=...`` query param."""
    import logging

    f = cli._SecretsRedactingFilter()
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname="", lineno=0,
        msg=(
            "HTTP Request: GET "
            "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
            "?apiKey=0123456789abcdef0123456789abcdef&regions=us"
        ),
        args=None, exc_info=None,
    )
    f.filter(record)
    rendered = record.getMessage()
    assert "0123456789abcdef0123456789abcdef" not in rendered
    assert "apiKey=***" in rendered


def test_redacting_filter_removes_telegram_bot_token():
    """Telegram puts the bot token in the URL path: ``/bot<id>:<token>``."""
    import logging

    f = cli._SecretsRedactingFilter()
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname="", lineno=0,
        msg=(
            "HTTP Request: POST "
            "https://api.telegram.org/bot123456789:AAFakeTokenForRedactionTest_000000/sendMessage"
        ),
        args=None, exc_info=None,
    )
    f.filter(record)
    rendered = record.getMessage()
    assert "AAFakeTokenForRedactionTest_000000" not in rendered
    assert "/bot***" in rendered


def test_redacting_filter_removes_bearer_token():
    import logging

    f = cli._SecretsRedactingFilter()
    record = logging.LogRecord(
        name="auth", level=logging.INFO, pathname="", lineno=0,
        msg="Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
        args=None, exc_info=None,
    )
    f.filter(record)
    rendered = record.getMessage()
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in rendered
    assert "Bearer ***" in rendered


def test_redacting_filter_passes_clean_records_unchanged():
    """A log line with no secrets must round-trip byte-for-byte."""
    import logging

    f = cli._SecretsRedactingFilter()
    original = "Scraper health: name=tipsport duration_ms=400 snapshots=4 matched_games=2 errors=0"
    record = logging.LogRecord(
        name="oddsiq.scheduler", level=logging.INFO, pathname="", lineno=0,
        msg=original, args=None, exc_info=None,
    )
    f.filter(record)
    assert record.getMessage() == original


def test_install_secrets_filter_is_idempotent():
    """Calling setup_logging twice must not stack filters on the root logger."""
    import logging

    cli.setup_logging()
    cli.setup_logging()
    root_filters = [
        f for f in logging.getLogger().filters
        if isinstance(f, cli._SecretsRedactingFilter)
    ]
    assert len(root_filters) == 1


def test_console_log_redacts_api_key_from_child_logger(monkeypatch):
    """Secrets in records propagated from oddsiq.* child loggers must be
    redacted on the console (stderr) StreamHandler — docker logs reads stderr.
    A filter on the root *logger* does NOT run for child-propagated records;
    only handler-level filters do."""
    import io
    import logging

    monkeypatch.setattr(config, "LOG_FILE_ENABLED", False)  # isolate to stream handler
    _clear_root_handlers()
    root = logging.getLogger()
    root.filters = [
        f for f in root.filters if not isinstance(f, cli._SecretsRedactingFilter)
    ]
    cli.setup_logging()
    stream_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]
    assert stream_handlers, "expected a console StreamHandler from basicConfig"
    captured = io.StringIO()
    for h in stream_handlers:
        h.stream = captured
    logging.getLogger("oddsiq.test").info("GET https://api.example/x?apiKey=ABCDEF1234")
    for h in stream_handlers:
        h.flush()
    out = captured.getvalue()
    assert "ABCDEF1234" not in out
    assert "apiKey=***" in out


# ---------------------------------------------------------------------------
# live-discover probe
# ---------------------------------------------------------------------------


def _live_game(
    *,
    external_id: str = "nba_live",
    home: str = "Boston Celtics",
    away: str = "Los Angeles Lakers",
    minutes_in: int = 20,
    status: str = "live",
):
    """Build a Game whose effective tipoff is `minutes_in` minutes in the past."""
    from datetime import datetime, timedelta, timezone
    from oddsiq.db.models import Game
    tipoff = datetime.now(timezone.utc) - timedelta(minutes=minutes_in)
    return Game(
        external_id=external_id,
        home_team=home,
        away_team=away,
        scheduled_at=tipoff,
        commence_time=tipoff,
        status=status,
    )


def _pretipoff_game(
    *,
    external_id: str = "nba_future",
    home: str = "Future Home",
    away: str = "Future Away",
):
    from datetime import datetime, timedelta, timezone
    from oddsiq.db.models import Game
    tipoff = datetime.now(timezone.utc) + timedelta(hours=2)
    return Game(
        external_id=external_id,
        home_team=home,
        away_team=away,
        scheduled_at=tipoff,
        commence_time=tipoff,
        status="scheduled",
    )


def test_setup_logging_attaches_rotating_file_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "oddsiq.log")
    monkeypatch.setattr(config, "LOG_FILE_ENABLED", True)
    _clear_root_handlers()
    root = logging.getLogger()
    cli.setup_logging()
    file_handlers = [h for h in root.handlers
                     if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename) == (tmp_path / "oddsiq.log").resolve()
    assert (tmp_path / "oddsiq.log").parent.exists()


def test_setup_logging_idempotent_does_not_double_attach(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "oddsiq.log")
    monkeypatch.setattr(config, "LOG_FILE_ENABLED", True)
    _clear_root_handlers()
    root = logging.getLogger()
    cli.setup_logging()
    cli.setup_logging()
    cli.setup_logging()
    file_handlers = [h for h in root.handlers
                     if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1


def test_setup_logging_disabled_skips_file_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "oddsiq.log")
    monkeypatch.setattr(config, "LOG_FILE_ENABLED", False)
    _clear_root_handlers()
    root = logging.getLogger()
    cli.setup_logging()
    file_handlers = [h for h in root.handlers
                     if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert file_handlers == []


def test_file_log_redacts_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "oddsiq.log")
    monkeypatch.setattr(config, "LOG_FILE_ENABLED", True)
    _clear_root_handlers()
    root = logging.getLogger()
    cli.setup_logging()
    logging.getLogger("oddsiq.test").info("GET https://api.example/x?apiKey=ABCDEF1234")
    for h in root.handlers:
        h.flush()
    text = (tmp_path / "oddsiq.log").read_text()
    assert "ABCDEF1234" not in text
    assert "apiKey=***" in text


# ---------------------------------------------------------------------------
# dashboard subcommand — B3
# ---------------------------------------------------------------------------


def test_dashboard_subcommand_parses():
    args = cli._parse_args(["dashboard"])
    assert args.command == "dashboard"
    assert args.host == "127.0.0.1"
    assert args.port == 8080


def test_dashboard_subcommand_accepts_host_port():
    args = cli._parse_args(["dashboard", "--host", "0.0.0.0", "--port", "9999"])
    assert args.host == "0.0.0.0"
    assert args.port == 9999


# ---------------------------------------------------------------------------
# live-scout-report subcommand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tipsport_smoke_unknown_sport_returns_1(capsys):
    code = await cli.tipsport_smoke(sport="bogus")
    assert code == 1
    assert "Unknown sport 'bogus'" in capsys.readouterr().err


def test_parse_args_delete_zombie_games():
    args = cli._parse_args(["delete-zombie-games", "--dry-run", "--grace-days", "5"])
    assert args.command == "delete-zombie-games"
    assert args.dry_run is True
    assert args.grace_days == 5
    assert args.include_scheduled is False
    args = cli._parse_args(["delete-zombie-games", "--include-scheduled"])
    assert args.dry_run is False
    assert args.grace_days == 3
    assert args.include_scheduled is True


def test_find_zombie_scheduled_games_age_rule():
    """Stuck-'scheduled' zombie = tipoff + per-sport backstop + grace days has
    passed while the game never went live/final (postponed/cancelled fixtures
    drop from the Odds API feed and surface as 'scheduled' until they age
    out). A game merely hours past tipoff — finals window still open — must
    NOT match, and live games are the other finder's business."""
    from datetime import datetime, timedelta, timezone
    from oddsiq.db.models import Game

    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    def _game(ext, *, hours_ago, status, sport="mlb"):
        return Game(
            external_id=ext, home_team="A", away_team="B",
            scheduled_at=now - timedelta(hours=hours_ago),
            status=status, sport=sport)

    games = [
        _game("dropped_fixture", hours_ago=300, status="scheduled"),  # zombie
        _game("just_past_tipoff", hours_ago=1, status="scheduled"),   # keep
        _game("inside_grace", hours_ago=73, status="scheduled"),      # 4h+3d=76h, keep
        _game("future", hours_ago=-5, status="scheduled"),            # keep
        _game("live_not_ours", hours_ago=300, status="live"),         # other finder
    ]
    zombies = cli._find_zombie_scheduled_games(games, now, grace_days=3)
    assert [g.external_id for g in zombies] == ["dropped_fixture"]


@pytest.mark.asyncio
async def test_delete_zombie_games_include_scheduled(tmp_path, monkeypatch):
    """--include-scheduled also removes dropped/postponed fixtures stuck
    'scheduled' past backstop+grace; without the flag they are untouched."""
    from datetime import datetime, timedelta, timezone
    from oddsiq.db import migrations
    from oddsiq.db.models import Game
    from oddsiq.db.store import Store

    db_path = tmp_path / "sched-zombies.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    try:
        now = datetime.now(timezone.utc)
        await store.upsert_game(Game(
            external_id="dropped", home_team="A", away_team="B",
            scheduled_at=now - timedelta(days=12), status="scheduled", sport="mlb"))
        await store.upsert_game(Game(
            external_id="upcoming", home_team="C", away_team="D",
            scheduled_at=now + timedelta(hours=5), status="scheduled", sport="mlb"))
    finally:
        await store.close()

    # Default: scheduled zombies untouched.
    assert await cli.delete_zombie_games(dry_run=False) == 0
    store = Store(db_path)
    await store.initialize()
    try:
        cur = await store._db.execute("SELECT COUNT(*) FROM games")
        assert (await cur.fetchone())[0] == 2
    finally:
        await store.close()

    # With the flag: only the aged-out fixture goes.
    assert await cli.delete_zombie_games(dry_run=False, include_scheduled=True) == 0
    store = Store(db_path)
    await store.initialize()
    try:
        cur = await store._db.execute("SELECT external_id FROM games")
        assert [r[0] for r in await cur.fetchall()] == ["upcoming"]
    finally:
        await store.close()


def test_find_zombie_live_games_respects_backstop_grace_and_status():
    """Zombie = status='live' AND tipoff + per-sport live backstop + grace days
    has passed. A live game inside the grace window, a fresh live game, and a
    non-live game must all be excluded."""
    from datetime import datetime, timedelta, timezone
    from oddsiq.db.models import Game

    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

    def _game(ext, *, hours_ago, status, sport):
        return Game(
            external_id=ext, home_team="A", away_team="B",
            scheduled_at=now - timedelta(hours=hours_ago),
            status=status, sport=sport)

    games = [
        # MLB backstop 4h + 3d grace = 76h; 100h ago -> zombie.
        _game("zombie_mlb", hours_ago=100, status="live", sport="mlb"),
        # NHL backstop 2.5h + 3d grace = 74.5h; 73h ago -> inside grace, keep.
        _game("graced_nhl", hours_ago=73, status="live", sport="nhl"),
        # Fresh live game -> keep.
        _game("fresh", hours_ago=1, status="live", sport="mlb"),
        # Old but not live -> not this command's business.
        _game("old_scheduled", hours_ago=300, status="scheduled", sport="mlb"),
    ]

    zombies = cli._find_zombie_live_games(games, now, grace_days=3)
    assert [g.external_id for g in zombies] == ["zombie_mlb"]


@pytest.mark.asyncio
async def test_delete_zombie_games_deletes_stuck_live_only(tmp_path, monkeypatch):
    """The one-shot must delete a game stuck 'live' past backstop+grace along
    with its snapshots/bets, and leave fresh live games and final games (with
    their rows) untouched."""
    from datetime import datetime, timedelta, timezone
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, OddsSnapshot, ValueBet
    from oddsiq.db.store import Store

    db_path = tmp_path / "zombies.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    try:
        now = datetime.now(timezone.utc)
        zombie_tip = now - timedelta(days=10)
        zombie_id = await store.upsert_game(Game(
            external_id="zombie", home_team="Atlanta Braves",
            away_team="San Francisco Giants", scheduled_at=zombie_tip,
            status="live", sport="mlb"))
        await store.insert_odds_snapshots_batch([OddsSnapshot(
            game_id=zombie_id, bookmaker="tipsport", market_type="moneyline",
            selection="home", odds=1.9, captured_at=zombie_tip, sport="mlb")])
        await store.insert_value_bet(ValueBet(
            game_id=zombie_id, bookmaker="tipsport", market_type="moneyline",
            selection="home", entry_odds=2.10, fair_odds=2.00, edge_pct=5.0,
            sport="mlb"))
        fresh_id = await store.upsert_game(Game(
            external_id="fresh_live", home_team="C", away_team="D",
            scheduled_at=now - timedelta(hours=1), status="live", sport="mlb"))
        final_id = await store.upsert_game(Game(
            external_id="done", home_team="E", away_team="F",
            scheduled_at=now - timedelta(days=30), status="final",
            home_score=3, away_score=1, sport="mlb"))
        await store.insert_value_bet(ValueBet(
            game_id=final_id, bookmaker="tipsport", market_type="moneyline",
            selection="home", entry_odds=2.10, fair_odds=2.00, edge_pct=5.0,
            sport="mlb"))
    finally:
        await store.close()

    code = await cli.delete_zombie_games(dry_run=False)
    assert code == 0

    store = Store(db_path)
    await store.initialize()
    try:
        cur = await store._db.execute(
            "SELECT external_id FROM games ORDER BY external_id")
        assert [r[0] for r in await cur.fetchall()] == ["done", "fresh_live"]
        for table in ("odds_snapshots", "value_bets"):
            cur = await store._db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE game_id=?", (zombie_id,))
            assert (await cur.fetchone())[0] == 0, f"zombie {table} must be gone"
        cur = await store._db.execute(
            "SELECT COUNT(*) FROM value_bets WHERE game_id=?", (final_id,))
        assert (await cur.fetchone())[0] == 1, "final game's bet must survive"
        assert (await store.get_games_by_status("live"))[0].id == fresh_id
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delete_zombie_games_dry_run_does_not_write(tmp_path, monkeypatch):
    """--dry-run must delete nothing and still exit 0."""
    from datetime import datetime, timedelta, timezone
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, OddsSnapshot
    from oddsiq.db.store import Store

    db_path = tmp_path / "zombies_dry.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    try:
        now = datetime.now(timezone.utc)
        zombie_tip = now - timedelta(days=10)
        zombie_id = await store.upsert_game(Game(
            external_id="zombie", home_team="A", away_team="B",
            scheduled_at=zombie_tip, status="live", sport="mlb"))
        await store.insert_odds_snapshots_batch([OddsSnapshot(
            game_id=zombie_id, bookmaker="tipsport", market_type="moneyline",
            selection="home", odds=1.9, captured_at=zombie_tip, sport="mlb")])
    finally:
        await store.close()

    code = await cli.delete_zombie_games(dry_run=True)
    assert code == 0

    store = Store(db_path)
    await store.initialize()
    try:
        cur = await store._db.execute("SELECT COUNT(*) FROM games")
        assert (await cur.fetchone())[0] == 1, "dry-run must not delete the game"
        cur = await store._db.execute("SELECT COUNT(*) FROM odds_snapshots")
        assert (await cur.fetchone())[0] == 1, "dry-run must not delete snapshots"
    finally:
        await store.close()


def test_parse_args_betting():
    args = cli._parse_args(["betting", "--arm"])
    assert args.command == "betting" and args.arm and not args.disarm
    args = cli._parse_args(["betting", "--disarm"])
    assert args.disarm and not args.arm
    args = cli._parse_args(["betting"])
    assert not args.arm and not args.disarm  # bare = status
    args = cli._parse_args([
        "betting", "--reconcile", "42", "--as-placed",
        "--accepted-odds", "2.08", "--receipt-id", "T123",
    ])
    assert args.reconcile_id == 42 and args.reconcile_as == "placed"


@pytest.mark.asyncio
async def test_betting_control_arm_disarm_status(tmp_path, monkeypatch, capsys):
    from oddsiq.db import migrations
    from oddsiq.db.store import Store

    db_path = tmp_path / "betting.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    migrations.run(db_path)

    assert await cli.betting_control(arm=True) == 0
    store = Store(db_path)
    await store.initialize()
    try:
        assert await store.get_meta("betting_armed") == "1"
    finally:
        await store.close()

    assert await cli.betting_control(disarm=True) == 0
    store = Store(db_path)
    await store.initialize()
    try:
        assert await store.get_meta("betting_armed") == "0"
    finally:
        await store.close()

    assert await cli.betting_control() == 0   # bare status
    out = capsys.readouterr().out
    assert "armed" in out.lower()
    assert "stake" in out.lower()


@pytest.mark.asyncio
async def test_betting_status_prints_effective_clamped_caps(tmp_path, monkeypatch, capsys):
    """A meta hand-edited past its env ceiling must NOT read back as if it
    were in force — the engine clamps it, so the status line has to show the
    enforced value (with the raw meta called out as clamped)."""
    from oddsiq.db import migrations
    from oddsiq.db.store import Store

    db_path = tmp_path / "caps.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    monkeypatch.setattr("oddsiq.cli.config.BETTING_MAX_STAKE_CZK", 500.0)
    monkeypatch.setattr("oddsiq.cli.config.BETTING_MAX_BETS_PER_DAY", 10)
    monkeypatch.setattr("oddsiq.cli.config.BETTING_MAX_OPEN_STAKES_CZK", 500.0)
    monkeypatch.setattr("oddsiq.cli.config.BETTING_MAX_DAILY_LOSS_CZK", 300.0)
    migrations.run(db_path)

    store = Store(db_path)
    await store.initialize()
    try:
        await store.set_meta("betting_max_bets_per_day", "999")
        await store.set_meta("betting_flat_stake_czk", "9999")
        await store.set_meta("betting_max_open_stakes_czk", "400.0")   # under the ceiling
        await store.set_meta("betting_daily_loss_limit_czk", "not-a-number")
    finally:
        await store.close()

    assert await cli.betting_control() == 0
    out = capsys.readouterr().out
    assert "Max bets/day:           10 (meta 999, clamped)" in out
    assert "Flat stake (Kč):        500 (meta 9999, clamped)" in out
    assert "Max open stakes (Kč):   400" in out and "clamped" not in out.split(
        "Max open stakes")[1].split("\n")[0]
    # Garbage falls back to the engine's default, flagged as such.
    assert "Daily loss limit (Kč):  300 (meta not-a-number, invalid)" in out


def test_effective_money_knob_matches_the_engine_clamp():
    # Unset meta -> the engine default, labelled.
    assert cli._effective_money_knob(None, 20.0, 500.0) == "20 (default)"
    # In-range meta prints bare (no misleading note).
    assert cli._effective_money_knob("250.0", 20.0, 500.0) == "250"
    # Over the ceiling -> the ceiling wins, raw shown.
    assert cli._effective_money_knob("9999", 20.0, 500.0) == "500 (meta 9999, clamped)"
    # Negative -> floor at 0 (mirrors _bounded_meta_float's minimum).
    assert cli._effective_money_knob("-5", 20.0, 500.0) == "0 (meta -5, clamped)"
    # Ints use int parsing, exactly like _bounded_meta_int.
    assert cli._effective_money_knob("7", 10, 10, as_int=True) == "7"
    assert cli._effective_money_knob("7.5", 10, 10, as_int=True) == "10 (meta 7.5, invalid)"
    # Non-finite is not a cap — it falls back to the default, like _meta_float.
    assert cli._effective_money_knob("inf", 20.0, 500.0) == "20 (meta inf, invalid)"


@pytest.mark.asyncio
async def test_betting_reconcile_reports_whether_it_disarmed(tmp_path, monkeypatch, capsys):
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, PlacedBet, ValueBet
    from oddsiq.db.store import Store
    from datetime import datetime, timedelta, timezone

    db_path = tmp_path / "reconcile.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    migrations.run(db_path)

    async def _seed(external_id: str, selection: str) -> int:
        """One game + value bet + ambiguous placed_bet -> the placed_bet id."""
        s = Store(db_path)
        await s.initialize()
        try:
            gid = await s.upsert_game(Game(
                external_id=external_id, home_team="New York Yankees",
                away_team="Boston Red Sox",
                scheduled_at=datetime.now(timezone.utc) + timedelta(hours=2),
                status="scheduled", sport="mlb",
            ))
            vb_id = await s.insert_value_bet(ValueBet(
                game_id=gid, bookmaker="tipsport", market_type="moneyline",
                selection=selection, entry_odds=2.1, fair_odds=1.95, edge_pct=7.0,
                flagged_at=datetime.now(timezone.utc), tier="warm", sport="mlb",
            ))
            return await s.insert_placed_bet(PlacedBet(
                value_bet_id=vb_id, game_id=gid, sport="mlb", status="unknown",
                stake_czk=20.0, requested_odds=2.1,
            ))
        finally:
            await s.close()

    first = await _seed("mlb-rec-1", "home")
    second = await _seed("mlb-rec-2", "away")

    store = Store(db_path)
    await store.initialize()
    try:
        await store.set_meta("betting_armed", "1")
    finally:
        await store.close()

    assert await cli.betting_control(
        reconcile_id=first, reconcile_as="rejected", reason="absent_from_history",
    ) == 0
    out = capsys.readouterr().out
    assert "Engine disarmed (was armed)" in out
    assert "remains disarmed" not in out

    # Already disarmed: the wording must not claim a disarm that didn't happen.
    assert await cli.betting_control(
        reconcile_id=second, reconcile_as="rejected", reason="absent_from_history",
    ) == 0
    assert "Engine remains disarmed" in capsys.readouterr().out
