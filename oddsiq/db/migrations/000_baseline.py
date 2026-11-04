"""Baseline migration: the Phase 1 schema that existed before the migration
system was introduced.

Every statement uses IF NOT EXISTS so the migration is a no-op on the user's
existing `oddsiq.db` (where the tables already exist) and a full bootstrap
on a fresh install.
"""

from __future__ import annotations

import sqlite3

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT UNIQUE,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    scheduled_at TIMESTAMP NOT NULL,
    status TEXT DEFAULT 'scheduled',
    home_score INTEGER,
    away_score INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    bookmaker TEXT NOT NULL,
    market_type TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL,
    odds REAL NOT NULL,
    is_closing BOOLEAN DEFAULT 0,
    raw_response_path TEXT,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS value_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    bookmaker TEXT NOT NULL,
    market_type TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL,
    entry_odds REAL NOT NULL,
    fair_odds REAL NOT NULL,
    edge_pct REAL NOT NULL,
    closing_odds REAL,
    pinnacle_closing REAL,
    clv_pct REAL,
    result TEXT,
    stake REAL,
    flagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_odds_game_book_market
    ON odds_snapshots(game_id, bookmaker, market_type);
CREATE INDEX IF NOT EXISTS idx_odds_captured
    ON odds_snapshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_odds_book_captured
    ON odds_snapshots(bookmaker, captured_at);
CREATE INDEX IF NOT EXISTS idx_odds_game_book_captured
    ON odds_snapshots(game_id, bookmaker, captured_at);
CREATE INDEX IF NOT EXISTS idx_value_bets_game
    ON value_bets(game_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_value_bets_unique_edge
    ON value_bets(game_id, bookmaker, market_type, selection);
"""


def apply(conn: sqlite3.Connection) -> None:
    """Create every Phase 1 table and index. Idempotent via IF NOT EXISTS."""
    for statement in TABLES_SQL.strip().split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)
    for statement in INDEXES_SQL.strip().split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)
