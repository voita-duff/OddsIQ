"""Allow a later clean detection after a suspect observation in one tier.

Freshness/coherence guards intentionally retain suspect rows for validation.
Without ``excluded_reason`` in the uniqueness key, that audit row would block
the same selection from becoming a clean, bettable detection after the feed
recovered. Clean rows still deduplicate against clean rows, and each suspect
reason deduplicates against itself.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_value_bets_unique_edge")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_value_bets_unique_edge "
        "ON value_bets(game_id, bookmaker, market_type, selection, "
        "IFNULL(line, -999.0), tier, IFNULL(excluded_reason, ''))"
    )
