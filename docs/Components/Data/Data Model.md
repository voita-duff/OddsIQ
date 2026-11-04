---
tags:
  - component/data
  - layer/storage
  - type/component
source-files:
  - oddsiq/db/migrations/000_baseline.py
  - oddsiq/db/migrations/005_add_sport_to_tables.py
  - oddsiq/db/models.py
  - oddsiq/db/store.py
related:
  - "[[SQLite Store]]"
  - "[[Game Lifecycle]]"
  - "[[OddsSnapshot Lifecycle]]"
  - "[[ValueBet Lifecycle]]"
  - "[[Migrations]]"
  - "[[Scheduler]]"
---

# Data Model

## Purpose

Defines the complete schema for OddsIQ's SQLite database — six tables total: three data tables (`games`, `odds_snapshots`, `value_bets`) carrying a `sport TEXT NOT NULL DEFAULT 'nba'` column (migration 005) for per-sport filtering, the `placed_bets` auto-betting decision ledger (migration 010), a `meta` key-value scratchpad for cross-restart state, and the migration runner's own `schema_version` table. (A fourth data table, `live_edge_candidates`, was dropped by migration 011 on 2026-08-27 with the live-edge subsystem removal — see below.) All domain tables are accessed exclusively through [[SQLite Store]]. (See [[Migrations]] for the runner.)

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/db/migrations/000_baseline.py` | `apply()` | Phase 1 baseline `CREATE TABLE`/`CREATE INDEX`; idempotent via `IF NOT EXISTS` |
| `oddsiq/db/migrations/001_add_line_to_value_bet_unique.py` | `apply()` | Recreates `idx_value_bets_unique_edge` with `IFNULL(line, -999.0)` |
| `oddsiq/db/migrations/002_add_commence_time_to_games.py` | `apply()` | `ALTER TABLE games ADD COLUMN commence_time TIMESTAMP`; nullable, no backfill |
| `oddsiq/db/migrations/003_add_tier_to_value_bets.py` | `apply()` | Adds `tier` column to `value_bets` and recreates the unique index to include it |
| `oddsiq/db/migrations/004_add_live_edge_candidates.py` | `apply()` | Creates the `live_edge_candidates` table (live-scout time series) |
| `oddsiq/db/migrations/005_add_sport_to_tables.py` | `apply()` | Adds `sport TEXT NOT NULL DEFAULT 'nba'` to all four data tables; backfills existing rows to `'nba'` |
| `oddsiq/db/migrations/006_add_markout_to_live_edge_candidates.py` | `apply()` | Adds markout columns (`markout_fair_odds`, `markout_edge_pct`, `markout_captured_at`, `markout_status` DEFAULT `'pending'`, `offer_stale`) to `live_edge_candidates`; backfills pre-existing rows to `'legacy'` |
| `oddsiq/db/migrations/007_add_excluded_reason.py` | `apply()` | Adds nullable `excluded_reason TEXT` to `value_bets` and `live_edge_candidates`; no backfill (existing rows stay NULL = included). Non-NULL = known-bad, hidden from reporting |
| `oddsiq/db/migrations/008_add_frozen_cycles_to_live_edge_candidates.py` | `apply()` | Adds `frozen_cycles` to `live_edge_candidates` (freeze-gate audit trail) |
| `oddsiq/db/migrations/009_add_tipsport_ids_to_value_bets.py` | `apply()` | Adds nullable Tipsport execution-identity columns (`tipsport_match_id`/`tipsport_opp_id`/`tipsport_url`) to `value_bets` |
| `oddsiq/db/migrations/010_add_placed_bets.py` | `apply()` | Creates the `placed_bets` auto-betting decision ledger (`value_bet_id` UNIQUE) |
| `oddsiq/db/migrations/011_drop_live_edge_candidates.py` | `apply()` | Drops `live_edge_candidates` + its unique index (live-edge subsystem removal, 2026-08-27); dataset archived first |
| `oddsiq/db/migrations/012_add_source_freshness.py` | `apply()` | Adds upstream sharp timestamps to snapshots and value bets |
| `oddsiq/db/migrations/013_scope_value_bet_dedup_by_exclusion.py` | `apply()` | Lets suspect evidence coexist with a later clean detection in the same tier |
| `oddsiq/db/models.py` | `Game`, `OddsSnapshot`, `ValueBet`, `PlacedBet` | Python dataclasses; underscored fields (`_home_team`, `_away_team`, `_commence_time`) are transient, not persisted |

## Inputs

N/A — this is a schema definition, not a runtime component.

## Outputs

Defines the structure for all data persisted by [[SQLite Store]].

## Interactions

- [[Migrations]] owns schema creation (forward-only `NNN_*.py` files in `oddsiq/db/migrations/`); [[SQLite Store]] only sets per-connection PRAGMAs in `Store.initialize()` and performs all CRUD operations
- [[Game Lifecycle]] describes how `games` rows transition through statuses
- [[OddsSnapshot Lifecycle]] describes how `odds_snapshots` rows accumulate and get marked as closing
- [[ValueBet Lifecycle]] describes how `value_bets` rows are created, enriched with CLV, and resolved

## Internal Logic

### The `sport` Column (migration 005)

All data tables — `games`, `odds_snapshots`, `value_bets` (and, until migration 011 dropped it, `live_edge_candidates`) — carry:

```sql
sport TEXT NOT NULL DEFAULT 'nba'
```

Added by migration 005; existing rows backfilled to `'nba'`. The column is **not** part of any unique or dedup key — `game_id` already scopes every odds/bet row to exactly one sport. Its purpose is fast per-sport filtering in the dashboard sport-tab bar (`?sport=nba|nhl|mlb|wnba`) and in the audit CLI (`audit-bookmakers --since`). The two infrastructure tables (`meta`, `schema_version`) do not carry a `sport` column.

### Full Schema

#### `games` — one row per game (any enabled sport)

```sql
CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT UNIQUE,              -- Odds API event id
    home_team TEXT NOT NULL,              -- canonical name ("Los Angeles Lakers")
    away_team TEXT NOT NULL,
    scheduled_at TIMESTAMP NOT NULL,      -- Odds API commence_time (ISO 8601 UTC)
    status TEXT DEFAULT 'scheduled',      -- scheduled -> live -> final | postponed
    home_score INTEGER,                   -- NULL until final
    away_score INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    commence_time TIMESTAMP,              -- Pinnacle-derived tipoff (migration 002); nullable
    sport TEXT NOT NULL DEFAULT 'nba'     -- "nba" | "nhl" | "mlb" (migration 005)
);
```

- `external_id` is the Odds API event id, used for upsert deduplication
- `status` follows the [[Game Lifecycle]]: `scheduled` → `live` → `final`, or `scheduled` → `postponed`
- `home_score`/`away_score` remain NULL until the game reaches `final` status
- `home_team`/`away_team` are canonical names after [[Team Name Normalization]]
- `commence_time` is the bookmaker-derived effective tipoff time, populated from Pinnacle event data when available. [[Pipeline Services]] prefers it over `scheduled_at` for tier transitions because Pinnacle's tipoff timestamp is more accurate than the schedule source.
- `sport` is the internal sport key stamped at upsert time from the [[SportProfile]] (migration 005)

#### `odds_snapshots` — time-series of odds observations

```sql
CREATE TABLE odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    bookmaker TEXT NOT NULL,              -- "pinnacle" | "tipsport"
    market_type TEXT NOT NULL,            -- "moneyline" | "spread" | "total"
    selection TEXT NOT NULL,              -- "home" | "away" | "over" | "under"
    line REAL,                            -- NULL for moneyline, -4.5 for spread, 215.5 for total
    odds REAL NOT NULL,                   -- decimal odds (e.g. 2.10)
    is_closing BOOLEAN DEFAULT 0,         -- 1 = last snapshot before tipoff
    raw_response_path TEXT,               -- path to saved JSON response
    source_updated_at TIMESTAMP,          -- upstream sharp market last_update (migration 012)
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sport TEXT NOT NULL DEFAULT 'nba'     -- denormalized from scraper profile (migration 005)
);
```

- `game_id` foreign key to `games.id`
- `bookmaker` is "pinnacle" or "tipsport" (sole soft book post-refactor)
- `line` is NULL for moneyline, negative for home spread (e.g. -4.5), and the total number for over/under (e.g. 215.5)
- `is_closing` set to 1 by `mark_closing_snapshots()` at tipoff — see [[OddsSnapshot Lifecycle]]
- `captured_at` semantics: request-init time, bumped on dedup hits ("last seen"). Never bumped when `is_closing=1` — live polls that match an already-closed snapshot insert a new non-closing row instead to preserve the pre-tipoff invariant.
- `source_updated_at` is Pinnacle's upstream market freshness. On an unchanged-price dedup bump, `captured_at`, `source_updated_at`, and `raw_response_path` move together so forensic evidence cannot point at an older payload.
- `sport` is stamped from the scraper's [[SportProfile]] key at insert time; not part of any unique key

#### `value_bets` — flagged betting opportunities

```sql
CREATE TABLE value_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    bookmaker TEXT NOT NULL,
    market_type TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL,
    entry_odds REAL NOT NULL,             -- Czech book odds at flag time
    fair_odds REAL NOT NULL,              -- Pinnacle fair (vig-removed) at flag time
    edge_pct REAL NOT NULL,               -- (entry/fair - 1) * 100
    closing_odds REAL,                    -- Czech book closing odds (set at tipoff)
    pinnacle_closing REAL,                -- Pinnacle fair closing odds (set at tipoff)
    clv_pct REAL,                         -- (entry/pin_closing - 1) * 100
    result TEXT,                          -- "win" | "loss" | "push" | "draw" (set after game final)
    stake REAL,                           -- always NULL (Kelly staking is P6)
    tier TEXT NOT NULL DEFAULT 'hot',     -- "hot" | "warm" — detection tier (migration 003)
    flagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sport TEXT NOT NULL DEFAULT 'nba',    -- denormalized from Game.sport (migration 005)
    excluded_reason TEXT,                 -- non-NULL = known-bad, hidden from reporting (migration 007)
    sharp_source_updated_at TIMESTAMP     -- immutable sharp timestamp behind fair_odds (migration 012)
);
```

- `entry_odds` and `fair_odds` are set at flag time by [[Edge Detection]]
- `closing_odds`, `pinnacle_closing`, `clv_pct` are set at tipoff by [[CLV Calculation]]
- `result` is set after game final by `resolve_bet_result()`. Moneyline ties resolve to `"draw"` (defensive — NHL/MLB don't tie in finals but a misfetched 3-way market must never double-loss).
- `stake` is always NULL — Kelly staking is Phase 6
- `tier` records the detection tier at detection time — `"hot"` or `"warm"` for a bettable row, and since 2026-09-06 `"far"` (`tiers.SHADOW_TIER`) for the NFL far-shadow rows written by `SportProfile.shadow_far_detection` sports: CLV is still computed at kickoff, but the engine skips them `tier_not_bettable` and every performance aggregate excludes them, surfacing them separately as `shadow`. Historical rows may carry `"medium"` from before the cadence rename; treated identically to `"warm"`
- `sport` is denormalized from the parent game (migration 005); not part of the dedup key
- `excluded_reason` (migration 007) is NULL for evaluation-dataset rows; a non-NULL slug marks known-bad rows that every reporting surface filters via `excluded_reason IS NULL`. Slugs are stamped two ways: **at insert time by the pipeline**, where [[Edge Detection|EdgeService]]'s `_suspect_reason()` returns the first match in a fixed order — `in_play_pregame`, `sharp_source_unknown`, `stale_sharp_source`, `implausible_edge_pregame`, then `incomplete_book_market` (gated by `PREGAME_REQUIRE_COMPLETE_BOOK_MARKET`, default `1`) or `incoherent_book_pregame` — and **retroactively by the one-shot `oddsiq mark-excluded`**, whose slugs so far are `basketball_3way_matchview_2026_06` (the 2026-06 basketball matchView contamination, its first use) and `wrong_game_listing` (the 2026-08-30 next-game-of-the-series forensics).
- `sharp_source_updated_at` freezes the Pinnacle timestamp used for `fair_odds`; later snapshot dedup bumps cannot rewrite decision evidence.
- Unique index `idx_value_bets_unique_edge` on `(game_id, bookmaker, market_type, selection, IFNULL(line, -999.0), tier, IFNULL(excluded_reason, ''))`. Clean still deduplicates against clean, while an excluded observation cannot suppress a later clean edge after recovery.

#### `placed_bets` — execution decision ledger

Migration 010 records exactly one betting decision per `value_bets` row (`value_bet_id UNIQUE`). A row is either a terminal qualification skip or follows `pending → placed | rejected | failed | unknown`, with a successfully placed row later becoming `settled`. The operator reconciliation command may correct only `pending`/`unknown`: a confirmed placement requires accepted odds plus a Tipsport receipt id, while a confirmed rejection requires an evidence note. Reconciliation never retries a wager and leaves the engine disarmed. Win P&L uses `accepted_odds`; when an old receipt omitted it, settlement falls back to valid `requested_odds` and records `settled_using_requested_odds` rather than fabricating a full-stake loss.

#### `live_edge_candidates` — dropped by migration 011 (2026-08-27)

The live-scout time series (created by migration 004, extended by 006/008) was dropped by migration 011 with the removal of the live-edge subsystem. It recorded passive live Tipsport-vs-Pinnacle moneyline edges post-tipoff, deliberately outside the `value_bets`/CLV lifecycle — which is why dropping it removed no pregame data. The full dataset is archived at `~/dev/oddsiq-live-edge-archive/` and the pre-removal code at git tag `archive/live-edges-2026-08-27`; migrations 004/006/008 stay in the forward-only chain (a fresh DB creates the table in 004 and drops it in 011). See [[Live-First]] for the retirement rationale.

#### `meta` — key/value scratchpad for cross-restart state

```sql
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL   -- ISO 8601 UTC, stamped on every set
);
```

A tiny key/value table for state that needs to survive restarts but doesn't warrant its own schema. Written via `set_meta()` (upsert) and read via `get_meta()` on [[SQLite Store]]. Does not carry a `sport` column.

Current keys:

| Key | Purpose |
| --- | --- |
| `odds_api_budget_used` | Last-known Odds API used count, persisted by [[Pipeline Services]] after polls. Read by [[PinnacleScraper]] as fallback when the startup probe fails. |
| `odds_api_budget_remaining` | Same, for remaining. |
| `missed_gameday_audit_known_external_ids` | JSON list of external_ids for final games already surfaced as having zero snapshots; prevents re-alerting known historical misses on restart. |
| `betting_armed`, `betting_disarm_reason` | Persistent real-money kill-switch state and its latest reason. |
| `betting_flat_stake_czk`, `betting_max_bets_per_day`, `betting_max_open_stakes_czk`, `betting_daily_loss_limit_czk`, `betting_sports` | Runtime betting knobs. Their effective exposure values are clamped by immutable environment ceilings in `config.py`. |

### Indexes

```sql
idx_odds_game_book_market    ON odds_snapshots(game_id, bookmaker, market_type)
idx_odds_captured            ON odds_snapshots(captured_at)
idx_odds_book_captured       ON odds_snapshots(bookmaker, captured_at)
idx_odds_game_book_captured  ON odds_snapshots(game_id, bookmaker, captured_at)
idx_value_bets_game          ON value_bets(game_id)
idx_value_bets_unique_edge   ON value_bets(game_id, bookmaker, market_type, selection, IFNULL(line, -999.0), tier)  -- UNIQUE
```

(The `idx_live_edge_unique` index went with the `live_edge_candidates` table in migration 011.)

## Configuration

N/A — schema is static. No runtime configuration.

## Error Handling

- `CREATE TABLE IF NOT EXISTS` makes baseline idempotent
- Schema changes flow through the forward-only migration runner — see [[Migrations]]. The runner applies pending `NNN_*.py` files at every CLI startup inside per-migration transactions.

## Known Issues

- **`stake` column is dead weight.** Exists in schema but always NULL. Kelly staking is Phase 6.

## Tests

Schema is tested through `tests/test_store.py` (37 tests), `tests/test_betting_store.py` (6 tests), and `tests/test_models.py` (4 tests). The numbered migration files also have focused migration tests.
