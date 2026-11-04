---
tags:
  - component/data
  - status/complete
  - phase/p1
  - layer/storage
  - type/component
source-files:
  - oddsiq/db/migrations/__init__.py
  - oddsiq/db/migrations/000_baseline.py
  - oddsiq/db/migrations/001_add_line_to_value_bet_unique.py
  - oddsiq/db/migrations/002_add_commence_time_to_games.py
  - oddsiq/db/migrations/003_add_tier_to_value_bets.py
  - oddsiq/db/migrations/004_add_live_edge_candidates.py
  - oddsiq/db/migrations/005_add_sport_to_tables.py
  - oddsiq/db/migrations/006_add_markout_to_live_edge_candidates.py
  - oddsiq/db/migrations/007_add_excluded_reason.py
  - oddsiq/db/migrations/008_add_frozen_cycles_to_live_edge_candidates.py
  - oddsiq/db/migrations/009_add_tipsport_ids_to_value_bets.py
  - oddsiq/db/migrations/010_add_placed_bets.py
  - oddsiq/db/migrations/011_drop_live_edge_candidates.py
  - oddsiq/db/migrations/012_add_source_freshness.py
  - oddsiq/db/migrations/013_scope_value_bet_dedup_by_exclusion.py
  - tests/test_migrations.py
  - tests/test_migration_005.py
  - tests/test_migration_006.py
  - tests/test_migration_007.py
  - tests/test_migration_008.py
  - tests/test_migration_009.py
  - tests/test_migration_010.py
  - tests/test_migration_012.py
  - tests/test_migration_013.py
related:
  - "[[SQLite Store]]"
  - "[[Data Model]]"
  - "[[Backup]]"
  - "[[CLI Entry Point]]"
  - "[[P6 - Kelly Staking]]"
---

# Migrations

## Purpose

Forward-only schema-migration runner. Discovers `oddsiq/db/migrations/NNN_*.py` files at CLI startup, applies any pending ones inside per-migration transactions, and records each applied version in a `schema_version` table the runner bootstraps itself. Replaces the previous one-shot `db/schema.py` bootstrap, unblocking P2+ schema changes (`bankroll_log`, `auto_bets`, column additions, backfills) without hand-editing the DB.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/db/migrations/__init__.py` | `run()` | Public API; called from `cli.py` via `asyncio.to_thread`. Bootstraps `schema_version`, discovers + applies pending migrations |
| `oddsiq/db/migrations/__init__.py` | `Migration` (dataclass), `_discover()`, `_apply_one()`, `_current_version()`, `_ensure_schema_version_table()` | Internal helpers |
| `oddsiq/db/migrations/000_baseline.py` | `apply()` | Phase 1 schema (games, odds_snapshots, value_bets, meta + indexes); idempotent via `IF NOT EXISTS` |
| `oddsiq/db/migrations/001_add_line_to_value_bet_unique.py` | `apply()` | Drops + recreates `idx_value_bets_unique_edge` to include `IFNULL(line, -999.0)` |
| `oddsiq/db/migrations/002_add_commence_time_to_games.py` | `apply()` | `ALTER TABLE games ADD COLUMN commence_time TIMESTAMP`; nullable, no backfill |
| `oddsiq/db/migrations/003_add_tier_to_value_bets.py` | `apply()` | Adds `value_bets.tier`, backfills existing rows to `hot`, and recreates the unique edge index to include tier |
| `oddsiq/db/migrations/004_add_live_edge_candidates.py` | `apply()` | Creates the `live_edge_candidates` table (live-scout time series) + indexes; deliberately outside the `value_bets`/CLV lifecycle |
| `oddsiq/db/migrations/005_add_sport_to_tables.py` | `apply()` | Adds `sport TEXT NOT NULL DEFAULT 'nba'` to `games`, `odds_snapshots`, `value_bets`, and `live_edge_candidates`; backfills all existing rows to `'nba'`. `sport` is NOT in any unique/dedup key — `game_id` already scopes rows to one sport; this column exists for fast per-sport filtering (dashboard tabs, audit). Idempotent via `PRAGMA table_info` guard. |
| `oddsiq/db/migrations/006_add_markout_to_live_edge_candidates.py` | `apply()` | Adds markout-confirmation columns (`markout_fair_odds`, `markout_edge_pct`, `markout_captured_at`, `markout_status` DEFAULT `'pending'`, `offer_stale`) to `live_edge_candidates`; backfills pre-existing rows to `markout_status='legacy'`. Idempotent via `PRAGMA table_info` guard. |
| `oddsiq/db/migrations/007_add_excluded_reason.py` | `apply()` | Adds a nullable `excluded_reason TEXT` column to BOTH `value_bets` and `live_edge_candidates`. NULL = part of the evaluation dataset; a non-NULL slug marks known-bad rows hidden from every reporting surface. No backfill (existing rows stay included). Idempotent via `PRAGMA table_info` guard. Stamping is a separate operator action (`oddsiq mark-excluded`), NOT part of the migration. |
| `oddsiq/db/migrations/008_add_frozen_cycles_to_live_edge_candidates.py` | `apply()` | Adds a nullable `frozen_cycles INTEGER` column to `live_edge_candidates`. Records how many consecutive recorded candidates held the Tipsport price unchanged at markout (a measure-first stale-line signal; counts `live_edge_candidates` rows — only polls that crossed `LIVE_EDGE_THRESHOLD` — so a lower bound on true freeze duration, not a literal poll count; ≥1, NULL = unmeasured/legacy). Set by the pipeline at markout resolution, never at insert. No backfill. Idempotent via `PRAGMA table_info` guard. |
| `oddsiq/db/migrations/011_drop_live_edge_candidates.py` | `apply()` | Drops `live_edge_candidates` + its unique index (the live-edge subsystem was fully removed 2026-08-27; the dataset was archived offline before the production run, and migrations 004/006/008 — which built the table — stay in the forward-only chain: a fresh DB creates it in 004 and drops it here). Idempotent (`DROP ... IF EXISTS`). |
| `oddsiq/db/migrations/012_add_source_freshness.py` | `apply()` | Adds nullable `odds_snapshots.source_updated_at` and `value_bets.sharp_source_updated_at`; historical rows remain NULL. |
| `oddsiq/db/migrations/013_scope_value_bet_dedup_by_exclusion.py` | `apply()` | Rebuilds the unique edge index with normalized `excluded_reason`, allowing a later clean edge after temporary suspect evidence without weakening clean-row dedup. |
| `oddsiq/db/migrations/010_add_placed_bets.py` | `apply()` | Creates the `placed_bets` auto-betting decision ledger: one row per decision, `value_bet_id UNIQUE` (double-bet impossible; no retries), CZK money columns, latencies, and settlement P&L. A pending/unknown row may later be operator-reconciled after checking book history; this corrects its state without issuing another wager. |
| `oddsiq/db/migrations/009_add_tipsport_ids_to_value_bets.py` | `apply()` | Adds nullable `tipsport_match_id INTEGER`, `tipsport_opp_id INTEGER`, `tipsport_url TEXT` to `value_bets` — Tipsport's own execution identity for the detected market (match id, opportunity/selection id, site-relative deep-link url), captured at detection time (parser transient fields → `find_edges` → EdgeService) so the future P0 availability probe / P1 placement can address the exact offer. The deep link is **engine-facing**, not phone-facing: the betting extension navigates to it — detections are not alerted (2026-08-10 rebrand), and the dashboard `/betting` detections list renders it as a `↗` deep link. Historical rows stay NULL; `odds_snapshots` deliberately untouched (dedup key / "last seen" semantics must not change). Idempotent via `PRAGMA table_info` guard. |
| `tests/test_migrations.py` | — | 19 tests: discovery order, idempotence, transaction rollback, schema_version bootstrap, migrations 001-004, <100ms up-to-date overhead |
| `tests/test_migration_005.py` | — | 2 tests: migration 005 adds `sport` column and backfills existing rows to `'nba'`; idempotent on re-run |
| `tests/test_migration_006.py` | — | 4 tests: migration 006 adds markout columns, backfills pre-existing rows to `'legacy'`, fresh inserts default to `'pending'`, idempotent on re-run |
| `tests/test_migration_007.py` | — | 3 tests: migration 007 adds `excluded_reason` to both tables, fresh rows default to NULL (operator-set flag, never set by the pipeline), idempotent on re-run. (The stamping write-path + reporting filters are covered in `tests/test_excluded_rows.py`.) |
| `tests/test_migration_008.py` | — | 3 tests: migration 008 adds `frozen_cycles` to `live_edge_candidates`, fresh rows default to NULL (set only at markout, never at insert), idempotent on re-run. |

## How a Migration Is Added

1. Create `oddsiq/db/migrations/NNN_short_name.py`, incrementing `NNN` by 1 over the highest existing version.
2. Export `def apply(conn: sqlite3.Connection) -> None`. The function may run any mix of DDL and data-transforming SQL; it runs inside a single transaction that also records the `schema_version` row.
3. Add a dedicated `tests/test_migration_NNN.py` with a before/after fixture verifying the post-state (tables, columns, row counts).
4. Next `python3 -m oddsiq.cli` applies the migration automatically; INFO logs confirm `Running migration NNN_short_name...` and `Migration NNN_short_name applied in ...s`.

## Interactions

- **[[CLI Entry Point]]** — calls `await asyncio.to_thread(migrations.run, config.DATABASE_PATH)` in `async_main()` between the startup backup and `Store.initialize()`. The `--check-budget` branch bypasses migrations entirely (it does not touch the DB).
- **[[Backup]]** — the startup backup fires *before* migrations, so every migration has a safety-net snapshot on disk. Recovery from a destructive migration is manual `cp` from the backup dir.
- **[[SQLite Store]]** — `Store.initialize()` no longer creates tables; it only sets per-connection PRAGMAs. Every caller that instantiates `Store` must have run migrations first (tests do this in fixtures).
- **[[Data Model]]** — the `schema_version` table added by the runner is not part of the application data model; it's an infrastructure table read/written only by the runner in `oddsiq/db/migrations/__init__.py`.

## Internal Logic

### Discovery

`_discover()` globs `oddsiq/db/migrations/*.py`, matches the `NNN_name.py` regex, and `importlib.import_module`s each. `__init__.py` is skipped because its name doesn't match the regex. The returned list is sorted by integer version, so the filesystem's lexicographic order doesn't matter — `000` through `999` all work.

### Bootstrap

Every call to `run()` begins with:

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMP NOT NULL
);
```

This resolves the chicken-and-egg problem of a first-ever run: before version 0 is applied, `schema_version` exists but is empty, so `_current_version()` returns `-1`.

### Transaction Wrapping

`_apply_one()` wraps each migration's `apply(conn)` plus the `INSERT INTO schema_version` in a single `BEGIN ... COMMIT`. On any exception, `conn.rollback()` drops the entire transaction. A migration that dies halfway never leaves a `schema_version` row claiming it succeeded.

### Forward-only

There is no `revert()`, no down migrations, no `--rollback`. Mistakes are recovered by restoring from the startup backup (`./backups/oddsiq-*-startup.db`) and fixing the migration code before retrying. This matches the modern default (Alembic defaults to stubbed downgrades; Prisma/Drizzle/golang-migrate encourage forward-only).

### Sync execution

Uses stdlib `sqlite3`, not `aiosqlite`, matching the convention set by `oddsiq/backup.py`. The CLI wraps the call in `asyncio.to_thread` so the event loop isn't blocked. Migrations run only at startup, before the scheduler, so a brief block is harmless.

## Configuration

None. Behavior is driven by what files exist in `oddsiq/db/migrations/`.

## Error Handling

- **Missing `apply` function in a migration file** — `_discover()` raises `RuntimeError` with the filename, halting startup.
- **Migration raises** — the transaction rolls back, the exception propagates out of `run()`, `cli.py` exits non-zero. The startup backup from moments earlier is intact; operator can `cp` it back over `oddsiq.db` to undo any partially-applied pre-failure migrations.
- **SQLite DB open failure** — `sqlite3.connect()` raises, `run()` propagates, startup aborts. Same recovery path.

## Known Issues

None currently. Residual risk: a migration may be syntactically valid SQL that destroys data (e.g. `DROP TABLE games`). The runner doesn't try to detect that — the defense is startup backup + code review + per-migration tests.

## Tests

`tests/test_migrations.py` (19 tests) covers:

- `_discover()` finds baseline and returns in sorted order.
- `_ensure_schema_version_table()` is idempotent.
- `_current_version()` returns `-1` on an empty `schema_version`.
- Baseline migration creates all Phase 1 tables on a fresh DB.
- Baseline migration is a no-op when tables already exist (simulated pre-existing `games` table scenario).
- Second `run()` against an up-to-date DB emits no "Running migration" log and leaves `schema_version` untouched.
- Summary log (`Schema: version N (up to date)`) always fires.
- A fixture migration that raises rolls back both its schema changes and its `schema_version` row.
- A post-failure re-read preserves prior `schema_version` rows.
- Run time on an up-to-date DB is under 100 ms (spec §Success Criteria).
- Migration 001 rebuilds the value-bet unique index with line-aware `IFNULL(line, -999.0)` semantics.
- Migration 002 adds nullable `games.commence_time`.
- Migration 003 adds `value_bets.tier`, backfills historical rows to `hot`, and rebuilds the unique index to include tier.
- Migration 004 creates the `live_edge_candidates` table (live-scout research time series) and its indexes.
- Migration 005 adds `sport TEXT NOT NULL DEFAULT 'nba'` to all four data tables (`games`, `odds_snapshots`, `value_bets`, `live_edge_candidates`) and backfills existing rows to `'nba'`.
- Migration 012 persists sharp-feed timestamps; migration 013 prevents a guard-stamped row from consuming a later clean detection's dedup slot.

`tests/test_migration_005.py` (2 tests) covers: migration 005 adds the `sport` column and backfills existing rows to `'nba'`; idempotent on re-run.

## See Also

- [[SQLite Store]] — the aiosqlite store, post-migration PRAGMA-only `initialize()`
- [[Data Model]] — the schema this system manages
- [[Backup]] — the safety net for destructive migrations
- [[CLI Entry Point]] — where `run()` is invoked
- [[P6 - Kelly Staking]] — adds `bankroll_log` and the `stake` write-path. P6 ships last; before it lands, the first post-baseline migrations have already shipped: `001_add_line_to_value_bet_unique.py` (closing-snapshot integrity track, 2026-04-25), `002_add_commence_time_to_games.py` (Pinnacle tipoff, 2026-04-27), and `003_add_tier_to_value_bets.py` (warm/hot-tier edge tracking, 2026-05).
