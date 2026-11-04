"""Add markout-confirmation columns to live_edge_candidates.

The live scout re-checks each candidate one poll cycle (~60s) later against a
fresh Pinnacle fair to separate real soft-book edges from feed-lag mirages.
`markout_status` defaults to 'pending'; rows that exist at migration time
predate the feature and are backfilled to 'legacy'. See the live-edge
validation design spec (2026-06-08).
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(live_edge_candidates)").fetchall()}
    if "markout_status" not in cols:
        conn.execute("ALTER TABLE live_edge_candidates ADD COLUMN markout_fair_odds REAL")
        conn.execute("ALTER TABLE live_edge_candidates ADD COLUMN markout_edge_pct REAL")
        conn.execute("ALTER TABLE live_edge_candidates ADD COLUMN markout_captured_at TIMESTAMP")
        conn.execute(
            "ALTER TABLE live_edge_candidates "
            "ADD COLUMN markout_status TEXT NOT NULL DEFAULT 'pending'"
        )
        conn.execute("ALTER TABLE live_edge_candidates ADD COLUMN offer_stale INTEGER")
        # Every row present now predates the markout feature.
        conn.execute("UPDATE live_edge_candidates SET markout_status = 'legacy'")
