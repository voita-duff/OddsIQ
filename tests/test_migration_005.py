import sqlite3
from pathlib import Path

from oddsiq.db import migrations

# live_edge_candidates also got the column in 005, but migration 011
# (2026-08-27) drops that table — the post-chain state has only these.
_TABLES = ["games", "odds_snapshots", "value_bets"]


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migration_005_adds_sport_and_backfills(tmp_path: Path):
    db = tmp_path / "t.db"
    # Apply all migrations up through 005 on a fresh DB.
    migrations.run(db)
    conn = sqlite3.connect(db)
    try:
        for t in _TABLES:
            assert "sport" in _columns(conn, t), f"{t} missing sport column"
        # Insert a game with no explicit sport -> defaults to 'nba'.
        conn.execute(
            "INSERT INTO games (external_id, home_team, away_team, scheduled_at) "
            "VALUES ('g1', 'A', 'B', '2026-06-01T00:00:00+00:00')"
        )
        conn.commit()
        row = conn.execute("SELECT sport FROM games WHERE external_id='g1'").fetchone()
        assert row[0] == "nba"
    finally:
        conn.close()


def test_migration_005_is_idempotent(tmp_path: Path):
    db = tmp_path / "t.db"
    migrations.run(db)
    migrations.run(db)  # second run must not raise
    conn = sqlite3.connect(db)
    try:
        assert "sport" in _columns(conn, "games")
    finally:
        conn.close()
