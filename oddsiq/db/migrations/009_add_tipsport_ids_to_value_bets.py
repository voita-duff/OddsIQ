"""Add nullable Tipsport execution-identity columns to value_bets.

`tipsport_match_id`, `tipsport_opp_id`, and `tipsport_url` are Tipsport's own
identifiers for the market a bet was detected on — the match id, the
opportunity (selection) id, and the site-relative deep-link url. They are
captured at detection time by EdgeService (threaded from the parser through
``find_edges`` as transient snapshot fields) so the future P0 availability
probe — and any later placement — can address the exact offer instead of
re-matching by team name, and so value-bet Telegram alerts can carry a
clickable deep link.

Historical rows stay NULL. ``odds_snapshots`` is deliberately untouched: its
dedup key and "last seen" bump semantics must not change.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(value_bets)").fetchall()}
    if "tipsport_match_id" not in cols:
        conn.execute("ALTER TABLE value_bets ADD COLUMN tipsport_match_id INTEGER")
    if "tipsport_opp_id" not in cols:
        conn.execute("ALTER TABLE value_bets ADD COLUMN tipsport_opp_id INTEGER")
    if "tipsport_url" not in cols:
        conn.execute("ALTER TABLE value_bets ADD COLUMN tipsport_url TEXT")
