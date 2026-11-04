from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# The single source of truth for the hooks that snapshot the DB: the CLI's
# startup/shutdown pair (oddsiq/cli.py) and the scheduler's in-process tick every
# BACKUP_INTERVAL_HOURS (Scheduler._maybe_periodic_backup). Validation, the
# retention filename regex, and the tiebreak below all derive from this one dict —
# "periodic" was missing from the hand-synced validation set from 2026-08-09
# (dc37a42, when the tick shipped) until 2026-09-06, so every periodic backup
# raised ValueError instead of writing a file.
#
# The values are a total order for timestamp ties (two files in the same
# wall-clock second must never be ordered by filesystem iteration): shutdown
# captures the session's final state, periodic a mid-session live state, and a
# later session's startup the same data one run later — the stalest view.
_MOMENT_RANK = {"startup": 0, "periodic": 1, "shutdown": 2}
_VALID_MOMENTS = frozenset(_MOMENT_RANK)

# Derived, so a new moment can never be accepted by _format_backup_filename yet
# stay invisible to retention pruning (never counted toward the keep budget,
# never deleted).
_BACKUP_FILENAME_RE = re.compile(
    r"^oddsiq-(?P<date>\d{8})-(?P<time>\d{6})-(?P<moment>"
    + "|".join(re.escape(m) for m in sorted(_MOMENT_RANK))
    + r")\.db$"
)


def _format_backup_filename(timestamp: datetime, moment: str) -> str:
    if moment not in _VALID_MOMENTS:
        raise ValueError(
            f"moment must be one of {sorted(_VALID_MOMENTS)}, got {moment!r}"
        )
    return f"oddsiq-{timestamp.strftime('%Y%m%d-%H%M%S')}-{moment}.db"


def _run_integrity_check(db_path: Path) -> bool:
    """Return True iff `PRAGMA integrity_check` returns exactly 'ok'."""
    if not db_path.exists():
        logger.warning("Integrity check: file %s does not exist", db_path)
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning("Integrity check could not open %s: %s", db_path, e)
        return False
    except OSError as e:
        logger.warning("Integrity check could not access %s: %s", db_path, e)
        return False
    result = row[0] if row else None
    if result != "ok":
        logger.warning("Integrity check on %s returned: %r", db_path, result)
        return False
    return True


def backup_database(
    db_path: Path,
    backup_dir: Path,
    moment: str,
    *,
    keep_daily: int = 7,
    keep_monthly: int = 6,
) -> Path | None:
    """Snapshot `db_path` into `backup_dir` using sqlite3's online backup API.

    Uses the stdlib online backup API (not shutil.copy2) so the result is WAL-safe:
    the destination is a single self-contained .db file with any WAL pages merged in.

    Returns the new backup path on success, or None on any failure (logged as WARNING).
    A failed integrity check deletes the corrupt file. After a successful write,
    old backups are pruned to (keep_daily most-recent days + keep_monthly most-recent months).
    """
    if not db_path.exists():
        logger.warning("Backup skipped: source database %s does not exist", db_path)
        return None

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Backup skipped: could not create %s: %s", backup_dir, e)
        return None

    timestamp = datetime.now(timezone.utc)
    dest = backup_dir / _format_backup_filename(timestamp, moment)

    try:
        src_conn = sqlite3.connect(db_path)
        try:
            dst_conn = sqlite3.connect(dest)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except (sqlite3.Error, OSError) as e:
        logger.warning("Backup failed writing %s: %s", dest, e)
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    if not _run_integrity_check(dest):
        logger.warning("Backup %s failed integrity check; deleting", dest)
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    logger.info("Backup saved: %s", dest)

    try:
        _prune_old_backups(backup_dir, keep_daily, keep_monthly, protect=dest)
    except Exception as e:
        logger.warning("Retention pruning failed (backup itself succeeded): %s", e)

    return dest


def _prune_old_backups(
    backup_dir: Path,
    keep_daily: int,
    keep_monthly: int,
    *,
    protect: Path | None = None,
) -> list[Path]:
    """Delete old backups, keeping the latest per day (up to keep_daily distinct days)
    and the latest per month (up to keep_monthly distinct months).

    Returns the sorted list of files kept. Files not matching our naming convention
    are ignored entirely (never deleted, never counted).
    """
    if not backup_dir.is_dir():
        return []

    parsed: list[tuple[Path, str, str]] = []
    for path in backup_dir.iterdir():
        if not path.is_file():
            continue
        m = _BACKUP_FILENAME_RE.match(path.name)
        if m is None:
            continue
        parsed.append((path, m.group("date"), m.group("time")))

    if not parsed:
        return []

    def _tiebreak(path_name: str) -> int:
        # Deterministic tiebreak when two backups share a timestamp: rank by
        # moment via _MOMENT_RANK, which is a TOTAL order (see its definition) —
        # without one, two tying files compare equal and filesystem iteration
        # order silently decides which is deleted. Only names that already
        # matched the regex reach here; an unparseable one loses every tie.
        m = _BACKUP_FILENAME_RE.match(path_name)
        if m is None:
            return -1
        return _MOMENT_RANK[m.group("moment")]

    by_date: dict[str, tuple[Path, str]] = {}
    for path, date_str, time_str in parsed:
        cur = by_date.get(date_str)
        if cur is None or (time_str, _tiebreak(path.name)) > (
            cur[1],
            _tiebreak(cur[0].name),
        ):
            by_date[date_str] = (path, time_str)
    newest_dates = sorted(by_date.keys(), reverse=True)[:keep_daily]
    daily_keep: set[Path] = {by_date[d][0] for d in newest_dates}

    by_month: dict[str, tuple[Path, str, str]] = {}
    for path, date_str, time_str in parsed:
        month = date_str[:6]
        cur = by_month.get(month)
        if cur is None or (date_str, time_str, _tiebreak(path.name)) > (
            cur[1],
            cur[2],
            _tiebreak(cur[0].name),
        ):
            by_month[month] = (path, date_str, time_str)
    newest_months = sorted(by_month.keys(), reverse=True)[:keep_monthly]
    monthly_keep: set[Path] = {by_month[m][0] for m in newest_months}

    keep: set[Path] = daily_keep | monthly_keep
    if protect is not None:
        # Never delete the backup we just wrote — even under a 0/0 retention
        # misconfig. Returning a path to a deleted file would silently mask
        # the loss (the caller's None-check wouldn't fire).
        keep.add(protect)
    # Always preserve the single most-recent backup, even under 0/0 retention or
    # a call that forgot `protect` — never leave the archive empty.
    newest = max(parsed, key=lambda t: (t[1], t[2], _tiebreak(t[0].name)))[0]
    keep.add(newest)

    for path, _, _ in parsed:
        if path not in keep:
            try:
                path.unlink()
            except OSError as e:
                logger.warning("Could not delete old backup %s: %s", path, e)

    return sorted(keep)
