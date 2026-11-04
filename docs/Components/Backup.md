---
tags:
  - component/data
  - status/complete
  - phase/p1
  - layer/storage
  - type/component
source-files:
  - oddsiq/backup.py
  - tests/test_backup.py
related:
  - "[[Backup and Retention]]"
  - "[[SQLite Store]]"
  - "[[CLI Entry Point]]"
  - "[[Scheduler]]"
  - "[[Data Model]]"
---

# Backup

## Purpose

Module-level implementation of OddsIQ's automated database backup. Writes WAL-safe snapshots of `oddsiq.db` on every pipeline startup and shutdown — and, since 2026-08-09, on a periodic in-process tick — validates them with `PRAGMA integrity_check`, and prunes old snapshots to a configurable daily + monthly retention window. For the operational view (when to use manual backups, verification checklist, archive strategy, storage projections), see [[Backup and Retention]].

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/backup.py` | `backup_database()` | Main entry point — WAL-safe `sqlite3.Connection.backup()` + integrity check + retention prune |
| `oddsiq/backup.py` | `_format_backup_filename()` | Filename generator (`oddsiq-YYYYMMDD-HHMMSS-{startup\|shutdown\|periodic}.db`); raises `ValueError` for any moment outside `_VALID_MOMENTS` |
| `oddsiq/backup.py` | `_MOMENT_RANK` | The ONE source of truth for the moment tags (`startup` 0, `periodic` 1, `shutdown` 2). `_VALID_MOMENTS` and `_BACKUP_FILENAME_RE` are both derived from it, and its values double as the total order `_prune_old_backups()` uses to break same-second timestamp ties (shutdown > periodic > startup) |
| `oddsiq/backup.py` | `_VALID_MOMENTS` | The accepted moment tags — `frozenset(_MOMENT_RANK)`, derived rather than hand-listed (the hand-synced set omitted `periodic` from 2026-08-09 to 2026-09-06) |
| `oddsiq/backup.py` | `_run_integrity_check()` | `PRAGMA integrity_check` wrapper — returns bool |
| `oddsiq/backup.py` | `_prune_old_backups()` | Retention policy: keeps latest per distinct day (×7) and per distinct month (×6) |
| `oddsiq/backup.py` | `_BACKUP_FILENAME_RE` | Validates the naming pattern during prune so manual archives are left alone. Its moment alternation is built by joining the sorted `_MOMENT_RANK` keys, so it cannot drift out of sync with `_VALID_MOMENTS` — a moment the regex didn't match would be invisible to retention (never counted toward the keep budget, never deleted) |
| `tests/test_backup.py` | — | 28 tests: filename formatting for all three moments, integrity-check truth table, WAL-safe capture, retention pruning, three-way tiebreak |

## Inputs

- **`db_path` (Path)** — source SQLite file to snapshot
- **`backup_dir` (Path)** — destination directory (created if missing)
- **`moment` (str)** — one of `"startup"`, `"shutdown"`, `"periodic"` (`_VALID_MOMENTS`); anything else raises `ValueError`
- **`keep_daily` (int, default 7)** — number of most-recent distinct days to keep
- **`keep_monthly` (int, default 6)** — number of most-recent distinct months to keep

## Outputs

- On success: returns the new backup's `Path`; writes `oddsiq-YYYYMMDD-HHMMSS-{startup|shutdown|periodic}.db` in `backup_dir`; prunes older files per the retention policy; logs `Backup saved: <path>` at INFO.
- On any failure (source missing, disk full, permission error, backup-API error, integrity-check fail, retention-prune error): returns `None`, logs at WARNING, never raises. A corrupt destination file is deleted before returning.

## Interactions

- **[[CLI Entry Point]]** — wires `backup.backup_database()` into `async_main()`: fires once before `store.initialize()` (startup hook, captures prior session's final state) and once inside the `finally` block after `store.close()` (shutdown hook, captures this session's clean state). Both via `asyncio.to_thread(...)` because the stdlib `sqlite3` backup API is blocking.
- **[[Scheduler]]** — the third caller: `Scheduler._maybe_periodic_backup()` (`oddsiq/pipeline/scheduler.py`) ticks the same routine from the main loop every `BACKUP_INTERVAL_HOURS` (moment `"periodic"`). The startup/shutdown hooks only fire when the process does, so a long-lived container would otherwise leave a hole in the archive.
- **[[SQLite Store]]** — no direct dependency, but WAL-mode (`PRAGMA journal_mode=WAL`) is why this module uses `sqlite3.Connection.backup()` instead of `shutil.copy2`: only the online backup API merges `.db-wal` pages into a single self-contained destination.
- **[[Backup and Retention]]** — operational runbook: when to use manual backups, verification checklist, archive strategy for season boundaries, storage projections.

## Internal Logic

### Filename Format

```
oddsiq-YYYYMMDD-HHMMSS-{startup|shutdown|periodic}.db
```

Timestamp is UTC (`datetime.now(timezone.utc)`). The moment tag is required and validated — `_format_backup_filename()` raises `ValueError` for anything outside `_VALID_MOMENTS` (`startup`, `shutdown`, `periodic`). The regex `_BACKUP_FILENAME_RE` accepts those same three tags and validates the pattern during retention pruning so stray files in the backup directory (manual `cp` dumps, season archives) are left alone. Validation set and regex are both DERIVED from `_MOMENT_RANK` (the regex's moment alternation is built by joining that dict's sorted keys), so they cannot drift apart — adding a moment to the one dict teaches `_format_backup_filename()`, the retention regex, and the same-second tiebreak at once. The derivation replaced hand-synced literals after `periodic` sat missing from the validation set for a month (2026-08-09 → 2026-09-06, `ValueError` on every periodic tick); the symmetric failure it forecloses is a moment the generator accepts but the regex doesn't match, which would be pruned neither in nor out and accumulate forever.

### WAL-Safe Backup

```python
src_conn = sqlite3.connect(db_path)
dst_conn = sqlite3.connect(dest)
src_conn.backup(dst_conn)
```

The stdlib online backup API is the key WAL-safety mechanism. In WAL mode (`PRAGMA journal_mode=WAL`, set by [[SQLite Store]]), a `.db-wal` file can hold committed-but-uncheckpointed pages. A naïve `shutil.copy2` of just `oddsiq.db` would silently lose that data; copying `.db` + `.db-wal` + `.db-shm` as three separate files is not atomic. `sqlite3.Connection.backup()` opens a read-only source connection, merges WAL pages into a freshly-created destination DB, and produces a single self-contained `.db` file with no `-wal`/`-shm` siblings. Safe whether the source is idle or actively written.

`tests/test_backup.py::test_backup_captures_wal_data_from_open_connection` validates this property empirically: opens a source, writes data, then takes a backup without a prior checkpoint — the backup reads the same data.

### Integrity Check

Every successful write is immediately validated:

```python
row = conn.execute("PRAGMA integrity_check").fetchone()
if row[0] != "ok":
    dest.unlink(missing_ok=True)
    return None
```

A corrupt destination is deleted and the function returns `None`. The caller (CLI) logs this at WARNING but does not block pipeline startup or shutdown.

### Retention Pruning

After a successful write, `_prune_old_backups()` keeps:

- The latest backup per distinct day, for the `keep_daily` most-recent days (default 7).
- The latest backup per distinct month, for the `keep_monthly` most-recent months (default 6).

Both sets are unioned — a single backup can qualify under both rules (the newest daily also tends to be the newest monthly). Files that don't match the `oddsiq-YYYYMMDD-HHMMSS-{startup|shutdown|periodic}.db` regex are ignored entirely (never deleted, never counted toward the budget). All three moments share ONE pool: the rules key off the timestamp in the filename, not the moment tag, so a `-periodic` file can be the day's keeper or the month's archive exactly like a `-shutdown` one (there is no per-moment quota).

**Tiebreak:** when two backups share a timestamp (possible during CI or if two runs land on the same second), `_tiebreak()` ranks them `shutdown` (2) > `periodic` (1) > `startup` (0). The order must be TOTAL over all three moments — two files that compare equal leave the choice of which one to delete to `Path.iterdir()`, i.e. to filesystem iteration order, which is not deterministic. Rationale for the ranking: shutdown captures a session's final state; periodic captures a mid-session live state; startup of a later session captures the same data one run later, so it is the stalest view.

### Failure Mode Policy

A failed backup never blocks the pipeline. Every error path in `backup_database()`:

1. Logs at WARNING (not ERROR — operator attention is useful but the pipeline can proceed).
2. Deletes a partial destination file if one was created.
3. Returns `None` to the caller.

The retention-prune step is in its own `try/except` so that a pruning failure after a successful write doesn't invalidate the fresh backup.

## Configuration

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `BACKUP_ENABLED` | `"1"` → `True` | env var via `config.py` | Master switch; `"0"` disables all three hooks |
| `BACKUP_INTERVAL_HOURS` | `24` | env var via `config.py` | Cadence of the scheduler's periodic tick (`<=0` disables it; startup/shutdown are unaffected) |
| `BACKUP_DIR` | `./backups` | env var via `config.py` | Destination directory (tilde-expanded) |
| `BACKUP_KEEP_DAILY` | `7` | env var via `config.py` | Daily-retention count |
| `BACKUP_KEEP_MONTHLY` | `6` | env var via `config.py` | Monthly-retention count |

See [[Config Constants#Backup]] for the canonical table.

## Error Handling

- **Source missing**: `backup_database()` logs WARNING, returns `None` without creating a destination.
- **Backup-dir creation failure (`OSError`)**: same treatment.
- **`sqlite3.Error` during `src.backup(dst)`**: destination is deleted, function returns `None`.
- **Integrity-check failure**: destination is deleted, function returns `None`.
- **Retention-prune failure**: caught inside its own `try/except`, logged WARNING; the successful backup write is preserved.

## Known Issues

None open. Pruning semantics and filename format are covered by the 28 tests in `test_backup.py`. Historical: `periodic` was missing from `_VALID_MOMENTS` between 2026-08-09 and 2026-09-06, so every periodic tick raised `ValueError` instead of writing a file — fix record in [[Backup and Retention]].

## Tests

`tests/test_backup.py` (28 tests) covers:

- Filename formatting for startup / shutdown / periodic moments; `ValueError` on anything else.
- `PRAGMA integrity_check` returns `True` on a valid DB, `False` on corrupt, non-existent, or unreadable files.
- `backup_database()` writes a valid `.db` that passes integrity — including one named `-periodic.db` for the periodic moment; captures WAL data from an active connection; deletes destination on integrity failure; returns `None` on source-missing / permission-error / backup-API-error.
- `_prune_old_backups()` keeps the correct daily + monthly set; preserves files that don't match the naming pattern; pools periodic files with startup/shutdown in the one daily/monthly budget; applies the three-way total tiebreak (shutdown > periodic > startup) on a timestamp collision.

## See Also

- [[Backup and Retention]] — operational runbook (manual backups, verification checklist, season archiving, storage projections)
- [[SQLite Store]] — the WAL-mode database this module snapshots
- [[CLI Entry Point]] — wires the startup + shutdown hooks
- [[Scheduler]] — `_maybe_periodic_backup()`, the third caller, driven by `BACKUP_INTERVAL_HOURS`
- [[Data Model]] — schema whose integrity this module preserves
