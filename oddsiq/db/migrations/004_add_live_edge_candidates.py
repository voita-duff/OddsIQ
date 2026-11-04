"""Add live_edge_candidates for the live scout (Tipsport live moneyline).

Stores one row per qualifying live observation (a time series). Separate from
value_bets so the CLV/result lifecycle and pregame dashboard/analysis are not
contaminated by rows that can never earn CLV. See the live-scout design spec.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS live_edge_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL REFERENCES games(id),
            bookmaker TEXT NOT NULL,
            market_type TEXT NOT NULL,
            selection TEXT NOT NULL,
            entry_odds REAL NOT NULL,
            bookmaker_opposite_odds REAL,
            pinnacle_odds REAL NOT NULL,
            pinnacle_opposite_odds REAL NOT NULL,
            fair_odds REAL NOT NULL,
            edge_pct REAL NOT NULL,
            minutes_into_game INTEGER NOT NULL,
            observed_at TIMESTAMP NOT NULL,
            pinnacle_captured_at TIMESTAMP NOT NULL,
            result TEXT
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_live_edge_unique "
        "ON live_edge_candidates(game_id, bookmaker, market_type, selection, observed_at)"
    )
