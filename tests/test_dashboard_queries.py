"""Tests for the read-only SQL functions powering the dashboard.

Each function takes the existing :class:`oddsiq.db.store.Store` and
returns a plain dict / list that templates render directly. The
queries are time-windowed via clock-injection where relevant
(``today_iso`` parameter for ``status_strip``) so tests can avoid
wall-clock flakiness.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from oddsiq.db import migrations
from oddsiq.db.models import Game, OddsSnapshot, ValueBet
from oddsiq.db.store import Store
from oddsiq.dashboard import queries


@pytest.fixture(autouse=True)
def _hermetic_enabled_sports(monkeypatch):
    """Pin the full 4-sport list so tile/agenda coverage tests are independent
    of the production enablement (MLB-only since 2026-08-08). queries.py binds
    ENABLED_SPORTS at import time, so patch the module binding directly."""
    monkeypatch.setattr(
        "oddsiq.dashboard.queries.ENABLED_SPORTS", ["nba", "nhl", "mlb", "wnba"]
    )
    monkeypatch.setattr(
        "oddsiq.sports.profiles.ENABLED_SPORTS", ["nba", "nhl", "mlb", "wnba"]
    )


@pytest_asyncio.fixture
async def store(tmp_path):
    db = tmp_path / "test.db"
    migrations.run(db)
    s = Store(db)
    await s.initialize()
    yield s
    await s.close()


async def _seed_game(store, status="scheduled", *, hours_from_now=2.0, sport="nba"):
    g = Game(
        external_id=f"ext-{status}-{hours_from_now}-{sport}",
        home_team="Boston Celtics",
        away_team="Miami Heat",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=hours_from_now),
        status=status,
        sport=sport,
    )
    return await store.upsert_game(g)


async def _seed_value_bet(
    store, game_id: int, *, edge_pct=5.0, flagged_at=None, selection="home", sport="nba"
):
    vb = ValueBet(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection=selection,
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=edge_pct,
        tier="hot",
        sport=sport,
    )
    if flagged_at is not None:
        vb.flagged_at = flagged_at
    return await store.insert_value_bet(vb)


async def _seed_upcoming(
    store,
    *,
    tipoff,
    sport="nba",
    home="Home Team",
    away="Away Team",
    status="scheduled",
):
    g = Game(
        external_id=f"up-{sport}-{status}-{tipoff.isoformat()}",
        home_team=home,
        away_team=away,
        scheduled_at=tipoff,
        commence_time=tipoff,
        status=status,
        sport=sport,
    )
    return await store.upsert_game(g)


@pytest.mark.asyncio
async def test_status_strip_counts_games_by_status(store):
    await _seed_game(store, status="scheduled")
    await _seed_game(store, status="scheduled", hours_from_now=4.0)
    await _seed_game(store, status="live")
    await _seed_game(store, status="final", hours_from_now=-1.0)
    summary = await queries.status_strip(store)
    assert summary["games_total"] == 4
    assert summary["games_scheduled"] == 2
    assert summary["games_live"] == 1
    assert summary["games_final"] == 1


@pytest.mark.asyncio
async def test_status_strip_counts_todays_value_bets(store):
    gid = await _seed_game(store)
    await _seed_value_bet(
        store,
        gid,
        edge_pct=5.0,
        flagged_at=datetime.now(timezone.utc),
        selection="home",
    )
    await _seed_value_bet(
        store, gid, edge_pct=7.0, selection="away"
    )  # also today (default flagged_at)
    summary = await queries.status_strip(store)
    assert summary["flags_today"] == 2
    assert summary["avg_edge_today"] == 6.0


@pytest.mark.asyncio
async def test_status_strip_ignores_value_bets_from_yesterday(store):
    gid = await _seed_game(store)
    yesterday = datetime.now(timezone.utc) - timedelta(days=2)
    await _seed_value_bet(store, gid, edge_pct=5.0, flagged_at=yesterday)
    summary = await queries.status_strip(store)
    assert summary["flags_today"] == 0
    assert summary["avg_edge_today"] == 0.0


@pytest.mark.asyncio
async def test_status_strip_today_window_is_injectable_and_utc(store):
    # A bet flagged at 23:30 UTC on 2026-05-29 is already 2026-05-30 in CET
    # (UTC+2). The "today" window must be UTC (matching SQLite's date()),
    # not the operator's local date, or near-midnight flags get misbucketed.
    gid = await _seed_game(store)
    await _seed_value_bet(
        store,
        gid,
        edge_pct=5.0,
        flagged_at=datetime(2026, 5, 29, 23, 30, tzinfo=timezone.utc),
    )
    same_day = await queries.status_strip(store, today_iso="2026-05-29")
    assert same_day["flags_today"] == 1
    next_day = await queries.status_strip(store, today_iso="2026-05-30")
    assert next_day["flags_today"] == 0


@pytest.mark.asyncio
async def test_status_strip_reads_budget_from_meta(store):
    await store.set_meta("odds_api_budget_used", "158")
    await store.set_meta("odds_api_budget_remaining", "342")
    summary = await queries.status_strip(store)
    assert summary["budget_used"] == 158
    assert summary["budget_remaining"] == 342


@pytest.mark.asyncio
async def test_status_strip_budget_none_when_meta_absent(store):
    summary = await queries.status_strip(store)
    assert summary["budget_used"] is None
    assert summary["budget_remaining"] is None


@pytest.mark.asyncio
async def test_coverage_matrix_includes_hot_and_warm_tier_games(store):
    # 40 min away → hot (pipeline hot is <60m), 90 min away → warm, 3h → far/excluded
    hot_id = await _seed_game(store, status="scheduled", hours_from_now=40 / 60)
    warm_id = await _seed_game(store, status="scheduled", hours_from_now=1.5)
    await _seed_game(store, status="scheduled", hours_from_now=3.0)  # far → excluded
    await _seed_game(store, status="final", hours_from_now=-1.0)  # final → excluded
    # In-progress games are EXCLUDED — the live tier was removed 2026-08-27,
    # so no scraper is expected to produce post-tipoff snapshots (a live row
    # would flag permanent false "missing").
    await _seed_game(store, status="live", hours_from_now=-0.5, sport="mlb")
    rows = await queries.coverage_matrix(store, scrapers=["pinnacle", "tipsport"])
    game_ids = {r["game_id"] for r in rows}
    assert game_ids == {hot_id, warm_id}
    hot_rows = [r for r in rows if r["game_id"] == hot_id]
    warm_rows = [r for r in rows if r["game_id"] == warm_id]
    assert all(r["tier"] == "hot" for r in hot_rows)
    assert all(r["tier"] == "warm" for r in warm_rows)
    # hot (2 scrapers) + warm (2 scrapers) = 4 rows
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_active_game_tiers_uses_pipeline_far_warm_hot(store):
    await _seed_game(store, status="scheduled", hours_from_now=40 / 60)  # hot (<60m)
    await _seed_game(store, status="scheduled", hours_from_now=1.5)  # warm (1-2h)
    await _seed_game(
        store, status="scheduled", hours_from_now=3.0
    )  # far (>2h) → excluded
    result = await queries.active_game_tiers(store)
    assert result == {"hot", "warm"}


@pytest.mark.asyncio
async def test_active_game_tiers_excludes_post_tipoff_games(store):
    # The live tier was removed 2026-08-27 — in-progress games are unpollable
    # and must not put any scraper expectation on the /live coverage view.
    await _seed_game(store, status="live", hours_from_now=-0.5)
    result = await queries.active_game_tiers(store)
    assert result == set()


@pytest.mark.asyncio
async def test_coverage_matrix_fresh_within_threshold(store):
    from oddsiq.db.models import OddsSnapshot
    from datetime import datetime, timezone

    hot_id = await _seed_game(store, status="scheduled", hours_from_now=10 / 60)
    snap = OddsSnapshot(
        game_id=hot_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.85,
        captured_at=datetime.now(timezone.utc),
    )
    await store.insert_odds_snapshot(snap)
    rows = await queries.coverage_matrix(store, scrapers=["pinnacle"])
    cell = next(r for r in rows if r["bookmaker"] == "pinnacle")
    assert cell["state"] == "fresh"


@pytest.mark.asyncio
async def test_coverage_matrix_missing_when_no_snapshots(store):
    await _seed_game(store, status="scheduled", hours_from_now=10 / 60)
    rows = await queries.coverage_matrix(store, scrapers=["pinnacle"])
    cell = next(r for r in rows if r["bookmaker"] == "pinnacle")
    assert cell["state"] == "missing"
    assert cell["age_s"] is None


@pytest.mark.asyncio
async def test_coverage_matrix_stale_in_hot_tier_after_5min(store):
    from oddsiq.db.models import OddsSnapshot
    from datetime import datetime, timedelta, timezone

    hot_id = await _seed_game(store, status="scheduled", hours_from_now=10 / 60)
    snap = OddsSnapshot(
        game_id=hot_id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        odds=2.05,
        captured_at=datetime.now(timezone.utc) - timedelta(minutes=6),
    )
    await store.insert_odds_snapshot(snap)
    rows = await queries.coverage_matrix(store, scrapers=["tipsport"])
    cell = next(r for r in rows if r["bookmaker"] == "tipsport")
    assert cell["state"] == "stale"


@pytest.mark.asyncio
async def test_coverage_matrix_fresh_in_warm_tier_at_10min(store):
    """Warm tier stale threshold is 15 min, so a 10-min-old snapshot is still fresh."""
    from oddsiq.db.models import OddsSnapshot
    from datetime import datetime, timedelta, timezone

    # 90 minutes = 5400s > 3600s → warm tier (not hot)
    warm_id = await _seed_game(store, status="scheduled", hours_from_now=1.5)
    snap = OddsSnapshot(
        game_id=warm_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.85,
        captured_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    await store.insert_odds_snapshot(snap)
    rows = await queries.coverage_matrix(store, scrapers=["pinnacle"])
    cell = next(r for r in rows if r["bookmaker"] == "pinnacle")
    assert cell["state"] == "fresh"


@pytest.mark.asyncio
async def test_coverage_matrix_excludes_live_past_backstop(store):
    """A live game past its sport's live_backstop_hours is no longer scouted."""
    await _seed_game(store, status="live", hours_from_now=-4.0)
    rows = await queries.coverage_matrix(store, scrapers=["pinnacle", "tipsport"])
    assert rows == []


@pytest.mark.asyncio
async def test_recent_value_bets_returns_n_most_recent(store):
    gid = await _seed_game(store)
    # Seed 12 bets with descending flagged_at
    base = datetime.now(timezone.utc)
    for i in range(12):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            # Each row gets a distinct selection so no two rows share
            # the same (game_id, bookmaker, market_type, selection,
            # IFNULL(line,-999), tier) unique key.
            selection=f"sel_{i}",
            entry_odds=2.0 + i * 0.01,
            fair_odds=1.9 + i * 0.01,
            edge_pct=3.0 + i * 0.1,
            tier="hot",
        )
        vb.flagged_at = base - timedelta(minutes=i)
        vb.line = float(i) if i > 5 else None
        await store.insert_value_bet(vb)
    rows = await queries.recent_value_bets(store, limit=10)
    assert len(rows) == 10
    # Descending order by flagged_at
    for a, b in zip(rows, rows[1:]):
        assert a["flagged_at"] >= b["flagged_at"]


@pytest.mark.asyncio
async def test_recent_value_bets_includes_game_team_names(store):
    gid = await _seed_game(store)
    vb = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.0,
        tier="hot",
    )
    await store.insert_value_bet(vb)
    rows = await queries.recent_value_bets(store, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["home_team"] == "Boston Celtics"
    assert r["away_team"] == "Miami Heat"
    assert r["bookmaker"] == "tipsport"
    assert r["market_type"] == "moneyline"
    assert r["selection"] == "home"
    assert r["edge_pct"] == 5.0
    assert r["clv_pct"] is None
    assert r["result"] is None


@pytest.mark.asyncio
async def test_recent_value_bets_includes_sport(store):
    gid = await _seed_game(store, sport="nhl")
    await _seed_value_bet(store, gid, sport="nhl")
    rows = await queries.recent_value_bets(store, limit=10)
    assert len(rows) == 1
    assert rows[0]["sport"] == "nhl"


@pytest.mark.asyncio
async def test_recent_db_events_returns_value_bet_and_snapshot_rows(store):
    from oddsiq.db.models import OddsSnapshot

    gid = await _seed_game(store)
    # 1 value bet
    vb = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.0,
        tier="hot",
    )
    await store.insert_value_bet(vb)
    # 1 snapshot
    snap = OddsSnapshot(
        game_id=gid,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.85,
        captured_at=datetime.now(timezone.utc),
    )
    await store.insert_odds_snapshot(snap)
    events = await queries.recent_db_events(store, limit=25)
    types = {e["type"] for e in events}
    assert "value_bet" in types
    assert "snapshot" in types


@pytest.mark.asyncio
async def test_recent_db_events_ordered_desc_by_ts(store):
    gid = await _seed_game(store)
    base = datetime.now(timezone.utc)
    for i in range(5):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection=f"sel_{i}",
            entry_odds=2.05,
            fair_odds=1.95,
            edge_pct=3.5,
            tier="hot",
        )
        vb.flagged_at = base - timedelta(minutes=i)
        await store.insert_value_bet(vb)
    events = await queries.recent_db_events(store, limit=25)
    # value_bet events should be ordered desc
    vbs = [e for e in events if e["type"] == "value_bet"]
    for a, b in zip(vbs, vbs[1:]):
        assert a["ts"] >= b["ts"]


@pytest.mark.asyncio
async def test_big_numbers_empty_db(store):
    n = await queries.big_numbers(store)
    assert n["bets_total"] == 0
    assert n["bets_resolved"] == 0
    assert n["bets_with_clv"] == 0
    assert n["avg_clv"] is None
    assert n["avg_edge_on_clv_subset"] is None
    assert n["win_rate"] is None


@pytest.mark.asyncio
async def test_big_numbers_uses_same_subset_for_edge_and_clv(store):
    gid = await _seed_game(store)
    # 5 bets, 3 with clv_pct populated
    base_edge = [4.0, 5.0, 6.0, 7.0, 8.0]
    base_clv = [3.5, 5.5, 6.5, None, None]
    for i, (edge, clv) in enumerate(zip(base_edge, base_clv)):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection=f"sel_{i}",
            entry_odds=2.05,
            fair_odds=1.95,
            edge_pct=edge,
            tier="hot",
        )
        bet_id = await store.insert_value_bet(vb)
        if clv is not None and bet_id is not None:
            await store.update_value_bet_closing(
                bet_id, closing_odds=2.0, pinnacle_closing=1.95, clv_pct=clv
            )
    n = await queries.big_numbers(store)
    assert n["bets_total"] == 5
    assert n["bets_with_clv"] == 3
    # avg edge on CLV subset = (4 + 5 + 6) / 3 = 5.0
    assert n["avg_edge_on_clv_subset"] == 5.0
    # avg CLV = (3.5 + 5.5 + 6.5) / 3 = 5.166...
    assert n["avg_clv"] == 5.17
    # Δ = avg_edge_subset - avg_clv = 5.0 - 5.17 = -0.17
    assert n["delta"] == -0.17
    # |Δ| < 1.0 → edge_approx_clv is True
    assert n["edge_approx_clv"] is True


@pytest.mark.asyncio
async def test_big_numbers_win_rate(store):
    gid = await _seed_game(store)
    for i, result in enumerate(["win", "win", "loss", None]):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection=f"sel_{i}",
            entry_odds=2.0,
            fair_odds=1.9,
            edge_pct=5.0,
            tier="hot",
        )
        bet_id = await store.insert_value_bet(vb)
        if result is not None and bet_id is not None:
            await store.update_value_bet_result(bet_id, result)
    n = await queries.big_numbers(store)
    assert n["bets_total"] == 4
    assert n["bets_resolved"] == 3
    assert n["n_win"] == 2
    # 2/3 = 66.7%
    assert n["win_rate"] == 66.7


@pytest.mark.asyncio
async def test_analysis_aggregates_dedupe_warm_and_hot_rows(store):
    """`tier` is part of the value-bet dedup key, so one opportunity can have
    separate warm + hot rows. Performance aggregates must count it ONCE — not
    double-count ROI / win-rate / P&L / calibration."""
    gid = await _seed_game(store)
    for tier in ("warm", "hot"):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=2.0,
            fair_odds=1.9,
            edge_pct=6.0,
            tier=tier,
        )
        bet_id = await store.insert_value_bet(vb)
        assert bet_id is not None  # both rows persist (tier in the unique key)
        await store.update_value_bet_result(bet_id, "win")
        await store.update_value_bet_closing(
            bet_id, closing_odds=2.0, pinnacle_closing=1.9, clv_pct=4.0
        )

    big = await queries.big_numbers(store)
    assert big["bets_total"] == 1  # not 2
    assert big["n_win"] == 1

    cal = await queries.edge_calibration(store)
    assert sum(b["n"] for b in cal) == 1  # not 2

    dist = await queries.edge_distribution(store)
    assert sum(b["count"] for b in dist) == 1  # not 2

    # per_book_scoreboard only lists books with odds_snapshots coverage.
    await store.insert_odds_snapshot(
        OddsSnapshot(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            odds=2.0,
        )
    )
    board = await queries.per_book_scoreboard(store)
    tip = next(r for r in board if r["bookmaker"] == "tipsport")
    assert tip["bets"] == 1  # not 2


@pytest.mark.asyncio
async def test_big_numbers_win_rate_excludes_draws(store):
    # A moneyline tie returns the stake — it is neither a win nor a loss, so it
    # must NOT dilute win-rate. Denominator is decided bets (win+loss) only.
    gid = await _seed_game(store)
    for i, result in enumerate(["win", "win", "loss", "draw"]):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection=f"sel_{i}",
            entry_odds=2.0,
            fair_odds=1.9,
            edge_pct=5.0,
            tier="hot",
        )
        bet_id = await store.insert_value_bet(vb)
        await store.update_value_bet_result(bet_id, result)
    n = await queries.big_numbers(store)
    assert n["bets_resolved"] == 4  # the draw IS resolved...
    assert n["n_win"] == 2
    assert n["n_draw"] == 1
    # ...but excluded from the rate: 2 / (2 win + 1 loss) = 66.7%, NOT 2/4 = 50%
    assert n["win_rate"] == 66.7


@pytest.mark.asyncio
async def test_clv_trend_empty_db(store):
    series = await queries.clv_trend(store)
    assert series == {"labels": [], "nightly": [], "rolling7": []}


@pytest.mark.asyncio
async def test_clv_trend_nightly_and_rolling(store):
    gid = await _seed_game(store)
    # Seed bets across 10 days with varying CLV. Use day 0 = today, day -9 = oldest.
    base = datetime.now(timezone.utc)
    for day_offset in range(10):
        flagged = base - timedelta(days=day_offset)
        # 2 bets per day for nightly avg to be a real average
        for i in range(2):
            vb = ValueBet(
                game_id=gid,
                bookmaker="tipsport",
                market_type="moneyline",
                selection=f"sel_{day_offset}_{i}",
                entry_odds=2.05,
                fair_odds=1.95,
                edge_pct=5.0,
                tier="hot",
            )
            vb.flagged_at = flagged
            bet_id = await store.insert_value_bet(vb)
            if bet_id is not None:
                await store.update_value_bet_closing(
                    bet_id,
                    closing_odds=2.0,
                    pinnacle_closing=1.95,
                    clv_pct=4.0 + day_offset * 0.2,
                )
    series = await queries.clv_trend(store)
    assert len(series["labels"]) == 10
    assert len(series["nightly"]) == 10
    assert len(series["rolling7"]) == 10
    # Labels are ascending dates (oldest first)
    assert series["labels"] == sorted(series["labels"])
    # Rolling at last index = mean of nightly[-7:]
    rolling_expected = round(sum(series["nightly"][-7:]) / 7, 2)
    assert series["rolling7"][-1] == rolling_expected


@pytest.mark.asyncio
async def test_clv_trend_rolling7_is_calendar_window_not_point_window(store):
    """rolling7 averages over the trailing 7 CALENDAR days, not the last 7 data
    points — a gap in betting days must not silently widen the window."""
    gid = await _seed_game(store)
    base = datetime.now(timezone.utc)
    # Three days with gaps: T-10, T-9, T(today). Distinct CLV per day.
    for day_offset, clv in ((10, 10.0), (9, 9.0), (0, 1.0)):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection=f"sel_{day_offset}",
            entry_odds=2.05,
            fair_odds=1.95,
            edge_pct=5.0,
            tier="hot",
        )
        vb.flagged_at = base - timedelta(days=day_offset)
        bet_id = await store.insert_value_bet(vb)
        if bet_id is not None:
            await store.update_value_bet_closing(
                bet_id,
                closing_odds=2.0,
                pinnacle_closing=1.95,
                clv_pct=clv,
            )
    series = await queries.clv_trend(store)
    # Labels are the FULL calendar range (T-10 .. T = 11 days) so betting gaps
    # render as visible holes instead of a silently compressed x-axis (the
    # honesty fix for the 2026-06-20..07-06 outage). Data days: first, second, last.
    assert len(series["labels"]) == 11
    assert series["nightly"][0] == 10.0 and series["nightly"][1] == 9.0
    assert series["nightly"][-1] == 1.0
    assert all(v is None for v in series["nightly"][2:-1])  # gap days are null
    # T(last): only itself within 7 calendar days -> 1.0 (NOT the mean of all 3
    # that a last-7-points window would give). T-9: T-10 & T-9 -> 9.5. T-10: 10.0.
    assert series["rolling7"][0] == 10.0 and series["rolling7"][1] == 9.5
    assert series["rolling7"][-1] == 1.0
    assert all(v is None for v in series["rolling7"][2:-1])


@pytest.mark.asyncio
async def test_clv_trend_respects_days_filter(store):
    gid = await _seed_game(store)
    base = datetime.now(timezone.utc)
    # 1 bet today, 1 bet 10 days ago
    for day_offset in (0, 10):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection=f"sel_{day_offset}",
            entry_odds=2.0,
            fair_odds=1.9,
            edge_pct=5.0,
            tier="hot",
        )
        vb.flagged_at = base - timedelta(days=day_offset)
        bet_id = await store.insert_value_bet(vb)
        if bet_id is not None:
            await store.update_value_bet_closing(
                bet_id,
                closing_odds=2.0,
                pinnacle_closing=1.95,
                clv_pct=3.0,
            )
    # Labels span the full calendar range (11 days); only 2 carry data.
    series_all = await queries.clv_trend(store)
    assert len(series_all["labels"]) == 11
    assert sum(1 for v in series_all["nightly"] if v is not None) == 2
    series_7 = await queries.clv_trend(store, days=7)
    assert len(series_7["labels"]) == 1
    # days <= 0 must behave like all-time, not build a broken modifier that
    # silently empties the chart.
    series_zero = await queries.clv_trend(store, days=0)
    assert len(series_zero["labels"]) == 11
    series_neg = await queries.clv_trend(store, days=-5)
    assert len(series_neg["labels"]) == 11


@pytest.mark.asyncio
async def test_per_book_scoreboard_empty_db(store):
    rows = await queries.per_book_scoreboard(store)
    assert rows == []


@pytest.mark.asyncio
async def test_per_book_scoreboard_excludes_sharp_reference(store):
    """The /analysis scoreboard shows SOFT books only (2026-08-08): the
    Pinnacle reference row was dead weight — always 0 bets, — edge, — CLV;
    its coverage stats live on /live's coverage matrix."""
    from oddsiq.db.models import OddsSnapshot

    gid = await _seed_game(store)
    for i in range(2):
        await store.insert_odds_snapshot(
            OddsSnapshot(
                game_id=gid,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home" if i == 0 else "away",
                odds=2.0 + i * 0.1,
                captured_at=datetime.now(timezone.utc),
            )
        )
    rows = await queries.per_book_scoreboard(store)
    assert rows == []


@pytest.mark.asyncio
async def test_per_book_scoreboard_soft_book_metrics(store):
    from oddsiq.db.models import OddsSnapshot

    gid = await _seed_game(store)
    # tipsport: 1 snap + 1 bet with clv
    await store.insert_odds_snapshot(
        OddsSnapshot(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            odds=2.05,
            captured_at=datetime.now(timezone.utc),
        )
    )
    vb = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.0,
        tier="hot",
    )
    bet_id = await store.insert_value_bet(vb)
    await store.update_value_bet_closing(
        bet_id, closing_odds=2.0, pinnacle_closing=1.95, clv_pct=4.0
    )
    # pinnacle snapshots exist but the sharp reference stays off the board
    await store.insert_odds_snapshot(
        OddsSnapshot(
            game_id=gid,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="away",
            odds=2.20,
            captured_at=datetime.now(timezone.utc),
        )
    )
    rows = await queries.per_book_scoreboard(store)
    by_book = {r["bookmaker"]: r for r in rows}
    assert set(by_book) == {"tipsport"}
    assert by_book["tipsport"]["bets"] == 1
    assert by_book["tipsport"]["avg_edge"] == 5.0
    assert by_book["tipsport"]["avg_clv"] == 4.0


@pytest.mark.asyncio
async def test_edge_distribution_empty_db(store):
    dist = await queries.edge_distribution(store)
    assert dist == [
        {"bucket": "3-5%", "count": 0},
        {"bucket": "5-7%", "count": 0},
        {"bucket": "7-10%", "count": 0},
        {"bucket": "10%+", "count": 0},
    ]


@pytest.mark.asyncio
async def test_edge_distribution_buckets(store):
    gid = await _seed_game(store)
    edges = [
        3.5,
        4.9,  # bucket 3-5%: 2
        5.0,
        6.0,
        6.9,  # bucket 5-7%: 3
        7.0,
        9.0,  # bucket 7-10%: 2
        10.0,
        12.0,
        15.0,  # bucket 10%+:  3
    ]
    for i, edge in enumerate(edges):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection=f"sel_{i}",
            entry_odds=2.0,
            fair_odds=1.9,
            edge_pct=edge,
            tier="hot",
        )
        await store.insert_value_bet(vb)
    dist = await queries.edge_distribution(store)
    counts = {d["bucket"]: d["count"] for d in dist}
    assert counts == {"3-5%": 2, "5-7%": 3, "7-10%": 2, "10%+": 3}


@pytest.mark.asyncio
async def test_edge_distribution_excludes_below_threshold(store):
    gid = await _seed_game(store)
    for i, edge in enumerate([2.0, 2.99, 3.0]):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection=f"sel_{i}",
            entry_odds=2.0,
            fair_odds=1.9,
            edge_pct=edge,
            tier="hot",
        )
        await store.insert_value_bet(vb)
    dist = await queries.edge_distribution(store)
    counts = {d["bucket"]: d["count"] for d in dist}
    assert counts["3-5%"] == 1  # only the 3.0 row


@pytest.mark.asyncio
async def test_edge_calibration_empty_db(store):
    rows = await queries.edge_calibration(store)
    # No resolved bets → no rows
    assert rows == []


@pytest.mark.asyncio
async def test_edge_calibration_marks_thin_samples(store):
    gid = await _seed_game(store)
    # bucket 3-5%: 12 resolved bets (NOT thin)
    # bucket 7-10%: 9 resolved bets (thin)
    # bucket 10%+: 3 resolved bets (thin)
    plan = [
        (3.5, "win", 12),
        (8.0, "loss", 9),
        (15.0, "win", 3),
    ]
    sel_counter = 0
    for edge, result, count in plan:
        for _ in range(count):
            vb = ValueBet(
                game_id=gid,
                bookmaker="tipsport",
                market_type="moneyline",
                selection=f"sel_{sel_counter}",
                entry_odds=2.0,
                fair_odds=1.9,
                edge_pct=edge,
                tier="hot",
            )
            bet_id = await store.insert_value_bet(vb)
            if bet_id is not None:
                await store.update_value_bet_result(bet_id, result)
            sel_counter += 1
    rows = await queries.edge_calibration(store)
    by = {r["bucket"]: r for r in rows}
    assert by["3-5%"]["n"] == 12
    assert by["3-5%"]["thin"] is False
    assert by["7-10%"]["n"] == 9
    assert by["7-10%"]["thin"] is True
    assert by["10%+"]["n"] == 3
    assert by["10%+"]["thin"] is True


@pytest.mark.asyncio
async def test_edge_calibration_expected_vs_actual_win(store):
    """With entry_odds=2.0, expected win = 50%. 6 wins / 10 bets = 60% actual."""
    gid = await _seed_game(store)
    for i in range(10):
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection=f"sel_{i}",
            entry_odds=2.0,
            fair_odds=1.9,
            edge_pct=4.0,
            tier="hot",
        )
        bet_id = await store.insert_value_bet(vb)
        if bet_id is not None:
            await store.update_value_bet_result(bet_id, "win" if i < 6 else "loss")
    rows = await queries.edge_calibration(store)
    r = next(r for r in rows if r["bucket"] == "3-5%")
    assert r["n"] == 10
    assert r["expected_win"] == 50.0
    assert r["actual_win"] == 60.0
    assert r["delta"] == 10.0
    assert r["thin"] is False


@pytest.mark.asyncio
async def test_big_numbers_avg_odds_only_resolved(store):
    """avg_odds (folded into big numbers when results-detail was removed,
    2026-08-08) is averaged only over RESOLVED bets, not all bets."""
    gid = await _seed_game(store)
    # Bet 1: resolved, odds 2.00
    vb1 = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.00,
        fair_odds=1.9,
        edge_pct=5.0,
        tier="hot",
    )
    bid1 = await store.insert_value_bet(vb1)
    await store.update_value_bet_result(bid1, "win")
    # Bet 2: unresolved, odds 5.00 — should NOT affect avg_odds
    vb2 = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="away",
        entry_odds=5.00,
        fair_odds=4.0,
        edge_pct=20.0,
        tier="hot",
    )
    await store.insert_value_bet(vb2)
    n = await queries.big_numbers(store)
    assert n["avg_odds"] == 2.00  # only the resolved bet's odds

    empty_check = await queries.big_numbers(store, sport="mlb")
    assert empty_check["avg_odds"] is None  # no resolved bets -> None, not 0


# ---------------------------------------------------------------------------
# Helper: seed game + value_bet in one call (for sport-filter tests)
# ---------------------------------------------------------------------------

_seed_counter = 0


async def _seed_vb(
    store, *, sport="nba", edge_pct=5.0, result=None, clv_pct=None, hours_from_now=2.0
):
    """Seed a Game + ValueBet with the given sport and return the bet id."""
    global _seed_counter
    _seed_counter += 1
    g = Game(
        external_id=f"sport-filter-{sport}-{_seed_counter}",
        home_team="Home Team",
        away_team="Away Team",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=hours_from_now),
        status="scheduled",
        sport=sport,
    )
    gid = await store.upsert_game(g)
    vb = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=edge_pct,
        tier="hot",
        sport=sport,
    )
    bet_id = await store.insert_value_bet(vb)
    if result is not None and bet_id is not None:
        await store.update_value_bet_result(bet_id, result)
    if clv_pct is not None and bet_id is not None:
        await store.update_value_bet_closing(
            bet_id, closing_odds=2.0, pinnacle_closing=1.95, clv_pct=clv_pct
        )
    return bet_id


# ---------------------------------------------------------------------------
# Sport-filter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_strip_filters_by_sport(store):
    await _seed_vb(store, sport="nba", edge_pct=5.0)
    await _seed_vb(store, sport="nhl", edge_pct=9.0)
    assert (await queries.status_strip(store, sport="nba"))["flags_today"] == 1
    assert (await queries.status_strip(store, sport="nhl"))["flags_today"] == 1
    assert (await queries.status_strip(store))["flags_today"] == 2  # no filter = all


@pytest.mark.asyncio
async def test_status_strip_sport_all_is_unfiltered(store):
    await _seed_vb(store, sport="nba")
    await _seed_vb(store, sport="mlb")
    assert (await queries.status_strip(store, sport="all"))["flags_today"] == 2


@pytest.mark.asyncio
async def test_status_strip_games_count_filtered_by_sport(store):
    """games_total respects the sport filter too."""
    await _seed_game(store, status="scheduled", sport="nba")
    await _seed_game(store, status="scheduled", sport="mlb")
    # Fresh DB: exactly 1 NBA + 1 MLB scheduled game.
    res_nba = await queries.status_strip(store, sport="nba")
    assert res_nba["games_scheduled"] == 1  # mlb game must not appear
    res_mlb = await queries.status_strip(store, sport="mlb")
    assert res_mlb["games_scheduled"] == 1
    res_all = await queries.status_strip(store)
    assert res_all["games_scheduled"] == 2  # both, unfiltered


@pytest.mark.asyncio
async def test_recent_value_bets_filters_by_sport(store):
    await _seed_vb(store, sport="nba")
    await _seed_vb(store, sport="mlb")
    assert len(await queries.recent_value_bets(store, sport="mlb")) == 1
    assert len(await queries.recent_value_bets(store)) == 2


@pytest.mark.asyncio
async def test_recent_value_bets_sport_none_is_unfiltered(store):
    await _seed_vb(store, sport="nba")
    await _seed_vb(store, sport="nhl")
    assert len(await queries.recent_value_bets(store, sport=None)) == 2


@pytest.mark.asyncio
async def test_big_numbers_and_edge_distribution_filter_by_sport(store):
    await _seed_vb(store, sport="nba", edge_pct=6.0)
    await _seed_vb(store, sport="nhl", edge_pct=8.0)
    assert (await queries.big_numbers(store, sport="nba"))["bets_total"] == 1
    dist_nhl = await queries.edge_distribution(store, sport="nhl")
    assert sum(b["count"] for b in dist_nhl) == 1


@pytest.mark.asyncio
async def test_big_numbers_sport_all_is_unfiltered(store):
    await _seed_vb(store, sport="nba")
    await _seed_vb(store, sport="mlb")
    assert (await queries.big_numbers(store, sport="all"))["bets_total"] == 2


@pytest.mark.asyncio
async def test_edge_distribution_sport_filters_buckets(store):
    await _seed_vb(store, sport="nba", edge_pct=4.0)  # 3-5% bucket
    await _seed_vb(store, sport="nba", edge_pct=8.0)  # 7-10% bucket
    await _seed_vb(store, sport="nhl", edge_pct=11.0)  # 10%+ bucket
    nba_dist = await queries.edge_distribution(store, sport="nba")
    assert sum(b["count"] for b in nba_dist) == 2
    nhl_dist = await queries.edge_distribution(store, sport="nhl")
    assert sum(b["count"] for b in nhl_dist) == 1


@pytest.mark.asyncio
async def test_per_book_scoreboard_filters_by_sport(store):
    from oddsiq.db.models import OddsSnapshot

    gid_nba = await _seed_game(store, sport="nba")
    gid_nhl = await _seed_game(store, sport="nhl")
    await store.insert_odds_snapshot(
        OddsSnapshot(
            game_id=gid_nba,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            odds=2.05,
            captured_at=datetime.now(timezone.utc),
            sport="nba",
        )
    )
    await store.insert_odds_snapshot(
        OddsSnapshot(
            game_id=gid_nhl,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            odds=2.10,
            captured_at=datetime.now(timezone.utc),
            sport="nhl",
        )
    )
    # Value bets
    vb_nba = ValueBet(
        game_id=gid_nba,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.0,
        tier="hot",
        sport="nba",
    )
    await store.insert_value_bet(vb_nba)
    rows_nba = await queries.per_book_scoreboard(store, sport="nba")
    assert len(rows_nba) == 1
    assert rows_nba[0]["bets"] == 1

    rows_nhl = await queries.per_book_scoreboard(store, sport="nhl")
    assert len(rows_nhl) == 1
    assert rows_nhl[0]["bets"] == 0  # no value bet for nhl

    rows_all = await queries.per_book_scoreboard(store)
    # only one bookmaker ("tipsport") in total, with 2 snaps + 1 bet
    assert len(rows_all) == 1
    assert rows_all[0]["snapshots"] == 2
    assert rows_all[0]["bets"] == 1


@pytest.mark.asyncio
async def test_edge_calibration_filters_by_sport(store):
    # nba bets: 10 resolved in 3-5% bucket
    for i in range(10):
        bid = await _seed_vb(store, sport="nba", edge_pct=4.0)
        await store.update_value_bet_result(bid, "win" if i < 6 else "loss")
    # nhl bets: 5 resolved in 5-7% bucket
    for i in range(5):
        bid = await _seed_vb(store, sport="nhl", edge_pct=6.0)
        await store.update_value_bet_result(bid, "loss")
    rows_nba = await queries.edge_calibration(store, sport="nba")
    assert len(rows_nba) == 1
    assert rows_nba[0]["bucket"] == "3-5%"
    assert rows_nba[0]["n"] == 10
    rows_nhl = await queries.edge_calibration(store, sport="nhl")
    assert len(rows_nhl) == 1
    assert rows_nhl[0]["bucket"] == "5-7%"
    assert rows_nhl[0]["n"] == 5


@pytest.mark.asyncio
async def test_recent_db_events_filters_by_sport(store):
    from oddsiq.db.models import OddsSnapshot

    gid_nba = await _seed_game(store, sport="nba")
    gid_nhl = await _seed_game(store, sport="nhl")
    # Insert value_bets of different sports
    vb_nba = ValueBet(
        game_id=gid_nba,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.0,
        tier="hot",
        sport="nba",
    )
    await store.insert_value_bet(vb_nba)
    vb_nhl = ValueBet(
        game_id=gid_nhl,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.10,
        fair_odds=1.98,
        edge_pct=6.0,
        tier="hot",
        sport="nhl",
    )
    await store.insert_value_bet(vb_nhl)
    # Insert snapshots for both sports
    await store.insert_odds_snapshot(
        OddsSnapshot(
            game_id=gid_nba,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="home",
            odds=1.85,
            captured_at=datetime.now(timezone.utc),
            sport="nba",
        )
    )
    await store.insert_odds_snapshot(
        OddsSnapshot(
            game_id=gid_nhl,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="home",
            odds=1.90,
            captured_at=datetime.now(timezone.utc),
            sport="nhl",
        )
    )
    # Filter to nba: expect 1 value_bet + 1 snapshot (pinnacle nba)
    events_nba = await queries.recent_db_events(store, sport="nba")
    vb_events = [e for e in events_nba if e["type"] == "value_bet"]
    snap_events = [e for e in events_nba if e["type"] == "snapshot"]
    assert len(vb_events) == 1
    assert len(snap_events) == 1
    # Unfiltered: 2 value_bets + 2 snapshot groups (one per sport/bookmaker)
    events_all = await queries.recent_db_events(store)
    assert len([e for e in events_all if e["type"] == "value_bet"]) == 2


@pytest.mark.asyncio
async def test_clv_trend_filters_by_sport(store):
    # 1 nba bet with CLV + 1 nhl bet with CLV
    await _seed_vb(store, sport="nba", edge_pct=5.0, clv_pct=4.0)
    await _seed_vb(store, sport="nhl", edge_pct=6.0, clv_pct=7.0)
    series_nba = await queries.clv_trend(store, sport="nba")
    assert len(series_nba["labels"]) == 1
    assert series_nba["nightly"][0] == 4.0
    series_nhl = await queries.clv_trend(store, sport="nhl")
    assert len(series_nhl["labels"]) == 1
    assert series_nhl["nightly"][0] == 7.0
    series_all = await queries.clv_trend(store)
    assert len(series_all["labels"]) == 1  # same day, so one date label
    assert series_all["nightly"][0] == 5.5  # avg(4.0, 7.0)


@pytest.mark.asyncio
async def test_active_game_tiers_filters_by_sport(store):
    # Hot nba game + hot nhl game — filter by nba should still return 'hot'
    await _seed_game(store, status="scheduled", hours_from_now=40 / 60, sport="nba")
    await _seed_game(store, status="scheduled", hours_from_now=40 / 60, sport="nhl")
    res_all = await queries.active_game_tiers(store)
    assert "hot" in res_all
    res_nba = await queries.active_game_tiers(store, sport="nba")
    assert "hot" in res_nba
    res_mlb = await queries.active_game_tiers(store, sport="mlb")
    assert "hot" not in res_mlb


@pytest.mark.asyncio
async def test_coverage_matrix_filters_by_sport(store):
    nba_id = await _seed_game(
        store, status="scheduled", hours_from_now=40 / 60, sport="nba"
    )
    nhl_id = await _seed_game(
        store, status="scheduled", hours_from_now=40 / 60, sport="nhl"
    )
    rows_nba = await queries.coverage_matrix(
        store, scrapers=["pinnacle", "tipsport"], sport="nba"
    )
    game_ids = {r["game_id"] for r in rows_nba}
    assert nba_id in game_ids
    assert nhl_id not in game_ids
    rows_all = await queries.coverage_matrix(store, scrapers=["pinnacle", "tipsport"])
    game_ids_all = {r["game_id"] for r in rows_all}
    assert {nba_id, nhl_id} == game_ids_all


@pytest.mark.asyncio
async def test_per_book_scoreboard_excludes_removed_books(store):
    # Legacy snapshot rows from a removed scraper (betano/allwyn/fortuna) must
    # NOT resurface in the per-book scoreboard — only current books appear.
    gid = await _seed_game(store)
    cap = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=gid,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="home",
                odds=1.90,
                captured_at=cap,
            ),
            OddsSnapshot(
                game_id=gid,
                bookmaker="betano",
                market_type="moneyline",
                selection="home",
                odds=2.00,
                captured_at=cap,
            ),
        ]
    )
    books = {r["bookmaker"] for r in await queries.per_book_scoreboard(store)}
    assert "tipsport" in books
    assert "betano" not in books


@pytest.mark.asyncio
async def test_recent_db_events_excludes_removed_books(store):
    # A removed scraper's leftover snapshot must not surface in the activity feed.
    gid = await _seed_game(store)
    cap = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=gid,
                bookmaker="betano",
                market_type="moneyline",
                selection="home",
                odds=2.00,
                captured_at=cap,
            ),
        ]
    )
    events = await queries.recent_db_events(store, limit=25)
    assert all(e.get("bookmaker") != "betano" for e in events)


@pytest.mark.asyncio
async def test_upcoming_games_groups_by_bet_window_and_day(store):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)  # 14:00 Prague (CEST)
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(minutes=30),
        sport="nba",
        home="Boston Celtics",
        away="Miami Heat",
    )  # hot  -> open
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=1, minutes=30),
        sport="mlb",
        home="New York Yankees",
        away="Boston Red Sox",
    )  # warm -> open
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=5),
        sport="nba",
        home="Los Angeles Lakers",
        away="Denver Nuggets",
    )  # far, today
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=20),
        sport="mlb",
        home="Chicago Cubs",
        away="St. Louis Cardinals",
    )  # far, tomorrow
    await _seed_upcoming(
        store, tipoff=now + timedelta(hours=26), sport="nba", home="X", away="Y"
    )  # beyond 24h -> excluded
    await _seed_upcoming(
        store, tipoff=now - timedelta(hours=1), sport="nba", home="Stale", away="Row"
    )  # past -> excluded

    result = await queries.upcoming_games(store, now=now)

    assert result["total"] == 4
    groups = {g["key"]: g for g in result["groups"]}
    assert set(groups) == {"open", "today", "tomorrow"}
    # open group ordered ascending by tipoff: hot (0.5h) then warm (1.5h)
    assert [g["window_label"] for g in groups["open"]["games"]] == ["HOT", "WARM"]
    assert groups["open"]["games"][0]["away"] == "Miami Heat"
    assert groups["open"]["games"][0]["home"] == "Boston Celtics"
    assert groups["open"]["label"] == "Bet window open"
    # today group: the 5h-out NBA game, far -> "opens HH:MM"
    assert len(groups["today"]["games"]) == 1
    assert groups["today"]["games"][0]["tier"] == "far"
    assert groups["today"]["games"][0]["window_label"].startswith("opens ")
    # tomorrow group: the 20h-out MLB game
    assert len(groups["tomorrow"]["games"]) == 1
    assert groups["tomorrow"]["games"][0]["display"] == "MLB"


@pytest.mark.asyncio
async def test_upcoming_games_tiles_cover_all_enabled_sports(store):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    await _seed_upcoming(
        store, tipoff=now + timedelta(hours=3), sport="nba", home="A", away="B"
    )
    await _seed_upcoming(
        store, tipoff=now + timedelta(hours=4), sport="nba", home="C", away="D"
    )
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=2, minutes=30),
        sport="mlb",
        home="E",
        away="F",
    )
    # no NHL games seeded
    result = await queries.upcoming_games(store, now=now)
    tiles = {t["sport"]: t for t in result["tiles"]}
    assert set(tiles) == {"nba", "nhl", "mlb", "wnba", "total"}
    assert tiles["nba"]["count"] == 2
    assert tiles["mlb"]["count"] == 1
    assert tiles["nhl"]["count"] == 0  # zero-game sport still present
    assert tiles["nhl"]["next_local"] is None
    assert tiles["wnba"]["count"] == 0
    assert tiles["wnba"]["display"] == "WNBA"
    assert tiles["total"]["count"] == 3
    assert tiles["nba"]["display"] == "NBA"
    assert (
        tiles["nba"]["next_local"] == "17:00"
    )  # earliest NBA (3h out) = 15:00 UTC -> 17:00 Prague


@pytest.mark.asyncio
async def test_upcoming_games_sport_filter_narrows_agenda_not_tiles(store):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=3),
        sport="nba",
        home="Boston Celtics",
        away="Miami Heat",
    )
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=3),
        sport="mlb",
        home="New York Yankees",
        away="Boston Red Sox",
    )
    result = await queries.upcoming_games(store, sport="nba", now=now)
    tiles = {t["sport"]: t for t in result["tiles"]}
    assert tiles["mlb"]["count"] == 1  # tiles stay all-sports (overview)
    assert result["total"] == 2
    agenda = [g for grp in result["groups"] for g in grp["games"]]
    assert len(agenda) == 1
    assert agenda[0]["sport"] == "nba"


@pytest.mark.asyncio
async def test_upcoming_games_excludes_live_and_final(store):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=3),
        sport="nba",
        status="live",
        home="A",
        away="B",
    )
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=3),
        sport="nba",
        status="final",
        home="C",
        away="D",
    )
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=3),
        sport="mlb",
        status="scheduled",
        home="E",
        away="F",
    )
    result = await queries.upcoming_games(store, now=now)
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_upcoming_games_countdown_and_window_labels(store):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    await _seed_upcoming(
        store, tipoff=now + timedelta(minutes=42), sport="nba", home="A", away="B"
    )  # hot
    await _seed_upcoming(
        store,
        tipoff=now + timedelta(hours=6, minutes=22),
        sport="mlb",
        home="C",
        away="D",
    )  # far
    result = await queries.upcoming_games(store, now=now)
    by_sport = {g["sport"]: g for grp in result["groups"] for g in grp["games"]}
    assert by_sport["nba"]["countdown"] == "in 42m"
    assert by_sport["nba"]["window_label"] == "HOT"
    assert by_sport["mlb"]["countdown"] == "in 6h 22m"
    assert by_sport["mlb"]["tier"] == "far"


# ---------------------------------------------------------------------------
# monthly_performance (/analysis monthly tabs)
# ---------------------------------------------------------------------------


async def _seed_month_vb(
    store,
    *,
    flagged_at,
    result=None,
    entry_odds=2.0,
    clv_pct=None,
    sport="mlb",
    tier="hot",
    selection="home",
):
    """Seed a Game + ValueBet flagged in a specific month; optionally settle it.
    Returns (game_id, bet_id)."""
    global _seed_counter
    _seed_counter += 1
    gid = await store.upsert_game(
        Game(
            external_id=f"monthly-{sport}-{_seed_counter}",
            home_team="Home Team",
            away_team="Away Team",
            scheduled_at=flagged_at + timedelta(hours=1),
            status="final",
            sport=sport,
        )
    )
    vb = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection=selection,
        entry_odds=entry_odds,
        fair_odds=entry_odds * 0.95,
        edge_pct=5.0,
        tier=tier,
        sport=sport,
    )
    vb.flagged_at = flagged_at
    bet_id = await store.insert_value_bet(vb)
    if result is not None:
        await store.update_value_bet_result(bet_id, result)
    if clv_pct is not None:
        await store.update_value_bet_closing(
            bet_id, closing_odds=2.0, pinnacle_closing=1.95, clv_pct=clv_pct
        )
    return gid, bet_id


@pytest.mark.asyncio
async def test_monthly_performance_groups_months_and_computes_profit(store):
    jun = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    aug = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    await _seed_month_vb(
        store, flagged_at=jun, result="win", entry_odds=2.5, clv_pct=4.0
    )
    await _seed_month_vb(store, flagged_at=jun, result="loss", clv_pct=2.0)
    await _seed_month_vb(store, flagged_at=aug, result="win", entry_odds=2.0)

    res = await queries.monthly_performance(store)
    # Calendar-true tabs: June through the CURRENT month, newest first; the
    # current month is selected by default even when it has no rows yet.
    now = datetime.now(timezone.utc)
    assert res["months"] == queries._month_range("2026-06", now)
    assert "2026-08" in res["months"]
    assert res["month"] == res["months"][0]
    res_aug = await queries.monthly_performance(store, month="2026-08")
    assert res_aug["bets"]["n_total"] == 1
    assert res_aug["bets"]["flat_pl"] == 1.0
    assert res_aug["bets"]["flat_roi_pct"] == 100.0

    res_jun = await queries.monthly_performance(store, month="2026-06")
    assert res_jun["month"] == "2026-06"
    b = res_jun["bets"]
    assert b["n_total"] == 2 and b["n_win"] == 1 and b["n_loss"] == 1
    assert b["flat_pl"] == 0.5  # +1.5 (win at 2.5) − 1.0 (loss)
    assert b["flat_roi_pct"] == 25.0
    assert b["avg_clv"] == 3.0  # (4.0 + 2.0) / 2

    # Garbage month param falls back to the newest month, never errors.
    res_bad = await queries.monthly_performance(store, month="not-a-month")
    assert res_bad["month"] == res["months"][0]  # unknown month -> current month


@pytest.mark.asyncio
async def test_monthly_performance_dedupes_tiers_and_hides_excluded(store):
    jun = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    gid, _ = await _seed_month_vb(store, flagged_at=jun, result="win", entry_odds=2.0)
    # Same opportunity re-detected in the warm tier: the store's dedup key
    # includes tier, so the row exists — monthly aggregates must collapse it.
    vb = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.1,
        fair_odds=2.0,
        edge_pct=5.0,
        tier="warm",
        sport="mlb",
    )
    vb.flagged_at = jun
    wid = await store.insert_value_bet(vb)
    assert wid is not None
    await store.update_value_bet_result(wid, "win")
    # A stamped row must not resurface in monthly stats.
    _, bad_bid = await _seed_month_vb(
        store, flagged_at=jun, result="win", entry_odds=3.0
    )
    await store._db.execute(
        "UPDATE value_bets SET excluded_reason='test_bad' WHERE id=?", (bad_bid,)
    )
    await store._db.commit()

    res = await queries.monthly_performance(store, month="2026-06")
    assert res["bets"]["n_total"] == 1
    assert res["bets"]["n_resolved"] == 1


@pytest.mark.asyncio
async def test_monthly_performance_sport_filter(store):
    jun = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    await _seed_month_vb(store, flagged_at=jun, result="win", sport="mlb")
    await _seed_month_vb(store, flagged_at=jun, result="win", sport="nba")
    res = await queries.monthly_performance(store, month="2026-06", sport="mlb")
    assert res["bets"]["n_total"] == 1


# ---------------------------------------------------------------------------
# Betting dashboard (/betting tab) — overview, placed-bets table, detections
# ---------------------------------------------------------------------------


async def _seed_placed(
    store,
    *,
    flagged_at=None,
    sport="mlb",
    status="placed",
    stake=20.0,
    accepted_odds=2.08,
    requested_odds=2.10,
    skip_reason=None,
    result=None,
    selection="home",
):
    """Seed a value bet + its placed_bets decision; optionally settle both."""
    from oddsiq.db.models import PlacedBet

    flagged_at = flagged_at or datetime.now(timezone.utc)
    gid, vb_id = await _seed_month_vb(
        store,
        flagged_at=flagged_at,
        sport=sport,
        entry_odds=requested_odds,
        selection=selection,
    )
    pb_id = await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb_id,
            game_id=gid,
            sport=sport,
            status=status,
            skip_reason=skip_reason,
            stake_czk=stake,
            requested_odds=requested_odds,
            accepted_odds=accepted_odds if status in ("placed", "settled") else None,
            balance_after_czk=None,
            latency_detect_ms=1500,
            latency_place_ms=900,
            commanded_at=flagged_at,
        )
    )
    if result is not None:
        await store.update_value_bet_result(vb_id, result)
        await store.settle_placed_bet(vb_id, result)
    return gid, vb_id, pb_id


@pytest.mark.asyncio
async def test_betting_overview_empty_db(store):
    o = await queries.betting_overview(store)
    assert o["at_risk_czk"] == 0.0
    assert o["realized_pl_czk"] == 0.0
    assert o["roi_pct"] is None  # no settled bets -> undefined, not 0
    assert o["balance_czk"] is None
    # Meta config surfaces defaults even when unset.
    assert o["armed"] is False
    assert o["flat_stake_czk"] == 20.0
    assert o["max_bets_per_day"] == 10
    assert o["betting_sports"] == ["mlb"]  # absent meta -> conservative default


@pytest.mark.asyncio
async def test_betting_overview_reads_betting_sports_meta(store):
    await store.set_meta("betting_sports", "nfl,mlb")
    o = await queries.betting_overview(store)
    assert o["betting_sports"] == ["mlb", "nfl"]  # sorted


@pytest.mark.asyncio
async def test_betting_overview_shows_effective_hard_clamped_caps(store, monkeypatch):
    from oddsiq import config

    monkeypatch.setattr(config, "BETTING_MAX_STAKE_CZK", 50.0)
    monkeypatch.setattr(config, "BETTING_MAX_BETS_PER_DAY", 2)
    monkeypatch.setattr(config, "BETTING_MAX_OPEN_STAKES_CZK", 75.0)
    monkeypatch.setattr(config, "BETTING_MAX_DAILY_LOSS_CZK", 40.0)
    for key in (
        "betting_flat_stake_czk",
        "betting_max_bets_per_day",
        "betting_max_open_stakes_czk",
        "betting_daily_loss_limit_czk",
    ):
        await store.set_meta(key, "999999")
    o = await queries.betting_overview(store)
    assert o["flat_stake_czk"] == 50.0
    assert o["max_bets_per_day"] == 2
    assert o["max_open_stakes_czk"] == 75.0
    assert o["daily_loss_limit_czk"] == 40.0


@pytest.mark.asyncio
async def test_betting_overview_kpis_and_meta(store):
    # one open placed bet (at risk) + one settled win + one settled loss
    await _seed_placed(store, status="placed", stake=50.0)
    _, _, pb_win = await _seed_placed(
        store, status="placed", stake=100.0, accepted_odds=2.0, result="win"
    )
    await _seed_placed(
        store, status="placed", stake=100.0, accepted_odds=2.0, result="loss"
    )
    # a settled bet carries a post-bet balance -> becomes the shown balance
    await store.update_placed_bet_result(
        pb_win, status="settled", balance_after_czk=1080.0
    )
    await store.set_meta("betting_armed", "1")
    await store.set_meta("betting_flat_stake_czk", "35")

    o = await queries.betting_overview(store)
    assert o["at_risk_czk"] == 50.0  # only the still-open bet
    assert o["realized_pl_czk"] == pytest.approx(0.0)  # +100 win, -100 loss
    assert o["roi_pct"] == pytest.approx(0.0)  # 0 / 200 staked
    assert o["armed"] is True
    assert o["flat_stake_czk"] == 35.0


@pytest.mark.asyncio
async def test_placed_bets_table_join_and_slippage(store):
    await _seed_placed(
        store, status="placed", requested_odds=2.10, accepted_odds=2.05, stake=20.0
    )
    rows = await queries.placed_bets_table(store, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["away_team"] and r["home_team"]  # joined to games
    assert r["selection_team"] == r["home_team"]  # selection resolved to a team
    assert r["requested_odds"] == 2.10 and r["accepted_odds"] == 2.05
    assert r["stake_czk"] == 20.0
    assert r["latency_detect_ms"] == 1500
    assert r["status"] == "placed"


@pytest.mark.asyncio
async def test_betting_overview_sport_filter(store):
    await _seed_placed(store, sport="mlb", status="placed", stake=20.0)
    await _seed_placed(store, sport="nba", status="placed", stake=99.0)
    o = await queries.betting_overview(store, sport="mlb")
    assert o["at_risk_czk"] == 20.0


@pytest.mark.asyncio
async def test_status_strip_counts_guarded_flags_today(store):
    """The strip must show guard-stamped detections alongside clean ones —
    all-Aug detections were guarded and the dashboard showed a dead detector."""
    gid = await _seed_game(store)
    now = datetime.now(timezone.utc)
    await _seed_value_bet(store, gid, flagged_at=now, selection="home")
    vb2 = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="away",
        entry_odds=3.1,
        fair_odds=2.7,
        edge_pct=12.0,
        flagged_at=now,
        tier="hot",
        sport="nba",
        excluded_reason="incoherent_book_pregame",
    )
    await store.insert_value_bet(vb2)
    s = await queries.status_strip(store)
    assert s["flags_today"] == 1
    assert s["suspect_today"] == 1


@pytest.mark.asyncio
async def test_recent_db_events_carry_matchup_and_tier(store):
    gid = await _seed_game(store)
    await _seed_value_bet(store, gid, selection="home")
    events = await queries.recent_db_events(store, limit=5)
    vb_events = [e for e in events if e["type"] == "value_bet"]
    assert vb_events, "expected a value_bet event"
    ev = vb_events[0]
    assert ev["home_team"] == "Boston Celtics"
    assert ev["away_team"] == "Miami Heat"
    assert "tier" in ev


@pytest.mark.asyncio
async def test_next_game_window_returns_upcoming_game(store):
    """Coverage-matrix empty state: 'next window opens at HH:MM' needs the
    next scheduled tipoff."""
    await _seed_game(store, hours_from_now=9.5)
    nw = await queries.next_game_window(store)
    assert nw is not None
    assert nw["away"] == "Miami Heat" and nw["home"] == "Boston Celtics"
    assert nw["opens_local"]  # Prague HH:MM of tipoff - 2h
    assert nw["tipoff_local"]


@pytest.mark.asyncio
async def test_next_game_window_none_when_no_scheduled_games(store):
    assert await queries.next_game_window(store) is None


@pytest.mark.asyncio
async def test_upcoming_tiles_hide_total_for_single_sport(store, monkeypatch):
    """With one enabled sport the TOTAL tile is pure duplication."""
    monkeypatch.setattr("oddsiq.dashboard.queries.ENABLED_SPORTS", ["mlb"])
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", ["mlb"])
    await _seed_game(store, hours_from_now=5.0, sport="mlb")
    board = await queries.upcoming_games(store)
    assert [t["sport"] for t in board["tiles"]] == ["mlb"]


@pytest.mark.asyncio
async def test_upcoming_games_carry_tipoff_iso_and_first_window(store):
    """Rows need the ISO tipoff for the client-side ticking countdown, and
    the board needs the first bet-window open time for the summary line."""
    await _seed_game(store, hours_from_now=9.0)
    board = await queries.upcoming_games(store)
    g = board["groups"][0]["games"][0]
    assert "tipoff_iso" in g and g["tipoff_iso"].startswith("20")
    assert board["first_window_opens_local"]


async def _seed_placed_bet(
    store,
    gid,
    vb_id,
    *,
    status="skipped",
    skip_reason="fair_too_old",
    stake=None,
    sport="nba",
):
    from oddsiq.db.models import PlacedBet

    pb = PlacedBet(
        value_bet_id=vb_id,
        game_id=gid,
        sport=sport,
        status=status,
        skip_reason=skip_reason if status == "skipped" else None,
        stake_czk=stake,
        requested_odds=2.05,
        commanded_at=datetime.now(timezone.utc),
    )
    return await store.insert_placed_bet(pb)


@pytest.mark.asyncio
async def test_placed_bets_table_includes_skipped_decisions(store):
    """The ledger is a DECISION ledger: a skip (with its reason) must be as
    visible as a placement — 'why didn't it bet?' is the operator's question."""
    gid = await _seed_game(store)
    vb_id = await _seed_value_bet(store, gid, selection="home")
    await _seed_placed_bet(
        store, gid, vb_id, status="skipped", skip_reason="fair_too_old"
    )
    rows = await queries.placed_bets_table(store)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"
    assert rows[0]["skip_reason"] == "fair_too_old"


@pytest.mark.asyncio
async def test_betting_overview_includes_day_caps_and_decisions(store):
    gid = await _seed_game(store)
    now = datetime.now(timezone.utc)
    vb_id = await _seed_value_bet(store, gid, flagged_at=now, selection="home")
    vb2 = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="away",
        entry_odds=3.1,
        fair_odds=2.7,
        edge_pct=12.0,
        flagged_at=now,
        tier="hot",
        sport="nba",
        excluded_reason="incoherent_book_pregame",
    )
    await store.insert_value_bet(vb2)
    await _seed_placed_bet(store, gid, vb_id, status="skipped")
    o = await queries.betting_overview(store)
    assert o["day"]["bets_commanded"] == 0  # skips don't consume the cap
    assert o["today"]["detections"] == 1
    assert o["today"]["guarded"] == 1
    assert o["today"]["skipped"] == 1
    assert o["today"]["placed"] == 0


@pytest.mark.asyncio
async def test_recent_value_bets_carry_odds_tier_url_and_game_id(store):
    gid = await _seed_game(store)
    await _seed_value_bet(store, gid, selection="home")
    rows = await queries.recent_value_bets(store, limit=5)
    r = rows[0]
    assert r["entry_odds"] == 2.05
    assert "tier" in r
    assert "tipsport_url" in r
    assert r["game_id"] == gid


@pytest.mark.asyncio
async def test_recent_value_bets_optionally_include_suspect(store):
    gid = await _seed_game(store)
    await _seed_value_bet(store, gid, selection="home")
    vb2 = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="away",
        entry_odds=3.1,
        fair_odds=2.7,
        edge_pct=12.0,
        flagged_at=datetime.now(timezone.utc),
        tier="hot",
        sport="nba",
        excluded_reason="incoherent_book_pregame",
    )
    await store.insert_value_bet(vb2)
    clean = await queries.recent_value_bets(store, limit=5)
    assert len(clean) == 1 and clean[0]["excluded_reason"] is None
    both = await queries.recent_value_bets(store, limit=5, include_suspect=True)
    assert len(both) == 2
    assert any(r["excluded_reason"] == "incoherent_book_pregame" for r in both)


async def _seed_resolved_bet(
    store, gid, *, flagged_at, result, entry_odds=2.0, selection="home", sport="nba"
):
    vb = ValueBet(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection=selection,
        entry_odds=entry_odds,
        fair_odds=1.9,
        edge_pct=5.0,
        flagged_at=flagged_at,
        tier="hot",
        sport=sport,
    )
    bet_id = await store.insert_value_bet(vb)
    await store.update_value_bet_result(bet_id, result)
    return bet_id


@pytest.mark.asyncio
async def test_profit_curve_cumulative_units_over_calendar(store):
    """The equity curve: cumulative flat-1u P&L by flag date, expanded to the
    full calendar range (gaps carry null; the chart bridges them)."""
    gid = await _seed_game(store)
    d1 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    d3 = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    await _seed_resolved_bet(
        store, gid, flagged_at=d1, result="win", entry_odds=2.5, selection="home"
    )
    await _seed_resolved_bet(store, gid, flagged_at=d3, result="loss", selection="away")
    curve = await queries.profit_curve(store)
    assert curve["labels"] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert curve["cumulative"][0] == 1.5  # win at 2.5 -> +1.5u
    assert curve["cumulative"][1] is None  # no bets that day
    assert curve["cumulative"][2] == 0.5  # -1u loss -> 1.5 - 1.0


@pytest.mark.asyncio
async def test_monthly_performance_supports_all_time(store):
    gid = await _seed_game(store)
    await _seed_resolved_bet(
        store,
        gid,
        flagged_at=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        result="win",
        entry_odds=2.0,
        selection="home",
    )
    await _seed_resolved_bet(
        store,
        gid,
        flagged_at=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
        result="loss",
        selection="away",
    )
    data = await queries.monthly_performance(store, month="all")
    assert data["month"] is None  # all-time selected
    assert data["months"] == queries._month_range("2026-07", datetime.now(timezone.utc))
    assert data["bets"]["n_total"] == 2
    assert data["bets"]["flat_pl"] == 0.0  # +1.0 win, -1.0 loss


@pytest.mark.asyncio
async def test_game_detail_returns_series_and_flags(store):
    """The /game drill-down: per-selection odds series (tipsport + pinnacle)
    over unified time labels, plus every value-bet flag for the game."""
    gid = await _seed_game(store, hours_from_now=1.0)
    t0 = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
    snaps = []
    for i, (book, sel, odds) in enumerate(
        [
            ("tipsport", "home", 2.10),
            ("tipsport", "home", 2.05),
            ("pinnacle", "home", 1.95),
            ("tipsport", "away", 1.80),
        ]
    ):
        snaps.append(
            OddsSnapshot(
                game_id=gid,
                bookmaker=book,
                market_type="moneyline",
                selection=sel,
                odds=odds,
                sport="nba",
                captured_at=t0 + timedelta(minutes=5 * i),
            )
        )
    await store.insert_odds_snapshots_batch(snaps)
    await _seed_value_bet(store, gid, selection="home")

    detail = await queries.game_detail(store, gid)
    assert detail is not None
    assert detail["game"]["home_team"] == "Boston Celtics"
    assert set(detail["series"]["home"].keys()) == {"tipsport", "pinnacle"}
    # tipsport home has two points, pinnacle home one; labels unify all times
    assert len(detail["labels"]) >= 3
    assert sum(1 for v in detail["series"]["home"]["tipsport"] if v is not None) == 2
    assert sum(1 for v in detail["series"]["home"]["pinnacle"] if v is not None) == 1
    assert len(detail["flags"]) == 1
    assert detail["flags"][0]["selection"] == "home"


@pytest.mark.asyncio
async def test_game_detail_none_for_unknown_game(store):
    assert await queries.game_detail(store, 424242) is None


@pytest.mark.asyncio
async def test_placed_bets_table_carries_rejection_error(store):
    """Six real offer_gone rejections showed a bare em-dash on the dashboard —
    the error column must reach the decisions table."""
    gid = await _seed_game(store)
    vb_id = await _seed_value_bet(store, gid, selection="home")
    from oddsiq.db.models import PlacedBet

    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb_id,
            game_id=gid,
            sport="nba",
            status="rejected",
            stake_czk=10.0,
            requested_odds=2.05,
            error="offer_gone",
            commanded_at=datetime.now(timezone.utc),
        )
    )
    rows = await queries.placed_bets_table(store)
    assert rows[0]["status"] == "rejected"
    assert rows[0]["error"] == "offer_gone"


@pytest.mark.asyncio
async def test_betting_overview_funnel_separates_rejected_from_placed(store):
    """A rejected execution attempt is NOT a placed bet — the funnel counted
    status != skipped as 'placed', so 6 rejections displayed as '6 placed'."""
    gid = await _seed_game(store)
    now = datetime.now(timezone.utc)
    vb1 = await _seed_value_bet(store, gid, flagged_at=now, selection="home")
    vb2 = await _seed_value_bet(store, gid, flagged_at=now, selection="away")
    from oddsiq.db.models import PlacedBet

    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb1,
            game_id=gid,
            sport="nba",
            status="rejected",
            stake_czk=10.0,
            error="offer_gone",
            commanded_at=now,
        )
    )
    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb2,
            game_id=gid,
            sport="nba",
            status="placed",
            stake_czk=10.0,
            commanded_at=now,
        )
    )
    o = await queries.betting_overview(store)
    assert o["today"]["placed"] == 1
    assert o["today"]["rejected"] == 1
    assert o["today"]["skipped"] == 0


# ---------------------------------------------------------------------------
# Activity calendar + calendar-true months (2026-09-06)
# ---------------------------------------------------------------------------


def _ym_minus(now, months_back):
    y, m = now.year, now.month - months_back
    while m <= 0:
        y, m = y - 1, m + 12
    return y, m


@pytest.mark.asyncio
async def test_monthly_months_are_calendar_true_through_current_month(store):
    """A guarded-only bet two months ago must still open its month tab, and
    the CURRENT month appears even with zero rows — a quiet September must
    read as quiet, not missing."""
    from oddsiq.db.models import Game, ValueBet

    now = datetime.now(timezone.utc)
    y, m = _ym_minus(now, 2)
    g = Game(
        external_id="cal-g1",
        home_team="A",
        away_team="B",
        scheduled_at=now,
        status="final",
        sport="mlb",
    )
    g.id = await store.upsert_game(g)
    vb = ValueBet(
        game_id=g.id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.0,
        fair_odds=1.9,
        edge_pct=5.0,
        tier="hot",
        sport="mlb",
        flagged_at=datetime(y, m, 15, 12, tzinfo=timezone.utc),
        excluded_reason="in_play_pregame",
    )
    await store.insert_value_bet(vb)

    data = await queries.monthly_performance(store)
    assert data["months"][0] == f"{now.year:04d}-{now.month:02d}"
    assert data["months"][-1] == f"{y:04d}-{m:02d}"
    assert len(data["months"]) == 3
    assert data["month"] == data["months"][0]  # defaults to the current month
    assert data["bets"]["n_total"] == 0  # honestly empty, not absent


def test_month_range_helper():
    from oddsiq.dashboard.queries import _month_range

    now = datetime(2026, 9, 6, tzinfo=timezone.utc)
    assert _month_range("2026-06", now) == ["2026-09", "2026-08", "2026-07", "2026-06"]
    assert _month_range(None, now) == []
    assert _month_range("2026-12", now) == ["2026-09"]  # never earlier than now


@pytest.mark.asyncio
async def test_activity_calendar_buckets_prague_days_and_marks_milestones(store):
    from oddsiq.db.models import Game, OddsSnapshot, ValueBet, PlacedBet

    g = Game(
        external_id="cal-g2",
        home_team="Los Angeles Angels",
        away_team="Philadelphia Phillies",
        scheduled_at=datetime(2026, 6, 11, 1, 0, tzinfo=timezone.utc),
        status="final",
        sport="mlb",
    )
    g.id = await store.upsert_game(g)
    # 22:30 UTC in June is 00:30 CEST the NEXT day — the operator's "tonight".
    flagged = datetime(2026, 6, 10, 22, 30, tzinfo=timezone.utc)
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=g.id,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="home",
                odds=2.1,
                captured_at=flagged,
                sport="mlb",
            )
        ]
    )
    vb = ValueBet(
        game_id=g.id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.1,
        fair_odds=2.0,
        edge_pct=5.0,
        tier="hot",
        sport="mlb",
        flagged_at=flagged,
    )
    vb.id = await store.insert_value_bet(vb)
    # result is written by settlement, not at insert
    await store._db.execute("UPDATE value_bets SET result='win' WHERE id=?", (vb.id,))
    await store._db.commit()
    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb.id,
            game_id=g.id,
            sport="mlb",
            status="rejected",
            stake_czk=10.0,
            requested_odds=2.1,
            error="offer_gone",
        )
    )

    cal = await queries.activity_calendar(
        store, now=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    )
    labels = [m["label"] for m in cal["months"]]
    assert labels == ["Jun 2026", "Jul 2026", "Aug 2026", "Sep 2026"]  # calendar-true
    june = cal["months"][0]
    by_date = {c["date"]: c for w in june["weeks"] for c in w if c}
    assert by_date["2026-06-11"]["clean"] == 1 and by_date["2026-06-11"]["level"] == 2
    assert by_date["2026-06-10"]["clean"] == 0
    assert by_date["2026-06-11"]["first_time"] == "00:30"
    assert by_date["2026-06-11"]["flat_pl"] == pytest.approx(1.1)
    ms_labels = [m["label"] for m in cal["milestones"]]
    assert "First value bet detected" in ms_labels
    assert any(
        "Multi-sport refactor" in x for x in ms_labels
    )  # static, inside the range
    assert "First value bet detected" in by_date["2026-06-11"]["milestones"]
    sept = {c["date"]: c for w in cal["months"][-1]["weeks"] for c in w if c}
    assert sept["2026-09-06"]["is_today"] is True
    assert sept["2026-09-07"]["future"] is True  # rendered muted, not clickable
    assert all(not c["future"] for w in june["weeks"] for c in w if c)


@pytest.mark.asyncio
async def test_activity_calendar_empty_db(store):
    cal = await queries.activity_calendar(store)
    assert cal["months"] == [] and cal["milestones"] == []


@pytest.mark.asyncio
async def test_day_detail_lists_detections_with_prague_times_and_decisions(store):
    from datetime import date as _date
    from oddsiq.db.models import Game, ValueBet, PlacedBet

    g = Game(
        external_id="cal-g3",
        home_team="Toronto Blue Jays",
        away_team="Seattle Mariners",
        scheduled_at=datetime(2026, 8, 29, 19, 49, tzinfo=timezone.utc),
        status="final",
        sport="mlb",
    )
    g.id = await store.upsert_game(g)
    vb = ValueBet(
        game_id=g.id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.97,
        edge_pct=4.07,
        tier="hot",
        sport="mlb",
        flagged_at=datetime(2026, 8, 29, 19, 41, tzinfo=timezone.utc),
    )
    vb.id = await store.insert_value_bet(vb)
    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb.id,
            game_id=g.id,
            sport="mlb",
            status="skipped",
            skip_reason="not_logged_in",
        )
    )

    d = await queries.day_detail(store, _date(2026, 8, 29))
    assert d["n_clean"] == 1 and d["n_skipped"] == 1
    row = d["detections"][0]
    assert row["time"] == "21:41"  # 19:41 UTC -> CEST
    assert row["team"] == "Toronto Blue Jays"
    assert row["dec_status"] == "skipped" and row["skip_reason"] == "not_logged_in"
    # A different Prague day sees nothing.
    assert (await queries.day_detail(store, _date(2026, 8, 28)))["detections"] == []


@pytest.mark.asyncio
async def test_far_shadow_rows_excluded_from_performance_aggregates(store):
    """Shadow far rows (NFL far-tier detection, never bettable) must not enter
    ROI/CLV aggregates built on VALUE_BETS_DEDUPED — even when they are the
    only row for a selection."""
    from oddsiq.db.models import Game, ValueBet

    now = datetime.now(timezone.utc)
    g = Game(
        external_id="shadow-g",
        home_team="Seattle Seahawks",
        away_team="New England Patriots",
        scheduled_at=now - timedelta(hours=4),
        status="final",
        sport="nfl",
    )
    g.id = await store.upsert_game(g)
    far = ValueBet(
        game_id=g.id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.0,
        fair_odds=1.9,
        edge_pct=5.3,
        tier="far",
        sport="nfl",
        flagged_at=now - timedelta(hours=8),
    )
    far.id = await store.insert_value_bet(far)
    await store._db.execute(
        "UPDATE value_bets SET result='win', clv_pct=4.0 WHERE id=?", (far.id,)
    )
    await store._db.commit()
    n = await store._db.execute(f"SELECT COUNT(*) FROM {queries.VALUE_BETS_DEDUPED}")
    assert (await n.fetchone())[0] == 0
    hot = ValueBet(
        game_id=g.id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.0,
        fair_odds=1.9,
        edge_pct=5.3,
        tier="hot",
        sport="nfl",
        flagged_at=now - timedelta(hours=4, minutes=30),
    )
    await store.insert_value_bet(hot)
    n = await store._db.execute(f"SELECT tier FROM {queries.VALUE_BETS_DEDUPED}")
    assert [r[0] for r in await n.fetchall()] == ["hot"]


@pytest.mark.asyncio
async def test_activity_calendar_counts_far_rows_as_shadow(store):
    from oddsiq.db.models import Game, ValueBet

    g = Game(
        external_id="shadow-cal",
        home_team="Detroit Lions",
        away_team="New Orleans Saints",
        scheduled_at=datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc),
        status="scheduled",
        sport="nfl",
    )
    g.id = await store.upsert_game(g)
    await store.insert_value_bet(
        ValueBet(
            game_id=g.id,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=1.4,
            fair_odds=1.34,
            edge_pct=4.5,
            tier="far",
            sport="nfl",
            flagged_at=datetime(2026, 9, 13, 12, 5, tzinfo=timezone.utc),
        )
    )
    cal = await queries.activity_calendar(
        store, now=datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc)
    )
    sept = {c["date"]: c for w in cal["months"][-1]["weeks"] for c in w if c}
    assert sept["2026-09-13"]["shadow"] == 1 and sept["2026-09-13"]["clean"] == 0
    assert (
        sept["2026-09-13"]["level"] == 1
    )  # shadow-only renders like guarded-only (faint)

    # Mixed day: a clean HOT row on the SAME Prague day promotes the cell to
    # level 2 without the shadow row leaking into `clean` or `flat_pl` (an
    # unsettled clean bet contributes no paper P&L).
    await store.insert_value_bet(
        ValueBet(
            game_id=g.id,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="away",
            entry_odds=2.6,
            fair_odds=2.4,
            edge_pct=5.1,
            tier="hot",
            sport="nfl",
            flagged_at=datetime(2026, 9, 13, 15, 30, tzinfo=timezone.utc),
        )
    )
    cal = await queries.activity_calendar(
        store, now=datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc)
    )
    sept = {c["date"]: c for w in cal["months"][-1]["weeks"] for c in w if c}
    day = sept["2026-09-13"]
    assert day["shadow"] == 1 and day["clean"] == 1
    assert day["level"] == 2
    assert day["flat_pl"] == 0.0


@pytest.mark.asyncio
async def test_day_detail_counts_far_rows_as_shadow_not_clean(store):
    from datetime import date as _date
    from oddsiq.db.models import Game, ValueBet

    g = Game(
        external_id="shadow-day",
        home_team="Detroit Lions",
        away_team="New Orleans Saints",
        scheduled_at=datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc),
        status="scheduled",
        sport="nfl",
    )
    g.id = await store.upsert_game(g)
    await store.insert_value_bet(
        ValueBet(
            game_id=g.id,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=1.4,
            fair_odds=1.34,
            edge_pct=4.5,
            tier="far",
            sport="nfl",
            flagged_at=datetime(2026, 9, 13, 12, 5, tzinfo=timezone.utc),
        )
    )
    d = await queries.day_detail(store, _date(2026, 9, 13))
    assert d["n_shadow"] == 1 and d["n_clean"] == 0 and len(d["detections"]) == 1


@pytest.mark.asyncio
async def test_day_detail_guard_stamped_far_row_counts_only_as_shadow(store):
    """A far-tier row that ALSO carries an excluded_reason guard stamp must
    land in exactly one bucket (shadow), not double-count into guarded too."""
    from datetime import date as _date
    from oddsiq.db.models import Game, ValueBet

    g = Game(
        external_id="shadow-guarded",
        home_team="Detroit Lions",
        away_team="New Orleans Saints",
        scheduled_at=datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc),
        status="scheduled",
        sport="nfl",
    )
    g.id = await store.upsert_game(g)
    await store.insert_value_bet(
        ValueBet(
            game_id=g.id,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=1.6,
            fair_odds=1.4,
            edge_pct=14.3,
            tier="far",
            sport="nfl",
            flagged_at=datetime(2026, 9, 13, 12, 5, tzinfo=timezone.utc),
            excluded_reason="implausible_edge_pregame",
        )
    )
    d = await queries.day_detail(store, _date(2026, 9, 13))
    assert (d["n_clean"], d["n_guarded"], d["n_shadow"]) == (0, 0, 1)


@pytest.mark.asyncio
async def test_status_strip_counts_far_rows_as_shadow(store):
    """far-tier SHADOW rows (never bettable) must not inflate either the clean
    or suspect tile — but they must still be VISIBLE: hiding them printed a big
    `0` on a shadow-only day, the "detector looks dead" regression. A far row
    is SHADOW even when it also carries an excluded_reason."""
    gid = await _seed_game(store, sport="nfl")
    now = datetime.now(timezone.utc)
    await store.insert_value_bet(
        ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=1.6,
            fair_odds=1.4,
            edge_pct=14.3,
            flagged_at=now,
            tier="far",
            sport="nfl",
        )
    )
    s = await queries.status_strip(store)
    assert s["flags_today"] == 0
    assert s["suspect_today"] == 0
    assert s["shadow_today"] == 1
    await store.insert_value_bet(
        ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="away",
            entry_odds=1.6,
            fair_odds=1.4,
            edge_pct=14.3,
            flagged_at=now,
            tier="far",
            sport="nfl",
            excluded_reason="implausible_edge_pregame",
        )
    )
    s = await queries.status_strip(store)
    assert s["flags_today"] == 0
    assert s["suspect_today"] == 0
    assert s["shadow_today"] == 2


@pytest.mark.asyncio
async def test_betting_overview_funnel_counts_far_rows_as_shadow(store):
    """The funnel's "clean" number is a promise the engine can act on it — a
    far-tier shadow row is never bettable, so it gets its own count."""
    gid = await _seed_game(store, sport="nfl")
    await store.insert_value_bet(
        ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=1.4,
            fair_odds=1.34,
            edge_pct=4.5,
            flagged_at=datetime.now(timezone.utc),
            tier="far",
            sport="nfl",
        )
    )
    o = await queries.betting_overview(store)
    assert o["today"]["shadow"] == 1
    assert o["today"]["detections"] == 0
    assert o["today"]["guarded"] == 0


@pytest.mark.asyncio
async def test_far_filter_does_not_drop_a_flexed_selection(store):
    """A flexed kickoff can push a game back to the far tier AFTER a hot
    detection, so the far row is the LATEST for its selection. The far filter
    must sit inside the ROW_NUMBER input: outside it, rn=1 would be the far
    row and the whole settled selection would vanish from ROI/CLV."""
    from oddsiq.db.models import Game, ValueBet

    now = datetime.now(timezone.utc)
    g = Game(
        external_id="flex-g",
        home_team="Kansas City Chiefs",
        away_team="Denver Broncos",
        scheduled_at=now + timedelta(hours=5),
        status="scheduled",
        sport="nfl",
    )
    g.id = await store.upsert_game(g)
    hot = ValueBet(
        game_id=g.id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=1.8,
        fair_odds=1.7,
        edge_pct=5.9,
        tier="hot",
        sport="nfl",
        flagged_at=now - timedelta(hours=2),
    )
    hot.id = await store.insert_value_bet(hot)
    await store._db.execute("UPDATE value_bets SET result='win' WHERE id=?", (hot.id,))
    far = ValueBet(
        game_id=g.id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=1.85,
        fair_odds=1.7,
        edge_pct=8.8,
        tier="far",
        sport="nfl",
        flagged_at=now - timedelta(hours=1),
    )  # LATER than the hot row
    await store.insert_value_bet(far)
    await store._db.commit()
    cur = await store._db.execute(
        f"SELECT tier, result FROM {queries.VALUE_BETS_DEDUPED}"
    )
    assert [tuple(r) for r in await cur.fetchall()] == [("hot", "win")]
