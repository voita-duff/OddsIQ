from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from oddsiq.db import migrations
from oddsiq.db.models import Game, OddsSnapshot, ValueBet
from oddsiq.db.store import Store


@pytest.mark.asyncio
async def test_sport_round_trips_and_defaults_nba(store):
    game = Game(
        external_id="g1",
        home_team="A",
        away_team="B",
        scheduled_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    gid = await store.upsert_game(game)
    fetched = (await store.get_games_by_status("scheduled"))[0]
    assert fetched.sport == "nba"  # default

    snap = OddsSnapshot(
        game_id=gid,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        odds=1.9,
    )
    await store.insert_odds_snapshots_batch([snap])
    latest = await store.get_latest_odds(gid, "tipsport", "moneyline", "home")
    assert latest.sport == "nba"


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
async def test_get_stranded_games_finds_aged_unfinished_games(store: Store):
    """Games stuck non-final long past tipoff (e.g. finalized during a >3-day
    outage, outside the scores window) must be surfaced for a startup warning."""
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    # Stranded: scheduled, tipoff 10h ago.
    await store.upsert_game(
        Game(
            external_id="stranded",
            home_team="A",
            away_team="B",
            scheduled_at=now - timedelta(hours=10),
            status="scheduled",
        )
    )
    # Live but only 1h past tipoff — within backstop, NOT stranded.
    await store.upsert_game(
        Game(
            external_id="fresh_live",
            home_team="C",
            away_team="D",
            scheduled_at=now - timedelta(hours=1),
            status="live",
        )
    )
    # Final game long ago — terminal, NOT stranded.
    await store.upsert_game(
        Game(
            external_id="done",
            home_team="E",
            away_team="F",
            scheduled_at=now - timedelta(hours=20),
            status="final",
        )
    )
    # Upcoming — NOT stranded.
    await store.upsert_game(
        Game(
            external_id="future",
            home_team="G",
            away_team="H",
            scheduled_at=now + timedelta(hours=2),
            status="scheduled",
        )
    )

    stranded = await store.get_stranded_games(now, min_age_hours=6.0)
    ext_ids = {g.external_id for g in stranded}
    assert ext_ids == {"stranded"}


@pytest.mark.asyncio
async def test_insert_and_get_game(store: Store):
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    assert game_id is not None
    fetched = await store.get_games_by_date(datetime(2026, 4, 10, tzinfo=timezone.utc))
    assert len(fetched) == 1
    assert fetched[0].home_team == "Boston Celtics"


@pytest.mark.asyncio
async def test_batch_dedup_is_line_aware(store: Store):
    """Snapshot dedup must scope to ``line``: a stable price on one line dedups
    (bumps captured_at) against its OWN line's latest row, even when a DIFFERENT
    line has a more recent snapshot. Regression for get_latest_odds ignoring line
    (which re-inserted a stable spread/total price every poll)."""
    game = Game(
        external_id="nba_lines",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    gid = await store.upsert_game(game)
    t1 = datetime(2026, 4, 10, 22, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 10, 22, 5, tzinfo=timezone.utc)  # later, different line
    t3 = datetime(2026, 4, 10, 22, 10, tzinfo=timezone.utc)
    await store.insert_odds_snapshot(
        OddsSnapshot(
            game_id=gid,
            bookmaker="pinnacle",
            market_type="spread",
            selection="home",
            line=-4.5,
            odds=1.90,
            captured_at=t1,
        )
    )
    await store.insert_odds_snapshot(
        OddsSnapshot(
            game_id=gid,
            bookmaker="pinnacle",
            market_type="spread",
            selection="home",
            line=-3.5,
            odds=2.00,
            captured_at=t2,
        )
    )
    # A stable -4.5 price (same odds+line as t1) at t3 must BUMP t1's row, not insert.
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=gid,
                bookmaker="pinnacle",
                market_type="spread",
                selection="home",
                line=-4.5,
                odds=1.90,
                captured_at=t3,
            )
        ]
    )
    latest_45 = await store.get_latest_odds(gid, "pinnacle", "spread", "home", -4.5)
    assert latest_45 is not None
    assert latest_45.captured_at == t3  # bumped (last-seen), not a new row
    cur = await store._db.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE game_id=? AND IFNULL(line,-999.0)=-4.5",
        (gid,),
    )
    (count_45,) = await cur.fetchone()
    assert count_45 == 1


@pytest.mark.asyncio
async def test_upsert_game_updates_existing(store: Store):
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    id1 = await store.upsert_game(game)
    game.status = "live"
    id2 = await store.upsert_game(game)
    assert id1 == id2
    games = await store.get_games_by_date(datetime(2026, 4, 10, tzinfo=timezone.utc))
    assert games[0].status == "live"


@pytest.mark.asyncio
async def test_upsert_game_does_not_regress_a_final_game(store: Store):
    """A finalized game (with scores) must survive a later feed glitch that
    re-reports it as live with unparseable (None) scores — upsert_game must not
    clobber status or wipe scores once final."""
    final = Game(
        external_id="nba_final",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
        status="final",
        home_score=112,
        away_score=105,
    )
    gid = await store.upsert_game(final)

    glitch = Game(
        external_id="nba_final",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
        status="live",
        home_score=None,
        away_score=None,
    )
    gid2 = await store.upsert_game(glitch)

    assert gid == gid2
    games = await store.get_games_by_date(datetime(2026, 4, 10, tzinfo=timezone.utc))
    g = next(x for x in games if x.external_id == "nba_final")
    assert g.status == "final"  # not regressed to live
    assert g.home_score == 112  # scores not wiped
    assert g.away_score == 105


@pytest.mark.asyncio
async def test_game_commence_time_roundtrip_and_upsert_preserves_existing(store: Store):
    scheduled = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    commence = scheduled + timedelta(minutes=10)
    game = Game(
        external_id="nba_commence",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=scheduled,
        commence_time=commence,
    )
    game_id = await store.upsert_game(game)

    fetched = await store.get_games_by_date(datetime(2026, 4, 10, tzinfo=timezone.utc))
    assert fetched[0].id == game_id
    assert fetched[0].commence_time == commence

    schedule_refresh = Game(
        external_id="nba_commence",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=scheduled,
        status="live",
    )
    await store.upsert_game(schedule_refresh)

    fetched = await store.get_games_by_date(datetime(2026, 4, 10, tzinfo=timezone.utc))
    assert fetched[0].status == "live"
    assert fetched[0].commence_time == commence


@pytest.mark.asyncio
async def test_update_game_commence_time(store: Store):
    scheduled = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    commence = scheduled + timedelta(minutes=8)
    game = Game(
        external_id="nba_commence_update",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=scheduled,
    )
    game_id = await store.upsert_game(game)

    await store.update_game_commence_time(game_id, commence)

    fetched = await store.get_games_by_date(datetime(2026, 4, 10, tzinfo=timezone.utc))
    assert fetched[0].commence_time == commence


@pytest.mark.asyncio
async def test_update_game_status_preserves_scores_when_omitted(store: Store):
    game = Game(
        external_id="nba_score",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    gid = await store.upsert_game(game)
    # Scores recorded at finalization.
    await store.update_game_status(gid, "final", 110, 102)
    # A later status-only update (e.g. the live-transition path, which passes
    # no scores) must not wipe the recorded scores.
    await store.update_game_status(gid, "live")
    fetched = await store.get_games_by_date(datetime(2026, 4, 10, tzinfo=timezone.utc))
    assert fetched[0].status == "live"
    assert fetched[0].home_score == 110
    assert fetched[0].away_score == 102


@pytest.mark.asyncio
async def test_insert_odds_snapshot(store: Store):
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    snap = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
    )
    snap_id = await store.insert_odds_snapshot(snap)
    assert snap_id is not None


@pytest.mark.asyncio
async def test_get_latest_odds(store: Store):
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    snap1 = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.90,
        captured_at=datetime(2026, 4, 10, 20, 0, tzinfo=timezone.utc),
    )
    snap2 = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        captured_at=datetime(2026, 4, 10, 21, 0, tzinfo=timezone.utc),
    )
    await store.insert_odds_snapshot(snap1)
    await store.insert_odds_snapshot(snap2)
    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest is not None
    assert latest.odds == 1.95


@pytest.mark.asyncio
async def test_insert_value_bet(store: Store):
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    vb = ValueBet(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.13,
    )
    vb_id = await store.insert_value_bet(vb)
    assert vb_id is not None


@pytest.mark.asyncio
async def test_value_bet_excluded_reason_round_trips(store: Store):
    """excluded_reason set at insert time (pregame plausibility/coherence
    guards) must persist and hydrate back — reporting surfaces filter on it."""
    game = Game(
        external_id="er_1",
        home_team="A",
        away_team="B",
        scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    vb = ValueBet(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=3.9,
        fair_odds=2.85,
        edge_pct=36.9,
        excluded_reason="implausible_edge_pregame",
    )
    vb_id = await store.insert_value_bet(vb)
    assert vb_id is not None
    fetched = (await store.get_pending_value_bets(game_id))[0]
    assert fetched.excluded_reason == "implausible_edge_pregame"


@pytest.mark.asyncio
async def test_mark_closing_odds(store: Store):
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)
    snap = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        captured_at=tipoff - timedelta(minutes=1),
    )
    await store.insert_odds_snapshot(snap)
    # Use a freshness window wide enough that the test's wall-clock captured_at
    # (defaulted in OddsSnapshot to datetime.now) qualifies as fresh.
    await store.mark_closing_snapshots(game_id, freshness_minutes=10**9)
    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest.is_closing is True


@pytest.mark.asyncio
async def test_mark_closing_skips_post_tipoff_snapshot(store: Store):
    """A row captured after scheduled tipoff is not a pregame closing line."""
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_post_tip",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)
    snap = OddsSnapshot(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="total",
        selection="over",
        odds=1.91,
        line=226.5,
        captured_at=tipoff + timedelta(seconds=10),
    )
    await store.insert_odds_snapshot(snap)

    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    latest = await store.get_latest_odds(game_id, "tipsport", "total", "over", 226.5)
    assert latest.is_closing is False


@pytest.mark.asyncio
async def test_mark_closing_post_tipoff_row_does_not_shadow_pre_tipoff(store: Store):
    """A post-tipoff row in the same partition must not block a valid pre-tipoff
    row from being marked is_closing.

    Regression for the audit's P1 finding: with `ROW_NUMBER() OVER (... ORDER BY
    captured_at DESC)` ranking before the pre-tipoff filter, a post-tipoff row
    won rn=1 and was then excluded by the outer WHERE, leaving the genuine
    pre-tipoff snapshot unmarked. The filter now runs inside the window so the
    pre-tipoff row wins rn=1.

    This can happen on a normal tipoff transition when a dedup bump pushes a
    previously fresh row's captured_at past tipoff via "last seen" semantics.
    """
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_shadowed",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)
    pre = OddsSnapshot(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        odds=2.05,
        captured_at=tipoff - timedelta(minutes=2),
    )
    post = OddsSnapshot(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        odds=2.05,
        captured_at=tipoff + timedelta(seconds=15),
    )
    await store.insert_odds_snapshot(pre)
    await store.insert_odds_snapshot(post)

    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    cursor = await store._db.execute(
        "SELECT id, is_closing, captured_at FROM odds_snapshots WHERE game_id=? ORDER BY captured_at",
        (game_id,),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 2
    pre_row, post_row = rows
    assert pre_row["is_closing"] == 1, (
        "pre-tipoff row must be marked closing even when a post-tipoff row "
        "exists in the same partition"
    )
    assert post_row["is_closing"] == 0, "post-tipoff row must never be marked closing"


@pytest.mark.asyncio
async def test_mark_closing_legacy_null_commence_time_uses_scheduled_at(store: Store):
    """No backfill contract: NULL commence_time falls back to scheduled_at."""
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_null_commence",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)
    fresh = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        captured_at=tipoff - timedelta(minutes=1),
    )
    post_tipoff = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="away",
        odds=2.05,
        captured_at=tipoff + timedelta(seconds=1),
    )
    await store.insert_odds_snapshot(fresh)
    await store.insert_odds_snapshot(post_tipoff)

    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    home = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    away = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "away")
    assert home.is_closing is True
    assert away.is_closing is False


@pytest.mark.asyncio
async def test_mark_closing_uses_commence_time_when_present(store: Store):
    scheduled = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    commence = scheduled + timedelta(minutes=10)
    game = Game(
        external_id="nba_effective_tipoff",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=scheduled,
        commence_time=commence,
    )
    game_id = await store.upsert_game(game)
    after_scheduled_before_commence = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        captured_at=scheduled + timedelta(minutes=5),
    )
    after_commence = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="away",
        odds=2.05,
        captured_at=commence + timedelta(seconds=1),
    )
    await store.insert_odds_snapshot(after_scheduled_before_commence)
    await store.insert_odds_snapshot(after_commence)

    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    home = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    away = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "away")
    assert home.is_closing is True
    assert away.is_closing is False


@pytest.mark.asyncio
async def test_mark_closing_skips_stale_snapshot(store: Store):
    """A snapshot captured >30min before tipoff must not be marked is_closing.
    Otherwise CLV is computed against a fake closing line (e.g., game 290 in
    live DB had only 6h-old Pinnacle data, all marked is_closing=1)."""
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_stale",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)
    # 6h pre-tipoff — clearly stale by any reasonable threshold.
    snap = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        captured_at=tipoff - timedelta(hours=6),
    )
    await store.insert_odds_snapshot(snap)

    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest.is_closing is False, (
        f"stale snapshot ({snap.captured_at}, 6h before tipoff) must not be "
        f"marked is_closing under a 30-min freshness threshold"
    )


@pytest.mark.asyncio
async def test_mark_closing_marks_fresh_snapshot(store: Store):
    """A snapshot captured 5 min before tipoff is well within the freshness
    window and must be marked."""
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_fresh",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)
    snap = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        captured_at=tipoff - timedelta(minutes=5),
    )
    await store.insert_odds_snapshot(snap)

    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest.is_closing is True


@pytest.mark.asyncio
async def test_mark_closing_handles_mixed_freshness(store: Store):
    """Freshness check is per (bookmaker, market, selection). One fresh
    selection on a game must be marked even if another selection is stale."""
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_mixed",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)

    # Fresh: home, captured 5 min pre-tipoff.
    fresh = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        captured_at=tipoff - timedelta(minutes=5),
    )
    # Stale: away, captured 6 h pre-tipoff (the only away snapshot).
    stale = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="away",
        odds=2.05,
        captured_at=tipoff - timedelta(hours=6),
    )
    await store.insert_odds_snapshot(fresh)
    await store.insert_odds_snapshot(stale)

    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    home = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    away = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "away")
    assert home.is_closing is True, "fresh home selection must be marked"
    assert away.is_closing is False, "stale away selection must not be marked"


@pytest.mark.asyncio
async def test_mark_closing_partitions_by_line(store: Store):
    """Different spread lines for the same (book, market, selection) each get
    their own closing marker. Without `line` in the partition, the latest row
    across all lines would win, and a bet flagged at -4.5 could be CLV-checked
    against a closing row at -6.5 (or vice-versa)."""
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_spread_lines",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)

    # Two rows for the SAME (tipsport, spread, home) but different lines, both
    # captured fresh inside the freshness window.
    line_a = OddsSnapshot(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="spread",
        selection="home",
        odds=1.91,
        line=-4.5,
        captured_at=tipoff - timedelta(minutes=10),
    )
    line_b = OddsSnapshot(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="spread",
        selection="home",
        odds=1.91,
        line=-3.5,
        captured_at=tipoff - timedelta(minutes=5),
    )
    await store.insert_odds_snapshot(line_a)
    await store.insert_odds_snapshot(line_b)

    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    # Both rows should be marked is_closing — they're the latest in their
    # respective (book, market, selection, line) partitions.
    cursor = await store._db.execute(
        "SELECT line, is_closing FROM odds_snapshots WHERE game_id=? ORDER BY line",
        (game_id,),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 2
    assert all(r["is_closing"] == 1 for r in rows), (
        f"both lines should be marked closing; got {[(r['line'], r['is_closing']) for r in rows]}"
    )


@pytest.mark.asyncio
async def test_get_closing_odds_filters_by_line(store: Store):
    """A spread bet at -4.5 must not match a closing row at -3.5."""
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_clv_line",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)

    closing_at_minus_3_5 = OddsSnapshot(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="spread",
        selection="home",
        odds=1.91,
        line=-3.5,
        captured_at=tipoff - timedelta(minutes=2),
    )
    await store.insert_odds_snapshot(closing_at_minus_3_5)
    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    # Looking up the bet's actual line (-4.5) must return None — no closing row
    # at that line exists. Old behavior returned the -3.5 row, fabricating CLV.
    miss = await store.get_closing_odds(
        game_id,
        "tipsport",
        "spread",
        "home",
        line=-4.5,
    )
    assert miss is None, (
        "wrong-line lookup must not return a different line's closing row"
    )

    # The matching line still resolves.
    hit = await store.get_closing_odds(
        game_id,
        "tipsport",
        "spread",
        "home",
        line=-3.5,
    )
    assert hit is not None
    assert hit.line == -3.5


@pytest.mark.asyncio
async def test_get_closing_odds_moneyline_null_line(store: Store):
    """Moneyline rows have line=NULL on both sides of the lookup; the
    IFNULL(line, -999.0) idiom must match them."""
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game = Game(
        external_id="nba_ml",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game_id = await store.upsert_game(game)

    snap = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        line=None,
        captured_at=tipoff - timedelta(minutes=2),
    )
    await store.insert_odds_snapshot(snap)
    await store.mark_closing_snapshots(game_id, freshness_minutes=30)

    # Both default-line and explicit-None lookups must hit.
    default_lookup = await store.get_closing_odds(
        game_id,
        "pinnacle",
        "moneyline",
        "home",
    )
    explicit_none = await store.get_closing_odds(
        game_id,
        "pinnacle",
        "moneyline",
        "home",
        line=None,
    )
    assert default_lookup is not None and default_lookup.line is None
    assert explicit_none is not None and explicit_none.line is None


@pytest.mark.asyncio
async def test_meta_roundtrip(store: Store):
    """set_meta upserts; get_meta returns the latest value or None."""
    assert await store.get_meta("odds_api_budget_remaining") is None
    await store.set_meta("odds_api_budget_remaining", "458")
    assert await store.get_meta("odds_api_budget_remaining") == "458"
    # Overwrite round-trips.
    await store.set_meta("odds_api_budget_remaining", "420")
    assert await store.get_meta("odds_api_budget_remaining") == "420"


async def _count_rows(
    store: Store, game_id: int, bookmaker: str, market_type: str, selection: str
) -> int:
    cursor = await store._db.execute(
        """SELECT COUNT(*) FROM odds_snapshots
           WHERE game_id=? AND bookmaker=? AND market_type=? AND selection=?""",
        (game_id, bookmaker, market_type, selection),
    )
    return (await cursor.fetchone())[0]


@pytest.mark.asyncio
async def test_batch_insert_dedupes_identical_snapshot(store: Store):
    """Second batch with same (odds, line) as the last stored row is a no-op.

    Most poll cycles return odds identical to the previous cycle. Keeping those
    duplicate rows bloats the DB and doesn't add CLV information — the closing
    line is whichever row is most recent, and an earlier identical row captures
    that value just as well.
    """
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)

    snap1 = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        line=None,
    )
    snap2 = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        line=None,
    )
    await store.insert_odds_snapshots_batch([snap1])
    await store.insert_odds_snapshots_batch([snap2])

    count = await _count_rows(store, game_id, "pinnacle", "moneyline", "home")
    assert count == 1


@pytest.mark.asyncio
async def test_batch_insert_dedupe_bumps_captured_at(store: Store):
    """Dedup must update the existing row's captured_at to the new snapshot's
    timestamp. captured_at means 'last seen', not 'first inserted'.

    Without this, the timestamp of the latest row drifts to whenever the odds
    last changed, even though the pipeline kept polling and seeing the same
    odds. CLV reads the most recent row as 'closing', so a stale captured_at
    misrepresents how fresh the closing observation is.
    """
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)

    t0 = datetime(2026, 4, 10, 22, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 10, 23, 28, tzinfo=timezone.utc)
    snap1 = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        line=None,
        captured_at=t0,
    )
    snap2 = OddsSnapshot(
        game_id=game_id,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
        line=None,
        captured_at=t1,
    )
    await store.insert_odds_snapshots_batch([snap1])
    await store.insert_odds_snapshots_batch([snap2])

    count = await _count_rows(store, game_id, "pinnacle", "moneyline", "home")
    assert count == 1, "dedup must not insert a new row"
    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest.captured_at == t1, (
        f"captured_at must bump to t1={t1.isoformat()}, got {latest.captured_at.isoformat()}"
    )


@pytest.mark.asyncio
async def test_batch_dedup_keeps_raw_file_and_source_time_aligned(store: Store):
    """A last-seen bump must also point at the payload and upstream timestamp
    that produced it; otherwise forensic replay pairs today's observation time
    with an older raw response."""
    game = Game(
        external_id="provenance-1",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    t0 = datetime(2026, 4, 10, 22, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 10, 22, 5, tzinfo=timezone.utc)
    source0 = t0 - timedelta(seconds=10)
    source1 = t1 - timedelta(seconds=20)
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.95,
                captured_at=t0,
                source_updated_at=source0,
                raw_response_path="raw/first.json",
            )
        ]
    )
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.95,
                captured_at=t1,
                source_updated_at=source1,
                raw_response_path="raw/latest.json",
            )
        ]
    )

    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest.captured_at == t1
    assert latest.source_updated_at == source1
    assert latest.raw_response_path == "raw/latest.json"


@pytest.mark.asyncio
async def test_batch_dedup_bump_keeps_a_known_raw_path_when_the_archive_write_failed(
    store: Store,
):
    """_save_raw_response returns None when the archive write raised OSError.
    A bump from such a poll must not erase the path of a payload that IS on
    disk — the row would then point at nothing and the earlier observation
    becomes unreplayable. A bump that DOES carry a path still replaces it."""
    game = Game(
        external_id="provenance-2",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    t0 = datetime(2026, 4, 10, 22, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 10, 22, 5, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 10, 22, 10, tzinfo=timezone.utc)

    def _snap(captured_at, raw_response_path):
        return OddsSnapshot(
            game_id=game_id,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="home",
            odds=1.95,
            captured_at=captured_at,
            source_updated_at=captured_at - timedelta(seconds=10),
            raw_response_path=raw_response_path,
        )

    await store.insert_odds_snapshots_batch([_snap(t0, "raw/first.json")])
    # Bump from a poll whose raw archive write failed.
    await store.insert_odds_snapshots_batch([_snap(t1, None)])
    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest.captured_at == t1  # last-seen still moves
    assert latest.raw_response_path == "raw/first.json"
    # source_updated_at deliberately overwrites (fail-closed direction).
    assert latest.source_updated_at == t1 - timedelta(seconds=10)

    # A bump that carries a real path replaces the old one.
    await store.insert_odds_snapshots_batch([_snap(t2, "raw/latest.json")])
    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest.captured_at == t2
    assert latest.raw_response_path == "raw/latest.json"

    # A bump WITHOUT upstream freshness evidence must NOT keep the old
    # timestamp: unlike the raw path, source_updated_at is overwritten even
    # with NULL (fail-closed) — a later "make these consistent" COALESCE here
    # would let stale evidence survive an observation that had none.
    t3 = datetime(2026, 4, 10, 22, 15, tzinfo=timezone.utc)
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.95,
                captured_at=t3,
                source_updated_at=None,
                raw_response_path=None,
            )
        ]
    )
    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest.captured_at == t3
    assert latest.raw_response_path == "raw/latest.json"  # COALESCE kept it
    assert latest.source_updated_at is None  # overwritten with NULL


@pytest.mark.asyncio
async def test_batch_insert_stores_when_odds_change(store: Store):
    """Different odds value must be stored — this is the CLV signal."""
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)

    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.95,
            )
        ]
    )
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=2.05,
            )
        ]
    )

    latest = await store.get_latest_odds(game_id, "pinnacle", "moneyline", "home")
    assert latest.odds == 2.05
    # Both rows retained — dedup compares to the most recent only.
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE game_id=? AND bookmaker=? AND market_type=? AND selection=?",
        (game_id, "pinnacle", "moneyline", "home"),
    )
    count = (await cursor.fetchone())[0]
    assert count == 2


@pytest.mark.asyncio
async def test_batch_insert_dedupes_per_selection_independently(store: Store):
    """Dedup is per (game, bookmaker, market, selection). A duplicate on `home`
    must not block `away` from inserting its own new row."""
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)

    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.95,
            )
        ]
    )
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.95,
            ),  # duplicate -> skip
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="away",
                odds=2.10,
            ),  # new selection -> insert
        ]
    )

    home_count = await _count_rows(store, game_id, "pinnacle", "moneyline", "home")
    away_count = await _count_rows(store, game_id, "pinnacle", "moneyline", "away")
    assert home_count == 1  # duplicate skipped
    assert away_count == 1  # new selection inserted


@pytest.mark.asyncio
async def test_batch_insert_dedupes_on_line_change_for_spreads(store: Store):
    """Same odds but a different spread line is a real line move — must insert."""
    game = Game(
        external_id="nba_123",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)

    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="spread",
                selection="home",
                odds=1.91,
                line=-8.5,
            )
        ]
    )
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=game_id,
                bookmaker="pinnacle",
                market_type="spread",
                selection="home",
                odds=1.91,
                line=-9.0,  # line moved
            )
        ]
    )

    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE game_id=? AND market_type='spread'",
        (game_id,),
    )
    count = (await cursor.fetchone())[0]
    assert count == 2


@pytest.mark.asyncio
async def test_value_bet_dedup_distinct_lines_for_spread(store: Store):
    """Two spread bets with different lines must both insert.
    Lakers -4.5 and Lakers -3.5 are genuinely different bets — the older
    unique key (game, book, market, selection) collapsed them; the new
    key includes line."""
    game = Game(
        external_id="nba_lines",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    vb1 = ValueBet(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="spread",
        selection="home",
        line=-4.5,
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.13,
    )
    vb2 = ValueBet(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="spread",
        selection="home",
        line=-3.5,
        entry_odds=2.10,
        fair_odds=2.00,
        edge_pct=5.0,
    )
    id1 = await store.insert_value_bet(vb1)
    id2 = await store.insert_value_bet(vb2)
    assert id1 is not None
    assert id2 is not None, (
        "second spread with different line must insert (latent bug under spreads/totals)"
    )
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM value_bets WHERE game_id=? AND market_type='spread'",
        (game_id,),
    )
    assert (await cursor.fetchone())[0] == 2


@pytest.mark.asyncio
async def test_value_bet_dedup_moneyline_null_line_still_collapses(store: Store):
    """Moneyline has no line (NULL). The new unique key uses IFNULL(line, -999)
    so two NULL-line moneyline rows still collide on the sentinel."""
    game = Game(
        external_id="nba_ml",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    vb = ValueBet(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.13,
    )
    id1 = await store.insert_value_bet(vb)
    id2 = await store.insert_value_bet(vb)
    assert id1 is not None
    assert id2 is None, (
        "second moneyline insert must dedupe (NULL coalesced to sentinel)"
    )
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM value_bets WHERE game_id=?",
        (game_id,),
    )
    assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_value_bet_dedup_same_spread_line_still_collapses(store: Store):
    """Two spread bets with the SAME line must still dedupe under the new key."""
    game = Game(
        external_id="nba_dup",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    game_id = await store.upsert_game(game)
    vb = ValueBet(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="spread",
        selection="home",
        line=-4.5,
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.13,
    )
    id1 = await store.insert_value_bet(vb)
    id2 = await store.insert_value_bet(vb)
    assert id1 is not None
    assert id2 is None, "second spread with identical line must dedupe"


@pytest.mark.asyncio
async def test_get_handled_live_game_ids(store: Store):
    tipoff = datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc)
    game1 = Game(
        external_id="nba_1",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
    )
    game2 = Game(
        external_id="nba_2",
        home_team="Miami Heat",
        away_team="Toronto Raptors",
        scheduled_at=tipoff,
    )
    gid1 = await store.upsert_game(game1)
    gid2 = await store.upsert_game(game2)

    # Insert snapshots for both games, but only mark closing for game1
    for gid in (gid1, gid2):
        snap = OddsSnapshot(
            game_id=gid,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="home",
            odds=1.95,
            captured_at=tipoff - timedelta(minutes=1),
        )
        await store.insert_odds_snapshot(snap)
    await store.mark_closing_snapshots(gid1, freshness_minutes=10**9)

    handled = await store.get_handled_live_game_ids()
    assert gid1 in handled
    assert gid2 not in handled


async def _seed_tipped_game(store):
    g = Game(
        external_id="closing_seed",
        home_team="Boston Celtics",
        away_team="Miami Heat",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    return await store.upsert_game(g)


@pytest.mark.asyncio
async def test_batch_does_not_bump_closing_row(store: Store):
    # A closing row exists pre-tipoff; a later identical-odds post-tipoff
    # insert must NOT bump it — it must insert a new non-closing row instead.
    gid = await _seed_tipped_game(store)
    pre = datetime(2026, 4, 10, 23, 15, tzinfo=timezone.utc)
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=gid,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.50,
                captured_at=pre,
            ),
        ]
    )
    await store.mark_closing_snapshots(gid, freshness_minutes=30)
    closing = await store.get_closing_odds(gid, "pinnacle", "moneyline", "home")
    assert closing is not None and closing.is_closing is True

    post = datetime(2026, 4, 11, 0, 30, tzinfo=timezone.utc)  # after tipoff
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=gid,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.50,
                captured_at=post,
            ),
        ]
    )
    # Closing row's captured_at is unchanged (still pre-tipoff, still closing).
    closing_after = await store.get_closing_odds(gid, "pinnacle", "moneyline", "home")
    assert closing_after.captured_at == pre
    # And a new non-closing row was inserted (two rows total now).
    cur = await store._db.execute(
        "SELECT COUNT(*) AS n FROM odds_snapshots WHERE game_id=? AND selection='home'",
        (gid,),
    )
    assert (await cur.fetchone())["n"] == 2


@pytest.mark.asyncio
async def test_delete_games_with_children_cascades_and_scopes(store: Store):
    """Deleting a game removes ITS odds_snapshots / value_bets /
    and the game row itself — other games untouched."""
    tipoff = datetime(2026, 6, 16, 23, 0, tzinfo=timezone.utc)
    ids = {}
    for ext in ("zombie", "keeper"):
        gid = await store.upsert_game(
            Game(
                external_id=ext,
                home_team="A",
                away_team="B",
                scheduled_at=tipoff,
                status="live",
                sport="mlb",
            )
        )
        ids[ext] = gid
        await store.insert_odds_snapshots_batch(
            [
                OddsSnapshot(
                    game_id=gid,
                    bookmaker="tipsport",
                    market_type="moneyline",
                    selection="home",
                    odds=1.9 if ext == "zombie" else 2.3,
                    captured_at=tipoff,
                )
            ]
        )
        await store.insert_value_bet(
            ValueBet(
                game_id=gid,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="home",
                entry_odds=2.10,
                fair_odds=2.00,
                edge_pct=5.0,
                sport="mlb",
            )
        )

    counts = await store.delete_games_with_children([ids["zombie"]])

    assert counts == {
        "games": 1,
        "odds_snapshots": 1,
        "value_bets": 1,
        "placed_bets": 0,
    }
    for table in ("odds_snapshots", "value_bets", "games"):
        col = "id" if table == "games" else "game_id"
        cur = await store._db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (ids["zombie"],)
        )
        assert (await cur.fetchone())[0] == 0, f"{table} rows must be gone"
        cur = await store._db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (ids["keeper"],)
        )
        assert (await cur.fetchone())[0] == 1, f"keeper's {table} rows must remain"


@pytest.mark.asyncio
async def test_delete_games_with_children_dry_run_writes_nothing(store: Store):
    """dry_run reports the same counts as a real delete but leaves every row in
    place; an empty id list is a zero no-op either way."""
    tipoff = datetime(2026, 6, 16, 23, 0, tzinfo=timezone.utc)
    gid = await store.upsert_game(
        Game(
            external_id="zombie",
            home_team="A",
            away_team="B",
            scheduled_at=tipoff,
            status="live",
        )
    )
    await store.insert_odds_snapshots_batch(
        [
            OddsSnapshot(
                game_id=gid,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="home",
                odds=1.9,
                captured_at=tipoff,
            ),
            OddsSnapshot(
                game_id=gid,
                bookmaker="pinnacle",
                market_type="moneyline",
                selection="home",
                odds=1.8,
                captured_at=tipoff,
            ),
        ]
    )

    counts = await store.delete_games_with_children([gid], dry_run=True)
    assert counts == {
        "games": 1,
        "odds_snapshots": 2,
        "value_bets": 0,
        "placed_bets": 0,
    }
    cur = await store._db.execute("SELECT COUNT(*) FROM games WHERE id=?", (gid,))
    assert (await cur.fetchone())[0] == 1, "dry-run must not delete the game"
    cur = await store._db.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE game_id=?", (gid,)
    )
    assert (await cur.fetchone())[0] == 2, "dry-run must not delete snapshots"

    zero = {"games": 0, "odds_snapshots": 0, "value_bets": 0, "placed_bets": 0}
    assert await store.delete_games_with_children([], dry_run=True) == zero
    assert await store.delete_games_with_children([]) == zero
