"""Add bookmaker-derived effective tipoff time to games.

`scheduled_at` remains the scraper-derived scheduled start. `commence_time` is
nullable and populated from Pinnacle event data when available; existing rows
fall back to `scheduled_at` with no backfill.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(games)").fetchall()
    }
    if "commence_time" not in columns:
        conn.execute("ALTER TABLE games ADD COLUMN commence_time TIMESTAMP")
