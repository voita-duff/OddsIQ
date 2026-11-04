---
tags:
  - component/data
  - layer/storage
  - type/component
source-files:
  - oddsiq/db/store.py
  - oddsiq/db/migrations/__init__.py
  - oddsiq/db/migrations/000_baseline.py
  - tests/test_store.py
related:
  - "[[Data Model]]"
  - "[[Game Lifecycle]]"
  - "[[OddsSnapshot Lifecycle]]"
  - "[[ValueBet Lifecycle]]"
  - "[[Architecture Decisions]]"
---

# SQLite Store

## Purpose

Persistence layer for all OddsIQ data. Wraps SQLite with WAL mode and foreign keys, providing typed operations for the six tables: `games`, `odds_snapshots`, `value_bets`, `placed_bets`, `meta`, `schema_version`. (The `live_edge_candidates` table was dropped by migration 011 on 2026-08-27 with the live-edge subsystem removal; its Store methods went with it.) All database access goes through this component -- no raw SQL outside the store.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/db/store.py` | `Store` | Async SQLite wrapper (aiosqlite); all DB operations live here |
| `oddsiq/db/store.py` | `Store.initialize()` | **PRAGMAs only** (`journal_mode=WAL`, `foreign_keys=ON`). Tables come from [[Migrations]] — see `cli.py` ordering: `migrations.run()` runs first |
| `oddsiq/db/store.py` | `Store.upsert_game()`, `update_game_status()`, `update_game_commence_time()` | Game lifecycle persistence |
| `oddsiq/db/store.py` | `Store.insert_odds_snapshots_batch()` | Batch insert; unchanged-price dedup bumps `captured_at`, `source_updated_at`, and `raw_response_path` together |
| `oddsiq/db/store.py` | `Store.mark_closing_snapshots(game_id, freshness_minutes)` | Freshness-gated closing-snapshot mark |
| `oddsiq/db/store.py` | `Store.clear_closing_flags(game_id)` | Resets `is_closing` for one game (used by `cleanup-closing-flags`) |
| `oddsiq/db/store.py` | `Store.insert_value_bet()` | Deduplicates on game/book/market/selection/line/tier plus normalized `excluded_reason` (migration 013) |
| `oddsiq/db/store.py` | `Store.update_value_bet_closing()`, `update_value_bet_result()` | CLV + result enrichment |
| `oddsiq/db/store.py` | `Store.get_meta()` / `set_meta()` | Cross-restart key/value scratchpad backing the `meta` table |
| `oddsiq/db/store.py` | `Store.get_handled_live_game_ids()`, `get_final_games_without_snapshots()`, `get_pending_value_bets()` | Restore-from-DB helpers used by [[Scheduler]] startup and `cli.audit_missed_gamedays()` |
| `oddsiq/db/store.py` | `Store.mark_rows_excluded()` | One-shot data-quality stamp behind `oddsiq mark-excluded` — stamps `excluded_reason` on **`value_bets` only** (by `flagged_at`) since the live-edge removal (2026-08-27; it previously also stamped `live_edge_candidates`) |
| `oddsiq/db/store.py` | `Store.delete_games_with_children()` | Zombie-game cascade behind `oddsiq delete-zombie-games` — deletes `placed_bets`, `value_bets`, `odds_snapshots`, then `games` (children first, one transaction; the dropped `live_edge_candidates` table is no longer in the cascade) |
| `oddsiq/db/store.py` | `Store.get_value_bets_without_clv()`, `get_final_games_with_incomplete_bets()` | CLV-specific recovery queries used by [[Scheduler]] startup stuck-bet recovery — `get_value_bets_without_clv()` revisits bets whose result was set without CLV (which `get_pending_value_bets()` can't) |
| `oddsiq/db/migrations/` | `000_baseline.py` … `013_scope_value_bet_dedup_by_exclusion.py` | Schema lives in migrations, not in `Store` — see [[Migrations]] |
| `tests/test_store.py` | — | upsert, batch insert + dedup, value-bet duplicate handling under `(line, tier)`, closing marks, freshness gate, handled-live restoration, meta roundtrip — see [[Test Suite Overview]] for current count |

## Inputs

- [[Game Lifecycle|Game]] dataclass instances (from the schedule scraper)
- [[OddsSnapshot Lifecycle|OddsSnapshot]] dataclass instances (from [[PinnacleScraper]], [[TipsportScraper]])
- [[ValueBet Lifecycle|ValueBet]] data dicts (from [[Edge Detection]])
- Game IDs, scores, CLV values, bet results (from [[Scheduler]], [[CLV Calculation]])

## Outputs

- Persisted rows in SQLite database (`oddsiq.db`)
- Query results: closing odds, pending value bets, game statuses
- `insert_value_bet()` returns `None` when the bet already exists (duplicate detection via `ON CONFLICT DO NOTHING`)

## Interactions

- [[Scheduler]] calls all store methods as part of the orchestration loop
- [[Edge Detection]] results flow through [[Scheduler]] into `insert_value_bet()`
- [[CLV Calculation]] pipeline reads closing odds via `get_closing_odds()` and writes results via `update_value_bet_closing()`
- Game lifecycle managed by the schedule scraper -> `upsert_game()` and [[Scheduler]] -> status updates
- Schema defined in [[Data Model]]

## Internal Logic

### Database Initialization

`Store.initialize()` only sets per-connection PRAGMAs — it does NOT create tables. Schema is owned by the [[Migrations]] runner, which `cli.py` calls **before** `Store(...).initialize()`. Migrations currently run through 013.

PRAGMAs set on initialize:

- **`journal_mode=WAL`** — Write-Ahead Logging; readers don't block writers
- **`foreign_keys=ON`** — enforces FK constraints (e.g. `odds_snapshots.game_id` → `games.id`)

### Key Operations

#### `upsert_game(game)`

Inserts a new game or updates an existing one by `external_id`. Uses `INSERT ... ON CONFLICT(external_id) DO UPDATE` to handle schedule refreshes that re-fetch the same games.

#### `insert_odds_snapshots_batch(snapshots)`

Inserts a batch of odds snapshots, skipping any row whose `(odds, line)` matches the most recent snapshot for the same `(game_id, bookmaker, market_type, selection)`. The existing row's local observation time, raw-response path, and upstream source timestamp are updated atomically so "last seen" remains auditable.

#### `insert_value_bet(value_bet_data)`

Inserts a value bet record. The conflict identity includes line, tier, and `IFNULL(excluded_reason, '')`. Repeated clean edges still collapse; repeated instances of one suspect class collapse; a temporary suspect observation can coexist with a later clean candidate, so recovery never loses the actionable edge.

#### `mark_closing_snapshots(game_id, freshness_minutes)`

Marks the last snapshot per `(bookmaker, market_type, selection, IFNULL(line, -999.0))` as `is_closing=1` — **only if** `captured_at` falls within `freshness_minutes` of effective tipoff. Stale snapshots (e.g. from a missed hot-tier window) stay `is_closing=0` so [[CLV Calculation]] is skipped rather than computed against a fake closing line.

It first looks up the game's `commence_time`/`scheduled_at` separately, computes `effective_tipoff = commence_time or scheduled_at` and `cutoff = effective_tipoff - freshness_minutes` in Python, then runs (no JOIN — the bind params carry `game_id`, `cutoff`, `effective_tipoff`):

```sql
UPDATE odds_snapshots SET is_closing = 1
WHERE id IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY bookmaker, market_type, selection, IFNULL(line, -999.0)
            ORDER BY captured_at DESC
        ) as rn
        FROM odds_snapshots
        WHERE game_id = ? AND captured_at >= ? AND captured_at <= ?
    ) WHERE rn = 1
)
```

The partition is **line-aware** via `IFNULL(line, -999.0)`, so distinct spread/total lines each get their own closing marker. The freshness/pre-tipoff filter (`captured_at >= cutoff AND captured_at <= effective_tipoff`) lives **inside** the window-function input, not an outer WHERE — otherwise a post-tipoff row could win `ROW_NUMBER()=1` and then be excluded, leaving the valid pre-tipoff candidate unmarked.

Fires once per game at tipoff via [[Scheduler]] `_handle_game_live()`, with `freshness_minutes=CLOSING_FRESHNESS_MINUTES` (default 30, configurable via env var).

#### `get_closing_odds(game_id, bookmaker, market_type, selection)`

Retrieves the closing snapshot for a specific (game, bookmaker, market, selection, line) combination — line-matched via `IFNULL(line, -999.0) = IFNULL(?, -999.0)`. Used by the [[CLV Calculation]] pipeline to get both Tipsport and Pinnacle closing odds.

#### `update_value_bet_closing(bet_id, closing_odds, pinnacle_closing, clv_pct)`

Sets the closing odds, Pinnacle closing fair odds, and CLV percentage on a value bet record. Called after CLV is calculated at tipoff.

#### `update_value_bet_result(bet_id, result)`

Sets the result ("win", "loss", or "push") on a value bet record. Called after game goes final and [[CLV Calculation|resolve_bet_result()]] determines the outcome.

#### `reconcile_placed_bet(...)` / `settle_placed_bet(...)`

`reconcile_placed_bet` is the fail-safe operator recovery path for a `pending`/`unknown` placement after checking Tipsport bet history. It can mark the row `placed` only with both a valid accepted price and receipt id, or `rejected` with a reason; it never retries the wager and never re-arms the engine. If the game already finalized, a confirmed placement settles immediately. `settle_placed_bet` prefers accepted odds for win P&L and falls back to requested odds with an explicit `settled_using_requested_odds` audit marker; no valid price leaves the row open rather than inventing a loss.

#### `get_meta(key)` / `set_meta(key, value)`

Key/value helpers backed by the `meta` table (see [[Data Model]]). `set_meta` upserts via `ON CONFLICT(key) DO UPDATE`, stamping `updated_at` with the current UTC timestamp. Used by [[Scheduler]] to persist the Pinnacle budget snapshot after each successful poll (throttled to once per 5 min), so that the [[PinnacleScraper]] probe has a fallback when the live probe fails on restart. Current keys: `odds_api_budget_used`, `odds_api_budget_remaining`.

### WAL Mode Benefits

WAL (Write-Ahead Logging) prevents reader-writer contention. Reads don't block writes and writes don't block reads. This matters when the [[Scheduler]] is reading game statuses while simultaneously inserting new odds snapshots. For a single-user, single-process system, WAL is the optimal journaling mode.

### Handled-Live State Restoration

On startup, `_handled_live` is restored from the database by querying games that already have `is_closing = 1` snapshots. This means games that went live before a restart are recognized as handled -- no new table or column needed, state is derived from the data itself.

## Configuration

| Setting | Value | Notes |
| --- | --- | --- |
| Journal mode | WAL | Set via `PRAGMA journal_mode=WAL` |
| Foreign keys | ON | Set via `PRAGMA foreign_keys=ON` |
| Database file | `oddsiq.db` | In project root |

## Error Handling

- `OperationalError` from SQLite (e.g., disk full, corrupt DB) propagates up to [[Scheduler]]'s `except Exception` handler
- `ON CONFLICT DO NOTHING` on `insert_value_bet()` handles duplicate detection gracefully
- Schema bootstrap is idempotent via [[Migrations]] (each migration uses `IF NOT EXISTS` patterns or column-presence checks)

## Known Issues

- **`stake` field always NULL.** Column exists in schema but nothing writes to it. Kelly staking is Phase 6. See [[ValueBet Lifecycle]] and [[Open - Stake Field Always None]].

## Tests

`tests/test_store.py` (36 tests) covers:

- Game upsert (insert and update); `update_game_commence_time()` write-once
- Odds snapshot batch insert + dedup (captured_at bump on `(odds, line)` match)
- Value bet insert and duplicate handling under the `IFNULL(line, -999.0)` unique key
- Closing snapshot marking with the freshness gate (rejects stale snapshots; honors `commence_time` over `scheduled_at`)
- `clear_closing_flags()` reset path
- Handled-live state restoration on startup; missed-gameday detection
- Meta key/value roundtrip (insert + overwrite + `updated_at` stamp)
