"""Migration 012: persist upstream sharp-feed freshness evidence."""

import sqlite3

from oddsiq.db import migrations


def test_migration_012_adds_nullable_source_freshness_columns(tmp_path):
    db = tmp_path / "t.db"
    migrations.run(db)
    conn = sqlite3.connect(db)
    try:
        snapshot_cols = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(odds_snapshots)").fetchall()
        }
        value_bet_cols = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(value_bets)").fetchall()
        }
        assert "source_updated_at" in snapshot_cols
        assert "sharp_source_updated_at" in value_bet_cols
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version=12"
        ).fetchone()[0] == 1
    finally:
        conn.close()
