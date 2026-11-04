from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from oddsiq.db import migrations
from oddsiq.db.models import Game, OddsSnapshot, ValueBet
from oddsiq.db.store import Store


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def audit_store(tmp_path: Path):
    """A fully migrated, empty Store on a tmp_path DB. Caller seeds it."""
    db_path = tmp_path / "audit.db"
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    yield store
    await store.close()


async def _seed_game(
    store: Store,
    external_id: str = "nba_test_1",
    home: str = "Boston Celtics",
    away: str = "Los Angeles Lakers",
    scheduled_at: datetime | None = None,
    commence_time: datetime | None = None,
    status: str = "scheduled",
    home_score: int | None = None,
    away_score: int | None = None,
) -> int:
    """Insert a game; return its DB id."""
    game = Game(
        id=None,
        external_id=external_id,
        home_team=home,
        away_team=away,
        scheduled_at=scheduled_at or _utc(2026, 5, 19, 2, 0),
        commence_time=commence_time,
        status=status,
        home_score=home_score,
        away_score=away_score,
    )
    return await store.upsert_game(game)


async def _seed_snapshot(
    store: Store,
    *,
    game_id: int,
    bookmaker: str,
    market_type: str = "moneyline",
    selection: str = "home",
    odds: float = 2.00,
    line: float | None = None,
    captured_at: datetime | None = None,
    is_closing: bool = False,
) -> None:
    snap = OddsSnapshot(
        game_id=game_id,
        bookmaker=bookmaker,
        market_type=market_type,
        selection=selection,
        odds=odds,
        line=line,
        captured_at=captured_at or _utc(2026, 5, 19, 1, 30),
        is_closing=is_closing,
    )
    await store.insert_odds_snapshots_batch([snap])


async def _seed_value_bet(
    store: Store,
    *,
    game_id: int,
    bookmaker: str,
    market_type: str = "moneyline",
    selection: str = "home",
    line: float | None = None,
    entry_odds: float = 2.10,
    fair_odds: float = 2.00,
    edge_pct: float = 5.00,
    clv_pct: float | None = None,
    result: str | None = None,
    tier: str = "hot",
    flagged_at: datetime | None = None,
) -> None:
    vb = ValueBet(
        game_id=game_id,
        bookmaker=bookmaker,
        market_type=market_type,
        selection=selection,
        line=line,
        entry_odds=entry_odds,
        fair_odds=fair_odds,
        edge_pct=edge_pct,
        clv_pct=clv_pct,
        result=result,
        tier=tier,
    )
    bet_id = await store.insert_value_bet(vb)
    # insert_value_bet only persists the initial columns; backfill the rest
    # so audit aggregations can observe them.
    if bet_id is not None and (clv_pct is not None or result is not None or flagged_at is not None):
        await store._db.execute(
            "UPDATE value_bets SET clv_pct=?, result=?, "
            "flagged_at=COALESCE(?, flagged_at) WHERE id=?",
            (clv_pct, result, flagged_at.isoformat() if flagged_at else None, bet_id),
        )
        await store._db.commit()


@pytest.mark.asyncio
async def test_run_audit_returns_five_sections_with_zero_rows_on_empty_db(audit_store):
    """Audit a virgin DB. All five sections exist as keys, each is an empty list."""
    from oddsiq.audit.bookmakers import run_audit

    result = await run_audit(audit_store)

    assert set(result.keys()) == {
        "snapshot_volumes",
        "value_bets",
        "clv_by_bucket",
        "closing_quality",
        "coverage_gap_proxy",
    }
    assert result["snapshot_volumes"] == []
    assert result["value_bets"] == []
    assert result["clv_by_bucket"] == []
    assert result["closing_quality"] == []
    assert result["coverage_gap_proxy"] == []


@pytest.mark.asyncio
async def test_run_audit_has_no_overlap_section(audit_store):
    """run_audit returns exactly the five non-overlap sections.

    With only one soft book (Tipsport), the multi-book overlap section
    is dead weight and must be absent from the audit dict.
    """
    from oddsiq.audit.bookmakers import run_audit

    g1 = await _seed_game(audit_store, external_id="g1")
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="tipsport")
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="pinnacle")

    audit = await run_audit(audit_store)
    assert "overlap" not in audit
    assert set(audit) == {
        "snapshot_volumes", "value_bets", "clv_by_bucket",
        "closing_quality", "coverage_gap_proxy",
    }


@pytest.mark.asyncio
async def test_snapshot_volumes_counts_per_book_and_match_rate(audit_store):
    """Section 1: snapshots and games matched per bookmaker, with match_rate.

    Setup: 2 games. Tipsport snapshots cover both. softbook2 snapshots cover
    only game 1. Expected: tipsport 2/2 (100%), softbook2 1/2 (50%).
    """
    from oddsiq.audit.bookmakers import run_audit

    g1 = await _seed_game(audit_store, external_id="g1", home="Boston Celtics", away="Lakers")
    g2 = await _seed_game(audit_store, external_id="g2", home="Brooklyn Nets", away="Suns")

    await _seed_snapshot(audit_store, game_id=g1, bookmaker="tipsport", odds=1.95)
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="tipsport", selection="away", odds=2.05)
    await _seed_snapshot(audit_store, game_id=g2, bookmaker="tipsport", odds=1.50)
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="softbook2", odds=1.90)
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="softbook2", selection="away", odds=2.10)

    result = await run_audit(audit_store)
    volumes = {row["bookmaker"]: row for row in result["snapshot_volumes"]}

    assert volumes["tipsport"]["snapshots"] == 3
    assert volumes["tipsport"]["games_matched"] == 2
    assert volumes["tipsport"]["total_games"] == 2
    assert volumes["tipsport"]["match_rate"] == 100.0

    assert volumes["softbook2"]["snapshots"] == 2
    assert volumes["softbook2"]["games_matched"] == 1
    assert volumes["softbook2"]["total_games"] == 2
    assert volumes["softbook2"]["match_rate"] == 50.0


@pytest.mark.asyncio
async def test_value_bets_section_counts_with_clv_result_and_avg(audit_store):
    """Section 2: per-book totals, with_clv, with_result, avg/median CLV, pos_clv_pct."""
    from oddsiq.audit.bookmakers import run_audit

    g1 = await _seed_game(audit_store, external_id="g1")
    g2 = await _seed_game(audit_store, external_id="g2")
    g3 = await _seed_game(audit_store, external_id="g3")

    # Tipsport: 3 bets. 2 with CLV, 2 with result, 1 with both clv+result populated,
    # one negative CLV.
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="tipsport", edge_pct=5.0, clv_pct=4.0, result="win")
    await _seed_value_bet(audit_store, game_id=g2, bookmaker="tipsport", edge_pct=6.0, clv_pct=-1.5, result="loss")
    await _seed_value_bet(audit_store, game_id=g3, bookmaker="tipsport", edge_pct=4.0)  # no clv, no result

    # softbook2: 1 bet, with clv + result, positive.
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="softbook2", edge_pct=5.5, clv_pct=3.0, result="win")

    result = await run_audit(audit_store)
    bets = {row["bookmaker"]: row for row in result["value_bets"]}

    assert bets["tipsport"]["total"] == 3
    assert bets["tipsport"]["with_clv"] == 2
    assert bets["tipsport"]["with_result"] == 2
    assert bets["tipsport"]["avg_clv"] == 1.25  # (4.0 + -1.5) / 2
    assert bets["tipsport"]["median_clv"] == 1.25
    assert bets["tipsport"]["pos_clv_pct"] == 50.0

    assert bets["softbook2"]["total"] == 1
    assert bets["softbook2"]["with_clv"] == 1
    assert bets["softbook2"]["with_result"] == 1
    assert bets["softbook2"]["avg_clv"] == 3.0
    assert bets["softbook2"]["median_clv"] == 3.0
    assert bets["softbook2"]["pos_clv_pct"] == 100.0


@pytest.mark.asyncio
async def test_clv_by_bucket_partitions_edges_correctly(audit_store):
    """Section 3: edge buckets [3,5)/[5,7)/[7,10)/[10,inf).

    Important: edge 5.0% must fall in '5-7%' (lower bound inclusive),
    edge 6.99% in '5-7%', edge 7.0% in '7-10%'.
    """
    from oddsiq.audit.bookmakers import run_audit

    g1 = await _seed_game(audit_store, external_id="g1")
    g2 = await _seed_game(audit_store, external_id="g2")
    g3 = await _seed_game(audit_store, external_id="g3")
    g4 = await _seed_game(audit_store, external_id="g4")

    # Boundary tests
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="softbook2", edge_pct=3.0, clv_pct=2.5)   # 3-5%
    await _seed_value_bet(audit_store, game_id=g2, bookmaker="softbook2", edge_pct=4.99, clv_pct=4.0)  # 3-5%
    await _seed_value_bet(audit_store, game_id=g3, bookmaker="softbook2", edge_pct=5.0, clv_pct=5.5)   # 5-7%
    await _seed_value_bet(audit_store, game_id=g4, bookmaker="softbook2", edge_pct=10.5, clv_pct=11.0) # 10%+

    result = await run_audit(audit_store)
    buckets = {row["edge_bucket"]: row for row in result["clv_by_bucket"] if row["bookmaker"] == "softbook2"}

    assert buckets["3-5%"]["bets"] == 2
    assert buckets["3-5%"]["avg_edge"] == 4.0  # (3.0 + 4.99) / 2 = 3.995, round(..., 2) = 4.0
    assert buckets["3-5%"]["pos_clv_pct"] == 100.0
    assert buckets["5-7%"]["bets"] == 1
    assert buckets["5-7%"]["avg_clv"] == 5.5
    assert buckets["10%+"]["bets"] == 1
    assert "7-10%" not in buckets  # no row when bucket is empty


@pytest.mark.asyncio
async def test_closing_quality_separates_with_closing_from_stale_skipped(audit_store):
    """Section 4: a resolved bet with clv_pct populated = with_closing.
    A resolved bet with clv_pct NULL = stale_skipped (closing was too stale to mark)."""
    from oddsiq.audit.bookmakers import run_audit

    g1 = await _seed_game(audit_store, external_id="g1")
    g2 = await _seed_game(audit_store, external_id="g2")
    g3 = await _seed_game(audit_store, external_id="g3")

    # Tipsport: 2 resolved bets, 1 has clv (clean), 1 has no clv (stale)
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="tipsport", clv_pct=4.0, result="win")
    await _seed_value_bet(audit_store, game_id=g2, bookmaker="tipsport", result="loss")  # no clv = stale

    # An unresolved bet with no result must NOT count toward bets_resolved
    await _seed_value_bet(audit_store, game_id=g3, bookmaker="tipsport", clv_pct=2.0)

    result = await run_audit(audit_store)
    quality = {row["bookmaker"]: row for row in result["closing_quality"]}

    assert quality["tipsport"]["bets_resolved"] == 2
    assert quality["tipsport"]["with_closing"] == 1
    assert quality["tipsport"]["stale_skipped"] == 1
    assert quality["tipsport"]["pct_clean"] == 50.0


@pytest.mark.asyncio
async def test_coverage_gap_proxy_counts_resolved_bets_with_null_clv(audit_store):
    """Section 5 (approximate): bets with result populated but clv_pct NULL
    are the proxy for 'closing snapshot was stale during hot tier'.
    Section 4 reports this as 'stale_skipped'; Section 5 surfaces it from
    the gap-detection angle so it shows up in routine audits even when
    Section 4 isn't read in detail."""
    from oddsiq.audit.bookmakers import run_audit

    g1 = await _seed_game(audit_store, external_id="g1")
    g2 = await _seed_game(audit_store, external_id="g2")

    await _seed_value_bet(audit_store, game_id=g1, bookmaker="tipsport", clv_pct=4.0, result="win")     # clean
    await _seed_value_bet(audit_store, game_id=g2, bookmaker="tipsport", result="loss")                  # gap proxy
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="softbook2", clv_pct=3.0, result="win")        # clean

    result = await run_audit(audit_store)
    gaps = {row["bookmaker"]: row for row in result["coverage_gap_proxy"]}

    assert gaps["tipsport"]["resolved_without_clv"] == 1
    assert "softbook2" not in gaps  # zero-gap books are omitted, not zero-row


@pytest.mark.asyncio
async def test_bookmaker_filter_excludes_other_books_from_every_section(audit_store):
    """--bookmaker softbook2: tipsport rows must be absent from every section."""
    from oddsiq.audit.bookmakers import run_audit

    g1 = await _seed_game(audit_store, external_id="g1")

    await _seed_snapshot(audit_store, game_id=g1, bookmaker="tipsport")
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="softbook2")
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="tipsport", clv_pct=4.0, result="win")
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="softbook2",   clv_pct=3.0, result="win")

    result = await run_audit(audit_store, bookmaker="softbook2")

    for section_name in ("snapshot_volumes", "value_bets", "clv_by_bucket", "closing_quality", "coverage_gap_proxy"):
        for row in result[section_name]:
            assert row["bookmaker"] == "softbook2", f"{section_name} leaked tipsport"


@pytest.mark.asyncio
async def test_since_filter_uses_value_bet_flagged_at(audit_store):
    """--since filters value-bet sections on the bet's OWN flagged_at (its real
    creation time), not a MIN(captured_at) snapshot proxy. Section 1 (snapshots)
    still filters on captured_at."""
    from oddsiq.audit.bookmakers import run_audit

    # g_a: snapshot captured BEFORE since, but the bet was flagged AFTER since.
    # flagged_at semantics → included; the old captured_at proxy → excluded.
    g_a = await _seed_game(audit_store, external_id="a", scheduled_at=_utc(2026, 5, 16, 2, 0))
    await _seed_snapshot(audit_store, game_id=g_a, bookmaker="softbook2",
                         captured_at=_utc(2026, 5, 10, 1, 30))
    await _seed_value_bet(audit_store, game_id=g_a, bookmaker="softbook2",
                          clv_pct=7.0, result="win", flagged_at=_utc(2026, 5, 16, 1, 35))

    # g_b: snapshot captured AFTER since, but the bet was flagged BEFORE since.
    # flagged_at semantics → excluded; the old captured_at proxy → included.
    g_b = await _seed_game(audit_store, external_id="b", scheduled_at=_utc(2026, 5, 16, 2, 0))
    await _seed_snapshot(audit_store, game_id=g_b, bookmaker="softbook2",
                         captured_at=_utc(2026, 5, 16, 1, 30))
    await _seed_value_bet(audit_store, game_id=g_b, bookmaker="softbook2",
                          clv_pct=5.0, result="loss", flagged_at=_utc(2026, 5, 10, 1, 35))

    result = await run_audit(audit_store, since="2026-05-15")
    bets = {row["bookmaker"]: row for row in result["value_bets"]}

    # Only g_a's bet passes the flagged_at filter — proving flagged_at, not the
    # captured_at proxy (which would have selected g_b instead, avg_clv 5.0).
    assert bets["softbook2"]["total"] == 1
    assert bets["softbook2"]["avg_clv"] == 7.0


@pytest.mark.asyncio
async def test_since_filter_includes_value_bet_without_matching_snapshot(audit_store):
    """A value bet always has a flagged_at but may lack a perfectly-matching
    odds_snapshot row. The old MIN(captured_at) INNER JOIN proxy silently DROPPED
    such bets from --since sections; filtering on flagged_at includes them."""
    from oddsiq.audit.bookmakers import run_audit

    g = await _seed_game(audit_store, external_id="no_snap", scheduled_at=_utc(2026, 5, 20, 2, 0))
    # No odds_snapshot for this bet at all.
    await _seed_value_bet(audit_store, game_id=g, bookmaker="softbook2",
                          clv_pct=5.0, result="win", flagged_at=_utc(2026, 5, 18, 1, 0))

    result = await run_audit(audit_store, since="2026-05-15")
    bets = {row["bookmaker"]: row for row in result["value_bets"]}

    assert bets["softbook2"]["total"] == 1  # would be dropped by the INNER JOIN proxy


@pytest.mark.asyncio
async def test_value_bet_sections_dedupe_warm_and_hot_tier_rows(audit_store):
    """`tier` is in the value-bet dedup key, so one opportunity can have warm +
    hot rows. Sections 2/3 must count it ONCE, not double-count CLV/bets."""
    from oddsiq.audit.bookmakers import run_audit

    g = await _seed_game(audit_store, external_id="dh")
    for tier in ("warm", "hot"):
        await _seed_value_bet(audit_store, game_id=g, bookmaker="tipsport",
                              edge_pct=6.0, clv_pct=4.0, result="win", tier=tier)

    result = await run_audit(audit_store)
    bets = {row["bookmaker"]: row for row in result["value_bets"]}
    buckets = [r for r in result["clv_by_bucket"] if r["bookmaker"] == "tipsport"]

    assert bets["tipsport"]["total"] == 1            # not 2 (Section 2)
    assert bets["tipsport"]["with_clv"] == 1
    assert sum(b["bets"] for b in buckets) == 1      # not 2 (Section 3)


@pytest.mark.asyncio
async def test_far_tier_shadow_rows_excluded_from_value_bets_sections(audit_store):
    """far-tier SHADOW rows (NFL far-tier detection, never bettable) must not
    enter the deduped value-bet sections, mirroring the dashboard view
    (2026-09-06) — even though they carry clv_pct/result like a real bet."""
    from oddsiq.audit.bookmakers import run_audit

    g = await _seed_game(audit_store, external_id="shadow")
    await _seed_value_bet(audit_store, game_id=g, bookmaker="tipsport",
                          edge_pct=14.3, clv_pct=4.0, result="win", tier="far")

    result = await run_audit(audit_store)
    assert result["value_bets"] == []
    assert result["clv_by_bucket"] == []


@pytest.mark.asyncio
async def test_format_text_contains_five_section_headers_and_row_data(audit_store):
    """format_text emits all five section headers and renders at least
    one row of data for sections that have any."""
    from oddsiq.audit.bookmakers import format_text, run_audit

    g1 = await _seed_game(audit_store, external_id="g1")
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="tipsport")
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="tipsport", edge_pct=5.5, clv_pct=4.0, result="win")

    audit = await run_audit(audit_store)
    text = format_text(audit)

    assert "Snapshot volumes per book" in text
    assert "Value bets per book" in text
    assert "CLV by edge bucket" in text
    assert "Closing snapshot quality" in text
    assert "Coverage gap proxy" in text
    assert "Multi-book overlap" not in text
    assert "tipsport" in text
    assert "5.5" in text or "5.50" in text  # edge_pct from the bet


@pytest.mark.asyncio
async def test_format_json_emits_valid_json_with_five_keys(audit_store):
    """format_json produces parseable JSON with the five section keys.
    Row values match what run_audit returned (same shape, same content)."""
    import json
    from oddsiq.audit.bookmakers import format_json, run_audit

    g1 = await _seed_game(audit_store, external_id="g1")
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="softbook2")

    audit = await run_audit(audit_store)
    text = format_json(audit)

    parsed = json.loads(text)
    assert set(parsed.keys()) == {
        "snapshot_volumes", "value_bets", "clv_by_bucket",
        "closing_quality", "coverage_gap_proxy",
    }
    assert parsed["snapshot_volumes"][0]["bookmaker"] == "softbook2"
    assert parsed["snapshot_volumes"][0]["snapshots"] == 1


@pytest.mark.asyncio
async def test_audit_handles_value_bet_with_no_matching_snapshot(audit_store):
    """A bet that exists without any matching snapshot rows. Should not crash,
    and --since (filtering on the bet's own flagged_at, not a snapshot proxy)
    must still include it when flagged in-window."""
    from oddsiq.audit.bookmakers import run_audit

    g1 = await _seed_game(audit_store, external_id="g1")
    # Bet exists (flagged today by default), but no snapshot for the same
    # (game, bookmaker, market, selection).
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="softbook2", clv_pct=4.0, result="win")

    # Without --since: bet appears.
    result = await run_audit(audit_store)
    bets = {row["bookmaker"]: row for row in result["value_bets"]}
    assert bets["softbook2"]["total"] == 1

    # With an in-window --since: the bet still appears (flagged_at >= since),
    # where the old MIN(captured_at) INNER JOIN proxy would have dropped it.
    result_since = await run_audit(audit_store, since="2026-01-01")
    bets_since = {row["bookmaker"]: row for row in result_since["value_bets"]}
    assert bets_since["softbook2"]["total"] == 1


@pytest.mark.asyncio
async def test_format_json_matches_text_semantically_on_known_fixture(audit_store):
    """Sanity: format_text and format_json render the same audit dict
    from the same input. Verify by checking that every JSON row's
    bookmaker shows up in the text version."""
    import json
    from oddsiq.audit.bookmakers import format_json, format_text, run_audit

    g1 = await _seed_game(audit_store, external_id="g1")
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="tipsport")
    await _seed_snapshot(audit_store, game_id=g1, bookmaker="softbook2")
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="tipsport", edge_pct=5.5, clv_pct=4.0, result="win")
    await _seed_value_bet(audit_store, game_id=g1, bookmaker="softbook2",   edge_pct=4.0, clv_pct=3.0, result="loss")

    audit = await run_audit(audit_store)
    text = format_text(audit)
    parsed = json.loads(format_json(audit))

    # Every bookmaker that appears in the JSON value_bets section appears in the text.
    for row in parsed["value_bets"]:
        assert row["bookmaker"] in text
