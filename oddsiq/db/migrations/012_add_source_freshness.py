"""Persist sharp-feed provenance on snapshots and value bets.

``odds_snapshots.source_updated_at`` is the upstream market timestamp supplied
by The Odds API. It is separate from OddsIQ's local ``captured_at``.
``value_bets.sharp_source_updated_at`` freezes the timestamp used for an edge
so later last-seen dedup bumps cannot rewrite the decision evidence.
Historical rows remain NULL.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    snapshot_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(odds_snapshots)").fetchall()
    }
    if "source_updated_at" not in snapshot_cols:
        conn.execute(
            "ALTER TABLE odds_snapshots ADD COLUMN source_updated_at TIMESTAMP"
        )

    value_bet_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(value_bets)").fetchall()
    }
    if "sharp_source_updated_at" not in value_bet_cols:
        conn.execute(
            "ALTER TABLE value_bets ADD COLUMN sharp_source_updated_at TIMESTAMP"
        )
