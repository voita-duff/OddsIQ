---
tags:
  - component/data
  - layer/tracking
  - type/component
source-files:
  - oddsiq/db/models.py
  - oddsiq/db/store.py
  - oddsiq/scrapers/pinnacle.py
  - oddsiq/scrapers/tipsport.py
related:
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[Pipeline Services]]"
  - "[[SQLite Store]]"
  - "[[CLV Calculation]]"
  - "[[Data Model]]"
---

# OddsSnapshot Lifecycle

## Purpose

Documents the full lifecycle of an `OddsSnapshot` record — from API response parsing through game matching, storage, and eventual marking as a closing line at tipoff. OddsSnapshots form a time-series of odds observations that power both [[Edge Detection]] (real-time comparison) and [[CLV Calculation]] (closing line reference).

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/db/models.py` | `OddsSnapshot` | Dataclass; `sport` stamped from the scraper's profile key; `_home_team`/`_away_team`/`_commence_time` are transient (not persisted) |
| `oddsiq/db/store.py` | `Store.insert_odds_snapshots_batch()` | Batch insert; bumps `captured_at` on dedup hit ("last seen") — but never bumps an `is_closing=1` row |
| `oddsiq/db/store.py` | `Store.mark_closing_snapshots(game_id, freshness_minutes)` | Marks last snapshot per (bookmaker, market, selection, line) as `is_closing=1` — only if `captured_at` is within `freshness_minutes` before effective tipoff |
| `oddsiq/db/store.py` | `Store.get_closing_odds()` | Reads `is_closing=1` snapshot for a (game, bookmaker, market, selection, line) |
| `oddsiq/pipeline/schedule_service.py` | `PollCoordinator` (via [[Pipeline Services]]) | Drives per-cycle scraper invocations + game matching + batch insert |

## Inputs

- Raw JSON responses from [[PinnacleScraper]] (The Odds API) and [[TipsportScraper]] (reverse-engineered REST)
- Game records from [[SQLite Store]] for matching snapshots to games

## Outputs

- Persisted `odds_snapshots` rows in [[SQLite Store]]
- Closing line snapshots (`is_closing = 1`) used by [[CLV Calculation]]
- Real-time snapshots fed to [[Edge Detection]] for edge comparison

## Interactions

- [[PinnacleScraper]] and [[TipsportScraper]] create `OddsSnapshot` instances from API responses, stamped with the sport key from the [[SportProfile]]
- [[Pipeline Services]] matches snapshots to games and triggers batch inserts via [[SQLite Store]]
- [[Edge Detection]] uses current-cycle snapshots for real-time edge comparison
- [[CLV Calculation]] uses closing snapshots (marked at tipoff) for CLV measurement
- Each snapshot references a [[Game Lifecycle|Game]] via `game_id` foreign key

## Internal Logic

### Full Lifecycle

```
[Pinnacle/Tipsport API]  ->  scraper.fetch_odds(date, profile)  ->  OddsSnapshot(game_id=0, sport=profile.key)
                                                                        |
                             _home_team, _away_team set by scraper
                             (transient; not persisted to DB)
                                                                        |
                          PollCoordinator: match snapshot to game  ->  snap.game_id = game.id
                                                                        |
                          Store.insert_odds_snapshots_batch()
                                                                        |
                          At tipoff: mark_closing_snapshots()  ->  is_closing = 1
                          (last snapshot per bookmaker/market/selection/line via ROW_NUMBER)
```

### Stage 1: Parsing

Each scraper's `fetch_odds(date, profile)` creates `OddsSnapshot` instances with `game_id=0` (placeholder — real game ID not yet known), `sport=profile.key`, and `captured_at` stamped at request initiation. The `sport` field is set from the [[SportProfile]] passed to `fetch_odds` so every snapshot is sport-tagged before any game matching occurs.

**Pinnacle**: Maps The Odds API `/odds` response. `h2h` → moneyline, `spreads` → spread, `totals` → total. Sets `_home_team`, `_away_team`, and `_commence_time` (parsed from the event's ISO 8601 `commence_time`).

**Tipsport**: Navigates the reverse-engineered REST API. Sets `_home_team` and `_away_team` via the team normalization layer. Market-type and selection mapping: `"1"` → home, `"2"` → away, `"o"` → over, `"u"` → under.

### Stage 2: Game Matching

The PollCoordinator compares each snapshot's `_home_team`/`_away_team` against stored game records. When matched, `snap.game_id` is set to the matching `game.id`. Unmatched snapshots are dropped and never inserted.

### Stage 3: Batch Insert with Dedup

All matched snapshots from a single poll cycle are processed by `Store.insert_odds_snapshots_batch()`. For each snapshot:

1. Fetch the most recent row for the same `(game_id, bookmaker, market_type, selection, line)` (line-aware via `IFNULL(-999.0)`)
2. If `odds` and `line` match the latest row AND the latest row is **not** `is_closing=1`:
   - Bump `captured_at` to the new snapshot's `captured_at` — "last seen" semantics. This keeps the closing mark fresh even when odds are stable, so `mark_closing_snapshots` has an accurate recency timestamp.
3. Otherwise insert a new row.

**Critical invariant**: an `is_closing=1` row is **never bumped**. A live poll that matches a closed snapshot inserts a new non-closing row instead. This preserves `is_closing ⟹ pre-tipoff` for CLV.

### Stage 4: Closing Mark (freshness-gated)

At tipoff, `mark_closing_snapshots(game_id, freshness_minutes)` marks the last captured snapshot per `(bookmaker, market_type, selection, IFNULL(line, -999.0))` as the closing line — but **only if** that snapshot's `captured_at` is within `freshness_minutes` before the game's effective tipoff.

The SQL:
```sql
UPDATE odds_snapshots SET is_closing = 1
WHERE id IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY bookmaker, market_type, selection, IFNULL(line, -999.0)
            ORDER BY captured_at DESC
        ) as rn
        FROM odds_snapshots
        WHERE game_id = ?
          AND captured_at >= ?   -- cutoff = effective_tipoff - freshness_minutes
          AND captured_at <= ?   -- effective_tipoff
    ) WHERE rn = 1
)
```

The freshness/pre-tipoff filter lives **inside** the window-function input (not an outer `WHERE`). Without this placement, a post-tipoff row that wins `ROW_NUMBER()=1` would then be excluded by the outer filter — leaving the genuine pre-tipoff candidate unmarked.

Stale snapshots stay at `is_closing=0` so [[CLV Calculation]] is skipped rather than computed against a fake closing line. The effective tipoff is `commence_time or scheduled_at`.

This fires once per game in `SettlementService.handle_game_live()`. The closing snapshot is used by [[CLV Calculation]] to determine the fair closing price.

## Configuration

| Constant | Value | Notes |
| --- | --- | --- |
| Polling intervals | See [[Polling Tiers]] | Determines how many snapshots accumulate per game |
| `CLOSING_FRESHNESS_MINUTES` | 30 min | Rejects stale closing candidates |
| `RAW_RESPONSE_KEEP_DAYS` | 14 days | Bounds raw-response retention volume |

## Error Handling

- Snapshots with unrecognizable team names (can't be matched to a game): dropped, never inserted
- Scraper parse failures: scraper returns empty list, no snapshots created for that cycle
- Batch insert failures: entire batch rolls back (SQLite transaction)
- `is_closing=1` bump guard: live-poll snapshots that match a closed row insert new non-closing rows instead of bumping

## Known Issues

- **Closing line approximation:** The "closing" snapshot is the last one captured before tipoff, not the actual last available odds. With the hot-tier poll interval, the real closing line could differ by up to one poll interval of movement.
- **Stale snapshot rejection:** When the pipeline misses the hot-tier window for a game (crash, scraper outage), the latest snapshot may be older than `CLOSING_FRESHNESS_MINUTES`. The freshness gate intentionally rejects it — better no CLV than a fake one against a snapshot taken hours before tipoff.
- **`_home_team`/`_away_team`/`_commence_time` carried in memory:** Transient fields used for game matching + tipoff fallback. Not persisted on the snapshot row.

## Tests

OddsSnapshot lifecycle is tested through `tests/test_store.py` (snapshot batch insert, closing marks, is_closing bump guard) and through scraper tests (`test_pinnacle.py`, `test_tipsport.py`) for the parsing stage.
