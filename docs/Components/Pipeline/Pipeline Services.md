---
tags:
  - component/pipeline
  - status/current
  - layer/orchestration
  - type/component
source-files:
  - oddsiq/pipeline/scheduler.py
  - oddsiq/pipeline/schedule_service.py
  - oddsiq/pipeline/poll_coordinator.py
  - oddsiq/pipeline/edge_service.py
  - oddsiq/pipeline/settlement_service.py
  - oddsiq/pipeline/budget_governor.py
  - oddsiq/pipeline/coverage_monitor.py
  - oddsiq/pipeline/stats.py
  - oddsiq/pipeline/tiers.py
related:
  - "[[System Overview]]"
  - "[[Architecture Decisions]]"
  - "[[Data Flow]]"
  - "[[Polling Tiers]]"
  - "[[Budget Governor]]"
  - "[[SportProfile]]"
  - "[[Edge Detection]]"
  - "[[CLV Calculation]]"
  - "[[Telegram Notifications]]"
---

# Pipeline Services

`oddsiq/pipeline/` holds the decomposed scheduler: a slim orchestrator (`Scheduler`) plus focused, independently-testable services. The former 1132-line monolith (`oddsiq/scheduler.py`) was split in the 2026-06-02 multi-sport refactor (Architecture Decision D5).

## Orchestrator: `Scheduler`

**File:** `oddsiq/pipeline/scheduler.py`

Single responsibility: the main loop, heartbeat, preflight, and delegation.

- Owns startup: loads handled-live state, runs historical bet recovery, checks browser preflight, probes budget, initial schedule refresh.
- Main loop (30s sleep granularity): emits heartbeat, calls `ScheduleService.maybe_refresh()`, calls `ScheduleService.process_live_transitions()`, computes pollable games + shortest governor-throttled interval, calls `PollCoordinator.poll()` per enabled sport when the interval has elapsed, persists budget snapshot, calls `ScheduleService.check_completed_games()`, calls `CoverageMonitor.check_gaps()`.
- Shutdown: forces budget persist, prints the session summary to the console (`print_summary()` — console/log only, the Telegram session summary was dropped in the 2026-08-10 rebrand), closes all scraper sessions.
- The only Telegram the orchestrator itself sends is the Tipsport login pair: `_on_login_change(event)` — wired as the `BettingBridge` `on_login_change` hook — sends `format_login_lost()` / `format_login_restored()` on confirmed status transitions.

The orchestrator owns nothing domain-specific — it delegates to services and reads only the governor interval and the games list.

## ScheduleService

**File:** `oddsiq/pipeline/schedule_service.py`

Per-sport game discovery + status transitions over The Odds API.

- `fetch_fresh_games()` — iterates `enabled_profiles()`, calls `OddsApiScheduleSource.fetch_games(profile.odds_api_key)` for each, returns a sport-stamped game list.
- `refresh()` — upserts games to DB; triggers `SettlementService.recover_finalized_game()` for any final games found.
- `maybe_refresh(now)` — throttled: every 5 min when any game is within 6 h of tipoff, every 1 h otherwise.
- `process_live_transitions(now)` — detects games whose tipoff just passed; calls `SettlementService.handle_game_live()` exactly once per game per process (uses `_handled_live` set, persisted to DB via `get_handled_live_game_ids()`). If `handle_game_live()` raises, the game stays out of `_handled_live` so it retries next cycle.
- `check_completed_games()` — throttled to one fetch per 5-min window; detects final transitions and calls `SettlementService.resolve_bets()`.

## PollCoordinator

**File:** `oddsiq/pipeline/poll_coordinator.py`

Per-sport tier decisions, concurrent scraper fetches, match/distribute, health log.

- `poll(profile, pollable, slate, now)` — categorizes each pollable game by tier; determines if Pinnacle is needed (`tiers.pinnacle_needed()`) — including the far tier for `shadow_far_detection` sports; dispatches `asyncio.gather()` across all scrapers to be fetched; then distributes matched snapshots to games; runs `EdgeService.detect_pregame_edges` per game (warm/hot; far as shadow for `shadow_far_detection` sports), collecting the returned `PendingBet`s across the cycle (a per-game failure logs `detect_failed:` and the loop continues), then calls `EdgeService.dispatch_pending()` ONCE after the loop. DB writes and edge dispatch stay ordered after the gather. (There is no live branch — the live tier was removed 2026-08-27; pollable games are pregame-only.)
- `_fetch_scraper_odds(scraper, game_date, profile)` — wraps one scraper's `fetch_odds(game_date, profile)` call; records duration, error count, zero-odds flag to `SessionStats`.
- Emits one `Scraper health:` INFO log per scraper per poll with the volume/match/error fields plus `unsafe_start_snapshots`, `sharp_source_age_max_ms`, `missing_source_updates`, `missing_event_ids`, and `event_id_mismatches`. Tipsport snapshots without their own parseable listing start fail closed. Pinnacle snapshots use exact Odds API event-id attribution before the existing team/time checks.
- Far-tier games receive no Pinnacle rows (the call wasn't made for them) — except in `shadow_far_detection` sports, where the far tier fetches Pinnacle and detects shadow rows.
- Reconciles `commence_time` from Pinnacle snapshots into `games.commence_time` (once per game per session).

## EdgeService

**File:** `oddsiq/pipeline/edge_service.py`

Pregame +EV detection → `value_bets` (+ the `PendingBet`s handed to the betting engine via `dispatch_pending`). Detections are log-only since the 2026-08-10 rebrand: `value_bet:` for a clean insert, `value_bet_suspect:` for a guard-stamped one, each alongside a console `VALUE BET` / `SUSPECT VALUE BET` line from `_print_value_bet()`.

- `detect_pregame_edges(game, game_snapshots, tier) -> list[PendingBet]` — called for warm and hot tier games (and far for `shadow_far_detection` sports; those rows are stamped `tier='far'` and skipped by `SessionStats`). Records value bets and RETURNS the clean inserts as `PendingBet(game, vb, pinnacle_fresh_at)` instead of calling the engine inline. The book-coherence guard pairs sides within ONE Tipsport listing (`tipsport_match_id`); the **completeness guard** in the same block stamps `incomplete_book_market` when that listing carries an execution id but only one side, and has its own off-switch `PREGAME_REQUIRE_COMPLETE_BOOK_MARKET` (default `1`) — `PREGAME_MIN_BOOK_OVERROUND<=0` disables only the coherence floor. Full guard order in [[Edge Detection]].
- **Shadow rows are never BET — but they ARE dispatched.** A clean far-tier detection is appended to the pending list exactly like a warm/hot one (there is no tier condition on the append); `BettingService._qualify` is what stops it, returning `tier_not_bettable` and writing a `skipped` row to `placed_bets`. So the engine sees every shadow detection and leaves an auditable skip for each — do not read "shadow" as "withheld from the engine".
- `dispatch_pending(pending)` — hands a poll cycle's clean detections to `BettingService.on_value_bet` one at a time, **earliest kickoff first, biggest edge as the tiebreak** (`_dispatch_order`): placements are serial and each bet has a hard deadline (the tipoff buffer), so a bet with an hour of slack must not spend the last window of one that is two minutes out; within one kickoff wave this is exactly edge order. Qualification therefore runs on the engine's clock at dispatch — a bet found at kickoff−125 s but reached after two placements is skipped `too_close_to_tipoff`, not started inside the flip window (2026-09-06, the NFL Sunday-burst rule). Each dispatch is isolated (`bet_dispatch_failed:` on exception; a stranded bet is unrecoverable because its row dedups the next cycle's re-detection, so one failure must not take the wave).
- Uses `find_edges(pinnacle_odds, tipsport_odds)` from `oddsiq/engine/edge.py`.
- Writes `ValueBet` rows with the tier stamped (warm- and hot-tier observations of the same selection coexist as independent rows via `ON CONFLICT ... DO NOTHING`); since migration 013 the conflict identity also carries `IFNULL(excluded_reason, '')`, so a guard-stamped observation and a later CLEAN detection of the same selection/tier coexist rather than the stamp swallowing the clean one.
- Logs (and prints) a detection on NEW inserts only — a dedup hit is silent (`insert_value_bet` returns `None` and the loop `continue`s). No Telegram: every bettor-facing message comes from `BettingService` (bet placed / rejected-failed / not-logged-in skip / auto-disarm), `SettlementService` (won/lost for a real `placed_bets` row), or the scheduler's login hook.

> **LiveScoutService — removed 2026-08-27.** The live-edge subsystem (post-tipoff polling, `live_scout_service.py`, markout classification, the `live_edge_candidates` table) was deleted entirely; the dataset is archived at `~/dev/oddsiq-live-edge-archive/` (code at git tag `archive/live-edges-2026-08-27`). See [[Live-First]].

## SettlementService

**File:** `oddsiq/pipeline/settlement_service.py`

Pregame CLV, result resolution, historical recovery.

- `handle_game_live(game)` — logs `game_live:`, marks closing snapshots, calculates CLV for pending value bets. CLV is stored and logged (`clv:`, `clv_suspect:` for a guard-stamped row, or `clv_shadow:` for a far-shadow row), never messaged. Called by `ScheduleService.process_live_transitions()` exactly once per game.
- `calculate_clv_for_pending_bets(game)` — for each value bet without CLV: gets Tipsport + Pinnacle closing odds (line-aware), removes vig (bet selection passed first so `fair_sel` is the wanted side), computes `clv = (entry_odds / closing_fair) - 1.0`. Uses `get_value_bets_without_clv` (not `get_pending_value_bets`) so it can revisit a bet whose result was set before its CLV was computed.
- `resolve_bets(game)` — calls `resolve_bet_result()` for each pending value bet (win/loss/push/draw) and logs `result:` (`result_suspect:` when stamped). The ONLY Telegram settlement sends is `format_bet_result()`, and only when the bet has a linked `placed_bets` row in status `settled` — paper value-bet settlements notify nothing.
- Real-money win settlement uses `accepted_odds`; when a confirmed receipt omitted that field it falls back to `requested_odds` and stamps `placed_bets.error` with `settled_using_requested_odds`. If neither price is valid, settlement stays open and logs `placed_bet_settlement_blocked` instead of recording a fabricated full loss.
- **Excluded bets settle silently** (2026-08-10): a bet stamped `excluded_reason` at detection was never alerted as a VALUE BET, so settlement mirrors the suppression — CLV and result are still written to the DB (the stale-price guards stay validatable) but the session CLV stats are not incremented and no WIN/LOSS Telegram can fire (the suspect branch `continue`s before the `placed_bets` lookup). Log lines `clv_suspect:` / `result_suspect:` replace `clv:` / `result:`; neither is in the dashboard activity-feed prefix whitelist (`log_tail._EVENT_PREFIXES`), so they stay out of the feed — unlike `value_bet_suspect:`, which was ADDED to that whitelist on 2026-08-14 so guarded detections are visible rather than making the dashboard look dead. Both suspect lines also carry a `tier=` field, so a far-tier shadow row that is ALSO excluded logs `clv_suspect: … tier=far` rather than `clv_shadow:` (the suspect branch wins the `continue`) — grep `tier=far` to find shadow rows in either line.
- **Shadow rows write CLV but skip session stats** (2026-09-06): a far-shadow value bet (`tier='far'`, `tiers.SHADOW_TIER` — the NFL `shadow_far_detection` experiment) gets its CLV computed and stored exactly like a bettable one (measuring it IS the experiment), but is not counted into `SessionStats` and logs `clv_shadow:` instead of `clv:` — otherwise the shutdown CLV line would blend the week-out shadow sample into the warm/hot number. There is no matching carve-out in `resolve_bets()`: result resolution has no `SessionStats` side effects, and the far-shadow review reads exactly those settled rows.
- `recover_historical_bets()` — one-shot startup pass; replays recovery for final games with missing CLV or result (beyond the 3-day Odds API window).
- `recover_finalized_game(game)` — safe to run repeatedly: `calculate_clv_for_pending_bets` is idempotent (CLV-specific query skips already-settled bets), `resolve_bets` filters `result IS NULL`.

## BudgetGovernor

**File:** `oddsiq/pipeline/budget_governor.py`

See [[Budget Governor]] for the full documentation.

In brief: probes Pinnacle quota at startup, projects monthly burn, returns governor-throttled intervals per tier, persists to `meta`.

## CoverageMonitor

**File:** `oddsiq/pipeline/coverage_monitor.py`

Gap detection: alerts when a scraper's matched coverage for a watched game goes stale.

- `record_match(game_id, scraper_name, now)` — called by `PollCoordinator` when a scraper's odds match a game. Clears any outstanding gap alert for the `(game, scraper)` pair.
- `check_gaps(games, scrapers, now)` — throttled to one run per 2 min. For each warm/hot-tier game, checks if any scraper's last match is >10 min stale. One alert per `(game_id, scraper_name)` until the scraper's next successful match.
- Reads in-memory timestamps (not DB rows) — snapshot dedup (stable odds → no new rows) doesn't mask a stuck scraper.

## SessionStats

**File:** `oddsiq/pipeline/stats.py`

In-memory aggregation of session-level counters (value bets flagged, total edge, CLV, per-book breakdowns, per-scraper poll/error/duration metrics). Consumed by `Scheduler.print_summary()` at shutdown — console/log only (the Telegram session summary was dropped in the 2026-08-10 rebrand).

## tiers (Pure Functions)

**File:** `oddsiq/pipeline/tiers.py`

Stateless tier functions shared by `Scheduler` and `PollCoordinator`. All tiers are pregame — post-tipoff games are unpollable (the live tier was removed 2026-08-27):

- `tier_for(seconds_to_tipoff)` → `"far"` / `"warm"` / `"hot"`, or `None` once the game has tipped off
- `get_poll_interval(seconds_to_tipoff)` → interval in seconds, or `None` (outside the polling window, including any post-tipoff time)
- `pinnacle_needed(active_tiers, *, shadow_far=False)` → True if any tier in the set is warm/hot — or `far` when `shadow_far=True` (`SportProfile.shadow_far_detection`)
- `SHADOW_TIER` → `"far"`, the constant every shadow-row filter reads (dashboard aggregates, per-book audit, `SessionStats`)
- `time_to_tipoff(game, now)` → seconds (negative when past tipoff)
- `effective_tipoff(game)` → `commence_time` or `scheduled_at`

See [[Polling Tiers]] for the tier boundary values and rationale.

## Interaction Diagram

```
Scheduler (orchestrator)
  │
  ├─► ScheduleService   — fetch_fresh_games(), maybe_refresh(),
  │                        process_live_transitions(), check_completed_games()
  │       └─► SettlementService  — handle_game_live(), recover_*(),
  │                                 calculate_clv_for_pending_bets(), resolve_bets()
  │
  ├─► BudgetGovernor    — probe(), interval_for_tier(), persist()
  │
  ├─► PollCoordinator   — poll(profile, pollable, slate, now)
  │       ├─► PinnacleScraper.fetch_odds(game_date, profile)   ─┐ asyncio.gather
  │       └─► TipsportScraper.fetch_odds(game_date, profile)   ─┘
  │           ↓ ordered after gather
  │       ├─► EdgeService.detect_pregame_edges()    (warm/hot; far = shadow for NFL)
  │       ├─► EdgeService.dispatch_pending()        (once per cycle, kickoff-ordered)
  │       └─► CoverageMonitor.record_match()
  │
  └─► CoverageMonitor.check_gaps()
```

Data flows left-to-right and top-to-bottom; nothing flows backwards (services don't call back into the orchestrator).
