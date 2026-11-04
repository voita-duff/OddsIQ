from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oddsiq.backup import (
    _BACKUP_FILENAME_RE,
    _MOMENT_RANK,
    _VALID_MOMENTS,
    _format_backup_filename,
    _prune_old_backups,
    _run_integrity_check,
    backup_database,
)


def _make_valid_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()


def _touch(p: Path) -> None:
    p.write_bytes(b"")


def test_format_backup_filename_startup():
    ts = datetime(2026, 4, 21, 14, 30, 22, tzinfo=timezone.utc)
    assert _format_backup_filename(ts, "startup") == "oddsiq-20260421-143022-startup.db"


def test_format_backup_filename_shutdown():
    ts = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert _format_backup_filename(ts, "shutdown") == "oddsiq-20261231-235959-shutdown.db"


def test_format_backup_filename_periodic():
    """The periodic in-process hook (`BACKUP_INTERVAL_HOURS`) asks for this third
    moment. It was rejected from 2026-08-09 to 2026-09-06, so no `-periodic`
    backup ever existed; the name must share the startup/shutdown shape."""
    ts = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    assert _format_backup_filename(ts, "periodic") == "oddsiq-20260906-120000-periodic.db"


def test_format_backup_filename_rejects_bad_moment():
    """Genuinely unknown moments still raise — widening for `periodic` must not
    turn the tag into a free-form string that silently escapes retention."""
    ts = datetime(2026, 4, 21, tzinfo=timezone.utc)
    for bogus in ("startupp", "weekly"):
        with pytest.raises(ValueError):
            _format_backup_filename(ts, bogus)


def test_integrity_check_ok_on_valid_db(tmp_path: Path):
    db = tmp_path / "ok.db"
    _make_valid_sqlite(db)
    assert _run_integrity_check(db) is True


def test_integrity_check_fails_on_corrupt_file(tmp_path: Path):
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"not a sqlite database")
    assert _run_integrity_check(bad) is False


def test_integrity_check_fails_on_missing_file(tmp_path: Path):
    missing = tmp_path / "missing.db"
    assert _run_integrity_check(missing) is False


def test_backup_creates_file_in_target_dir(tmp_path: Path):
    src = tmp_path / "live.db"
    _make_valid_sqlite(src)
    backup_dir = tmp_path / "backups"

    result = backup_database(src, backup_dir, "startup")

    assert result is not None
    assert result.exists()
    assert result.parent == backup_dir
    assert result.name.startswith("oddsiq-")
    assert result.name.endswith("-startup.db")


def test_backup_contents_match_source(tmp_path: Path):
    src = tmp_path / "live.db"
    _make_valid_sqlite(src)
    backup_dir = tmp_path / "backups"

    result = backup_database(src, backup_dir, "shutdown")

    assert result is not None
    conn = sqlite3.connect(result)
    rows = conn.execute("SELECT v FROM t").fetchall()
    conn.close()
    assert rows == [("hello",)]


def test_backup_writes_a_valid_file_for_the_periodic_moment(tmp_path: Path):
    """`Scheduler._maybe_periodic_backup()` calls this with moment="periodic".
    Until 2026-09-06 it raised ValueError from outside any try, so the scheduler's
    main loop lost the rest of that cycle instead of writing a backup."""
    src = tmp_path / "live.db"
    _make_valid_sqlite(src)
    backup_dir = tmp_path / "backups"

    result = backup_database(src, backup_dir, "periodic")

    assert result is not None
    assert result.exists()
    assert result.parent == backup_dir
    assert result.name.startswith("oddsiq-")
    assert result.name.endswith("-periodic.db")
    assert _run_integrity_check(result) is True

    conn = sqlite3.connect(result)
    try:
        rows = conn.execute("SELECT v FROM t").fetchall()
    finally:
        conn.close()
    assert rows == [("hello",)]


def test_backup_returns_none_when_source_missing(tmp_path: Path):
    src = tmp_path / "nope.db"
    backup_dir = tmp_path / "backups"

    result = backup_database(src, backup_dir, "startup")

    assert result is None
    assert not backup_dir.exists() or not list(backup_dir.iterdir())


def test_backup_creates_backup_dir_if_missing(tmp_path: Path):
    src = tmp_path / "live.db"
    _make_valid_sqlite(src)
    backup_dir = tmp_path / "deep" / "nested" / "backups"
    assert not backup_dir.exists()

    result = backup_database(src, backup_dir, "startup")

    assert result is not None
    assert backup_dir.is_dir()


def test_backup_deletes_file_if_integrity_check_fails(tmp_path: Path, monkeypatch):
    src = tmp_path / "live.db"
    _make_valid_sqlite(src)
    backup_dir = tmp_path / "backups"

    from oddsiq import backup as backup_mod
    monkeypatch.setattr(backup_mod, "_run_integrity_check", lambda p: False)

    result = backup_database(src, backup_dir, "startup")

    assert result is None
    assert list(backup_dir.iterdir()) == []


def test_backup_captures_wal_data_from_open_connection(tmp_path: Path):
    """Empirical validation of why we use sqlite3.Connection.backup() over shutil.copy2.

    In WAL mode, a committed row can live entirely in the .db-wal file until checkpoint.
    A naive shutil.copy2 of just the .db file would miss it; the online backup API merges
    WAL pages into the destination and produces a complete snapshot.

    The test proves: with a live WAL-mode connection holding uncheckpointed committed data,
    backup_database still produces a backup containing that data.
    """
    src = tmp_path / "live.db"

    # Open a persistent connection in WAL mode, write data, do NOT checkpoint or close.
    live_conn = sqlite3.connect(src)
    try:
        live_conn.execute("PRAGMA journal_mode=WAL")
        live_conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        live_conn.execute("INSERT INTO t (v) VALUES ('wal_only')")
        live_conn.commit()

        # Confirm we actually created a live WAL file (proof the test setup is realistic).
        wal_file = src.with_suffix(src.suffix + "-wal")
        assert wal_file.exists() and wal_file.stat().st_size > 0, (
            "Test setup expected a non-empty WAL file; environment may not support WAL."
        )

        backup_dir = tmp_path / "backups"
        result = backup_database(src, backup_dir, "startup")

        assert result is not None
        # Backup must be a single self-contained file with no -wal/-shm siblings.
        siblings = list(backup_dir.iterdir())
        assert len(siblings) == 1, f"expected one file in backup dir, got {siblings}"

        # The backup must contain the row that was only in the live WAL.
        backup_conn = sqlite3.connect(result)
        try:
            rows = backup_conn.execute("SELECT v FROM t").fetchall()
        finally:
            backup_conn.close()
        assert rows == [("wal_only",)]
    finally:
        live_conn.close()


def test_prune_keeps_last_n_daily(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    for day in range(1, 11):
        _touch(backup_dir / f"oddsiq-202604{day:02d}-120000-shutdown.db")

    kept = _prune_old_backups(backup_dir, keep_daily=3, keep_monthly=0)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == [
        "oddsiq-20260408-120000-shutdown.db",
        "oddsiq-20260409-120000-shutdown.db",
        "oddsiq-20260410-120000-shutdown.db",
    ]
    assert set(kept) == {backup_dir / n for n in remaining}


def test_prune_keeps_latest_per_day_when_multiple_on_same_day(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _touch(backup_dir / "oddsiq-20260421-090000-startup.db")
    _touch(backup_dir / "oddsiq-20260421-230000-shutdown.db")
    _touch(backup_dir / "oddsiq-20260420-120000-shutdown.db")

    _prune_old_backups(backup_dir, keep_daily=1, keep_monthly=0)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == ["oddsiq-20260421-230000-shutdown.db"]


def test_prune_keeps_monthly_archive_in_addition_to_daily(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _touch(backup_dir / "oddsiq-20260115-120000-shutdown.db")
    _touch(backup_dir / "oddsiq-20260315-120000-shutdown.db")
    _touch(backup_dir / "oddsiq-20260410-120000-shutdown.db")
    _touch(backup_dir / "oddsiq-20260411-120000-shutdown.db")
    _touch(backup_dir / "oddsiq-20260412-120000-shutdown.db")

    _prune_old_backups(backup_dir, keep_daily=2, keep_monthly=2)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == [
        "oddsiq-20260315-120000-shutdown.db",
        "oddsiq-20260411-120000-shutdown.db",
        "oddsiq-20260412-120000-shutdown.db",
    ]


def test_prune_ignores_unknown_filenames(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _touch(backup_dir / "oddsiq-20260115-120000-shutdown.db")
    _touch(backup_dir / "oddsiq-20260410-120000-shutdown.db")
    _touch(backup_dir / "not-our-file.db")
    _touch(backup_dir / "README.txt")

    _prune_old_backups(backup_dir, keep_daily=1, keep_monthly=0)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == [
        "README.txt",
        "not-our-file.db",
        "oddsiq-20260410-120000-shutdown.db",
    ]


def test_prune_on_empty_dir_noop(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    kept = _prune_old_backups(backup_dir, keep_daily=7, keep_monthly=6)
    assert kept == []


def test_prune_on_missing_dir_noop(tmp_path: Path):
    backup_dir = tmp_path / "does-not-exist"
    kept = _prune_old_backups(backup_dir, keep_daily=7, keep_monthly=6)
    assert kept == []


def test_prune_keeps_newest_even_without_protect(tmp_path: Path):
    """Under 0/0 retention with no explicit protect, pruning must still keep the
    single most-recent backup — never leave the archive empty (defensive against
    a future caller that prunes without passing protect)."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _touch(backup_dir / "oddsiq-20260410-120000-shutdown.db")
    _touch(backup_dir / "oddsiq-20260411-120000-shutdown.db")

    kept = _prune_old_backups(backup_dir, keep_daily=0, keep_monthly=0)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == ["oddsiq-20260411-120000-shutdown.db"]
    assert [p.name for p in kept] == ["oddsiq-20260411-120000-shutdown.db"]


def test_prune_prefers_shutdown_over_startup_on_timestamp_collision(tmp_path: Path):
    """When startup and shutdown backups land in the same wall-clock second, pruning
    must be deterministic. Prefer shutdown (represents the final state of the session)
    over startup (represents the stale pre-run view). Without this, non-deterministic
    iter order can silently delete whichever file filesystem listed first."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _touch(backup_dir / "oddsiq-20260421-183538-startup.db")
    _touch(backup_dir / "oddsiq-20260421-183538-shutdown.db")

    _prune_old_backups(backup_dir, keep_daily=1, keep_monthly=0)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == ["oddsiq-20260421-183538-shutdown.db"]


def test_prune_treats_periodic_as_one_pool_with_startup_and_shutdown(tmp_path: Path):
    """Periodic backups join the SAME daily/monthly retention pool as the other
    moments — one pool keyed by date/time, not a per-moment quota. Before
    2026-09-06 `_BACKUP_FILENAME_RE` matched only startup|shutdown, so a
    `-periodic` file would have been invisible to pruning: never counted toward
    the budget and never deleted (unbounded growth once the moment was accepted)."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _touch(backup_dir / "oddsiq-20260115-120000-periodic.db")  # old month → pruned
    _touch(backup_dir / "oddsiq-20260315-120000-startup.db")   # kept: monthly archive
    _touch(backup_dir / "oddsiq-20260410-120000-periodic.db")  # outside keep_daily → pruned
    _touch(backup_dir / "oddsiq-20260411-120000-periodic.db")  # kept: daily
    _touch(backup_dir / "oddsiq-20260412-120000-shutdown.db")  # kept: daily + newest month

    _prune_old_backups(backup_dir, keep_daily=2, keep_monthly=2)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == [
        "oddsiq-20260315-120000-startup.db",
        "oddsiq-20260411-120000-periodic.db",
        "oddsiq-20260412-120000-shutdown.db",
    ]


def test_prune_tiebreak_is_total_across_all_three_moments(tmp_path: Path):
    """The timestamp tiebreak must be a TOTAL order now that three moments exist,
    or two files sharing a wall-clock second compare equal and filesystem iteration
    order silently decides which one dies. Ranking: shutdown (session's final
    state) > periodic (mid-session live state) > startup (stale pre-run view)."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _touch(backup_dir / "oddsiq-20260421-183538-startup.db")
    _touch(backup_dir / "oddsiq-20260421-183538-periodic.db")

    _prune_old_backups(backup_dir, keep_daily=1, keep_monthly=0)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == ["oddsiq-20260421-183538-periodic.db"]


def test_prune_prefers_shutdown_over_periodic_on_timestamp_collision(tmp_path: Path):
    """The top rung of the same total order: a periodic tick and the shutdown
    backup can share a wall-clock second (SIGTERM landing right after a tick).
    Shutdown — the session's final state — must win, deterministically, rather
    than letting filesystem iteration order pick the survivor."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _touch(backup_dir / "oddsiq-20260421-183538-periodic.db")
    _touch(backup_dir / "oddsiq-20260421-183538-shutdown.db")

    _prune_old_backups(backup_dir, keep_daily=1, keep_monthly=0)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == ["oddsiq-20260421-183538-shutdown.db"]


def test_moment_rank_is_the_single_source_of_truth():
    """`_MOMENT_RANK` names the accepted moments ONCE; the validation set and the
    retention filename regex both derive from it. Hand-syncing three definitions
    is exactly what shipped `periodic` accepted-by-nobody (2026-08-09 → 2026-09-06:
    every periodic backup raised ValueError, and had it not, the regex would have
    hidden the file from pruning — never counted, never deleted)."""
    assert set(_VALID_MOMENTS) == set(_MOMENT_RANK)
    for moment in _MOMENT_RANK:
        assert _BACKUP_FILENAME_RE.match(f"oddsiq-20260421-183538-{moment}.db"), moment
    assert _BACKUP_FILENAME_RE.match("oddsiq-20260421-183538-weekly.db") is None


def test_backup_database_prunes_after_writing(tmp_path: Path):
    src = tmp_path / "live.db"
    _make_valid_sqlite(src)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _touch(backup_dir / "oddsiq-20260101-120000-shutdown.db")
    _touch(backup_dir / "oddsiq-20260102-120000-shutdown.db")
    _touch(backup_dir / "oddsiq-20260103-120000-shutdown.db")

    result = backup_database(src, backup_dir, "shutdown", keep_daily=1, keep_monthly=0)

    assert result is not None
    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert len(remaining) == 1
    assert remaining[0] == result.name


def test_backup_keeps_just_written_file_under_zero_retention(tmp_path: Path):
    """keep_daily=0 AND keep_monthly=0 must still preserve the backup just
    written — otherwise backup_database returns a path to a deleted file and
    silently masks data loss (the caller's None-check never fires)."""
    src = tmp_path / "live.db"
    _make_valid_sqlite(src)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _touch(backup_dir / "oddsiq-20260101-120000-shutdown.db")

    result = backup_database(src, backup_dir, "shutdown", keep_daily=0, keep_monthly=0)

    assert result is not None
    assert result.exists()  # the just-written backup survives 0/0 retention
    names = sorted(p.name for p in backup_dir.iterdir())
    assert names == [result.name]  # old backup pruned; current one protected
