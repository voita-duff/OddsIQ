"""Tests for the excluded_reason data-quality annotation (migration 007).

The 2026-06-02..2026-06-12 basketball matchView bug recorded Tipsport's
regulation 3-way prices as the NBA moneyline, so affected value_bets rows
carry systematically inflated edge/CLV. Remediation annotates instead of
deleting: `Store.mark_rows_excluded` stamps `excluded_reason` on the
contaminated window, and every REPORTING surface (dashboard queries,
per-book audit) hides stamped rows. Pipeline-lifecycle queries
(settlement / CLV recovery) deliberately stay unfiltered — excluded rows
are inert there and must not change pipeline behavior. (The
live_edge_candidates table was removed 2026-08-27 with the live-edge
subsystem; the one-shot now stamps value_bets only.)
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from oddsiq.audit import bookmakers
from oddsiq.dashboard import queries
from oddsiq.db import migrations
from oddsiq.db.models import Game, ValueBet
from oddsiq.db.store import Store

REASON = "basketball_3way_matchview_2026_06"
FROM_ISO = "2026-06-02T00:00:00+00:00"
UNTIL_ISO = "2026-06-12T00:00:00+00:00"


def _utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# Migration 007 itself is covered in tests/test_migration_007.py.

# --- store: stamping + filtered reads ---


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    db = tmp_path / "test.db"
    migrations.run(db)
    s = Store(db)
    await s.initialize()
    yield s
    await s.close()


async def _seed_game(store: Store, *, external_id: str, sport: str) -> int:
    return await store.upsert_game(
        Game(
            external_id=external_id,
            home_team="Home Team",
            away_team="Away Team",
            scheduled_at=_utc(2026, 6, 9, 0, 44),
            status="final",
            home_score=111,
            away_score=115,
            sport=sport,
        )
    )


async def _seed_vb(
    store: Store,
    game_id: int,
    *,
    sport: str,
    flagged_at: datetime,
    edge_pct: float = 3.0,
) -> int:
    vb = ValueBet(
        game_id=game_id,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="away",
        entry_odds=2.12,
        fair_odds=2.06,
        edge_pct=edge_pct,
        tier="hot",
        sport=sport,
    )
    vb.flagged_at = flagged_at
    return await store.insert_value_bet(vb)


async def _excluded_reason_of(store: Store, table: str, row_id: int) -> str | None:
    cur = await store._db.execute(
        f"SELECT excluded_reason FROM {table} WHERE id = ?", (row_id,)
    )
    return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_mark_rows_excluded_stamps_only_matching_sport_and_window(store):
    g_nba_in = await _seed_game(store, external_id="nba-in", sport="nba")
    g_nba_old = await _seed_game(store, external_id="nba-old", sport="nba")
    g_nba_post = await _seed_game(store, external_id="nba-post", sport="nba")
    g_mlb = await _seed_game(store, external_id="mlb-in", sport="mlb")

    vb_in = await _seed_vb(
        store, g_nba_in, sport="nba", flagged_at=_utc(2026, 6, 9, 0, 34)
    )
    vb_old = await _seed_vb(store, g_nba_old, sport="nba", flagged_at=_utc(2026, 5, 20))
    vb_post = await _seed_vb(
        store, g_nba_post, sport="nba", flagged_at=_utc(2026, 6, 12, 1, 0)
    )
    vb_mlb = await _seed_vb(
        store, g_mlb, sport="mlb", flagged_at=_utc(2026, 6, 9, 0, 34)
    )

    counts = await store.mark_rows_excluded(
        sports=["nba", "wnba"],
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
    )
    assert counts == {"value_bets": 1}

    assert await _excluded_reason_of(store, "value_bets", vb_in) == REASON
    assert await _excluded_reason_of(store, "value_bets", vb_old) is None
    assert await _excluded_reason_of(store, "value_bets", vb_post) is None
    assert await _excluded_reason_of(store, "value_bets", vb_mlb) is None


@pytest.mark.asyncio
async def test_mark_rows_excluded_dry_run_counts_without_writing(store):
    g = await _seed_game(store, external_id="nba-in", sport="nba")
    vb = await _seed_vb(store, g, sport="nba", flagged_at=_utc(2026, 6, 9, 0, 34))

    counts = await store.mark_rows_excluded(
        sports=["nba"],
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
        dry_run=True,
    )
    assert counts == {"value_bets": 1}
    assert await _excluded_reason_of(store, "value_bets", vb) is None


@pytest.mark.asyncio
async def test_mark_rows_excluded_skips_already_excluded_rows(store):
    g = await _seed_game(store, external_id="nba-in", sport="nba")
    await _seed_vb(store, g, sport="nba", flagged_at=_utc(2026, 6, 9, 0, 34))

    first = await store.mark_rows_excluded(
        sports=["nba"],
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
    )
    assert first == {"value_bets": 1}
    second = await store.mark_rows_excluded(
        sports=["nba"],
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
    )
    assert second == {"value_bets": 0}


# --- dashboard reporting surfaces ---


@pytest.mark.asyncio
async def test_dashboard_value_bet_aggregates_omit_excluded(store):
    g_nba = await _seed_game(store, external_id="nba-in", sport="nba")
    g_mlb = await _seed_game(store, external_id="mlb-in", sport="mlb")
    await _seed_vb(store, g_nba, sport="nba", flagged_at=_utc(2026, 6, 9, 0, 34))
    await _seed_vb(store, g_mlb, sport="mlb", flagged_at=_utc(2026, 6, 9, 0, 34))
    await store.mark_rows_excluded(
        sports=["nba"],
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
    )

    big = await queries.big_numbers(store)
    assert big["bets_total"] == 1

    strip = await queries.status_strip(store, today_iso="2026-06-09")
    assert strip["flags_today"] == 1


@pytest.mark.asyncio
async def test_dashboard_recent_lists_omit_excluded(store):
    g_nba = await _seed_game(store, external_id="nba-in", sport="nba")
    g_mlb = await _seed_game(store, external_id="mlb-in", sport="mlb")
    await _seed_vb(store, g_nba, sport="nba", flagged_at=_utc(2026, 6, 9, 0, 34))
    await _seed_vb(store, g_mlb, sport="mlb", flagged_at=_utc(2026, 6, 9, 0, 34))
    await store.mark_rows_excluded(
        sports=["nba"],
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
    )

    bets = await queries.recent_value_bets(store)
    assert [b["sport"] for b in bets] == ["mlb"]


@pytest.mark.asyncio
async def test_dashboard_activity_feed_omits_excluded(store):
    g_nba = await _seed_game(store, external_id="nba-in", sport="nba")
    g_mlb = await _seed_game(store, external_id="mlb-in", sport="mlb")
    await _seed_vb(store, g_nba, sport="nba", flagged_at=_utc(2026, 6, 9, 0, 34))
    await _seed_vb(store, g_mlb, sport="mlb", flagged_at=_utc(2026, 6, 9, 0, 34))
    await store.mark_rows_excluded(
        sports=["nba"],
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
    )

    events = await queries.recent_db_events(store, limit=10)
    vb_events = [e for e in events if e["type"] == "value_bet"]
    assert len(vb_events) == 1


# --- per-book audit ---


@pytest.mark.asyncio
async def test_audit_value_bets_section_omits_excluded(store):
    g_nba = await _seed_game(store, external_id="nba-in", sport="nba")
    g_mlb = await _seed_game(store, external_id="mlb-in", sport="mlb")
    await _seed_vb(store, g_nba, sport="nba", flagged_at=_utc(2026, 6, 9, 0, 34))
    await _seed_vb(store, g_mlb, sport="mlb", flagged_at=_utc(2026, 6, 9, 0, 34))
    await store.mark_rows_excluded(
        sports=["nba"],
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
    )

    audit = await bookmakers.run_audit(store)
    rows = audit["value_bets"]
    assert len(rows) == 1
    assert rows[0]["bookmaker"] == "tipsport"
    assert rows[0]["total"] == 1


# --- CLI one-shot ---


async def _seed_minimal_contaminated_db(db_path: Path) -> int:
    """Migrate db_path and seed one in-window NBA bet. Returns value_bet_id."""
    migrations.run(db_path)
    store = Store(db_path)
    await store.initialize()
    try:
        g = await _seed_game(store, external_id="nba-in", sport="nba")
        return await _seed_vb(store, g, sport="nba", flagged_at=_utc(2026, 6, 9, 0, 34))
    finally:
        await store.close()


def _reason_via_sqlite(db_path: Path, table: str, row_id: int) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"SELECT excluded_reason FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_cli_mark_excluded_stamps_rows(tmp_path, monkeypatch):
    from oddsiq import cli

    db_path = tmp_path / "cli.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    vb = await _seed_minimal_contaminated_db(db_path)

    code = await cli.mark_excluded(
        sports="nba,wnba",
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
        dry_run=False,
    )
    assert code == 0
    assert _reason_via_sqlite(db_path, "value_bets", vb) == REASON


@pytest.mark.asyncio
async def test_cli_mark_excluded_dry_run_writes_nothing(tmp_path, monkeypatch):
    from oddsiq import cli

    db_path = tmp_path / "cli.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    vb = await _seed_minimal_contaminated_db(db_path)

    code = await cli.mark_excluded(
        sports="nba,wnba",
        from_iso=FROM_ISO,
        until_iso=UNTIL_ISO,
        reason=REASON,
        dry_run=True,
    )
    assert code == 0
    assert _reason_via_sqlite(db_path, "value_bets", vb) is None


@pytest.mark.asyncio
async def test_cli_mark_excluded_rejects_inverted_window(tmp_path, monkeypatch):
    from oddsiq import cli

    db_path = tmp_path / "cli.db"
    monkeypatch.setattr("oddsiq.cli.config.DATABASE_PATH", db_path)
    vb = await _seed_minimal_contaminated_db(db_path)

    code = await cli.mark_excluded(
        sports="nba",
        from_iso=UNTIL_ISO,
        until_iso=FROM_ISO,
        reason=REASON,
        dry_run=False,
    )
    assert code == 1
    assert _reason_via_sqlite(db_path, "value_bets", vb) is None
