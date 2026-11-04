"""Add `line` to the value_bets uniqueness key.

The baseline unique index was (game_id, bookmaker, market_type, selection).
Under PINNACLE_MARKETS=h2h that's correct (moneyline has no line value). The
moment spreads or totals are enabled, two genuinely different bets — e.g.
Lakers spread -4.5 detected at 23:40 and Lakers spread -3.5 detected at 23:55
after the line moved — collapse onto the same tuple, and the second is
silently dropped via insert_value_bet's ON CONFLICT DO NOTHING.

Fix preemptively: include `line` in the unique key. Coalesce NULL to a
sentinel (-999.0, an impossible spread/total value) so moneyline dedup still
fires under SQLite's NULL-is-distinct-in-unique-indexes semantics. The
ON CONFLICT clause in insert_value_bet uses the same IFNULL expression so
SQLite matches it to this index.

No data backfill needed: existing rows are all moneyline (line IS NULL) and
remap cleanly to (..., IFNULL(NULL, -999.0)) = (..., -999.0) — same dedup
semantics as before for moneyline.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_value_bets_unique_edge")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_value_bets_unique_edge "
        "ON value_bets(game_id, bookmaker, market_type, selection, IFNULL(line, -999.0))"
    )
