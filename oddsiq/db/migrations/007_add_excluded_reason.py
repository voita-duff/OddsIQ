"""Add a nullable excluded_reason data-quality flag to value_bets and
live_edge_candidates.

NULL means the row is part of the evaluation dataset. A non-NULL reason
slug marks a row whose recorded prices are known-bad and excludes it from
every REPORTING surface (dashboard queries, per-book audit, live-scout
report) while preserving it for audit. Pipeline-lifecycle queries
(settlement, CLV recovery, markout resolution) do not filter on it.

Introduced for the 2026-06-02..2026-06-12 basketball matchView incident:
Tipsport's regulation 3-way prices were scraped as the NBA moneyline
(fixed in `a1669a4`), inflating edge/CLV on affected rows. Stamping is a
one-shot operator action via `oddsiq mark-excluded`, not part of this
migration — production bounds depend on the VM's deploy times.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    for table in ("value_bets", "live_edge_candidates"):
        cols = {row[1] for row in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        if "excluded_reason" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN excluded_reason TEXT")
