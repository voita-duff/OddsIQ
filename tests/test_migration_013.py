"""Migration 013: suspect evidence cannot suppress a later clean edge."""

import sqlite3

from oddsiq.db import migrations


def test_migration_013_scopes_value_bet_dedup_by_exclusion_reason(tmp_path):
    db = tmp_path / "t.db"
    migrations.run(db)
    conn = sqlite3.connect(db)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_value_bets_unique_edge'"
        ).fetchone()[0]
        assert "IFNULL(excluded_reason, '')" in sql
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=13"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
