---
tags:
  - component/data
  - layer/tracking
  - type/component
source-files:
  - oddsiq/db/models.py
  - oddsiq/db/store.py
  - oddsiq/pipeline/schedule_service.py
  - oddsiq/pipeline/settlement_service.py
  - oddsiq/scrapers/odds_api_schedule.py
related:
  - "[[Pipeline Services]]"
  - "[[SQLite Store]]"
  - "[[CLV Calculation]]"
  - "[[Data Model]]"
  - "[[SportProfile]]"
---

# Game Lifecycle

## Purpose

Documents the full lifecycle of a `Game` record from creation through final resolution. Games are the top-level entity — every [[OddsSnapshot Lifecycle|OddsSnapshot]] and [[ValueBet Lifecycle|ValueBet]] is linked to a game via `game_id`.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/db/models.py` | `Game` | Dataclass; `sport: str = "nba"` stamped at discovery; `commence_time` is the Pinnacle-derived tipoff |
| `oddsiq/db/store.py` | `Store.upsert_game()` | `INSERT ... ON CONFLICT(external_id) DO UPDATE` — idempotent; updates status, scores, commence_time (via COALESCE) |
| `oddsiq/db/store.py` | `Store.update_game_status()` | Sets status + optional scores; COALESCE keeps existing scores when callers omit them |
| `oddsiq/scrapers/odds_api_schedule.py` | `OddsApiScheduleSource.fetch_games()` | Fetches full slate (upcoming + live + recently-final) from `/sports/{key}/scores?daysFrom=3` per enabled [[SportProfile]] |
| `oddsiq/pipeline/schedule_service.py` | `ScheduleService` | Owns the game slate, refresh throttle, completion check, live-transition dispatch |
| `oddsiq/pipeline/schedule_service.py` | `ScheduleService.process_live_transitions()` | Once-per-game tipoff handler; delegates to `SettlementService.handle_game_live()` |
| `oddsiq/pipeline/schedule_service.py` | `ScheduleService.check_completed_games()` | Polls the Odds API for live → final transitions; throttled to one fetch per `SCHEDULE_COMPLETION_CHECK_INTERVAL` (default 900 s) |
| `oddsiq/pipeline/settlement_service.py` | `SettlementService.handle_game_live()` | Marks closing snapshots + computes CLV at tipoff |
| `oddsiq/pipeline/settlement_service.py` | `SettlementService.resolve_bets()` | Sets win/loss/push/draw on value_bets |
| `oddsiq/pipeline/settlement_service.py` | `SettlementService.recover_historical_bets()` | One-shot startup backfill for final games with incomplete bets |

## Inputs

- Game schedule and scores from The Odds API `/scores` endpoint (per enabled sport)
- Tipoff time comparison against current time (via [[Pipeline Services]])

## Outputs

- `Game` records in [[SQLite Store]] with status transitions
- Triggers: closing snapshot marking, CLV calculation, bet resolution

## Interactions

- `OddsApiScheduleSource` creates `Game` instances from Odds API `/scores` responses, stamped with the internal sport key
- [[Pipeline Services]] (`ScheduleService`) manages all status transitions and triggers downstream actions
- [[SQLite Store]] persists game state via `upsert_game()`
- `SettlementService` triggers CLV and result resolution at the live and final transitions
- [[OddsSnapshot Lifecycle|OddsSnapshots]] and [[ValueBet Lifecycle|ValueBets]] reference games via `game_id` foreign key

## Internal Logic

### State Machine

```
[Odds API /scores]  ->  OddsApiScheduleSource.fetch_games()  ->  Game(status=...)
                         (one call per enabled SportProfile)          |
                     Store.upsert_game()  <---------------------------+
                     (INSERT or UPDATE by external_id)
                                    |
          +-------------------------+----------------------+
          |                         |                      |
     time_to_tipoff <= 0      status="final"         status="postponed"
     process_live_transitions  check_completed()       (no action)
          |                         |
    status -> "live"          update scores
    mark closing snapshots    resolve_bets()
    calculate CLV
```

Note: the `live` status is a **lifecycle** state only — post-tipoff games are not polled (the live polling tier was removed 2026-08-27); the `live → final` transition is driven by the `/scores` completion check, not by odds polling.

### Status Transitions

| From | To | Trigger | Actions |
| --- | --- | --- | --- |
| (new) | `scheduled` | `OddsApiScheduleSource._parse_scores_event()` | `Store.upsert_game()` creates the record; sport stamped from profile |
| `scheduled` / `live` | `live` (in-memory) | `time_to_tipoff <= 0` in `ScheduleService.process_live_transitions()` | `SettlementService.handle_game_live()`: update DB status, mark closing snapshots via [[SQLite Store]], calculate CLV via [[CLV Calculation]], log `clv:` per enriched bet (no Telegram — see [[Telegram Notifications]]) |
| `live` | `final` | Odds API `/scores` reports `completed=true` on refresh | `check_completed_games()`: update `home_score`/`away_score`, call `resolve_bets()` which sets win/loss/push/draw on value_bets |
| `scheduled` | `postponed` | Effectively dropped from the Odds API feed | No action — The Odds API exposes no postponed status; games that drop from the feed surface as `scheduled` until they age out |

### Status Mapping from the Odds API

`OddsApiScheduleSource._parse_scores_event()` derives status from the `/scores` response fields:

| Condition | Internal Status |
| --- | --- |
| `completed=true` AND both scores parse | `"final"` |
| `completed=true` but scores unparseable | `"live"` (conservative) |
| `scores` present, `completed=false` | `"live"` (in-progress) |
| No `scores` field | `"scheduled"` |

### Multi-Sport Discovery

`ScheduleService.fetch_fresh_games()` iterates `enabled_profiles()` (NBA, NHL, MLB, WNBA as configured) and calls `OddsApiScheduleSource.fetch_games(profile.odds_api_key)` once per sport. Each returned `Game` is stamped with the internal sport key from the profile (e.g. `"nba"`, `"nhl"`, `"mlb"`). All games across all enabled sports are combined into a single in-memory slate.

### Tipoff Source Selection

The effective tipoff for a game is `game.commence_time or game.scheduled_at`. For games discovered via the Odds API `/scores` endpoint, `scheduled_at` and `commence_time` are both set to the Pinnacle-derived `commence_time` at parse time (the `/scores` event always carries a `commence_time`). The `commence_time` column allows downstream code to distinguish Pinnacle-confirmed tipoffs from any legacy rows that lacked it.

### handle_game_live() — The Tipoff Transition

Fires **once per game per process lifetime** when `time_to_tipoff <= 0`. `_handled_live` (restored on startup from games with `is_closing=1` snapshots) prevents re-processing. This is the most critical transition in the system because it triggers:

1. `mark_closing_snapshots()` — marks the last odds snapshot per (bookmaker, market, selection, line) as the closing line. The partition is line-aware via `IFNULL(line, -999.0)`, so distinct spread/total lines each get their own closing marker. The freshness + pre-tipoff filter lives **inside** the window function's input so a post-tipoff row cannot shadow a valid pre-tipoff candidate.
2. CLV calculation for any pending `value_bets` on this game (`calculate_clv_for_pending_bets`)
3. A `clv:` (or `clv_suspect:`) log line per enriched bet — CLV is log/dashboard-only, never a phone message (2026-08-10 rebrand)

Adding to `_handled_live` happens ONLY after `handle_game_live()` returns successfully. A transient error leaves the game out of the set so the next scheduler iteration retries.

### check_completed_games()

Throttled to one Odds API fetch per `SCHEDULE_COMPLETION_CHECK_INTERVAL` — **default 900 s (15 min), env-overridable**; it was a hardcoded 300 s until 2026-08-08, when finals checking measured as the single largest Odds API burn (MLB is live 10–14 h/day). The throttle uses `_last_completion_check`, which successful schedule refreshes also stamp. Widening it only delays settlement and the won/lost Telegram by ~10 min; CLV is unaffected, because closing snapshots are marked at tipoff rather than at final. When a game is found final:

1. Updates `home_score` and `away_score` on the `games` record
2. Calls `resolve_bets()`, which runs `resolve_bet_result()` on each pending value_bet for win/loss/push/draw
3. Logs `result:` (or `result_suspect:`) per resolved bet, and sends a won/lost Telegram **only** for a bet that has a `placed_bets` row at status `settled` — real money only. Paper value bets settle silently (see [[Telegram Notifications]])

### Startup Recovery

`SettlementService.recover_historical_bets()` runs once at startup. It finds final games with value_bets missing CLV or result (stranded outside the 3-day Odds API window) and replays `recover_finalized_game()` for each.

Without this pass, bets stranded beyond yesterday/today/tomorrow never self-heal even after the underlying bug is fixed.

The recovery pass only replays `status='final'` games — a game whose final never arrived at all (pipeline down past the 3-day scores window, or a suspended game dropped from the feed) is invisible to it and stays `live` forever. Startup flags those via the stranded-game WARNING; remediation is the operator-run `oddsiq delete-zombie-games [--dry-run]`, which deletes the stuck games together with their snapshots and bets (see [[CLI Entry Point]]).

## Configuration

| Constant | Source | Notes |
| --- | --- | --- |
| Schedule refresh interval | `config.SCHEDULE_REFRESH_INTERVAL` (normal) / `config.SCHEDULE_GAME_WINDOW_REFRESH` (active) | ScheduleService throttle in `maybe_refresh()` |
| `SCHEDULE_COMPLETION_CHECK_INTERVAL` | 900 s (15 min) | Minimum gap between `check_completed_games()` fetches, throttled via `_last_completion_check`; was a hardcoded 300 s before 2026-08-08 |
| `CLOSING_FRESHNESS_MINUTES` | default 30 | Passed to `mark_closing_snapshots()` at tipoff |

## Error Handling

- `upsert_game()` is idempotent — safe to call repeatedly with the same `external_id`
- If the Odds API is down during refresh, stale game data is used (last successful refresh)
- If `handle_game_live()` fails transiently (DB blip, CLV edge case), the game is left out of `_handled_live` and retried next cycle
- CLV is skipped for any bet whose Pinnacle closing snapshot is missing — `clv_pct` remains NULL

## Tests

Game lifecycle is tested indirectly through `tests/test_store.py` (game upsert, handled-live restoration), `tests/test_schedule_service.py`, and `tests/test_settlement_service.py`.
