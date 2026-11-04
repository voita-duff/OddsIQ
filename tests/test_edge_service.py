import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, create_autospec

from oddsiq.db import migrations
from oddsiq.db.models import Game, OddsSnapshot
from oddsiq.db.store import Store
from oddsiq.pipeline.betting_service import BettingService
from oddsiq.pipeline.edge_service import EdgeService
from oddsiq.pipeline.stats import SessionStats


@pytest.fixture(autouse=True)
def _silence_real_telegram(monkeypatch):
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", AsyncMock())


@pytest_asyncio.fixture
async def store(tmp_path):
    db = tmp_path / "t.db"
    migrations.run(db)
    s = Store(db)
    await s.initialize()
    yield s
    await s.close()


def _betting_double():
    """A BettingService stand-in that CHECKS the call shape.

    A bare ``AsyncMock()`` swallows any call — renaming the
    ``pinnacle_fresh_at`` keyword at the ``dispatch_pending`` call site would
    keep every dispatch test green while production stranded EVERY bet as
    `bet_dispatch_failed:`. ``create_autospec`` binds each call to the real
    ``on_value_bet`` signature and raises TypeError *before* recording it, so
    a renamed keyword fails the awaited-once assertions below. (The companion
    ``test_dispatch_call_shape_binds_to_the_real_on_value_bet_signature``
    checks the other direction: the real method accepts that shape.)"""
    return create_autospec(BettingService, instance=True)


def _snap(book, sel, odds, mid=None):
    return OddsSnapshot(
        game_id=1,
        bookmaker=book,
        market_type="moneyline",
        selection=sel,
        odds=odds,
        captured_at=datetime.now(timezone.utc),
        _tipsport_match_id=mid,
    )


async def _game(store, sport="nba"):
    g = Game(
        external_id="e1",
        home_team="Boston Celtics",
        away_team="Miami Heat",
        scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=20),
        commence_time=datetime.now(timezone.utc) + timedelta(minutes=20),
        status="scheduled",
        sport=sport,
    )
    g.id = await store.upsert_game(g)
    return g


async def _game_at(store, external_id, kickoff, *, home, away, sport="nfl"):
    """Persist a game with an EXPLICIT kickoff (scheduled_at == commence_time),
    so dispatch-order tests control each bet's deadline exactly."""
    g = Game(
        external_id=external_id,
        home_team=home,
        away_team=away,
        scheduled_at=kickoff,
        commence_time=kickoff,
        status="scheduled",
        sport=sport,
    )
    g.id = await store.upsert_game(g)
    return g


@pytest.mark.asyncio
async def test_detect_pregame_edges_flags_value_bet(store):
    g = await _game(store)
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.3), _snap("tipsport", "away", 1.7)],
    }
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 1
    assert bets[0].tier == "hot" and bets[0].selection == "home"


@pytest.mark.asyncio
async def test_detect_pregame_edges_persists_tipsport_ids(store):
    """Transient snapshot ids land on the persisted value bet (and survive a
    store roundtrip) — the execution identity the P0 probe will address."""
    g = await _game(store)
    tip_home = _snap("tipsport", "home", 2.3)
    tip_home._tipsport_match_id = 8109316
    tip_home._tipsport_opp_id = 2624446456
    tip_home._tipsport_url = "/kurzy/zapas/baseball-x/8109316"
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [tip_home, _snap("tipsport", "away", 1.7)],
    }
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 1
    assert bets[0].tipsport_match_id == 8109316
    assert bets[0].tipsport_opp_id == 2624446456
    assert bets[0].tipsport_url == "/kurzy/zapas/baseball-x/8109316"


@pytest.mark.asyncio
async def test_clean_bet_reaches_betting_service_with_fresh_fair(store):
    """A clean insert is RETURNED as a pending bet (not dispatched inline);
    dispatch_pending hands it to the engine with the conservative (oldest)
    observation time of the exact Pinnacle pair. Suspect
    (guard-stamped) bets never become pending."""
    g = await _game(store, sport="mlb")
    pin_home = _snap("pinnacle", "home", 2.0)
    pin_away = _snap("pinnacle", "away", 2.0)
    # Make the home snapshot unambiguously newer than its paired away side.
    pin_home.captured_at = pin_away.captured_at + timedelta(seconds=5)
    snaps = {
        "pinnacle": [pin_home, pin_away],
        # 2.10/1.85 -> two-way implied 1.017 (coherent book), home edge 5%.
        "tipsport": [_snap("tipsport", "home", 2.1), _snap("tipsport", "away", 1.85)],
    }
    betting = _betting_double()
    svc = EdgeService(store=store, stats=SessionStats(), betting=betting)
    pending = await svc.detect_pregame_edges(g, snaps, tier="hot")
    betting.on_value_bet.assert_not_awaited()  # nothing dispatched yet
    assert len(pending) == 1
    await svc.dispatch_pending(pending)
    betting.on_value_bet.assert_awaited_once()
    kwargs = betting.on_value_bet.await_args.kwargs
    args = betting.on_value_bet.await_args.args
    vb = kwargs.get("vb") or args[1]
    fresh = kwargs.get("pinnacle_fresh_at") or args[2]
    assert vb.selection == "home" and vb.excluded_reason is None
    # The persisted row id MUST ride along — the betting engine keys its
    # decision row on it and silently no-ops on None (caught in e2e 2026-08-09).
    assert vb.id is not None
    assert fresh == pin_away.captured_at
    assert kwargs["pinnacle_source_updated_at"] == pin_away.captured_at


@pytest.mark.asyncio
async def test_suspect_bet_never_reaches_betting_service(store):
    g = await _game(store, sport="mlb")
    # 15% edge -> implausible_edge_pregame guard stamps it suspect.
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.3), _snap("tipsport", "away", 1.7)],
    }
    betting = _betting_double()
    svc = EdgeService(store=store, stats=SessionStats(), betting=betting)
    pending = await svc.detect_pregame_edges(g, snaps, tier="hot")
    assert pending == []
    await svc.dispatch_pending(pending)
    betting.on_value_bet.assert_not_awaited()


@pytest.mark.asyncio
async def test_detect_pregame_edges_noop_without_pinnacle(store):
    g = await _game(store)
    snaps = {"tipsport": [_snap("tipsport", "home", 2.3)]}
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    assert (await store.get_pending_value_bets(g.id)) == []


@pytest.mark.asyncio
async def test_detect_pregame_edges_stamps_tier(store):
    g = await _game(store)
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.3), _snap("tipsport", "away", 1.7)],
    }
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="warm")
    bets = await store.get_pending_value_bets(g.id)
    assert bets[0].tier == "warm"


@pytest.mark.asyncio
async def test_value_bet_carries_game_sport(store):
    g = await _game(store, sport="mlb")
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.3), _snap("tipsport", "away", 1.7)],
    }
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert bets and bets[0].sport == "mlb"


# --- Pregame plausibility cap + book-coherence guard ------------------------
# Calibrated 2026-07-10 on settled data: pregame edges >= 8% went 2W-9L
# (-63% flat ROI) while showing inflated CLV — stale-Tipsport mirages the
# CLV metric cannot catch (it compares the same stale entry price to close).


@pytest.mark.asyncio
async def test_implausible_edge_stamped_not_alerted_not_counted(store):
    """An edge >= PREGAME_MAX_PLAUSIBLE_EDGE is still recorded (so settlement
    keeps validating the cap) but is stamped excluded_reason and neither
    alerts nor counts toward session stats."""
    from oddsiq.notifications import telegram as tg

    g = await _game(store)
    # fair 2.0/2.0; tipsport home 2.9 => +45% edge; book sum 1/2.9+1/1.4 = 1.06 (coherent)
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.9), _snap("tipsport", "away", 1.4)],
    }
    stats = SessionStats()
    svc = EdgeService(store=store, stats=stats)
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 1
    assert bets[0].excluded_reason == "implausible_edge_pregame"
    assert stats.value_bets_flagged == 0
    tg.send_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_play_offer_stamped_suspect(store):
    """A Tipsport match entry under /live/ means the offer is already in-play:
    a "pregame" edge against it is a tipoff-boundary mirage (2026-08-26/27
    forensics: ALL three clean /live/-url detections were commanded and ALL
    died offer_gone — the prematch odd button doesn't exist on a live page)."""
    from oddsiq.notifications import telegram as tg

    g = await _game(store)
    home = _snap("tipsport", "home", 2.3)
    away = _snap("tipsport", "away", 1.7)
    home._tipsport_url = "/live/zapas/baseball-athletics-minnesota-twins/8441288"
    away._tipsport_url = "/live/zapas/baseball-athletics-minnesota-twins/8441288"
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [home, away],
    }
    stats = SessionStats()
    svc = EdgeService(store=store, stats=stats)
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 1
    assert bets[0].excluded_reason == "in_play_pregame"
    assert stats.value_bets_flagged == 0
    tg.send_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_pregame_url_not_stamped_in_play(store):
    """A /kurzy/ (prematch) URL — and a missing URL — must NOT trip the
    in-play guard; the clean path stays clean. (Odds kept under the 8%
    plausibility cap and coherent so no OTHER guard fires either.)"""
    g = await _game(store)
    home = _snap("tipsport", "home", 2.08)  # 4% edge vs fair 2.0
    away = _snap("tipsport", "away", 1.7)
    home._tipsport_url = "/kurzy/zapas/baseball-athletics-minnesota-twins/8441288"
    away._tipsport_url = "/kurzy/zapas/baseball-athletics-minnesota-twins/8441288"
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [home, away],
    }
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 1
    assert bets[0].excluded_reason is None


@pytest.mark.asyncio
async def test_incoherent_book_stamps_both_sides(store):
    """If the soft book's own two-way implied sum < 1.0 (an arb against
    itself), the book is stale/incoherent — every edge it produces is stamped,
    even when each side individually sits below the plausibility cap."""
    from oddsiq.notifications import telegram as tg

    g = await _game(store)
    # fair 2.0/2.0; tipsport 2.15/2.15 => both +7.5% (below cap), implied sum 0.93
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.15), _snap("tipsport", "away", 2.15)],
    }
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 2
    assert all(b.excluded_reason == "incoherent_book_pregame" for b in bets)
    tg.send_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_sharp_source_timestamp_is_excluded(store):
    g = await _game(store)
    pin = [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)]
    for snap in pin:
        snap._source_freshness_missing = True
    tips = [
        _snap("tipsport", "home", 2.1, mid=44),
        _snap("tipsport", "away", 1.8, mid=44),
    ]
    pending = await EdgeService(store=store, stats=SessionStats()).detect_pregame_edges(
        g, {"pinnacle": pin, "tipsport": tips}, tier="hot"
    )

    bets = await store.get_pending_value_bets(g.id)
    assert pending == []
    assert bets[0].excluded_reason == "sharp_source_unknown"


@pytest.mark.asyncio
async def test_stale_sharp_source_timestamp_is_excluded(store):
    g = await _game(store)
    observed = datetime.now(timezone.utc)
    pin = [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)]
    for snap in pin:
        snap.captured_at = observed
        snap.source_updated_at = observed - timedelta(seconds=301)
    tips = [
        _snap("tipsport", "home", 2.1, mid=45),
        _snap("tipsport", "away", 1.8, mid=45),
    ]
    pending = await EdgeService(store=store, stats=SessionStats()).detect_pregame_edges(
        g, {"pinnacle": pin, "tipsport": tips}, tier="hot"
    )

    bets = await store.get_pending_value_bets(g.id)
    assert pending == []
    assert bets[0].excluded_reason == "stale_sharp_source"
    assert bets[0].sharp_source_updated_at == observed - timedelta(seconds=301)


@pytest.mark.asyncio
async def test_incomplete_tipsport_market_is_excluded(store):
    g = await _game(store)
    tips = [_snap("tipsport", "home", 2.1, mid=46)]
    pending = await EdgeService(store=store, stats=SessionStats()).detect_pregame_edges(
        g,
        {
            "pinnacle": [
                _snap("pinnacle", "home", 2.0),
                _snap("pinnacle", "away", 2.0),
            ],
            "tipsport": tips,
        },
        tier="hot",
    )

    bets = await store.get_pending_value_bets(g.id)
    assert pending == []
    assert bets[0].excluded_reason == "incomplete_book_market"


async def _one_sided_detection(store):
    """A Tipsport listing carrying an execution id but only ONE side — the
    input the completeness guard exists for."""
    g = await _game(store)
    pending = await EdgeService(store=store, stats=SessionStats()).detect_pregame_edges(
        g,
        {
            "pinnacle": [
                _snap("pinnacle", "home", 2.0),
                _snap("pinnacle", "away", 2.0),
            ],
            "tipsport": [_snap("tipsport", "home", 2.1, mid=47)],
        },
        tier="hot",
    )
    return g, pending


@pytest.mark.asyncio
async def test_completeness_guard_kill_switch_off_leaves_the_bet_clean(
    store, monkeypatch
):
    """PREGAME_REQUIRE_COMPLETE_BOOK_MARKET=0 stops the stamp without a
    rebuild. The coherence floor still runs (floor > 0 keeps the block
    entered), but a one-sided market has no implied sum to judge, so the
    detection comes through clean and bettable."""
    from oddsiq import config

    monkeypatch.setattr(config, "PREGAME_REQUIRE_COMPLETE_BOOK_MARKET", False)
    g, pending = await _one_sided_detection(store)

    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 1
    assert bets[0].excluded_reason is None
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_completeness_guard_kill_switch_off_with_floor_disabled_skips_block(
    store,
    monkeypatch,
):
    """With the floor ALSO disabled the whole overround block is skipped —
    the pre-2026-09-06 meaning of PREGAME_MIN_BOOK_OVERROUND<=0 ("disables"),
    which the completeness guard had silently taken away."""
    from oddsiq import config

    monkeypatch.setattr(config, "PREGAME_REQUIRE_COMPLETE_BOOK_MARKET", False)
    monkeypatch.setattr(config, "PREGAME_MIN_BOOK_OVERROUND", 0.0)
    g, pending = await _one_sided_detection(store)

    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 1
    assert bets[0].excluded_reason is None
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_completeness_guard_kill_switch_on_still_stamps(store, monkeypatch):
    """The default direction, pinned explicitly against the flag: on ->
    stamped, even with the coherence floor disabled."""
    from oddsiq import config

    monkeypatch.setattr(config, "PREGAME_REQUIRE_COMPLETE_BOOK_MARKET", True)
    monkeypatch.setattr(config, "PREGAME_MIN_BOOK_OVERROUND", 0.0)
    g, pending = await _one_sided_detection(store)

    bets = await store.get_pending_value_bets(g.id)
    assert pending == []
    assert bets[0].excluded_reason == "incomplete_book_market"


@pytest.mark.asyncio
async def test_clean_detection_survives_earlier_stale_source_in_same_tier(store):
    """A temporary source-feed stall is retained as audit evidence but must
    not consume the tier's dedup slot and hide the later actionable edge."""
    g = await _game(store)
    observed = datetime.now(timezone.utc)
    stale_pin = [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)]
    for snap in stale_pin:
        snap.captured_at = observed
        snap.source_updated_at = observed - timedelta(seconds=301)
    tips = [
        _snap("tipsport", "home", 2.1, mid=47),
        _snap("tipsport", "away", 1.8, mid=47),
    ]
    svc = EdgeService(store=store, stats=SessionStats())
    assert (
        await svc.detect_pregame_edges(
            g, {"pinnacle": stale_pin, "tipsport": tips}, tier="hot"
        )
        == []
    )

    fresh_pin = [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)]
    for snap in fresh_pin:
        snap.source_updated_at = snap.captured_at
    pending = await svc.detect_pregame_edges(
        g, {"pinnacle": fresh_pin, "tipsport": tips}, tier="hot"
    )

    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 2
    assert {b.excluded_reason for b in bets} == {"stale_sharp_source", None}
    assert len(pending) == 1
    assert pending[0].vb.excluded_reason is None


@pytest.mark.asyncio
async def test_plausible_edge_unaffected_and_alerts(store):
    from oddsiq.notifications import telegram as tg

    g = await _game(store)
    # fair 2.0/2.0; tipsport home 2.1 => +5% edge; book sum 1/2.1+1/1.8 = 1.03
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.1), _snap("tipsport", "away", 1.8)],
    }
    stats = SessionStats()
    svc = EdgeService(store=store, stats=stats)
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 1
    assert bets[0].excluded_reason is None
    assert stats.value_bets_flagged == 1
    # Detections are no longer alerted to the phone (2026-08-10 rebrand).
    tg.send_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_plausibility_cap_disabled_when_nonpositive(store, monkeypatch):
    monkeypatch.setattr("oddsiq.config.PREGAME_MAX_PLAUSIBLE_EDGE", 0.0)
    g = await _game(store)
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.9), _snap("tipsport", "away", 1.4)],
    }
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 1
    assert bets[0].excluded_reason is None


@pytest.mark.asyncio
async def test_duplicate_tier_records_both_rows_without_alerting(store):
    """A hot-tier re-detection of a warm-tier selection is recorded as an
    independent row (dedup-key design: tier is part of the key) — and, since
    the 2026-08-10 rebrand, detections never alert the phone at all."""
    from oddsiq.notifications import telegram as tg

    g = await _game(store)
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.1), _snap("tipsport", "away", 1.8)],
    }
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="warm")
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 2
    tg.send_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_incoherent_guard_pairs_within_one_listing(store):
    """Two Tipsport listings for one game in a single batch (the 2026-08-30
    wrong-game ghost scenario): the coherence guard must pair sides WITHIN one
    listing. Mixing the ghost's away with the real listing's home used to
    manufacture a coherent-looking sum and let the ghost's edges through
    clean."""
    g = await _game(store)
    # fair 2.0/2.0. Ghost listing 222: 2.15/2.15 (incoherent alone, 0.93;
    # both sides +7.5% edges). Real listing 111: 1.9/1.9 (coherent, no edge).
    # Ghost FIRST so the old last-wins mixing would have read the real pair.
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [
            _snap("tipsport", "home", 2.15, mid=222),
            _snap("tipsport", "away", 2.15, mid=222),
            _snap("tipsport", "home", 1.9, mid=111),
            _snap("tipsport", "away", 1.9, mid=111),
        ],
    }
    svc = EdgeService(store=store, stats=SessionStats())
    await svc.detect_pregame_edges(g, snaps, tier="hot")
    bets = await store.get_pending_value_bets(g.id)
    assert len(bets) == 2  # both ghost sides detected...
    assert all(b.tipsport_match_id == 222 for b in bets)
    assert all(b.excluded_reason == "incoherent_book_pregame" for b in bets)


def _dispatch_order_ids(betting) -> list[int]:
    return [
        c.args[0].id if c.args else c.kwargs["game"].id
        for c in betting.on_value_bet.await_args_list
    ]


@pytest.mark.asyncio
async def test_dispatch_pending_same_kickoff_places_largest_edge_first(store):
    """Sunday-burst rule: within ONE kickoff wave the engine sees the biggest
    edge first (placements are serial, 5-25s each, and the tipoff buffer
    closes on the whole wave at once)."""
    kickoff = datetime.now(timezone.utc) + timedelta(minutes=20)
    g1 = await _game_at(
        store, "e1", kickoff, home="Houston Texans", away="Buffalo Bills"
    )
    g2 = await _game_at(
        store, "e2", kickoff, home="Seattle Seahawks", away="New England Patriots"
    )
    betting = _betting_double()
    svc = EdgeService(store=store, stats=SessionStats(), betting=betting)
    pin = [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)]
    small = await svc.detect_pregame_edges(
        g1,
        {
            "pinnacle": pin,
            "tipsport": [
                _snap("tipsport", "home", 2.08),
                _snap("tipsport", "away", 1.9),
            ],
        },
        tier="hot",
    )  # +4.0%
    big = await svc.detect_pregame_edges(
        g2,
        {
            "pinnacle": pin,
            "tipsport": [
                _snap("tipsport", "home", 2.14),
                _snap("tipsport", "away", 1.85),
            ],
        },
        tier="hot",
    )  # +7.0%
    await svc.dispatch_pending(small + big)  # detection order: small first
    # Same kickoff => the ordering key collapses to edge order.
    assert _dispatch_order_ids(betting) == [g2.id, g1.id]


@pytest.mark.asyncio
async def test_dispatch_pending_earliest_kickoff_first_even_with_smaller_edge(store):
    """Across staggered kickoffs (a hot wave and a warm wave in one poll) the
    deadline wins: a bet with an hour of slack must not spend the last window
    of one that is minutes from kickoff, however much bigger its edge is."""
    now = datetime.now(timezone.utc)
    soon = await _game_at(
        store,
        "soon",
        now + timedelta(minutes=20),
        home="Houston Texans",
        away="Buffalo Bills",
    )
    later = await _game_at(
        store,
        "later",
        now + timedelta(minutes=90),
        home="Seattle Seahawks",
        away="New England Patriots",
    )
    betting = _betting_double()
    svc = EdgeService(store=store, stats=SessionStats(), betting=betting)
    pin = [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)]
    small_soon = await svc.detect_pregame_edges(
        soon,
        {
            "pinnacle": pin,
            "tipsport": [
                _snap("tipsport", "home", 2.08),
                _snap("tipsport", "away", 1.9),
            ],
        },
        tier="hot",
    )  # +4.0%
    big_later = await svc.detect_pregame_edges(
        later,
        {
            "pinnacle": pin,
            "tipsport": [
                _snap("tipsport", "home", 2.14),
                _snap("tipsport", "away", 1.85),
            ],
        },
        tier="warm",
    )  # +7.0%
    await svc.dispatch_pending(big_later + small_soon)
    assert _dispatch_order_ids(betting) == [soon.id, later.id]


@pytest.mark.asyncio
async def test_dispatch_pending_isolates_a_failing_bet(store):
    """A raising on_value_bet (its store calls can hit SQLITE_BUSY under WAL)
    must not take the rest of the wave with it: every stranded bet is
    UNRECOVERABLE — its value_bets row already exists, so the next cycle's
    re-detection dedups away and it is never re-pended for this tier."""
    kickoff = datetime.now(timezone.utc) + timedelta(minutes=20)
    g1 = await _game_at(
        store, "iso-1", kickoff, home="Houston Texans", away="Buffalo Bills"
    )
    g2 = await _game_at(
        store, "iso-2", kickoff, home="Seattle Seahawks", away="New England Patriots"
    )
    betting = _betting_double()
    betting.on_value_bet.side_effect = [RuntimeError("db busy"), None]
    svc = EdgeService(store=store, stats=SessionStats(), betting=betting)
    pin = [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)]
    first = await svc.detect_pregame_edges(
        g1,
        {
            "pinnacle": pin,
            "tipsport": [
                _snap("tipsport", "home", 2.14),
                _snap("tipsport", "away", 1.85),
            ],
        },
        tier="hot",
    )  # +7.0%
    second = await svc.detect_pregame_edges(
        g2,
        {
            "pinnacle": pin,
            "tipsport": [
                _snap("tipsport", "home", 2.08),
                _snap("tipsport", "away", 1.9),
            ],
        },
        tier="hot",
    )  # +4.0%

    await svc.dispatch_pending(first + second)  # must not raise

    assert betting.on_value_bet.await_count == 2
    assert _dispatch_order_ids(betting) == [g1.id, g2.id]


@pytest.mark.asyncio
async def test_shadow_far_rows_are_pending_but_not_counted_in_session_stats(store):
    """Far-tier shadow rows still flow to the engine (it records the honest
    `tier_not_bettable` skip) but must not inflate the shutdown summary's
    value-bet counters — they are not bettable value bets."""
    g = await _game(store, sport="nfl")
    stats = SessionStats()
    betting = _betting_double()
    svc = EdgeService(store=store, stats=stats, betting=betting)
    snaps = {
        "pinnacle": [_snap("pinnacle", "home", 2.0), _snap("pinnacle", "away", 2.0)],
        "tipsport": [_snap("tipsport", "home", 2.1), _snap("tipsport", "away", 1.85)],
    }
    pending = await svc.detect_pregame_edges(g, snaps, tier="far")
    assert len(pending) == 1 and pending[0][1].tier == "far"
    assert stats.value_bets_flagged == 0
    assert stats.total_edge == 0.0
    assert stats.value_bets_per_book.get("tipsport", 0) == 0
    await svc.dispatch_pending(pending)
    betting.on_value_bet.assert_awaited_once()


def test_dispatch_call_shape_binds_to_the_real_on_value_bet_signature():
    """dispatch_pending calls on_value_bet(game, vb, pinnacle_fresh_at=...); the
    real BettingService method must accept exactly that shape. This is the
    static half of the contract; the dispatch tests above hold the other half
    (their autospec'd double rejects a renamed keyword at the call site)."""
    import inspect

    sig = inspect.signature(BettingService.on_value_bet)
    sig.bind(None, object(), object(), pinnacle_fresh_at=datetime.now(timezone.utc))
