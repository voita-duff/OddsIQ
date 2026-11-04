"""Tests for oddsiq.db.migrations — the migration runner.

Uses the real oddsiq/db/migrations/000_baseline.py for the happy-path
cases. Failure-path and fixture cases build ad-hoc Migration records
in-memory and feed them directly to the private helpers.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from oddsiq.db import migrations
from oddsiq.db.migrations import Migration


# ---------- Fixtures ----------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _make_migration(version: int, name: str, apply_fn) -> Migration:
    return Migration(version=version, name=name, apply=apply_fn)


def _count_tables(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]


# ---------- Discovery ----------

def test_discover_finds_baseline():
    found = migrations._discover()
    assert len(found) >= 1
    assert found[0].version == 0
    assert found[0].name == "baseline"
    assert callable(found[0].apply)


def test_discover_returns_sorted_by_version():
    found = migrations._discover()
    versions = [m.version for m in found]
    assert versions == sorted(versions)


# ---------- Schema version bootstrap ----------

def test_ensure_schema_version_creates_table(db_path):
    conn = sqlite3.connect(db_path)
    try:
        migrations._ensure_schema_version_table(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_ensure_schema_version_is_idempotent(db_path):
    conn = sqlite3.connect(db_path)
    try:
        migrations._ensure_schema_version_table(conn)
        migrations._ensure_schema_version_table(conn)
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_current_version_empty_returns_minus_one(db_path):
    conn = sqlite3.connect(db_path)
    try:
        migrations._ensure_schema_version_table(conn)
        assert migrations._current_version(conn) == -1
    finally:
        conn.close()


# ---------- Baseline idempotence ----------

def test_baseline_on_fresh_db_creates_phase1_tables(db_path):
    migrations.run(db_path)
    conn = sqlite3.connect(db_path)
    try:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        # baseline tables + schema_version
        assert {"games", "odds_snapshots", "value_bets", "meta", "schema_version"} <= names
    finally:
        conn.close()


def test_baseline_idempotent_against_preexisting_tables(db_path):
    # Pre-create the tables as they exist today (partial — enough to prove
    # IF NOT EXISTS works).
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE games (id INTEGER PRIMARY KEY, external_id TEXT UNIQUE, "
        "home_team TEXT NOT NULL, away_team TEXT NOT NULL, scheduled_at TIMESTAMP NOT NULL)"
    )
    conn.commit()
    conn.close()

    # Running the full migration must not raise.
    migrations.run(db_path)

    conn = sqlite3.connect(db_path)
    try:
        # schema_version now has row (0, baseline, ...)
        row = conn.execute(
            "SELECT version, name FROM schema_version WHERE version=0"
        ).fetchone()
        assert row is not None
        assert row[1] == "baseline"
    finally:
        conn.close()


# ---------- Skip-already-applied ----------

def test_second_run_is_noop_after_baseline(db_path, caplog):
    migrations.run(db_path)

    # Snapshot the post-first-run row count; second run must not change it.
    conn = sqlite3.connect(db_path)
    try:
        before_count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    finally:
        conn.close()
    assert before_count >= 1  # at least baseline applied

    # Discard first-run records before asserting on the second run.
    # Full-suite order can leave the migration logger capturing INFO already.
    caplog.clear()
    with caplog.at_level("INFO", logger="oddsiq.db.migrations"):
        migrations.run(db_path)

    # No "Running migration" log on the second call.
    assert not any("Running migration" in r.message for r in caplog.records)
    # Schema version row count unchanged.
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert count == before_count
    finally:
        conn.close()


def test_summary_log_always_fires(db_path, caplog):
    with caplog.at_level("INFO", logger="oddsiq.db.migrations"):
        migrations.run(db_path)
    assert any("Schema: version" in r.message for r in caplog.records)


# ---------- Transaction rollback on failure ----------

def test_failed_migration_rolls_back_entirely(db_path):
    """A migration that raises must leave no schema_version row AND no
    partial schema changes visible after the connection is closed."""
    migrations.run(db_path)  # baseline in place

    def broken_apply(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE should_not_survive (id INTEGER)")
        raise RuntimeError("simulated failure")

    broken = _make_migration(99, "broken_fixture", broken_apply)

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(RuntimeError, match="simulated failure"):
            migrations._apply_one(conn, broken)
    finally:
        conn.close()

    # Separate connection sees no should_not_survive table and no
    # schema_version row for version 99.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='should_not_survive'"
        ).fetchone()
        assert row is None
        row = conn.execute(
            "SELECT version FROM schema_version WHERE version=99"
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_apply_one_skips_migration_already_applied_concurrently(db_path):
    """Race-safety: the pipeline and dashboard both run migrations at startup.
    If a concurrent runner applied a migration while we waited for the write
    lock, _apply_one must re-check the version inside the transaction and SKIP
    it — not re-run apply() and crash on a duplicate schema_version row."""
    migrations.run(db_path)  # bring to latest; version 0 already recorded

    calls = {"n": 0}

    def already_applied_apply(conn: sqlite3.Connection) -> None:
        calls["n"] += 1  # must NOT run for an already-applied version

    dup = _make_migration(0, "baseline", already_applied_apply)

    conn = sqlite3.connect(db_path)
    try:
        migrations._apply_one(conn, dup)  # must not raise
    finally:
        conn.close()

    assert calls["n"] == 0  # apply() skipped, not re-run

    conn = sqlite3.connect(db_path)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version=0"
        ).fetchone()[0]
        assert cnt == 1  # no duplicate schema_version row
    finally:
        conn.close()


def test_failed_migration_preserves_prior_schema_version_rows(db_path):
    """Rollback of N+1 must not touch the schema_version row for N."""
    migrations.run(db_path)

    def broken_apply(conn: sqlite3.Connection) -> None:
        raise RuntimeError("boom")

    broken = _make_migration(99, "broken", broken_apply)
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(RuntimeError):
            migrations._apply_one(conn, broken)
    finally:
        conn.close()

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE version=0"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


# ---------- Success-criterion timing assertion ----------

def test_migration_001_rebuilds_value_bets_unique_index_with_line(db_path):
    """Migration 001 must drop the old (game, book, market, selection) unique
    index and replace it with one that includes IFNULL(line, -999.0)."""
    migrations.run(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_value_bets_unique_edge'"
        ).fetchone()
        assert row is not None, "idx_value_bets_unique_edge must exist post-migration"
        index_sql = row[0]
        assert "IFNULL(line, -999" in index_sql, (
            f"idx_value_bets_unique_edge must include IFNULL(line, -999.0); "
            f"got: {index_sql}"
        )
    finally:
        conn.close()


def test_migration_002_adds_nullable_commence_time_to_games(db_path):
    migrations.run(db_path)
    conn = sqlite3.connect(db_path)
    try:
        columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(games)").fetchall()
        }
        assert "commence_time" in columns
        assert columns["commence_time"][3] == 0  # nullable
    finally:
        conn.close()


def test_migration_003_adds_tier_with_hot_default(db_path):
    """Migration 003 must add value_bets.tier NOT NULL DEFAULT 'hot' so
    pre-migration rows (all hot-tier-detected) backfill cleanly."""
    migrations.run(db_path)
    conn = sqlite3.connect(db_path)
    try:
        columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(value_bets)").fetchall()
        }
        assert "tier" in columns, "value_bets.tier column must exist post-migration"
        # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
        assert columns["tier"][3] == 1, "tier must be NOT NULL"
        assert "'hot'" in str(columns["tier"][4]), (
            f"tier default must be 'hot' so existing rows backfill correctly; "
            f"got: {columns['tier'][4]}"
        )
    finally:
        conn.close()


def test_migration_003_unique_index_includes_tier(db_path):
    """The rebuilt unique index must include the tier column so medium and
    hot observations of the same (game, book, market, selection, line) can
    coexist as separate rows."""
    migrations.run(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_value_bets_unique_edge'"
        ).fetchone()
        assert row is not None
        index_sql = row[0]
        assert "tier" in index_sql, (
            f"idx_value_bets_unique_edge must include tier; got: {index_sql}"
        )
        # Line still part of the key — don't regress migration 001.
        assert "IFNULL(line" in index_sql
    finally:
        conn.close()


def test_migration_003_backfills_existing_rows_to_hot(db_path):
    """Pre-003 rows (no tier column) must end up with tier='hot' after the
    migration runs. Real production rows have all been hot-tier-detected
    so 'hot' is the correct default."""
    # Apply migrations 000-002 first (so we have a value_bets row INSERTed
    # without the tier column), then run 003 and verify the row picked up
    # the default.
    conn = sqlite3.connect(db_path)
    try:
        # Bring schema up to 002 manually by inserting a value_bet pre-003.
        from oddsiq.db.migrations import _ensure_schema_version_table  # noqa: PLC0415
        _ensure_schema_version_table(conn)

        # Apply 000, 001, 002 but not 003.
        from oddsiq.db.migrations import _discover, _apply_one  # noqa: PLC0415
        for m in _discover():
            if m.version >= 3:
                break
            _apply_one(conn, m)

        # Insert a pre-003 row (no tier column yet).
        conn.execute(
            "INSERT INTO games (external_id, home_team, away_team, scheduled_at) "
            "VALUES ('g1', 'A', 'B', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO value_bets "
            "(game_id, bookmaker, market_type, selection, entry_odds, fair_odds, edge_pct) "
            "VALUES (1, 'tipsport', 'moneyline', 'home', 2.10, 1.95, 7.7)"
        )
        conn.commit()
    finally:
        conn.close()

    # Now run pending migrations (003 onward).
    migrations.run(db_path)

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT tier FROM value_bets WHERE bookmaker='tipsport'"
        ).fetchone()
        assert row is not None
        assert row[0] == "hot", f"backfill default must be 'hot'; got {row[0]!r}"
    finally:
        conn.close()


def test_migration_011_drops_live_edge_candidates(tmp_path):
    """The live-edge subsystem was removed 2026-08-27: a fully migrated DB
    must NOT contain live_edge_candidates (004 creates it, 011 drops it),
    and re-running the chain must stay clean (DROP IF EXISTS idempotency)."""
    import sqlite3
    from oddsiq.db import migrations

    db = tmp_path / "m011.db"
    migrations.run(db)
    migrations.run(db)  # idempotent second run
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "live_edge_candidates" not in tables
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        assert "idx_live_edge_unique" not in indexes
    finally:
        conn.close()

def test_migration_004_is_idempotent(tmp_path):
    from oddsiq.db import migrations
    db = tmp_path / "m004b.db"
    migrations.run(db)
    migrations.run(db)  # second run must be a no-op, not raise


def test_run_on_up_to_date_db_under_100ms(db_path):
    """Spec success criterion: 'Migration runner overhead on an up-to-date
    DB is under 100ms end-to-end.' Measured against a pre-populated DB so
    only connection-open + schema_version bootstrap + MAX(version) read +
    summary log execute."""
    migrations.run(db_path)  # baseline applied once, first run doesn't count.

    started = time.monotonic()
    migrations.run(db_path)
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < 100, f"Runner took {elapsed_ms:.1f}ms (budget 100ms)"


def test_run_creates_missing_db_parent_dirs(tmp_path):
    """A nested, not-yet-existing DATABASE_PATH parent must be created — Docker
    mounts the DB at /data/db/oddsiq.db where /data/db doesn't exist on a fresh
    volume. Without this, sqlite3.connect raises 'unable to open database file'."""
    from oddsiq.db import migrations
    nested = tmp_path / "db" / "sub" / "oddsiq.db"
    migrations.run(nested)  # must not raise
    assert nested.exists()
