"""Migration 009: nullable Tipsport execution-identity columns on value_bets.

`tipsport_match_id` / `tipsport_opp_id` / `tipsport_url` are Tipsport's own
identifiers for the market a bet was detected on — captured at detection time
so the future P0 availability probe (and any later placement) can address the
exact offer instead of re-matching by team name. Historical rows stay NULL.
"""

import sqlite3
from pathlib import Path

from oddsiq.db import migrations


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migration_009_adds_tipsport_id_columns(tmp_path: Path):
    db = tmp_path / "t.db"
    migrations.run(db)
    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, "value_bets")
        assert "tipsport_match_id" in cols
        assert "tipsport_opp_id" in cols
        assert "tipsport_url" in cols
        # odds_snapshots is deliberately untouched (dedup key / "last seen"
        # semantics must not change).
        assert "tipsport_match_id" not in _columns(conn, "odds_snapshots")
    finally:
        conn.close()


def test_migration_009_fresh_rows_default_to_null(tmp_path: Path):
    db = tmp_path / "t.db"
    migrations.run(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO games (external_id, home_team, away_team, scheduled_at, sport) "
            "VALUES ('g1','A','B','2026-08-09T00:00:00+00:00','mlb')"
        )
        gid = conn.execute("SELECT id FROM games WHERE external_id='g1'").fetchone()[0]
        conn.execute(
            "INSERT INTO value_bets (game_id, bookmaker, market_type, selection, "
            "entry_odds, fair_odds, edge_pct, tier, sport) "
            f"VALUES ({gid}, 'tipsport', 'moneyline', 'home', 2.1, 2.0, 5.0, 'hot', 'mlb')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT tipsport_match_id, tipsport_opp_id, tipsport_url FROM value_bets"
        ).fetchone()
        assert row == (None, None, None)
    finally:
        conn.close()
