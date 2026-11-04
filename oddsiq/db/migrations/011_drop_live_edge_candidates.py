"""Drop the `live_edge_candidates` table — the live-edge subsystem was fully
removed 2026-08-27 (decision: no live betting exists or is planned, and
the live tier cost ~150-225 Odds API credits/day for data nobody acted on).

The complete dataset (46,921 rows, 2026-06-07 → 2026-08-27) was archived
BEFORE this migration ran on production: SQL dump + CSV + restore/reimplement
guide in an offline archive outside the repo, plus a tagged copy of
the code in the pre-release history. VM backups predating the drop
retain the table as well.

Idempotent: DROP TABLE IF EXISTS, so a DB that never had the table (or had it
dropped already) migrates cleanly. Migrations 004/006/008 (which created and
extended the table) stay in the chain — the runner is forward-only, and a
fresh DB simply creates the table in 004 and drops it here.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_live_edge_unique")
    conn.execute("DROP TABLE IF EXISTS live_edge_candidates")
