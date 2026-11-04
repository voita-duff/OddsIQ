---
tags:
  - component/pipeline
  - status/current
  - layer/orchestration
  - type/diagram
source-files:
  - oddsiq/pipeline/scheduler.py
  - oddsiq/pipeline/poll_coordinator.py
  - oddsiq/pipeline/schedule_service.py
  - oddsiq/pipeline/edge_service.py
  - oddsiq/pipeline/settlement_service.py
  - oddsiq/scrapers/pinnacle.py
  - oddsiq/scrapers/tipsport.py
  - oddsiq/scrapers/odds_api_schedule.py
  - oddsiq/engine/edge.py
  - oddsiq/engine/vig.py
  - oddsiq/engine/clv.py
  - oddsiq/db/store.py
related:
  - "[[System Overview]]"
  - "[[Pipeline Services]]"
  - "[[SportProfile]]"
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[Team Name Normalization]]"
  - "[[Edge Detection]]"
  - "[[Vig Removal]]"
  - "[[CLV Calculation]]"
  - "[[Telegram Notifications]]"
  - "[[SQLite Store]]"
  - "[[Polling Tiers]]"
  - "[[Budget Governor]]"
---

# Data Flow

The complete data flow through OddsIQ, from external APIs to alerts and bet resolution.

## Pipeline Diagram

```
         The Odds API /scores?daysFrom=3
         (per enabled sport: NBA / NHL / MLB / WNBA)
                      |
            OddsApiScheduleSource.fetch_games()
                      |
                 +----v----+
                 |  games  |  (SQLite, sport-stamped)
                 +----+----+
                      |
           per-sport poll loop (PollCoordinator)
                      |
        +-------------+-------------+
        |                           |
  PinnacleScraper             TipsportScraper
  /sports/{key}/odds           /rest/offer/v2/offer
  (sharp reference +           (sole soft book;
   commence_time)               profile-keyed)
        |                           |
        |   curl_cffi → 403 → JSESSIONID-sync → Playwright
        |
        +---[asyncio.gather — concurrent per sport]---+
                                                      |
                            match by team names (sport-scoped canonical set)
                            (_home_team, _away_team)
                                                      |
                                          +-----------v-----------+
                                          |    odds_snapshots     |
                                          |  (SQLite, sport col)  |
                                          +-----------+-----------+
                                                      |
                                        +-------------+-------------+
                                        |                           |
                                    tier=warm                   tier=hot
                                        |                           |
                                        +------- EdgeService -------+
                                                 find_edges()
                                                      |
                                                 value_bets
                                                 + log value_bet:
                                                 + PendingBet → BettingService → placed_bets
                                                      |
                    at tipoff (SettlementService.handle_game_live):
                      mark_closing_snapshots()
                      calculate_clv_for_pending_bets()
                      → log clv:
                              |
                    at game final (SettlementService.resolve_bets):
                      resolve_bet_result()
                      → log result:
                      → Telegram won/lost ONLY if a placed_bets row settled
```

(Polling is pregame-only: the live tier, `LiveScoutService`, and the `live_edge_candidates` branch were removed 2026-08-27 — post-tipoff games are unpollable; see [[Live-First]].)

## Stage-by-Stage Breakdown

### 1. Schedule Fetch

`ScheduleService.fetch_fresh_games()` iterates `enabled_profiles()` (NBA / NHL / MLB / WNBA) and calls `OddsApiScheduleSource.fetch_games(profile.odds_api_key)` for each.

- **Endpoint:** `GET /sports/{key}/scores?daysFrom=3&dateFormat=iso`
- **Response shape:** uniform across sports — `commence_time`, `completed` flag, scores array. No per-sport status string parsing needed (balldontlie required mapping e.g. "Qtr"/"Half"/"OT").
- **Status mapping:** `completed=True` + both scores parsed → `"final"`; scores present but `completed=False` → `"live"`; no scores → `"scheduled"`.
- **Output:** `Game` objects sport-stamped with the profile's `key` (e.g. `"nba"`, `"nhl"`, `"mlb"`).
- **Storage:** Upserted into `games` via `Store.upsert_game()` (INSERT or UPDATE by `external_id`).
- **Failure handling:** the schedule source shares `oddsiq/scrapers/retry.with_backoff` (up to 3 total attempts / 2 retries, full-jitter exponential backoff, `Retry-After` clamped to `MAX_DELAY=30s`), retrying on 429/5xx/transport errors and raising `ScheduleFetchError` immediately on a hard 4xx, an unexpected non-2xx, or a malformed (non-JSON) body. `ScheduleService.refresh()` preserves the prior game cache on `ScheduleFetchError`.
- **Refresh cadence:** Every 5 min when any game is within 6 h of tipoff; every 1 h otherwise.

### 2. Odds Polling

`PollCoordinator.poll(profile, pollable, slate, now)` runs once per enabled sport per tick. It categorizes each pollable game by tier, then fetches odds concurrently:

**[[PinnacleScraper]]** (via The Odds API)
- Single `GET /sports/{odds_api_key}/odds` returns odds for all games of that sport in one request.
- Gated by tier: called only when any game is in the warm or hot tier (`tiers.pinnacle_needed()`). Far-tier games never trigger a Pinnacle call; post-tipoff games are unpollable.
- Market: `h2h` (moneyline) only by default (`PINNACLE_MARKETS=h2h`). The [[Budget Governor]] throttles the cadence when quota is at risk.

**[[TipsportScraper]]** (reverse-engineered Tipsport.cz API)
- One `POST /rest/offer/v2/offer` per poll cycle, keyed by `profile.tipsport_competition_id` and the 2-way moneyline `matchView` from `profile.tipsport_moneyline_matchview`.
- NHL (`superSportId=23`), NBA, and WNBA (`superSportId=7`) use `WINNER_FINAL_DECISION`; only MLB uses the default `WINNER_WHOLE_MATCH`. The regulation 3-way (`WINNER_WHOLE_MATCH` for hockey and basketball) is excluded — fail-closed (`TipsportMarketUnavailable` when the tab is absent).
- HTTP path: `curl_cffi` Chrome 146 impersonation → on rejection, copy `JSESSIONID` from [[CloudflareSession|Playwright]] into curl → Playwright fallback only if curl still fails after sync.
- Session lifecycle: proactive refresh at 20 min; reset after 3 consecutive failures.

Scrapers are fetched concurrently with `asyncio.gather()`. DB writes + edge dispatch stay ordered after the gather.

### 3. Game Matching

Each `OddsSnapshot` carries `_home_team`, `_away_team`, (Pinnacle only) `_commence_time`, and the sport as transient fields. `PollCoordinator._matches_game()` compares these directly against the `Game` objects in the current sport's pollable list.

[[Team Name Normalization]] resolves bookmaker labels to canonical names using the per-sport team set from the `SportProfile` (`profile.canonical_teams`, `profile.team_mappings`). Matching is scoped to the sport — an NBA team name never collides with an NHL name.

Matched snapshots get `game_id` assigned, then batch-inserted into `odds_snapshots`. The store bumps `captured_at` on dedup hits ("last seen" semantics) but never bumps an `is_closing=1` row.

Pinnacle's `_commence_time` is reconciled into `games.commence_time` once per game per session.

### 4. Edge Detection (Pregame)

`EdgeService.detect_pregame_edges()` runs for games in **warm and hot tiers** (≤ 2 h to tipoff). Far-tier games are skipped — lines aren't sharp enough (confirmed by [[First Game Night|Production Observations]]: −33% CLV on a far-tier detection).

1. Groups Pinnacle odds by `(market_type, abs(line))` — normalizes spread sign.
2. For each Tipsport snapshot, looks up the Pinnacle pair by exact `(market_type, abs(line))` match. Line mismatch → skip.
3. [[Vig Removal|Removes vig]] from the Pinnacle pair → fair odds for each side.
4. Calculates edge: `edge = (tipsport_odds / fair_odds) - 1.0`.
5. Filters by `EDGE_THRESHOLD` (default 3%).
6. Inserts into `value_bets` (deduped on `(game_id, bookmaker, market_type, selection, IFNULL(line, -999.0), tier) DO NOTHING` — tier is part of the key; warm- and hot-tier observations coexist as independent rows).

### 4b. Live Scout — removed 2026-08-27

There is no post-tipoff detection stage anymore. The live scout (`LiveScoutService.detect_live_edges()` → `live_edge_candidates`, markout confirmation, live-tier Pinnacle polling) was removed with the live-edge subsystem; the dataset is archived offline. See [[Live-First]].

### 5. Detection output + betting handoff

When a value bet is inserted (not a duplicate), it is **log-only** — no phone message (2026-08-10 rebrand):

- `EdgeService` logs `value_bet: bookmaker=… game_id=… market=… selection=… line=… edge=… tier=…` (a guard-stamped bet logs `value_bet_suspect:` instead) and prints a console `VALUE BET` block.
- A **clean** detection additionally becomes a `PendingBet(game, vb, pinnacle_fresh_at)`. The coordinator collects a whole poll cycle's pending bets, then `EdgeService.dispatch_pending()` hands them to [[Pipeline Services|BettingService]] **earliest kickoff first** (biggest edge as the tiebreak), one at a time, so each is re-qualified on the clock at dispatch (two-phase dispatch, 2026-09-06). A single failing dispatch logs `bet_dispatch_failed:` and never takes the rest of the wave with it.
- Telegram enters only downstream of a real placement — see [[Telegram Notifications]].

### 6. Closing Line Capture (At Tipoff)

When `ScheduleService.process_live_transitions()` detects a game's tipoff has passed (once per game per process), `SettlementService.handle_game_live()` fires:

1. **Mark closing snapshots:** `Store.mark_closing_snapshots(game_id, CLOSING_FRESHNESS_MINUTES)` — freshness filter runs inside the `ROW_NUMBER()` window, so a post-tipoff row can never win `rn=1` and claim the closing mark.
2. **Calculate CLV** for each pending value bet: get Tipsport closing odds + Pinnacle closing pair (both sides, line-aware), remove vig → `closing_fair`, compute `clv = (entry_odds / closing_fair) - 1.0`.
3. Log `clv:` (or `clv_suspect:` for a guard-stamped bet). No Telegram — CLV is dashboard/log-only.

### 7. Bet Resolution (At Game Final)

When `ScheduleService.check_completed_games()` detects a final transition:

1. Updates `games` with final scores.
2. `SettlementService.resolve_bets()` calls `resolve_bet_result()` for each pending value bet (win/loss/push/draw).
3. Logs `result:` (or `result_suspect:`). Telegram fires **only** when `get_placed_bet_for_value_bet()` returns a `settled` row — a real-money win/loss with Kč P&L. Paper value-bet settlements notify nothing.

### 8. Session Summary (On Shutdown)

On `SIGINT`/`SIGTERM`:

1. Forces a final budget persist.
2. Prints session summary (games tracked, bets flagged, CLV stats, budget) — console/log only, no phone message (2026-08-10 rebrand).
3. Closes the shared [[Telegram Notifications]] HTTP client.
4. Closes all scraper sessions (Playwright, httpx).
5. Closes the [[SQLite Store|database]] connection.
