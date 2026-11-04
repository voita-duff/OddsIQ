"""Add a denormalized `sport` column to every data table for the multi-sport
refactor. Existing rows backfill to 'nba' (all history is NBA). `sport` is NOT
part of any unique/dedup key — game_id already scopes each row to one sport;
this column is for fast per-sport filtering (dashboard tabs, audit)."""

from __future__ import annotations

import sqlite3

_TABLES = ("games", "odds_snapshots", "value_bets", "live_edge_candidates")


def apply(conn: sqlite3.Connection) -> None:
    for table in _TABLES:
        columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "sport" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN sport TEXT NOT NULL DEFAULT 'nba'"
            )
