"""Migration 010: the `placed_bets` table — one row per auto-betting DECISION
per value bet.

`value_bet_id` is UNIQUE so a double-bet on the same detection is impossible
by schema (no retries in Phase 1). Statuses: skipped | pending | placed |
rejected | failed | unknown | settled. See
docs/Design/2026-08-09-p1-auto-betting-design.md.
"""

import sqlite3
from pathlib import Path

from oddsiq.db import migrations


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _seed_value_bet(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO games (external_id, home_team, away_team, scheduled_at, sport) "
        "VALUES ('g1','A','B','2026-08-09T00:00:00+00:00','mlb')"
    )
    gid = conn.execute("SELECT id FROM games").fetchone()[0]
    conn.execute(
        "INSERT INTO value_bets (game_id, bookmaker, market_type, selection, "
        "entry_odds, fair_odds, edge_pct, tier, sport) "
        f"VALUES ({gid}, 'tipsport', 'moneyline', 'home', 2.1, 2.0, 5.0, 'hot', 'mlb')"
    )
    return conn.execute("SELECT id FROM value_bets").fetchone()[0]


def test_migration_010_creates_placed_bets(tmp_path: Path):
    db = tmp_path / "t.db"
    migrations.run(db)
    conn = sqlite3.connect(db)
    try:
        cols = _columns(conn, "placed_bets")
        assert {
            "id", "value_bet_id", "game_id", "sport", "status", "skip_reason",
            "stake_czk", "requested_odds", "min_odds", "accepted_odds",
            "receipt_id", "error", "balance_after_czk", "commanded_at",
            "resolved_at", "latency_detect_ms", "latency_place_ms",
            "settled_pl_czk", "created_at",
        } <= cols
    finally:
        conn.close()


def test_migration_010_value_bet_id_is_unique(tmp_path: Path):
    """One decision per value bet — the double-bet guard lives in the schema."""
    db = tmp_path / "t.db"
    migrations.run(db)
    conn = sqlite3.connect(db)
    try:
        vb_id = _seed_value_bet(conn)
        conn.execute(
            "INSERT INTO placed_bets (value_bet_id, game_id, sport, status) "
            f"VALUES ({vb_id}, 1, 'mlb', 'pending')"
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO placed_bets (value_bet_id, game_id, sport, status) "
                f"VALUES ({vb_id}, 1, 'mlb', 'pending')"
            )
            assert False, "second decision row for the same value bet must be rejected"
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()
