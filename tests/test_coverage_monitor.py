import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from oddsiq.db import migrations
from oddsiq.db.models import Game
from oddsiq.db.store import Store
from oddsiq.pipeline.coverage_monitor import CoverageMonitor


@pytest_asyncio.fixture
async def store(tmp_path):
    db = tmp_path / "t.db"
    migrations.run(db)
    s = Store(db)
    await s.initialize()
    yield s
    await s.close()


def _capture_telegram(monkeypatch) -> list[str]:
    """Capture coverage-gap WARNING logs (the rebrand replaced the Telegram
    alert with a log line — 2026-08-10). Each captured string is the formatted
    ``Coverage gap: game_id=... scraper=... tier=... minutes_stale=...`` line."""
    sent: list[str] = []

    def fake_warning(msg, *args, **kwargs):
        text = (msg % args) if args else msg
        if str(text).startswith("Coverage gap:"):
            sent.append(text)

    monkeypatch.setattr("oddsiq.pipeline.coverage_monitor.logger.warning", fake_warning)
    return sent


async def _make_game(
    store: Store,
    *,
    external_id: str,
    seconds_to_tipoff: float,
) -> Game:
    """Build and persist a game with tipoff `seconds_to_tipoff` seconds from now."""
    now = datetime.now(timezone.utc)
    game = Game(
        external_id=external_id,
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=now + timedelta(seconds=seconds_to_tipoff),
        status="scheduled",
    )
    game.id = await store.upsert_game(game)
    return game


@pytest.mark.asyncio
async def test_check_gaps_prunes_alert_keys_for_dropped_games(store):
    """Gap-alert dedup keys for games no longer in the slate are pruned so the
    set can't grow unbounded over a long-lived process."""
    mon = CoverageMonitor()
    mon._coverage_gap_alerted.add((999, "pinnacle"))  # stale: game long gone
    current = await _make_game(store, external_id="still_here", seconds_to_tipoff=5400)

    await mon.check_gaps([current], [], datetime.now(timezone.utc))

    assert (999, "pinnacle") not in mon._coverage_gap_alerted


def _scrapers(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=n) for n in names]


@pytest.mark.asyncio
async def test_coverage_gap_quiet_when_both_scrapers_fresh(store: Store, monkeypatch):
    """Both scrapers' last-success within 10 min → no alert, no log."""
    mon = CoverageMonitor()
    hot_game = await _make_game(store, external_id="nba_hot", seconds_to_tipoff=600)
    games = [hot_game]
    scrapers = _scrapers("pinnacle", "tipsport")
    now = datetime.now(timezone.utc)
    mon._last_game_coverage_success = {
        (hot_game.id, "pinnacle"): now,
        (hot_game.id, "tipsport"): now,
    }
    sent = _capture_telegram(monkeypatch)

    await mon.check_gaps(games, scrapers, now)

    assert sent == []


@pytest.mark.asyncio
async def test_coverage_gap_alert_when_pinnacle_stale(store: Store, monkeypatch):
    """Pinnacle last-success >10 min old while a game is in hot tier → alert fires."""
    mon = CoverageMonitor()
    hot_game = await _make_game(store, external_id="nba_hot", seconds_to_tipoff=600)
    games = [hot_game]
    scrapers = _scrapers("pinnacle", "tipsport")
    now = datetime.now(timezone.utc)
    mon._last_game_coverage_success = {
        (hot_game.id, "pinnacle"): now - timedelta(minutes=12),
        (hot_game.id, "tipsport"): now,
    }
    sent = _capture_telegram(monkeypatch)

    await mon.check_gaps(games, scrapers, now)

    assert len(sent) == 1
    msg = sent[0]
    assert "scraper=pinnacle" in msg.lower()
    assert f"game_id={hot_game.id}" in msg


class _ThrottledGov:
    """Budget-governor stand-in returning a fixed (throttled) interval."""

    def __init__(self, interval: int) -> None:
        self._interval = interval

    def interval_for_tier(self, tier: str, now) -> int:
        return self._interval


@pytest.mark.asyncio
async def test_coverage_threshold_scales_with_budget_throttle(store: Store, monkeypatch):
    """Under budget throttle, warm cadence stretches to 900-1800s. A fixed 600s
    staleness bar would false-alert every cycle on a HEALTHY pipeline simply not
    due to poll yet — so the threshold must scale with the throttled interval."""
    warm_game = await _make_game(store, external_id="nba_warm_thr", seconds_to_tipoff=90 * 60)
    games = [warm_game]
    scrapers = _scrapers("pinnacle")
    now = datetime.now(timezone.utc)

    # 12 min stale would trip the fixed 600s bar, but 2.5 * 900 = 2250s tolerance.
    mon = CoverageMonitor(governor=_ThrottledGov(900))
    mon._last_game_coverage_success = {
        (warm_game.id, "pinnacle"): now - timedelta(minutes=12),
    }
    sent = _capture_telegram(monkeypatch)
    await mon.check_gaps(games, scrapers, now)
    assert sent == []  # not stale relative to the throttled cadence

    # ...but a genuinely stale gap (40 min > 2250s) still alerts.
    mon2 = CoverageMonitor(governor=_ThrottledGov(900))
    mon2._last_game_coverage_success = {
        (warm_game.id, "pinnacle"): now - timedelta(minutes=40),
    }
    sent2 = _capture_telegram(monkeypatch)
    await mon2.check_gaps(games, scrapers, now)
    assert len(sent2) == 1


@pytest.mark.asyncio
async def test_coverage_gap_alert_when_scraper_never_succeeded(store: Store, monkeypatch):
    """Hot tier with no matched coverage yet must alert, not silently continue."""
    mon = CoverageMonitor()
    hot_game = await _make_game(store, external_id="nba_hot", seconds_to_tipoff=600)
    games = [hot_game]
    scrapers = _scrapers("pinnacle", "tipsport")
    now = datetime.now(timezone.utc)
    sent = _capture_telegram(monkeypatch)

    await mon.check_gaps(games, scrapers, now)

    assert len(sent) == 2
    assert all("minutes_stale=None" in msg for msg in sent)
    assert any("scraper=pinnacle" in msg.lower() for msg in sent)
    assert any("scraper=tipsport" in msg.lower() for msg in sent)


@pytest.mark.asyncio
async def test_coverage_gap_includes_extra_stub_scraper(store: Store, monkeypatch):
    """Coverage-gap tracking follows every enabled scraper in the scheduler list."""
    mon = CoverageMonitor()
    hot_game = await _make_game(store, external_id="nba_hot_stub", seconds_to_tipoff=600)
    games = [hot_game]
    scrapers = _scrapers("pinnacle", "tipsport", "stubbook")
    now = datetime.now(timezone.utc)
    sent = _capture_telegram(monkeypatch)

    await mon.check_gaps(games, scrapers, now)

    assert len(sent) == 3
    assert any("stubbook" in msg.lower() for msg in sent)


@pytest.mark.asyncio
async def test_coverage_gap_alert_deduped_on_repeat_check(store: Store, monkeypatch):
    """Second consecutive check with the same gap must not fire a new alert."""
    mon = CoverageMonitor()
    hot_game = await _make_game(store, external_id="nba_hot", seconds_to_tipoff=600)
    games = [hot_game]
    scrapers = _scrapers("pinnacle", "tipsport")
    now = datetime.now(timezone.utc)
    mon._last_game_coverage_success = {
        (hot_game.id, "pinnacle"): now - timedelta(minutes=12),
        (hot_game.id, "tipsport"): now,
    }
    sent = _capture_telegram(monkeypatch)

    await mon.check_gaps(games, scrapers, now)
    # Reset throttle so the second call would run the check (dedup is what blocks re-alert).
    mon._last_coverage_check = None
    await mon.check_gaps(games, scrapers, now)

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_coverage_gap_alerts_warm_but_ignores_far_and_final(
    store: Store, monkeypatch
):
    """Warm-tier edge detection is active, so stale scraper coverage should
    alert there too. Far and final games still don't alert."""
    mon = CoverageMonitor()
    scrapers = _scrapers("pinnacle", "tipsport")
    warm = Game(
        external_id="nba_warm", home_team="Miami Heat", away_team="Toronto Raptors",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1, minutes=30),  # 90min -> warm
        status="scheduled",
    )
    warm.id = await store.upsert_game(warm)
    far = Game(
        external_id="nba_far", home_team="Denver Nuggets", away_team="Phoenix Suns",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=3),
        status="scheduled",
    )
    far.id = await store.upsert_game(far)
    finalgame = Game(
        external_id="nba_final", home_team="Chicago Bulls", away_team="New York Knicks",
        scheduled_at=datetime.now(timezone.utc) - timedelta(hours=1),
        status="final",
    )
    finalgame.id = await store.upsert_game(finalgame)
    games = [warm, far, finalgame]
    now = datetime.now(timezone.utc)
    mon._last_game_coverage_success = {
        (warm.id, "pinnacle"): datetime.now(timezone.utc) - timedelta(minutes=30),
        (warm.id, "tipsport"): datetime.now(timezone.utc) - timedelta(minutes=30),
    }
    sent = _capture_telegram(monkeypatch)

    await mon.check_gaps(games, scrapers, now)

    assert len(sent) == 2
    assert all(f"game_id={warm.id}" in msg for msg in sent)
    assert all(f"game_id={far.id}" not in msg for msg in sent)
    assert all(f"game_id={finalgame.id}" not in msg for msg in sent)


@pytest.mark.asyncio
async def test_coverage_gap_quiet_on_empty_slate(store: Store, monkeypatch):
    """No games at all → check returns without alerting, even if scrapers are stale."""
    mon = CoverageMonitor()
    scrapers = _scrapers("pinnacle", "tipsport")
    games = []
    now = datetime.now(timezone.utc)
    mon._last_game_coverage_success = {
        (999, "pinnacle"): datetime.now(timezone.utc) - timedelta(hours=1),
        (999, "tipsport"): datetime.now(timezone.utc) - timedelta(hours=1),
    }
    sent = _capture_telegram(monkeypatch)

    await mon.check_gaps(games, scrapers, now)

    assert sent == []


@pytest.mark.asyncio
async def test_match_timestamps_pruned_to_slate_scoreboard_keeps_last_seen():
    """_last_game_coverage_success must not grow unbounded over a 24/7 process:
    check_gaps prunes entries for games gone from the slate, while the
    dashboard's per-scraper "last seen" survives the prune."""
    mon = CoverageMonitor()
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    mon.record_match(1, "tipsport", now - timedelta(minutes=3))
    mon.record_match(99, "tipsport", now - timedelta(minutes=5))  # game left the slate

    slate_game = Game(external_id="cov_slate", home_team="A", away_team="B",
                      scheduled_at=now + timedelta(hours=5), status="scheduled")
    slate_game.id = 1
    await mon.check_gaps([slate_game], [], now)

    assert (99, "tipsport") not in mon._last_game_coverage_success
    assert (1, "tipsport") in mon._last_game_coverage_success
    assert mon.last_coverage("tipsport") != "never"
