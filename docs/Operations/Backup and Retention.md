---
tags:
  - type/runbook
  - layer/operations
  - status/complete
  - phase/p0
source-files:
  - oddsiq/cli.py
  - oddsiq/backup.py
related:
  - "[[SQLite Store]]"
  - "[[Data Model]]"
  - "[[CLI Entry Point]]"
  - "[[Runbook]]"
---

# Backup and Retention

The SQLite database (`oddsiq.db`) is the product — months of odds snapshots, CLV history, and bet resolution data are irreplaceable. Raw JSON responses in `./data/raw/` are secondary (useful for debugging parser changes).

Automated backups are wired into `cli.py` — see the `oddsiq/backup.py` module and the startup/shutdown hooks in `async_main()` — plus a periodic in-process tick driven from the scheduler's main loop. For the module-level view (filename format, WAL-safe mechanics, integrity-check path, retention implementation), see [[Backup]].

## Manual Backup

Occasionally useful for ad-hoc snapshots (the pipeline auto-backs up on every run, so this is rarely needed):

```bash
cp oddsiq.db ./backups/oddsiq-$(date +%Y%m%d)-manual.db
```

## Automated Backup (startup + shutdown + periodic)

Two hooks in `async_main()` (`oddsiq/cli.py`) call `backup.backup_database()` via `asyncio.to_thread`:

- **Startup hook** — fires BEFORE `store.initialize()`. Captures the prior session's final state intact, including any crashed-session WAL data that would otherwise be orphaned.
- **Shutdown hook** — fires INSIDE the `finally` block, AFTER `store.close()`. Captures this session's clean final state even if `scheduler.run()` raised.

If a backup fails (source missing, disk full, integrity check failure), `backup_database()` logs the specific WARNING (`Backup skipped: …`, `Backup failed writing …`, `Backup … failed integrity check; deleting`) and returns `None`; the caller then logs `Startup backup failed; see logs.` / `Shutdown backup failed; see logs.` and continues normally — a backup failure NEVER blocks startup or shutdown. Logged only: there is no phone alert since the 2026-08-10 rebrand ([[Telegram Notifications]] carries real-money events only), so backup health is watched via the log and the dashboard.

### Periodic (in-process, every `BACKUP_INTERVAL_HOURS`)

A third hook lives in the scheduler rather than the CLI: `Scheduler._maybe_periodic_backup()` (`oddsiq/pipeline/scheduler.py`), called from the scheduler's main loop, reuses the same `backup.backup_database()` routine — same WAL-safe copy, same integrity check, same retention pruning — on a `BACKUP_INTERVAL_HOURS` cadence (default 24; `<=0` disables, as does `BACKUP_ENABLED=0`). The first pass only arms the clock, since the CLI's startup backup has just run.

Why it exists: the startup/shutdown hooks only fire when the process does. The container ran 2026-07-22 → 2026-08-08 without a restart, so those two hooks produced nothing for 17 days — a 17-day hole in the archive of the one irreplaceable asset.

> **FIXED 2026-09-06 — the periodic hook produced no backup at all between 2026-08-09 and 2026-09-06.** The tick passed the moment `"periodic"`, but `backup._VALID_MOMENTS` was `{"startup", "shutdown"}` and `backup._format_backup_filename()` raised `ValueError` on anything else, from outside any `try` in `backup_database()`. Because `Scheduler._maybe_periodic_backup()` re-arms its clock (`self._last_periodic_backup = now`) *before* calling into the backup, the raise did not crash-loop: it fired once per `BACKUP_INTERVAL_HOURS` (~daily at the default 24), surfacing only as an `Error in scheduler main loop` traceback from the main loop's `except Exception` handler, which also cost the rest of that one cycle (budget alert, schedule refresh, polling, finals check) plus a 10 s sleep. Broken from the day the feature shipped (`dc37a42`, 2026-08-09), so **no `-periodic` file has ever existed on the VM** — the archive holds startup/shutdown snapshots only until the 2026-09-06 deploy. The fix widens `backup._VALID_MOMENTS` and `backup._BACKUP_FILENAME_RE` to accept `periodic` (the regex mattered too: an unmatched name is invisible to retention — never counted, never pruned) and makes the timestamp-collision tiebreak a total order over the three moments. Unknown moments still raise.

### API choice: `sqlite3.Connection.backup()` (not `shutil.copy2`)

The stdlib online backup API is used unconditionally at both moments. In WAL mode (`PRAGMA journal_mode=WAL`, `store.py`), a `.db-wal` file can hold committed-but-uncheckpointed pages. A naive `shutil.copy2` of just `oddsiq.db` would silently lose that data; copying `.db` + `.db-wal` + `.db-shm` as three separate files is not atomic. `sqlite3.Connection.backup()` opens a read-only source connection, merges WAL pages into a freshly-created destination DB, and produces a single self-contained `.db` file with no `-wal`/`-shm` siblings. Safe whether the source is idle or actively written. `tests/test_backup.py::test_backup_captures_wal_data_from_open_connection` validates this property empirically.

### Filename format

`oddsiq-YYYYMMDD-HHMMSS-{startup|shutdown|periodic}.db` — UTC timestamp plus moment tag. Example:

```
oddsiq-20260421-143022-shutdown.db
oddsiq-20260421-143022-periodic.db
```

`_format_backup_filename()` validates the tag and raises `ValueError` on anything outside those three. Backups predating 2026-09-06 carry only the `-startup` / `-shutdown` tags (see the fix record above).

### Integrity check

Every backup is immediately validated via `PRAGMA integrity_check`. If the check returns anything other than `ok`, the corrupt file is deleted and a WARNING is logged. Callers see `None` returned.

### Retention

After each successful write, old backups are pruned to:

- The latest backup per day, for the `BACKUP_KEEP_DAILY` most-recent distinct days (default 7).
- The latest backup per month, for the `BACKUP_KEEP_MONTHLY` most-recent distinct months (default 6).

All three moments share ONE retention pool — the rules key off the timestamp in the filename, not the moment tag, so a `-periodic` file can be the day's keeper or the month's archive exactly like a `-shutdown` one (there is no per-moment quota). Files not matching the `oddsiq-YYYYMMDD-HHMMSS-{startup|shutdown|periodic}.db` pattern in the backup directory are left alone.

**Retention horizon changed the moment the periodic hook started working (2026-09-06).** Before the fix the pool was fed only by process starts and stops, so on a container that ran for weeks the two hooks produced nothing and the caps rarely bit — the horizon was measured in restarts, not days. They did bite eventually, though: `keep_daily` counts distinct days that HAVE a backup, not calendar days, so once more than 7 restart days had accumulated the oldest fell out (Aug 25 → Sep 6 alone carries 7 distinct deploy dates). Now that a `-periodic` file lands every `BACKUP_INTERVAL_HOURS` (~daily at the default 24), the defaults `BACKUP_KEEP_DAILY=7` and `BACKUP_KEEP_MONTHLY=6` (`oddsiq/config.py`) are ENFORCED against a genuinely daily stream: one file per day survives for the 7 most-recent days (the latest by timestamp, with the moment tiebreak deciding a same-second tie), everything older collapses to one keeper per month for 6 months, and the rest is deleted. Practical consequence — a startup/shutdown snapshot taken at a deploy is pruned to (at most) its month's single archive within about 7 days, because each of the next 7 days brings its own periodic file that outranks it for a daily slot. Two mitigations: manually named copies such as `oddsiq-pre-zombie-delete-*.db`, `oddsiq-pre-live-edge-drop-*.db`, and `oddsiq-pre-wrong-game-stamp-*.db` do NOT match the auto-backup filename pattern, so retention never sees or prunes them (that is the right home for a snapshot taken before a destructive one-shot); and if deploy-time snapshots should simply survive longer, raise `BACKUP_KEEP_DAILY` in the VM `.env` — the database is ~54 MB, so a wider window costs little disk.

### Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `BACKUP_ENABLED` | `1` | Master switch (`0` disables all three hooks) |
| `BACKUP_INTERVAL_HOURS` | `24` | Periodic in-process cadence in hours (`<=0` disables); startup/shutdown are unaffected |
| `BACKUP_DIR` | `./backups` | Destination directory |
| `BACKUP_KEEP_DAILY` | `7` | Number of most-recent daily backups to keep |
| `BACKUP_KEEP_MONTHLY` | `6` | Number of most-recent monthly archives to keep |

## Backup Verification

A backup you have never tested is not a backup. Pick the latest auto-backup (filename pattern `oddsiq-YYYYMMDD-HHMMSS-{startup|shutdown|periodic}.db`) and run:

```bash
# Replace with the actual filename — auto backups are timestamped per run
LATEST=$(ls -t ./backups/oddsiq-*.db | head -1)
sqlite3 "$LATEST" "
  SELECT 'games: '                 || count(*) FROM games;
  SELECT 'snapshots: '             || count(*) FROM odds_snapshots;
  SELECT 'value_bets: '            || count(*) FROM value_bets;
  SELECT 'placed_bets: '           || count(*) FROM placed_bets;
  SELECT 'meta: '                  || count(*) FROM meta;
  SELECT 'schema_version: '        || count(*) FROM schema_version;
"
```

### Verification Checklist

- File size is non-zero and close to the live database size.
- **All six tables exist** with row counts matching or slightly behind the live DB: `games`, `odds_snapshots`, `value_bets`, `placed_bets`, `meta`, `schema_version`. (Backups taken before 2026-08-27 additionally contain `live_edge_candidates` — migration 011 dropped it with the live-edge subsystem removal; the dataset also lives in the standalone archive at `~/dev/oddsiq-live-edge-archive/`.)
- `PRAGMA integrity_check` returns `ok` — already run automatically by `oddsiq/backup.py` after every backup write; a corrupt destination is deleted and the function returns `None`. A manual re-check is belt-and-suspenders.
- The most recent `captured_at` timestamp is from tonight's session.

## Raw Response Retention

Raw JSON responses are stored under `./data/raw/` in dated subdirectories. The pipeline prunes these automatically at startup and shutdown via `scraper_base.prune_raw_responses()`. Configuration:

| Env var | Default | Purpose |
| --- | --- | --- |
| `RAW_RESPONSE_RETENTION_ENABLED` | `1` | Enable auto-pruning |
| `RAW_RESPONSE_KEEP_DAYS` | `14` | Keep dated subdirs this many days; older ones are deleted |

To manually purge old raw responses beyond the retention window:

```bash
# List dated directories older than 14 days (example; adjust the path)
find ./data/raw -maxdepth 2 -type d -mtime +14

# Delete a specific month's raw Pinnacle data
rm -rf ./data/raw/pinnacle/2026-01*
```

## Storage Projections

Based on observed data at current single-book (Tipsport + Pinnacle) volume with NBA/NHL/MLB/WNBA:

| Timeframe | `odds_snapshots` Rows | `value_bets` Rows | Estimated DB Size | Raw JSON (`./data/raw/`) |
| --- | --- | --- | --- | --- |
| 1 night (few active games) | ~500–2,000 | 0–10 | ~200–400 KB | ~2–5 MB |
| 1 month (multi-sport) | ~30,000–60,000 | ~50–200 | ~5–10 MB | ~50–100 MB |
| 1 full season | varies by sports active | ~200–800 | ~20–40 MB | ~200–500 MB |

SQLite handles millions of rows without issue. The database itself will not become a problem. Raw JSON is the space concern; the 14-day auto-prune prevents unbounded growth during 24/7 operation.

## Retention Rules

| Data | Retention | Rationale |
| --- | --- | --- |
| `games` table | Keep forever | Tiny (< 5,000 rows/season across all sports), needed for joins |
| `value_bets` table | Keep forever | The core analytical dataset, irreplaceable |
| `live_edge_candidates` (dropped 2026-08-27) | Archived at `~/dev/oddsiq-live-edge-archive/` | Table dropped by migration 011 with the live-edge subsystem; pre-removal backups still contain it |
| `odds_snapshots` table | Keep forever (active season), archive after season end | Needed for CLV recalculation and historical analysis |
| Raw JSON | Auto-pruned after `RAW_RESPONSE_KEEP_DAYS` (default 14) | Only useful for debugging parser changes; not long-term analysis |
| Backup copies | Keep last 7 daily + 6 monthly archives (configurable) | Prevents backup directory from growing unbounded |

## When to Archive

At the end of a season (or after the last game night):

### 1. Export the season database

```bash
cp oddsiq.db ~/Desktop/oddsiq-archive/season-2025-26.db
```

### 2. Optionally purge `odds_snapshots` from the live DB

```sql
-- Keep value_bets, placed_bets, and games; purge bulk snapshot data
DELETE FROM odds_snapshots WHERE game_id IN (
    SELECT id FROM games WHERE scheduled_at < '2026-10-01'
);
VACUUM;
```

> **WARNING:** Do NOT delete `value_bets`, `placed_bets`, or `games` from any season. These are small and form the long-term performance record. See [[Data Model]] for schema details.

## See Also

- [[SQLite Store]] — database implementation (WAL mode, async)
- [[Data Model]] — full schema for all six tables
- [[CLI Entry Point]] — where automated backup hooks are wired
- [[Runbook]] — operational guide including shutdown procedure
