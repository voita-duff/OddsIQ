import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from oddsiq.db import migrations
from oddsiq.db.models import Game, OddsSnapshot
from oddsiq.db.store import Store
from oddsiq.scrapers.pinnacle import PinnacleScraper
from oddsiq.scrapers.tipsport import TipsportScraper
from oddsiq.pipeline.poll_coordinator import PollCoordinator
from oddsiq.pipeline.edge_service import EdgeService
from oddsiq.pipeline.coverage_monitor import CoverageMonitor
from oddsiq.pipeline.stats import SessionStats
from oddsiq.sports.profiles import NBA, MLB, NFL


def _make_coordinator(store):
    stats = SessionStats()
    coverage = CoverageMonitor()
    scrapers = [PinnacleScraper(), TipsportScraper()]
    return PollCoordinator(
        scrapers=scrapers,
        store=store,
        stats=stats,
        edge_service=EdgeService(store=store, stats=stats),
        coverage=coverage,
    )


def _stub_scrapers(coord, pin_returns=None, tip_returns=None):
    pin = next(s for s in coord._scrapers if s.name == "pinnacle")
    tip = next(s for s in coord._scrapers if s.name == "tipsport")
    pin.fetch_odds = AsyncMock(return_value=pin_returns or [])
    tip.fetch_odds = AsyncMock(return_value=tip_returns or [])
    return pin, tip


@pytest.fixture(autouse=True)
def _silence_real_telegram(monkeypatch):
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", AsyncMock())


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


def _pin_snap(
    home: str, away: str, commence_time: datetime | None = None
) -> OddsSnapshot:
    """Build a single Pinnacle moneyline snapshot matching the given team names."""
    return OddsSnapshot(
        game_id=0,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        _home_team=home,
        _away_team=away,
        _commence_time=commence_time,
    )


def _pin_pair(
    home: str,
    away: str,
    *,
    home_odds: float = 1.70,
    away_odds: float = 2.20,
) -> list[OddsSnapshot]:
    return [
        OddsSnapshot(
            game_id=0,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="home",
            odds=home_odds,
            _home_team=home,
            _away_team=away,
        ),
        OddsSnapshot(
            game_id=0,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="away",
            odds=away_odds,
            _home_team=home,
            _away_team=away,
        ),
    ]


_AUTO_START = object()


def _book_snap(
    home: str,
    away: str,
    bookmaker: str = "tipsport",
    commence_time: datetime | None | object = _AUTO_START,
) -> OddsSnapshot:
    # 1.85 vs the _pin_pair fair (~1.77) is a ~4.3% edge: above EDGE_THRESHOLD
    # (3%) but below PREGAME_MAX_PLAUSIBLE_EDGE (8%), so these fixtures flag a
    # NORMAL value bet rather than a suspect-stamped stale-price artifact.
    if commence_time is _AUTO_START:
        commence_time = (
            datetime.now(timezone.utc) + timedelta(hours=2)
            if bookmaker == "tipsport"
            else None
        )
    return OddsSnapshot(
        game_id=0,
        bookmaker=bookmaker,
        market_type="moneyline",
        selection="home",
        odds=1.85,
        _home_team=home,
        _away_team=away,
        _commence_time=commence_time,
    )


def _tip_snap(
    home: str,
    away: str,
    commence_time: datetime | None | object = _AUTO_START,
) -> OddsSnapshot:
    return _book_snap(home, away, "tipsport", commence_time)


def _capture_telegram(monkeypatch) -> list[str]:
    """Capture coverage-gap WARNING logs. The 2026-08-10 rebrand replaced the
    coverage-gap Telegram alert with a ``Coverage gap: game_id=... scraper=...``
    log line; these tests assert the gap is *detected* (one entry per gap)."""
    sent: list[str] = []

    def fake_warning(msg, *args, **kwargs):
        text = (msg % args) if args else msg
        if str(text).startswith("Coverage gap:"):
            sent.append(text)

    monkeypatch.setattr("oddsiq.pipeline.coverage_monitor.logger.warning", fake_warning)
    return sent


# ---------------------------------------------------------------------------
# Post-tipoff purity (the live tier was removed 2026-08-27)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_tipoff_game_is_skipped_entirely(store: Store):
    """A game that crossed tipoff (e.g. mid-cycle race between the pollable
    computation and the poll) must produce NO Pinnacle fetch, NO stored
    snapshots, and NO value bets — post-tipoff games are unpollable."""
    coord = _make_coordinator(store)
    game = await _make_game(store, external_id="mlb_post_tip", seconds_to_tipoff=-600)
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=_pin_pair(game.home_team, game.away_team),
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    await coord.poll(MLB, [game], [game], datetime.now(timezone.utc))

    assert pin.fetch_odds.await_count == 0, (
        "post-tipoff-only window must not fetch Pinnacle"
    )
    cur = await store._db.execute(
        "SELECT COUNT(*) AS n FROM odds_snapshots WHERE game_id=?", (game.id,)
    )
    assert (await cur.fetchone())["n"] == 0, "post-tipoff games take no snapshots"
    assert await store.get_pending_value_bets(game.id) == []


@pytest.mark.asyncio
async def test_hot_game_polls_even_beside_post_tipoff_game(store: Store):
    """A hot pregame game alongside a tipped-off game must still fetch Pinnacle
    and detect normally; the tipped-off sibling contributes and receives nothing."""
    coord = _make_coordinator(store)
    hot = await _make_game(store, external_id="nba_hot_pre", seconds_to_tipoff=600)
    live = Game(
        external_id="nba_live_co",
        home_team="Miami Heat",
        away_team="Toronto Raptors",
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        status="scheduled",
    )
    live.id = await store.upsert_game(live)
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=_pin_pair(hot.home_team, hot.away_team),
        tip_returns=[_tip_snap(hot.home_team, hot.away_team)],
    )

    await coord.poll(NBA, [hot, live], [hot, live], datetime.now(timezone.utc))

    assert pin.fetch_odds.await_count == 1, (
        "a hot pregame game must still trigger Pinnacle beside a tipped-off game"
    )


# ---------------------------------------------------------------------------
# Polling-tier Pinnacle budget behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_far_tier_skips_pinnacle_fetch(store: Store):
    """Game >2h from tipoff: Pinnacle fetch is skipped, Tipsport runs normally."""
    coord = _make_coordinator(store)
    far_game = await _make_game(
        store, external_id="nba_far", seconds_to_tipoff=3 * 3600
    )
    slate = [far_game]
    pin, tip = _stub_scrapers(coord)

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [far_game], slate, now)

    assert pin.fetch_odds.await_count == 0
    assert tip.fetch_odds.await_count == 1


@pytest.mark.asyncio
async def test_warm_tier_pinnacle_fetched_every_cycle(store: Store):
    """Warm tier (1-2h) fires Pinnacle every poll, not just once per game.
    Tipsport cadence is unchanged. The single Odds API key plus the Budget
    Governor's cadence throttling keeps this affordable."""
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_warm", seconds_to_tipoff=5400
    )  # 90min out (warm)
    slate = [game]
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=[_pin_snap(game.home_team, game.away_team)],
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [game], slate, now)
    await coord.poll(NBA, [game], slate, now)
    await coord.poll(NBA, [game], slate, now)

    assert pin.fetch_odds.await_count == 3, "Pinnacle now fires every warm-tier cycle"
    assert tip.fetch_odds.await_count == 3


@pytest.mark.asyncio
async def test_warm_tier_pinnacle_still_fires_on_empty_response(store: Store):
    """An empty Pinnacle response (off-hours, league break) doesn't suppress
    the next warm-tier Pinnacle call. There's no "opened" gate anymore;
    every cycle gets its shot at fresh data."""
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_warm_empty", seconds_to_tipoff=5400
    )  # 90min out (warm)
    slate = [game]
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=[],
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [game], slate, now)
    await coord.poll(NBA, [game], slate, now)

    assert pin.fetch_odds.await_count == 2
    assert tip.fetch_odds.await_count == 2


@pytest.mark.asyncio
async def test_hot_tier_pinnacle_fetched_every_poll(store: Store):
    """Hot tier (<60m): every poll hits Pinnacle. Cadence is untouched."""
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_hot", seconds_to_tipoff=600
    )  # 10m out (hot)
    slate = [game]
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=[_pin_snap(game.home_team, game.away_team)],
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    for _ in range(3):
        await coord.poll(NBA, [game], slate, now)

    assert pin.fetch_odds.await_count == 3
    assert tip.fetch_odds.await_count == 3


@pytest.mark.asyncio
async def test_poll_updates_commence_time_once_per_game(store: Store):
    """Multiple Pinnacle snapshots from one event should cause one DB timing update."""
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_commence_once", seconds_to_tipoff=600
    )
    slate = [game]
    commence = datetime.now(timezone.utc) + timedelta(minutes=40)
    pin_returns = [
        _pin_snap(game.home_team, game.away_team, commence),
        OddsSnapshot(
            game_id=0,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="away",
            odds=2.05,
            _home_team=game.home_team,
            _away_team=game.away_team,
            _commence_time=commence,
        ),
    ]
    _stub_scrapers(
        coord,
        pin_returns=pin_returns,
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )
    wrapped_update = AsyncMock(wraps=store.update_game_commence_time)
    store.update_game_commence_time = wrapped_update

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [game], slate, now)

    assert wrapped_update.await_count == 1
    assert game.commence_time == commence
    fetched = await store.get_games_by_date(game.scheduled_at)
    assert fetched[0].commence_time == commence


@pytest.mark.asyncio
async def test_poll_does_not_update_commence_time_when_pinnacle_skipped(store: Store):
    """Tipsport-only cycles must never write a NULL or inferred commence_time."""
    coord = _make_coordinator(store)
    far_game = await _make_game(
        store, external_id="nba_far_commence", seconds_to_tipoff=3 * 3600
    )
    slate = [far_game]
    _stub_scrapers(
        coord,
        pin_returns=[],
        tip_returns=[_tip_snap(far_game.home_team, far_game.away_team)],
    )
    wrapped_update = AsyncMock(wraps=store.update_game_commence_time)
    store.update_game_commence_time = wrapped_update

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [far_game], slate, now)

    assert wrapped_update.await_count == 0
    assert far_game.commence_time is None


@pytest.mark.asyncio
async def test_commence_time_reclassifies_hot_game_back_to_warm(store: Store):
    """A later Pinnacle commence_time should affect the next poll cycle's tier.

    With warm-tier Pinnacle polling enabled, both hot- and warm-tier polls
    fire Pinnacle every cycle, so reclassification doesn't suppress fetches —
    we just verify Pinnacle keeps firing across the tier change.
    """
    coord = _make_coordinator(store)
    game = await _make_game(store, external_id="nba_reclass", seconds_to_tipoff=600)
    slate = [game]
    commence = datetime.now(timezone.utc) + timedelta(
        hours=1.5
    )  # 90min out -> warm tier
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=[_pin_snap(game.home_team, game.away_team, commence)],
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [game], slate, now)
    assert game.commence_time == commence
    assert pin.fetch_odds.await_count == 1

    await coord.poll(NBA, [game], slate, now)
    assert pin.fetch_odds.await_count == 2

    await coord.poll(NBA, [game], slate, now)
    assert pin.fetch_odds.await_count == 3  # warm-tier Pinnacle every cycle
    assert tip.fetch_odds.await_count == 3


@pytest.mark.asyncio
async def test_far_game_does_not_store_pinnacle_snapshots_when_fetched_for_hot_game(
    store: Store,
):
    """Pinnacle call covers every game in one response; rows must only be stored
    for tiers that need them. A far-tier game gets nothing, even when Pinnacle
    ran for a hot-tier game alongside it."""
    coord = _make_coordinator(store)
    hot_game = await _make_game(store, external_id="nba_hot", seconds_to_tipoff=600)
    far_game = Game(
        external_id="nba_far",
        home_team="Miami Heat",
        away_team="Toronto Raptors",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=3),
        status="scheduled",
    )
    far_game.id = await store.upsert_game(far_game)
    slate = [hot_game, far_game]

    pin_snapshots = [
        _pin_snap(hot_game.home_team, hot_game.away_team),
        _pin_snap(far_game.home_team, far_game.away_team),
    ]
    tip_snapshots = [
        _tip_snap(hot_game.home_team, hot_game.away_team),
        _tip_snap(far_game.home_team, far_game.away_team),
    ]
    _stub_scrapers(coord, pin_returns=pin_snapshots, tip_returns=tip_snapshots)

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [hot_game, far_game], slate, now)

    hot_pin = await store.get_latest_odds(hot_game.id, "pinnacle", "moneyline", "home")
    far_pin = await store.get_latest_odds(far_game.id, "pinnacle", "moneyline", "home")
    far_tip = await store.get_latest_odds(far_game.id, "tipsport", "moneyline", "home")
    assert hot_pin is not None
    assert far_pin is None  # no Pinnacle row for the far-tier game
    assert far_tip is not None  # Tipsport keeps polling


@pytest.mark.asyncio
async def test_doubleheader_does_not_corrupt_commence_time(store: Store):
    """Two slate games sharing a team pair (MLB doubleheader / series wrap around
    the UTC-day boundary) are distinct events. When Pinnacle's full-slate response
    carries both, each game must keep ONLY the event whose tipoff matches it —
    game1's commence_time must not be overwritten by the other leg's tipoff."""
    coord = _make_coordinator(store)
    now = datetime.now(timezone.utc)
    home, away = "Houston Astros", "Athletics"
    game1 = Game(
        external_id="mlb_dh_1",
        home_team=home,
        away_team=away,
        scheduled_at=now + timedelta(minutes=10),
        status="scheduled",
    )  # hot
    game1.id = await store.upsert_game(game1)
    game2 = Game(
        external_id="mlb_dh_2",
        home_team=home,
        away_team=away,
        scheduled_at=now + timedelta(hours=3),
        status="scheduled",
    )  # far
    game2.id = await store.upsert_game(game2)
    slate = [game1, game2]

    c1 = now + timedelta(minutes=10)
    c2 = now + timedelta(hours=3)

    def _pair_at(commence):
        return [
            OddsSnapshot(
                game_id=0,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.70,
                _home_team=home,
                _away_team=away,
                _commence_time=commence,
            ),
            OddsSnapshot(
                game_id=0,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="away",
                odds=2.20,
                _home_team=home,
                _away_team=away,
                _commence_time=commence,
            ),
        ]

    _stub_scrapers(
        coord,
        pin_returns=_pair_at(c1) + _pair_at(c2),
        tip_returns=[_tip_snap(home, away)],
    )

    await coord.poll(NBA, [game1, game2], slate, now)

    # game1 (hot) must reconcile to its OWN event's tipoff, not the 3h-away leg.
    assert game1.commence_time is not None
    assert abs((game1.commence_time - c1).total_seconds()) < 60
    assert abs((game1.commence_time - c2).total_seconds()) > 3600


@pytest.mark.asyncio
async def test_start_less_tipsport_snapshot_is_dropped_not_attributed(
    store: Store, caplog
):
    """Fail-closed start-time matching, on the hardest shape: two pollable games
    sharing a team pair (MLB doubleheader) and a Tipsport listing whose own
    start didn't parse. That snapshot is attributed to NEITHER leg — it is not
    stored against either game, it produces no detection, and it is counted
    once on the tipsport `Scraper health:` line as `unsafe_start_snapshots`.
    This is what makes the old shared-pair skip guard unnecessary: the snapshot
    never reaches a game, so there is nothing left to disambiguate."""
    coord = _make_coordinator(store)
    now = datetime.now(timezone.utc)
    home, away = "Houston Astros", "Athletics"
    g1 = Game(
        external_id="dh1",
        home_team=home,
        away_team=away,
        scheduled_at=now + timedelta(minutes=10),
        status="scheduled",
    )  # hot
    g1.id = await store.upsert_game(g1)
    g2 = Game(
        external_id="dh2",
        home_team=home,
        away_team=away,
        scheduled_at=now + timedelta(minutes=40),
        status="scheduled",
    )  # hot
    g2.id = await store.upsert_game(g2)
    slate = [g1, g2]
    # A juicy Tipsport price + Pinnacle pair that WOULD flag an edge if the
    # start-less listing were attributed to either leg.
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(home, away, home_odds=1.70, away_odds=2.20),
        tip_returns=[_tip_snap(home, away, commence_time=None)],
    )

    with caplog.at_level(logging.INFO, logger="oddsiq.pipeline.poll_coordinator"):
        caplog.clear()
        await coord.poll(NBA, [g1, g2], slate, now)

    # Attached to neither leg: no tipsport row was stored at all.
    cur = await store._db.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE bookmaker='tipsport'"
    )
    (stored,) = await cur.fetchone()
    assert stored == 0
    # ...so no value bet can be flagged off it, for either leg.
    cur = await store._db.execute("SELECT COUNT(*) FROM value_bets")
    (n,) = await cur.fetchone()
    assert n == 0
    # The drop is visible in telemetry, counted exactly once, and warned about.
    messages = [r.getMessage() for r in caplog.records]
    tip_health = [m for m in messages if "Scraper health" in m and "name=tipsport" in m]
    assert tip_health, "missing tipsport health line"
    assert "unsafe_start_snapshots=1" in tip_health[0]
    assert "matched_snapshots=0" in tip_health[0]
    assert any("snapshots without listing start" in m for m in messages)


@pytest.mark.asyncio
async def test_health_log_does_not_count_far_tier_pinnacle_as_unmatched(
    store: Store, caplog
):
    """A Pinnacle snapshot for a far-tier pollable game is deliberately not stored
    (no CLV baseline yet). It must be reported as an on-slate 'other' game, not
    inflate the pinnacle unmatched_snapshots health metric."""
    coord = _make_coordinator(store)
    hot_game = await _make_game(store, external_id="nba_hot_h", seconds_to_tipoff=600)
    far_game = Game(
        external_id="nba_far_h",
        home_team="Miami Heat",
        away_team="Toronto Raptors",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=3),
        status="scheduled",
    )
    far_game.id = await store.upsert_game(far_game)
    slate = [hot_game, far_game]
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(hot_game.home_team, hot_game.away_team)
        + _pin_pair(far_game.home_team, far_game.away_team),
        tip_returns=[
            _tip_snap(hot_game.home_team, hot_game.away_team),
            _tip_snap(far_game.home_team, far_game.away_team),
        ],
    )
    now = datetime.now(timezone.utc)
    with caplog.at_level(logging.INFO, logger="oddsiq.pipeline.poll_coordinator"):
        await coord.poll(NBA, [hot_game, far_game], slate, now)

    pin_health = [
        r.getMessage()
        for r in caplog.records
        if "Scraper health" in r.getMessage() and "name=pinnacle" in r.getMessage()
    ]
    assert pin_health, "missing pinnacle health line"
    assert "unmatched_snapshots=0" in pin_health[0]
    assert "other_games=1" in pin_health[0]  # the far-tier game


@pytest.mark.asyncio
async def test_warm_tier_pinnacle_snapshots_dedupe_when_odds_unchanged(store: Store):
    """Warm tier fires Pinnacle every cycle. When the odds don't move
    between polls, the store's batch dedup keeps the row count at 1 — so the
    quota cost goes up but disk usage stays flat unless lines actually move.
    """
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_warm", seconds_to_tipoff=5400
    )  # 90min out (warm)
    slate = [game]
    _stub_scrapers(
        coord,
        pin_returns=[_pin_snap(game.home_team, game.away_team)],
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    for _ in range(5):
        await coord.poll(NBA, [game], slate, now)

    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE game_id=? AND bookmaker=?",
        (game.id, "pinnacle"),
    )
    count = (await cursor.fetchone())[0]
    assert count == 1, "identical Pinnacle odds across 5 polls should dedupe to 1 row"


@pytest.mark.asyncio
async def test_warm_tier_fires_edge_detection(store: Store):
    """A +EV soft-book line 90min before tipoff must flag a value bet.

    Edge detection runs in warm tier (1-2h) as well as hot tier. Real-money
    operation needs earlier detection so the operator can place before the
    line moves — this test guards that behavior in evaluation mode too.
    """
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_warm_edge", seconds_to_tipoff=5400
    )  # 90min out (warm)
    slate = [game]
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(game.home_team, game.away_team),
        tip_returns=[_tip_snap(game.home_team, game.away_team)],  # Tipsport home @ 2.05
    )

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [game], slate, now)

    cursor = await store._db.execute(
        "SELECT bookmaker, market_type, selection, entry_odds FROM value_bets WHERE game_id=?",
        (game.id,),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1, (
        f"expected one warm-tier value bet, got: {[dict(r) for r in rows]}"
    )
    assert rows[0]["bookmaker"] == "tipsport"
    assert rows[0]["selection"] == "home"
    assert rows[0]["entry_odds"] == 1.85


@pytest.mark.asyncio
async def test_warm_and_hot_tier_observations_coexist(store: Store):
    """Same selection detected in warm then hot must produce TWO value_bets,
    one per tier. Without `tier` in the unique key, the second insert would
    ON CONFLICT DO NOTHING and the better hot-tier edge would be silently
    dropped — locking CLV to the warm-tier entry price."""
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_both_tiers", seconds_to_tipoff=5400
    )  # 90min out (warm)
    slate = [game]
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=_pin_pair(game.home_team, game.away_team),
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    # First poll — warm tier.
    await coord.poll(NBA, [game], slate, now)

    # Shift tipoff inward so the SAME game is now in hot tier on the next poll.
    # Re-issue the same Tipsport odds; the unique key + tier should allow a
    # second row at tier='hot'.
    game.scheduled_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await store.update_game_commence_time(
        game.id,
        datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    game.commence_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    now2 = datetime.now(timezone.utc)
    await coord.poll(NBA, [game], slate, now2)

    cursor = await store._db.execute(
        "SELECT tier, COUNT(*) AS n FROM value_bets WHERE game_id=? GROUP BY tier ORDER BY tier",
        (game.id,),
    )
    rows = await cursor.fetchall()
    by_tier = {row["tier"]: row["n"] for row in rows}
    assert by_tier == {"hot": 1, "warm": 1}, f"expected one row per tier; got {by_tier}"


@pytest.mark.asyncio
async def test_repeat_warm_tier_polls_dedupe_value_bet(store: Store):
    """Two warm-tier polls with the same odds for the same selection must
    still dedupe — tier alone shouldn't unlock duplicates within the same
    tier. The point of `tier` in the unique key is cross-tier coexistence,
    not turning off intra-tier dedup."""
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_warm_repeat", seconds_to_tipoff=5400
    )  # 90min out (warm)
    slate = [game]
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(game.home_team, game.away_team),
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [game], slate, now)
    await coord.poll(NBA, [game], slate, now)

    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM value_bets WHERE game_id=? AND tier='warm'",
        (game.id,),
    )
    count = (await cursor.fetchone())[0]
    assert count == 1, "two warm-tier polls of the same selection should dedupe"


@pytest.mark.asyncio
async def test_far_tier_still_skips_edge_detection(store: Store):
    """Far tier (>2h) must remain detection-skipped even after the medium-tier
    expansion. Far lines haven't been sharpened enough to compare against."""
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_far_edge", seconds_to_tipoff=4 * 3600
    )
    slate = [game]
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(game.home_team, game.away_team),
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [game], slate, now)

    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM value_bets WHERE game_id=?",
        (game.id,),
    )
    count = (await cursor.fetchone())[0]
    assert count == 0, "far-tier games must not trigger edge detection"


@pytest.mark.asyncio
async def test_multi_bookmaker_edges_store_independent_value_bets(store: Store):
    """Same game/selection can produce separate value bets per soft bookmaker.

    Uses a second stub scraper named 'softbook2' to prove that two soft books
    can independently generate value_bets for the same game/selection.
    """
    coord = _make_coordinator(store)
    game = await _make_game(store, external_id="nba_multi_book", seconds_to_tipoff=600)
    slate = [game]
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=_pin_pair(game.home_team, game.away_team),
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )
    softbook2 = SimpleNamespace(
        name="softbook2",
        fetch_odds=AsyncMock(
            return_value=[_book_snap(game.home_team, game.away_team, "softbook2")]
        ),
    )
    coord._scrapers = [pin, tip, softbook2]

    now = datetime.now(timezone.utc)
    await coord.poll(NBA, [game], slate, now)

    cursor = await store._db.execute(
        "SELECT bookmaker, COUNT(*) AS n FROM value_bets GROUP BY bookmaker"
    )
    rows = await cursor.fetchall()
    assert {row["bookmaker"]: row["n"] for row in rows} == {
        "tipsport": 1,
        "softbook2": 1,
    }


@pytest.mark.asyncio
async def test_per_scraper_health_log_fires_with_expected_fields(store: Store, caplog):
    """Each poll cycle must emit one 'Scraper health' INFO log per scraper with
    name/duration_ms/snapshots/matched_snapshots/matched_games/
    other_games/unmatched_snapshots/errors fields.

    The soak playbook relies on this log for the Phase 2 enable confirmation
    and the Phase 3 watch — losing it silently would blind the operator.
    """
    coord = _make_coordinator(store)
    game = await _make_game(store, external_id="nba_health_log", seconds_to_tipoff=600)
    slate = [game]
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=_pin_pair(game.home_team, game.away_team),
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    with caplog.at_level(logging.INFO, logger="oddsiq.pipeline.poll_coordinator"):
        await coord.poll(NBA, [game], slate, now)

    health_lines = [r for r in caplog.records if "Scraper health" in r.getMessage()]
    by_name: dict[str, str] = {}
    for record in health_lines:
        msg = record.getMessage()
        # Format includes fetch volume, active/pollable matches, other scheduled
        # matches, and truly unmatched snapshots.
        for token in msg.split():
            if token.startswith("name="):
                by_name[token.split("=", 1)[1]] = msg
                break

    assert "pinnacle" in by_name, (
        f"missing pinnacle health line in {[r.getMessage() for r in health_lines]}"
    )
    assert "tipsport" in by_name
    for msg in by_name.values():
        for field in (
            "duration_ms=",
            "snapshots=",
            "matched_snapshots=",
            "matched_games=",
            "other_games=",
            "unmatched_snapshots=",
            "errors=",
            "sharp_source_age_max_ms=",
            "missing_source_updates=",
            "missing_event_ids=",
            "event_id_mismatches=",
        ):
            assert field in msg, f"field {field} missing from health log: {msg}"


@pytest.mark.asyncio
async def test_poll_fetches_scrapers_concurrently(store: Store):
    """A slow scraper must not block the next scraper from starting its fetch.

    This would time out with the old sequential loop because the first fetch
    waits until both scraper fetches have started.
    """
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_concurrent_poll", seconds_to_tipoff=600
    )
    slate = [game]

    both_started = asyncio.Event()
    started: list[str] = []

    def make_scraper(
        name: str,
        returns: list[OddsSnapshot],
    ) -> SimpleNamespace:
        async def fetch_odds(_game_date, _profile):
            started.append(name)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            return returns

        return SimpleNamespace(name=name, fetch_odds=fetch_odds)

    coord._scrapers = [
        make_scraper("pinnacle", _pin_pair(game.home_team, game.away_team)),
        make_scraper("tipsport", [_tip_snap(game.home_team, game.away_team)]),
    ]

    now = datetime.now(timezone.utc)
    await asyncio.wait_for(coord.poll(NBA, [game], slate, now), timeout=1.0)

    assert set(started) == {"pinnacle", "tipsport"}


@pytest.mark.asyncio
async def test_health_log_surfaces_scraper_internal_error_count(store: Store, caplog):
    """fetch_odds is non-raising on internal failure — scrapers swallow the
    exception and return []. The scheduler's outer try/except therefore
    can't see those errors; it must read ``scraper.last_fetch_error_count``
    so the per-poll health log doesn't lie with errors=0 when something
    actually failed inside the scraper."""
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="nba_health_internal", seconds_to_tipoff=600
    )
    slate = [game]
    pin, tip = _stub_scrapers(
        coord,
        pin_returns=_pin_pair(game.home_team, game.away_team),
        tip_returns=[],  # tipsport returned no snapshots
    )
    # Mark tipsport as having silently swallowed 2 internal errors during
    # the fetch (e.g. 2 of 3 markets failed but the third recovered).
    tip.last_fetch_error_count = 2

    now = datetime.now(timezone.utc)
    with caplog.at_level(logging.INFO, logger="oddsiq.pipeline.poll_coordinator"):
        await coord.poll(NBA, [game], slate, now)

    tip_health = [
        r.getMessage()
        for r in caplog.records
        if "Scraper health" in r.getMessage() and "name=tipsport" in r.getMessage()
    ]
    assert tip_health, (
        f"missing tipsport health line in {[r.getMessage() for r in caplog.records]}"
    )
    assert "errors=2" in tip_health[0], (
        f"expected errors=2 from internal count, got: {tip_health[0]}"
    )


@pytest.mark.asyncio
async def test_empty_scraper_warning_includes_status_and_updates_summary_stats(
    store: Store, caplog
):
    coord = _make_coordinator(store)
    game = await _make_game(store, external_id="nba_status_log", seconds_to_tipoff=600)
    slate = [game]
    stub_scraper = SimpleNamespace(
        name="stubbook",
        fetch_odds=AsyncMock(return_value=[]),
        last_fetch_error_count=1,
        last_fetch_status_code=403,
    )
    coord._scrapers = [stub_scraper]

    now = datetime.now(timezone.utc)
    with caplog.at_level(logging.WARNING, logger="oddsiq.pipeline.poll_coordinator"):
        await coord.poll(NBA, [game], slate, now)

    assert any(
        "Scraper stubbook returned no odds (status=403)" in r.getMessage()
        for r in caplog.records
    )
    assert coord._stats.scraper_polls["stubbook"] == 1
    assert coord._stats.scraper_zero_odds["stubbook"] == 1
    assert coord._stats.scraper_errors["stubbook"] == 1


@pytest.mark.asyncio
async def test_value_bet_emits_structured_log(store: Store, caplog):
    """When a new value bet is inserted, a structured value_bet: log line must be emitted.

    Mirrors the setup of test_multi_bookmaker_edges_store_independent_value_bets,
    narrowed to one game and one soft book (tipsport), and asserts on the log event
    the dashboard activity feed parses.
    """
    coord = _make_coordinator(store)
    game = await _make_game(store, external_id="nba_vbet_log", seconds_to_tipoff=600)
    slate = [game]
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(game.home_team, game.away_team),
        tip_returns=[_tip_snap(game.home_team, game.away_team)],
    )

    now = datetime.now(timezone.utc)
    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.edge_service")
    await coord.poll(NBA, [game], slate, now)

    structured = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("value_bet:")
    ]
    assert len(structured) == 1, (
        f"expected 1 value_bet: log line, got {len(structured)}: {structured}"
    )
    line = structured[0]
    for key in (
        "bookmaker=",
        "game_id=",
        "market=",
        "selection=",
        "line=",
        "edge_pct=",
        "entry_odds=",
        "fair_odds=",
        "tier=",
    ):
        assert key in line, f"missing key {key!r} in: {line}"


@pytest.mark.asyncio
async def test_future_game_odds_do_not_clear_active_game_coverage_gap(
    store: Store, caplog, monkeypatch
):
    """A scraper can be fetch-healthy while missing the watched game.

    This mirrors the game-night case where a scraper returned odds for a
    future OKC/SAS fixture after Cleveland/Knicks disappeared. The health log
    should show the other scheduled match, and the coverage-gap check should
    still alert for the active game.
    """
    coord = _make_coordinator(store)
    active_game = await _make_game(
        store,
        external_id="nba_active_coverage",
        seconds_to_tipoff=600,
    )
    future_game = Game(
        external_id="nba_future_coverage",
        home_team="Oklahoma City Thunder",
        away_team="San Antonio Spurs",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=8),
        status="scheduled",
    )
    future_game.id = await store.upsert_game(future_game)
    slate = [active_game, future_game]
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(active_game.home_team, active_game.away_team),
        tip_returns=[
            _tip_snap(
                future_game.home_team,
                future_game.away_team,
                future_game.scheduled_at,
            )
        ],
    )

    sent = _capture_telegram(monkeypatch)
    now = datetime.now(timezone.utc)
    with caplog.at_level(logging.INFO, logger="oddsiq.pipeline.poll_coordinator"):
        await coord.poll(NBA, [active_game], slate, now)

    tip_health = [
        r.getMessage()
        for r in caplog.records
        if "Scraper health" in r.getMessage() and "name=tipsport" in r.getMessage()
    ]
    assert tip_health, (
        f"missing tipsport health line in {[r.getMessage() for r in caplog.records]}"
    )
    assert "matched_games=0" in tip_health[0]
    assert "other_games=1" in tip_health[0]
    assert "unmatched_snapshots=0" in tip_health[0]
    assert (
        active_game.id,
        "tipsport",
    ) not in coord._coverage._last_game_coverage_success

    await coord._coverage.check_gaps(slate, coord._scrapers, datetime.now(timezone.utc))

    assert len(sent) == 1
    assert "scraper=tipsport" in sent[0].lower()
    assert f"game_id={active_game.id}" in sent[0]


@pytest.mark.asyncio
async def test_coverage_gap_realerts_after_scraper_recovers(store: Store, monkeypatch):
    """After a successful match clears the dedup key, a new gap triggers a new alert."""
    coord = _make_coordinator(store)
    hot_game = await _make_game(store, external_id="nba_hot", seconds_to_tipoff=600)
    slate = [hot_game]
    now = datetime.now(timezone.utc)
    coord._coverage._last_game_coverage_success = {
        (hot_game.id, "pinnacle"): now - timedelta(minutes=12),
        (hot_game.id, "tipsport"): now,
    }
    sent = _capture_telegram(monkeypatch)

    await coord._coverage.check_gaps(slate, coord._scrapers, datetime.now(timezone.utc))
    assert len(sent) == 1

    # Simulate successful Pinnacle coverage via poll: it both updates
    # _coverage._last_game_coverage_success and clears the matching dedup keys.
    pin, _tip = _stub_scrapers(
        coord,
        pin_returns=[_pin_snap(hot_game.home_team, hot_game.away_team)],
        tip_returns=[_tip_snap(hot_game.home_team, hot_game.away_team)],
    )
    await coord.poll(NBA, [hot_game], slate, datetime.now(timezone.utc))
    assert pin.fetch_odds.await_count == 1  # sanity: scraper ran

    # Now simulate Pinnacle going stale again.
    coord._coverage._last_game_coverage_success[(hot_game.id, "pinnacle")] = (
        datetime.now(timezone.utc) - timedelta(minutes=12)
    )
    coord._coverage._last_coverage_check = None
    await coord._coverage.check_gaps(slate, coord._scrapers, datetime.now(timezone.utc))

    assert len(sent) == 2


@pytest.mark.asyncio
async def test_wedged_pinnacle_fires_exactly_one_alert_per_gap(
    store: Store, monkeypatch
):
    """End-to-end harness: Pinnacle.fetch_odds raises on every poll while a game
    is in hot tier. Across multiple poll + check cycles, exactly one coverage
    alert fires. After Pinnacle recovers and goes stale again, a second alert
    can fire — proving the dedup key releases on recovery."""
    coord = _make_coordinator(store)
    hot_game = await _make_game(store, external_id="nba_hot", seconds_to_tipoff=600)
    slate = [hot_game]

    pin = next(s for s in coord._scrapers if s.name == "pinnacle")
    tip = next(s for s in coord._scrapers if s.name == "tipsport")
    pin.fetch_odds = AsyncMock(side_effect=RuntimeError("wedged pinnacle"))
    tip.fetch_odds = AsyncMock(
        return_value=[_tip_snap(hot_game.home_team, hot_game.away_team)]
    )

    sent = _capture_telegram(monkeypatch)

    # Seed prior game coverage from 15 min ago so the check has something
    # measurable to go stale against.
    coord._coverage._last_game_coverage_success[(hot_game.id, "pinnacle")] = (
        datetime.now(timezone.utc) - timedelta(minutes=15)
    )
    coord._coverage._last_game_coverage_success[(hot_game.id, "tipsport")] = (
        datetime.now(timezone.utc)
    )

    # Simulate 5 scheduler iterations: poll (Pinnacle raises) + coverage check.
    for _ in range(5):
        now = datetime.now(timezone.utc)
        await coord.poll(NBA, [hot_game], slate, now)
        coord._coverage._last_coverage_check = (
            None  # bypass 2-min throttle for the test
        )
        await coord._coverage.check_gaps(
            slate, coord._scrapers, datetime.now(timezone.utc)
        )

    assert pin.fetch_odds.await_count == 5  # Pinnacle was attempted each cycle
    assert len(sent) == 1  # but only one coverage alert despite 5 failures
    assert "pinnacle" in sent[0].lower()

    # Now simulate Pinnacle recovering on the next poll, then wedging again.
    pin.fetch_odds = AsyncMock(
        return_value=[_pin_snap(hot_game.home_team, hot_game.away_team)]
    )
    await coord.poll(
        NBA, [hot_game], slate, datetime.now(timezone.utc)
    )  # success: clears dedup, updates timestamp
    coord._coverage._last_game_coverage_success[(hot_game.id, "pinnacle")] = (
        datetime.now(timezone.utc) - timedelta(minutes=12)
    )
    coord._coverage._last_coverage_check = None
    await coord._coverage.check_gaps(slate, coord._scrapers, datetime.now(timezone.utc))

    assert len(sent) == 2  # recovery + new gap → second alert is allowed


@pytest.mark.asyncio
async def test_poll_updates_matched_coverage(store: Store):
    """A successful scraper fetch records matched-coverage success per game/scraper."""
    coord = _make_coordinator(store)
    hot_game = await _make_game(store, external_id="nba_hot", seconds_to_tipoff=600)
    slate = [hot_game]
    _stub_scrapers(
        coord,
        pin_returns=[_pin_snap(hot_game.home_team, hot_game.away_team)],
        tip_returns=[_tip_snap(hot_game.home_team, hot_game.away_team)],
    )

    before = datetime.now(timezone.utc)
    await coord.poll(NBA, [hot_game], slate, datetime.now(timezone.utc))

    assert (
        coord._coverage._last_game_coverage_success[(hot_game.id, "pinnacle")] >= before
    )
    assert (
        coord._coverage._last_game_coverage_success[(hot_game.id, "tipsport")] >= before
    )


@pytest.mark.asyncio
async def test_poll_does_not_record_coverage_on_empty_odds(store: Store):
    """An empty scraper return records NO matched-coverage success for that
    scraper, while a non-empty one does."""
    coord = _make_coordinator(store)
    hot_game = await _make_game(
        store, external_id="nba_hot_empty", seconds_to_tipoff=600
    )
    slate = [hot_game]
    _stub_scrapers(
        coord,
        pin_returns=[],
        tip_returns=[_tip_snap(hot_game.home_team, hot_game.away_team)],
    )

    await coord.poll(NBA, [hot_game], slate, datetime.now(timezone.utc))

    assert (hot_game.id, "pinnacle") not in coord._coverage._last_game_coverage_success
    assert (hot_game.id, "tipsport") in coord._coverage._last_game_coverage_success


@pytest.mark.asyncio
async def test_poll_survives_fetch_task_level_exception(store, monkeypatch):
    """If a per-scraper fetch task raises ABOVE the inner try/except (i.e. the
    _fetch_scraper_odds wrapper itself errors), the gather must not cancel the
    sibling tasks or crash the poll. Pinnacle's odds still land."""
    coord = _make_coordinator(store)
    game = await _make_game(
        store, external_id="g_hot", seconds_to_tipoff=1800
    )  # hot tier
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair("Boston Celtics", "Los Angeles Lakers"),
        tip_returns=[_book_snap("Boston Celtics", "Los Angeles Lakers")],
    )
    orig = coord._fetch_scraper_odds

    async def flaky(scraper, game_date, profile):
        if scraper.name == "tipsport":
            raise RuntimeError("task-level boom")  # escapes the inner guard
        return await orig(scraper, game_date, profile)

    monkeypatch.setattr(coord, "_fetch_scraper_odds", flaky)

    # Must not raise despite the tipsport task erroring.
    await coord.poll(NBA, [game], [game], datetime.now(timezone.utc))

    cur = await store._db.execute(
        "SELECT COUNT(*) AS n FROM odds_snapshots WHERE bookmaker='pinnacle' AND game_id=?",
        (game.id,),
    )
    assert (await cur.fetchone())["n"] >= 1  # sibling task's odds still landed


# ---------------------------------------------------------------------------
# _matches_game commence window for TIPSPORT snapshots (2026-08-30 wrong-game
# fix): the parser now carries the listing's own start, so a next-day listing
# for the same team pair no longer attaches to today's game.
# ---------------------------------------------------------------------------


def _tip_snap_at(home: str, away: str, commence_time: datetime | None) -> OddsSnapshot:
    return OddsSnapshot(
        game_id=0,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="away",
        odds=1.53,
        _home_team=home,
        _away_team=away,
        _commence_time=commence_time,
        _tipsport_match_id=8442783,
    )


def test_matches_game_rejects_next_day_tipsport_listing():
    now = datetime.now(timezone.utc)
    game = Game(
        external_id="g-fri",
        home_team="Los Angeles Angels",
        away_team="Philadelphia Phillies",
        scheduled_at=now + timedelta(hours=1),
        commence_time=now + timedelta(hours=1),
        status="scheduled",
    )
    ghost = _tip_snap_at(
        "Los Angeles Angels", "Philadelphia Phillies", now + timedelta(hours=19)
    )  # tomorrow's listing
    assert PollCoordinator._matches_game(ghost, game) is False


def test_matches_game_accepts_tipsport_listing_within_window():
    now = datetime.now(timezone.utc)
    game = Game(
        external_id="g-fri2",
        home_team="Toronto Blue Jays",
        away_team="Seattle Mariners",
        scheduled_at=now + timedelta(hours=1),
        commence_time=now + timedelta(hours=1),
        status="scheduled",
    )
    # Tipsport keeps its original start through an Odds API delay update —
    # observed 42 min apart (2026-08-29 Blue Jays), well inside the ±2h window.
    near = _tip_snap_at(
        "Toronto Blue Jays", "Seattle Mariners", now + timedelta(minutes=18)
    )
    assert PollCoordinator._matches_game(near, game) is True


def test_matches_game_tipsport_without_start_fails_closed():
    """Payload drift must stop attribution instead of recreating ghost edges."""
    now = datetime.now(timezone.utc)
    game = Game(
        external_id="g-fb",
        home_team="Boston Celtics",
        away_team="Miami Heat",
        scheduled_at=now + timedelta(hours=1),
        commence_time=now + timedelta(hours=1),
        status="scheduled",
    )
    legacy = _tip_snap_at("Boston Celtics", "Miami Heat", None)
    assert PollCoordinator._matches_game(legacy, game) is False


def test_matches_game_pinnacle_requires_exact_provider_event_id():
    now = datetime.now(timezone.utc)
    game = Game(
        external_id="schedule-event-id",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=now + timedelta(hours=1),
        commence_time=now + timedelta(hours=1),
        status="scheduled",
    )
    snap = _pin_snap("Boston Celtics", "Los Angeles Lakers", game.commence_time)
    snap._source_event_id = "different-event-id"
    assert PollCoordinator._matches_game(snap, game) is False
    snap._source_event_id = "schedule-event-id"
    assert PollCoordinator._matches_game(snap, game) is True


@pytest.mark.asyncio
async def test_doubleheader_with_time_scoped_tipsport_snapshots_still_detects(
    store: Store,
):
    """2026-09-06: with Tipsport listings carrying their own start, each
    doubleheader leg gets exactly its own listing, so a shared team pair is
    no longer ambiguous — edge detection must RUN (the old blanket skip cost
    a real +3.2% hot edge on the Sep 4 Guardians–Tigers doubleheader). A
    start-less listing never gets here at all: it is dropped by fail-closed
    matching (see the drop test above)."""
    coord = _make_coordinator(store)
    now = datetime.now(timezone.utc)
    home, away = "Houston Astros", "Athletics"
    c1, c2 = now + timedelta(minutes=10), now + timedelta(hours=2, minutes=30)
    g1 = Game(
        external_id="dh-t1",
        home_team=home,
        away_team=away,
        scheduled_at=c1,
        commence_time=c1,
        status="scheduled",
    )  # hot
    g1.id = await store.upsert_game(g1)
    g2 = Game(
        external_id="dh-t2",
        home_team=home,
        away_team=away,
        scheduled_at=c2,
        commence_time=c2,
        status="scheduled",
    )  # far (pollable)
    g2.id = await store.upsert_game(g2)

    def _tip(commence, odds):
        return OddsSnapshot(
            game_id=0,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            odds=odds,
            _home_team=home,
            _away_team=away,
            _commence_time=commence,
            _tipsport_match_id=1 if commence == c1 else 2,
            _tipsport_opp_id=11,
            _tipsport_url="/kurzy/zapas/x/1",
        )

    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(home, away, home_odds=1.70, away_odds=2.20),
        tip_returns=[_tip(c1, 2.05), _tip(c2, 1.50)],
    )
    await coord.poll(NBA, [g1, g2], [g1, g2], now)

    cur = await store._db.execute("SELECT game_id FROM value_bets")
    rows = [r[0] for r in await cur.fetchall()]
    assert rows == [g1.id]  # leg 1's own listing produced its edge; leg 2 untouched


@pytest.mark.asyncio
async def test_far_tier_shadow_detection_only_for_flagged_sport(store: Store):
    """NFL (shadow_far_detection=True): a far-tier game (2-6h out) fetches
    Pinnacle, stores its rows and records edges stamped tier='far'. MLB keeps
    the MLB-tuned behavior: far tier = Tipsport only, no Pinnacle, no edges."""
    now = datetime.now(timezone.utc)
    home, away = "Seattle Seahawks", "New England Patriots"

    for profile, expect_pinnacle in ((NFL, True), (MLB, False)):
        coord = _make_coordinator(store)
        g = Game(
            external_id=f"far-{profile.key}",
            home_team=home,
            away_team=away,
            scheduled_at=now + timedelta(hours=3),
            status="scheduled",
            sport=profile.key,
        )
        g.id = await store.upsert_game(g)
        pin, _tip = _stub_scrapers(
            coord,
            pin_returns=_pin_pair(home, away, home_odds=1.70, away_odds=2.20),
            tip_returns=[_book_snap(home, away)],
        )
        await coord.poll(profile, [g], [g], now)

        assert pin.fetch_odds.await_count == (1 if expect_pinnacle else 0), profile.key
        cur = await store._db.execute(
            "SELECT COUNT(*) FROM odds_snapshots WHERE game_id=? AND bookmaker='pinnacle'",
            (g.id,),
        )
        (n_pin,) = await cur.fetchone()
        assert (n_pin > 0) is expect_pinnacle, profile.key
        cur = await store._db.execute(
            "SELECT tier, excluded_reason FROM value_bets WHERE game_id=?", (g.id,)
        )
        rows = await cur.fetchall()
        if expect_pinnacle:
            assert rows and all(r[0] == "far" for r in rows), profile.key
            assert all(r[1] is None for r in rows), profile.key
        else:
            assert rows == [], profile.key


@pytest.mark.asyncio
async def test_health_log_counts_shadow_far_pinnacle_as_matched(store: Store, caplog):
    """For a shadow sport the far-tier Pinnacle rows ARE stored, so they must be
    counted matched exactly once — not double-counted as an on-slate 'other'
    game, which would mask genuinely unmatched (normalization-miss) snapshots."""
    now = datetime.now(timezone.utc)
    coord = _make_coordinator(store)
    g = Game(
        external_id="nfl_far_h",
        home_team="Seattle Seahawks",
        away_team="New England Patriots",
        scheduled_at=now + timedelta(hours=3),
        status="scheduled",
        sport="nfl",
    )
    g.id = await store.upsert_game(g)
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(g.home_team, g.away_team)
        + _pin_pair("Unmapped Home", "Unmapped Away"),  # normalization miss
        tip_returns=[_book_snap(g.home_team, g.away_team)],
    )
    with caplog.at_level(logging.INFO, logger="oddsiq.pipeline.poll_coordinator"):
        await coord.poll(NFL, [g], [g], now)
    line = next(
        r.getMessage()
        for r in caplog.records
        if "Scraper health" in r.getMessage() and "name=pinnacle" in r.getMessage()
    )
    assert "matched_games=1" in line
    assert "other_games=0" in line  # NOT also counted as on-slate other
    assert "unmatched_snapshots=2" in line  # the miss is still visible


@pytest.mark.asyncio
async def test_poll_dispatches_pending_bets_after_the_game_loop(store: Store):
    """The coordinator collects a cycle's clean detections and dispatches them
    once per poll (biggest edge first) instead of inline during detection."""
    coord = _make_coordinator(store)
    betting = AsyncMock()
    coord._edge_service._betting = betting
    now = datetime.now(timezone.utc)
    g = Game(
        external_id="disp-1",
        home_team="Houston Astros",
        away_team="Athletics",
        scheduled_at=now + timedelta(minutes=30),
        status="scheduled",
    )  # hot
    g.id = await store.upsert_game(g)
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(
            "Houston Astros", "Athletics", home_odds=1.70, away_odds=2.20
        ),
        tip_returns=[
            OddsSnapshot(
                game_id=0,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="home",
                odds=1.88,
                _home_team="Houston Astros",
                _away_team="Athletics",
                _commence_time=g.scheduled_at,
                _tipsport_match_id=1,
                _tipsport_opp_id=2,
                _tipsport_url="/kurzy/zapas/x/1",
            ),
            OddsSnapshot(
                game_id=0,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="away",
                odds=1.90,
                _home_team="Houston Astros",
                _away_team="Athletics",
                _commence_time=g.scheduled_at,
                _tipsport_match_id=1,
                _tipsport_opp_id=3,
                _tipsport_url="/kurzy/zapas/x/1",
            ),
        ],  # +6.0%, complete two-way market
    )
    await coord.poll(NBA, [g], [g], now)
    betting.on_value_bet.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_detection_failure_in_one_game_does_not_strand_the_others(
    store: Store,
):
    """One game's detection failure must not abort the sport's cycle: the bets
    already collected would be UNRECOVERABLE (their value_bets rows exist, so
    the next cycle's re-detection dedups away and never re-pends them)."""
    coord = _make_coordinator(store)
    now = datetime.now(timezone.utc)
    g1 = await _make_game(store, external_id="strand_1", seconds_to_tipoff=600)  # hot
    g2 = Game(
        external_id="strand_2",
        home_team="Miami Heat",
        away_team="Toronto Raptors",
        scheduled_at=now + timedelta(minutes=20),
        status="scheduled",
    )  # hot
    g2.id = await store.upsert_game(g2)
    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(g1.home_team, g1.away_team)
        + _pin_pair(g2.home_team, g2.away_team),
        tip_returns=[
            _tip_snap(g1.home_team, g1.away_team),
            _tip_snap(g2.home_team, g2.away_team),
        ],
    )
    real_detect = coord._edge_service.detect_pregame_edges

    async def flaky(game, game_snapshots, tier):
        if game.id == g1.id:
            raise RuntimeError("boom")
        return await real_detect(game, game_snapshots, tier)

    coord._edge_service.detect_pregame_edges = flaky
    dispatch = AsyncMock()
    coord._edge_service.dispatch_pending = dispatch

    await coord.poll(NBA, [g1, g2], [g1, g2], now)  # must not raise

    dispatch.assert_awaited_once()
    pending = dispatch.await_args.args[0]
    assert [pb.game.id for pb in pending] == [g2.id]


@pytest.mark.asyncio
async def test_poll_mixed_shadow_and_bettable_rows_all_reach_the_engine(store: Store):
    """NFL runs far-tier SHADOW detection alongside normal hot detection. Both
    kinds of row reach the engine in one dispatch, ordered by kickoff — the
    hot game (30 min out) before the far one (3h out), whose deadline is far
    away and whose row is not bettable anyway."""
    coord = _make_coordinator(store)
    betting = AsyncMock()
    coord._edge_service._betting = betting
    now = datetime.now(timezone.utc)
    hot_at, far_at = now + timedelta(minutes=30), now + timedelta(hours=3)
    hot = Game(
        external_id="nfl_mixed_hot",
        home_team="Seattle Seahawks",
        away_team="New England Patriots",
        scheduled_at=hot_at,
        commence_time=hot_at,
        status="scheduled",
        sport="nfl",
    )
    hot.id = await store.upsert_game(hot)
    far = Game(
        external_id="nfl_mixed_far",
        home_team="Dallas Cowboys",
        away_team="Green Bay Packers",
        scheduled_at=far_at,
        commence_time=far_at,
        status="scheduled",
        sport="nfl",
    )
    far.id = await store.upsert_game(far)

    def _tip(game, commence):
        # 1.85 vs the _pin_pair fair (~1.77): a clean ~4.3% edge.
        common = {
            "game_id": 0,
            "bookmaker": "tipsport",
            "market_type": "moneyline",
            "_home_team": game.home_team,
            "_away_team": game.away_team,
            "_commence_time": commence,
            "_tipsport_match_id": game.id,
            "_tipsport_url": f"/kurzy/zapas/x/{game.id}",
        }
        return [
            OddsSnapshot(selection="home", odds=1.85, _tipsport_opp_id=11, **common),
            OddsSnapshot(selection="away", odds=1.95, _tipsport_opp_id=12, **common),
        ]

    _stub_scrapers(
        coord,
        pin_returns=_pin_pair(hot.home_team, hot.away_team)
        + _pin_pair(far.home_team, far.away_team),
        tip_returns=[*_tip(far, far_at), *_tip(hot, hot_at)],  # far listed FIRST
    )

    await coord.poll(NFL, [far, hot], [far, hot], now)

    assert betting.on_value_bet.await_count == 2
    order = [
        c.args[0].id if c.args else c.kwargs["game"].id
        for c in betting.on_value_bet.await_args_list
    ]
    assert order == [hot.id, far.id]  # earliest kickoff dispatched first
    cur = await store._db.execute(
        "SELECT game_id, tier FROM value_bets ORDER BY game_id"
    )
    assert {r["game_id"]: r["tier"] for r in await cur.fetchall()} == {
        hot.id: "hot",
        far.id: "far",
    }
