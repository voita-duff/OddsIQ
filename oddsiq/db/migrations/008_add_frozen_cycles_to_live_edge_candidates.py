"""Add a nullable frozen_cycles measurement to live_edge_candidates.

frozen_cycles records how many consecutive live polls the Tipsport price for a
candidate's (game, selection) had held unchanged at markout time (>=1; 1 = first
seen, never frozen). It is a measure-first signal: persisted so the live-scout
report can surface how often "edges" sit on a stuck/suspended soft line, and so
classify_markout can downgrade a would-be 'confirmed' edge whose line has been
frozen for >= LIVE_MARKOUT_FREEZE_REJECT_CYCLES polls (a stale offer, not value).

NULL means the row pre-dates this migration / was never marked out — distinct
from 1 (observed once, not frozen).
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(live_edge_candidates)").fetchall()
    }
    if "frozen_cycles" not in cols:
        conn.execute(
            "ALTER TABLE live_edge_candidates ADD COLUMN frozen_cycles INTEGER"
        )
