"""Migration runner.

Discovers oddsiq/db/migrations/NNN_*.py files, sorts by numeric prefix,
and applies each pending one in its own transaction. Version state lives
in a `schema_version` table the runner creates itself. Forward-only; no
down migrations.

Usage from cli.py:

    await asyncio.to_thread(migrations.run, config.DATABASE_PATH)

Uses stdlib sqlite3 (not aiosqlite), matching the oddsiq/backup.py
convention of wrapping sync sqlite3 in asyncio.to_thread.

The runner lives in this package's ``__init__.py`` rather than a sibling
``migrations.py`` module: Python resolves a package over a same-named
sibling, so a sibling module would be unreachable. The package and the
runner are the same import target — ``from oddsiq.db import migrations``
exposes both ``run()`` and the package's submodules (000_baseline.py,
001_*.py, ...) under the same namespace.
"""

from __future__ import annotations

import importlib
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# The runner lives in the package's __init__.py, so __file__ resolves to
# oddsiq/db/migrations/__init__.py and .parent is the package dir itself.
_MIGRATIONS_DIR = Path(__file__).parent
_MIGRATION_FILENAME_RE = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.py$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def run(db_path: Path) -> None:
    """Discover, sort, and apply pending migrations. Raises on failure.

    Opens sqlite3 connections to `db_path` (creating the file — and any
    missing parent directories — if absent). Each migration runs in its own
    transaction — a migration that raises rolls back in its entirety,
    including its schema_version row.
    """
    migrations = _discover()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # busy_timeout (the connect `timeout`) lets a second concurrent runner — the
    # pipeline and dashboard both migrate at startup — WAIT for the write lock
    # instead of crashing with "database is locked" on a fresh-volume first boot.
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        _ensure_schema_version_table(conn)
        current = _current_version(conn)
        pending = [m for m in migrations if m.version > current]
        for migration in pending:
            _apply_one(conn, migration)
            current = migration.version
        logger.info("Schema: version %d (up to date)", current if current >= 0 else 0)
    finally:
        conn.close()


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMP NOT NULL
        )"""
    )
    conn.commit()


def _discover() -> list[Migration]:
    """Find every NNN_name.py under oddsiq/db/migrations/ and return the
    sorted list of Migration records. Files that don't match the naming
    pattern (notably __init__.py itself) are ignored."""
    found: list[Migration] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.py")):
        m = _MIGRATION_FILENAME_RE.match(path.name)
        if m is None:
            continue
        version = int(m.group("version"))
        name = m.group("name")
        module_name = f"oddsiq.db.migrations.{path.stem}"
        module = importlib.import_module(module_name)
        apply_fn = getattr(module, "apply", None)
        if not callable(apply_fn):
            raise RuntimeError(
                f"Migration {path.name} is missing a callable apply(conn) function"
            )
        found.append(Migration(version=version, name=name, apply=apply_fn))
    found.sort(key=lambda migration: migration.version)
    return found


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), -1) FROM schema_version"
    ).fetchone()
    return int(row[0])


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> None:
    """Run migration.apply(conn) inside a transaction, then insert the
    schema_version row. Both operations share the transaction so a failed
    apply() leaves no schema_version trace."""
    logger.info("Running migration %03d_%s...", migration.version, migration.name)
    started = time.monotonic()
    try:
        # BEGIN IMMEDIATE grabs the write lock up front so two concurrent runners
        # serialize here rather than both reaching apply(). Once we hold the lock,
        # re-check the version: a runner that won the race may have already applied
        # this migration while we waited, in which case skip it instead of
        # re-running apply() and colliding on the schema_version PRIMARY KEY.
        conn.execute("BEGIN IMMEDIATE")
        if _current_version(conn) >= migration.version:
            conn.rollback()
            logger.info(
                "Migration %03d_%s already applied (concurrent runner); skipping",
                migration.version,
                migration.name,
            )
            return
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    elapsed = time.monotonic() - started
    logger.info(
        "Migration %03d_%s applied in %.2fs", migration.version, migration.name, elapsed
    )
