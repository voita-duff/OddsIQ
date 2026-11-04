"""Add `tier` to value_bets and include it in the uniqueness key.

Before this migration, value_bets uniqueness was
``(game_id, bookmaker, market_type, selection, IFNULL(line, -999.0))``.
With edge detection now firing in BOTH hot (<30 min) and medium (30 min-2 h)
tiers, a medium-tier detection at 11:00 AM (smaller edge) would silently
pre-empt a better hot-tier detection at 11:55 AM via ON CONFLICT DO NOTHING:

    1. Medium tier observes Tipsport home +5% edge → INSERT row at 11:00
    2. Hot tier observes the same selection at +8% edge → ON CONFLICT,
       drop the better entry. CLV is then computed against the 11:00 entry
       odds (locked to a price that may not have been actionable), and the
       Telegram alert for the better edge never fires.

Fix: add ``tier`` to value_bets and include it in the unique index. Each
tier's detection is now an independent observation:

- Two CLV computations per (game, book, market, selection, line) — one for
  the medium-tier entry, one for the hot-tier entry. Useful in evaluation
  mode to compare which tier's detections actually held up to closing.
- Each tier gets its own Telegram alert (operator sees both observations).

Existing rows backfill to ``tier='hot'`` because every value_bet captured
before this migration was hot-tier-detected (find_edges only ran in
``tier == 'hot'``). No data loss, no behavioral change for historical CLV.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    # Add the column with a NOT NULL default so existing rows pick up 'hot'
    # automatically — that matches their actual detection tier (the only
    # tier that fired find_edges before this migration).
    conn.execute("ALTER TABLE value_bets ADD COLUMN tier TEXT NOT NULL DEFAULT 'hot'")
    # Rebuild the unique index to include tier. The ON CONFLICT clause in
    # insert_value_bet must mirror this expression list exactly so SQLite
    # maps the conflict to this index.
    conn.execute("DROP INDEX IF EXISTS idx_value_bets_unique_edge")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_value_bets_unique_edge "
        "ON value_bets(game_id, bookmaker, market_type, selection, "
        "IFNULL(line, -999.0), tier)"
    )
