"""Migration 007: nullable `excluded_reason` on value_bets (it also added
the column to live_edge_candidates, dropped by migration 011 on 2026-08-27).

NULL = part of the evaluation dataset; a non-NULL slug marks known-bad
rows (e.g. the 2026-06 basketball matchView incident) that every
reporting surface hides. Stamping itself is an operator action
(`oddsiq mark-excluded`) covered in tests/test_excluded_rows.py.
"""

import sqlite3
from pathlib import Path

from oddsiq.db import migrations


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migration_007_adds_excluded_reason_to_value_bets(tmp_path: Path):
    db = tmp_path / "t.db"
    migrations.run(db)
    conn = sqlite3.connect(db)
    try:
        assert "excluded_reason" in _columns(conn, "value_bets")
    finally:
        conn.close()


def test_migration_007_fresh_rows_default_to_null(tmp_path: Path):
    """A newly inserted row must come out NULL (= included in reporting):
    the column is an operator-set flag, never set by the pipeline."""
    db = tmp_path / "t.db"
    migrations.run(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO games (external_id, home_team, away_team, scheduled_at, sport) "
            "VALUES ('g1','A','B','2026-06-09T00:44:00+00:00','nba')"
        )
        gid = conn.execute("SELECT id FROM games WHERE external_id='g1'").fetchone()[0]
        conn.execute(
            "INSERT INTO value_bets (game_id, bookmaker, market_type, selection, "
            "entry_odds, fair_odds, edge_pct, tier, sport) "
            f"VALUES ({gid}, 'tipsport', 'moneyline', 'away', 2.12, 2.06, 3.0, 'hot', 'nba')"
        )
        conn.commit()
        assert (
            conn.execute("SELECT excluded_reason FROM value_bets").fetchone()[0] is None
        )
    finally:
        conn.close()


def test_migration_007_is_idempotent(tmp_path: Path):
    db = tmp_path / "t.db"
    migrations.run(db)
    migrations.run(db)  # second run must not raise
    conn = sqlite3.connect(db)
    try:
        assert "excluded_reason" in _columns(conn, "value_bets")
    finally:
        conn.close()
