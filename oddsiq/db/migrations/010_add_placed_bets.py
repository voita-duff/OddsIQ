"""Create the `placed_bets` table — the auto-betting decision ledger.

One row per betting DECISION per value bet: either a `skipped` row with the
qualification failure reason, or an execution attempt that progresses through
`pending → placed | rejected | failed | unknown → settled`. The UNIQUE
constraint on `value_bet_id` makes a double-bet on the same detection
impossible at the schema level (Phase 1 has no retries — a skipped or failed
decision is terminal for that value bet). A later operator reconciliation may
resolve a pending/unknown row after checking book history; it is a state
correction, never another placement decision.

Money columns are suffixed `_czk` (Tipsport account currency). Latencies:
`latency_detect_ms` = flagged_at → command dispatch; `latency_place_ms` =
command dispatch → extension result. `settled_pl_czk` is set by the
settlement pass when the underlying value bet resolves.

See docs/Design/2026-08-09-p1-auto-betting-design.md.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS placed_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            value_bet_id INTEGER NOT NULL UNIQUE REFERENCES value_bets(id),
            game_id INTEGER NOT NULL REFERENCES games(id),
            sport TEXT NOT NULL,
            status TEXT NOT NULL,
            skip_reason TEXT,
            stake_czk REAL,
            requested_odds REAL,
            min_odds REAL,
            accepted_odds REAL,
            receipt_id TEXT,
            error TEXT,
            balance_after_czk REAL,
            commanded_at TIMESTAMP,
            resolved_at TIMESTAMP,
            latency_detect_ms INTEGER,
            latency_place_ms INTEGER,
            settled_pl_czk REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_placed_bets_game ON placed_bets(game_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_placed_bets_status ON placed_bets(status)"
    )
