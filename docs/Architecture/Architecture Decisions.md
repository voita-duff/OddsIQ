---
tags:
  - type/decision-record
  - status/current
  - layer/architecture
source-files: []
related:
  - "[[System Overview]]"
  - "[[SQLite Store]]"
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[CloudflareSession]]"
  - "[[Edge Detection]]"
  - "[[Vig Removal]]"
  - "[[Pipeline Services]]"
  - "[[CLV Calculation]]"
  - "[[Failure Modes]]"
  - "[[SportProfile]]"
  - "[[Budget Governor]]"
  - "[[Live-First]]"
---

# Architecture Decisions

Foundational architecture decisions for OddsIQ, with rationale, tradeoffs, and current status. Decisions D1–D17 reflect the locked choices from the 2026-06-02 multi-sport refactor spec; earlier decisions remain where still true.

---

## D1 · Sport as the Primary Fan-Out Axis

**Decision:** The pipeline's primary fan-out axis is **sport**, not bookmaker. `enabled_profiles()` returns the active [[SportProfile]] list (NBA / NHL / MLB / WNBA); the orchestrator iterates it per tick.

**Why:** The multi-book era (Betano/Allwyn/Fortuna) revealed that Tipsport has the most exploitable pricing gaps (+5.87% avg CLV over 48 resolved bets at the time; that figure was later found to be inflated by the basketball matchView bug, see [[Fixed - Basketball MatchView Data Contamination]]). Adding NHL and MLB (both confirmed on Tipsport, both in season) gives more signal without the operational cost of multi-book maintenance. Sport is a cleaner axis than bookmaker for a single-soft-book pipeline.

**Implemented:** `oddsiq/sports/profiles.py` — `ENABLED_SPORTS = ["nba", "nhl", "mlb", "wnba"]`; `enabled_profiles()` drives both `ScheduleService.fetch_fresh_games()` and `PollCoordinator.poll()`.

---

## D2 · Enabled Sports: NBA / NHL / MLB / WNBA (NFL / NCAAF / NCAAB Deferred)

**Decision:** Three professional leagues shipped with the 2026-06-02 refactor; college and NFL are deferred. **Update 2026-06-11:** WNBA enabled as the fourth league on explicit user direction (15-team pro roster, same profile pattern — see [[Multi-Sport Expansion]]).

**Why:** NBA/NHL/MLB have clean ~30-team rosters, are confirmed on Tipsport, and are in season; WNBA's 15-team roster fits the same shape. College leagues have hundreds of teams requiring a different normalization approach. NFL is deferred (off-season) and can be added as a profile when ready. See [[Multi-Sport Expansion]] for deferred sport details and competition IDs.

---

## D3 · Schedule and Scores: Unified on The Odds API (balldontlie Retired)

**Decision:** `OddsApiScheduleSource` calls `/sports/{key}/scores?daysFrom=3` for game discovery, live status, and final scores. balldontlie is removed.

**Why:** The Odds API already supplies Pinnacle odds and carries game metadata including `commence_time`. A single uniform endpoint (`completed` flag + scores array) works across all sports with no per-sport status-string parsing (balldontlie required mapping "Qtr"/"Half"/"OT"). The 20k/month budget upgrade makes the extra `/scores` calls negligible.

**Validated (2026-06-03):** The `/scores` upcoming set and its lookahead match `/odds` exactly; a separate `/odds` fetch for schedule purposes is redundant. Finals for NHL/MLB are reliably reported.

---

## D4 · Migration 005: `sport` Column on All Key Tables

**Decision:** `sport TEXT NOT NULL DEFAULT 'nba'` added to `games`, `odds_snapshots`, `value_bets` (and, until migration 011 dropped it on 2026-08-27, `live_edge_candidates`). Existing rows backfill to `'nba'` (all prior history is NBA — correct).

**Why:** Per-sport queries (dashboard tabs, audit) need to filter without joining. The denormalization is intentional: `game_id` already scopes rows to one sport, but the sport column avoids repeated joins for read-heavy paths.

**Note:** `sport` is not part of any unique/dedup key — those constraints stay exactly as they were.

---

## D5 · Scheduler Decomposed into Orchestrator + Focused Services

**Decision:** The former 1132-line `oddsiq/scheduler.py` became `oddsiq/pipeline/scheduler.py` (slim orchestrator) plus focused, independently-testable services. See [[Pipeline Services]] for the full breakdown.

**Why:** The monolith was hard to test (everything tangled) and hard to evolve (any change risked global side effects). Each service now has one responsibility and its own unit tests.

---

## D6 · Data-Driven Cadence: far / warm / hot / live

**Decision:** Tiers based on DB analysis of 54 historical value bets (as shipped 2026-06-02; the live tier was removed 2026-08-27 — see D18):

| Tier | Window | Cadence | Pinnacle? |
|---|---|---|---|
| (skip) | > 6h | — | no |
| far | 2–6h | 30 min | no |
| warm | 1–2h | 300s | yes |
| hot | < 60m | 90s | yes |

**Why (data):** 89% of value bets land in the last hour, peaking at 15–30 min before tipoff. The old hot tier was <30 m (120s); widening it to <60 m captures more of the peak window. The old medium tier (30 m–2 h, 600s) becomes warm (1–2 h, 300s) with Pinnacle always called — rare early edges need a current Pinnacle reference to be detected. (The in-play `live` tier this decision originally included was removed 2026-08-27 — post-tipoff games are unpollable.) See [[Polling Tiers]] for the full table.

---

## D7 · Budget Governor Gates All Pinnacle Fetches

**Decision:** A [[Budget Governor]] sits in front of every Pinnacle call and throttles cadence when the projected monthly burn exceeds the quota — warm widens first, hot (the betting-relevant tier) is protected. A hard reserve floor (`BUDGET_RESERVE_FLOOR`, default 1500) triggers the most conservative level regardless of projection. (The throttle table's live column was removed with the live tier, 2026-08-27.)

**Why:** The 20k/month budget upgrade enables aggressive cadences but at peak season (NBA + NHL + MLB daily) the nominal burn can exceed 20k. The governor prevents a mid-month hard stop while protecting the hot tier that produces the value bets.

See [[Budget Governor]] for the throttle level table and math.

---

## D8 · Betano / Allwyn / Fortuna Deleted Entirely

**Decision:** The three opt-in Czech books are removed from the codebase — code, tests, config, docs. Recoverable from the pre-release history.

**Why:** All three price more sharply than Tipsport (tighter vig, fewer exploitable gaps). Operational cost (Playwright + session management for Betano; key rotation for Allwyn; two-step REST + tournament ID changes for Fortuna) outweighs the marginal signal. Tipsport is the proven soft book (+5.87% avg CLV). The archive tag preserves full recoverability if a Tipsport-ban fallback is ever needed.

---

## D9 · Live Promoted to First-Class Path — REVERSED 2026-08-27 (see D18)

**Decision (2026-06-02, since reversed):** Live was designated the intended primary value path (strong user prior: Tipsport's live lines lag the sharp more than pregame). The live scout recorded candidates crossing `LIVE_EDGE_THRESHOLD`, markout-confirmed each one poll cycle later, and reported live-value metrics — always in its own `live_edge_candidates` time series, never touching `value_bets`/CLV/closing.

**Outcome:** The thesis never validated (Tipsport's `WINNER_FINAL_DECISION` tab carried no in-play prices for basketball/hockey, so signal only came from MLB), there was no live betting to act on it, and the data went unused while costing quota. The whole subsystem was removed 2026-08-27 — see D18 and [[Live-First]].

---

## D10 · Draw / Tie Handling Added to Settlement

**Decision:** `resolve_bet_result()` handles an explicit `"draw"` outcome. Scrapers fetch only the 2-way winner-incl-OT market per sport (matches Pinnacle `h2h`); any regulation 3-way is excluded.

**Why:** NBA can't tie (2-way is clean), but NHL/MLB and possible future sports could surface 3-way markets on Tipsport. Fail-closed exclusion prevents a misfetched regulation market from resolving as a double-loss. The `WINNER_FINAL_DECISION` market rule in the NHL profile enforces this at fetch time.

---

## D11 · Tipsport Is the Sole Soft Book; Parameterized by SportProfile

**Decision:** The [[TipsportScraper]] takes a `SportProfile` and uses `profile.tipsport_competition_id`, `profile.tipsport_moneyline_matchview`, `profile.tipsport_sport_slug` — no hardcoded NBA constants remain in the scraper.

**Why:** Sole-book status justifies a quality pass: clean parameterization, validated live path, thorough per-sport tests. Adding a new sport = adding a profile, not modifying the scraper.

See [[TipsportScraper]] for the Cloudflare/Playwright/JSESSIONID-sync mechanics.

---

## D18 · Live-Edge Subsystem Removed (2026-08-27)

**Decision:** The live-edge subsystem was removed in its entirety: the post-tipoff `live` polling tier (tiers are now far/warm/hot — post-tipoff games are unpollable), `LiveScoutService` + markout classification, live-tier Pinnacle polling (freed ~150–225 Odds API credits/day), the `live_edge_candidates` table (dropped by migration 011), the `SportProfile.live_tipsport_coverage` field, the live config knobs (`POLL_TIER_LIVE`, `LIVE_EDGE_THRESHOLD`, `LIVE_SCOUT_MAX_HOURS`, `LIVE_EDGE_ALERT_THRESHOLD`, the five `LIVE_MARKOUT_*`), the `live-discover` / `live-scout-report` CLI commands, and the dashboard's live-edge surfaces.

**Why:** No live betting exists to act on the signal, the live tier's quota cost was real, and the accumulated data went unused. The removal reverses D9.

**What survives (lifecycle, unchanged):** `status='live'` game transitions + `game_live:`/`game_final:` logs, closing snapshots marked at tipoff (clock-driven — CLV provably unaffected), finals via `/scores` + settlement, `SportProfile.live_backstop_hours` as a pure lifecycle window (zombie-game detection + `/scores` refresh scoping), and `delete-zombie-games` (cascade no longer includes the dropped table).

**Archive:** the dataset and the pre-removal code are kept offline, outside this repository. See [[Live-First]] for the full retirement record.

---

## 8.1 · SQLite over PostgreSQL (Unchanged)

**Decision:** [[SQLite Store|SQLite]] with WAL mode.

**Why:** Single-user, single-process system on a laptop. No concurrent writes, no network database needed. SQLite gives zero-config setup, file-based backups, and sub-millisecond reads. WAL mode prevents reader-writer contention.

**Schema evolution:** Forward-only migration runner at `oddsiq/db/migrations/`. Fourteen migrations shipped: `000_baseline.py` through `013_scope_value_bet_dedup_by_exclusion.py`; 012 added sharp-source provenance and 013 allows clean recovery after excluded evidence. See [[Migrations]] for the full list.

---

## 8.2 · Pinnacle via The Odds API, Not Direct (Updated)

**Decision:** Use [[PinnacleScraper|The Odds API]] aggregator. The Odds API now also supplies the schedule and scores source (D3), replacing balldontlie.

**Budget:** Upgraded from 500 to 20,000 requests/month. One `/odds` call per sport per cycle covers all that sport's games. Budget is managed by the [[Budget Governor]]; a single key, no rotation — the governor throttles cadence to fit the quota.

---

## 8.3 · Tipsport Reverse-Engineering (Unchanged; Now Parameterized)

**Decision:** Reverse-engineer Tipsport's internal REST API (`POST /rest/offer/v2/offer`).

**Current approach:**
- One `POST` per poll cycle keyed by `profile.tipsport_competition_id` and the profile's `tipsport_moneyline_matchview`.
- HTTP path: `curl_cffi` Chrome 146 → JSESSIONID-sync from Playwright cookie jar on curl rejection → Playwright fallback only if curl still fails after sync.
- Session lifecycle: proactive refresh at 20 min; reset after 3 consecutive failures.
- Moneyline-only (no spread/total markets). Pinnacle spread/total snapshots without a Tipsport counterpart produce zero edges; see [[Markets]].

See [[TipsportScraper]] and [[Failure Modes]] for the full failure table.

---

## 8.4 · Exact Line Matching (Unchanged)

**Decision:** Only compare odds when Pinnacle and Tipsport offer the exact same line.

**Why:** Interpolating fair odds between lines introduces model risk. Moneylines (no line) always match. For spreads/totals, line mismatches skip the comparison rather than interpolate. This is acceptable because the pipeline is currently moneyline-only.

---

## 8.5 · Single-Threaded Async (Unchanged; Concurrent Scraper Fetch per Sport)

**Decision:** One `asyncio` event loop, one process.

**Implementation:** `PollCoordinator` dispatches scraper fetches with `asyncio.gather()` per sport — Pinnacle and Tipsport fetch concurrently within a sport. DB writes + edge/live dispatch stay ordered after the gather. The per-sport loop is sequential (each sport completes before the next starts).

---

## 8.6 · Detection-Only, Evaluation Mode — SUPERSEDED 2026-08-09

**Decision (since superseded):** Flag bets only; no auto-placement. Evaluation mode gates on **passive CLV evidence** from the 24/7 pipeline, not manual bet placement (the "200 manually placed bets" gate is a known dead criterion — overnight CET tipoffs are unreachable for the user).

**Planned future:** [[P5 - Auto-Betting]] (capstone in design intent).

**Superseded 2026-08-09:** auto-betting was explicitly commissioned and the P1 engine shipped — `BettingService` (qualification chain, meta-table money knobs, circuit breaker), the token-authenticated WebSocket `BettingBridge`, the Chrome-extension actuator, the `placed_bets` decision ledger (migration 010), and `oddsiq betting --arm/--disarm`. It is live on the VM, armed, betting real Kč on the sports listed in the meta `betting_sports` key. The decision text above is kept as the historical record; the **evaluation criterion survives it** — a new sport or market is still validated on passive CLV evidence (and a Tipsport hold measurement) before the operator ticks it into `betting_sports`. Current design specs: `docs/Design/2026-08-09-p1-auto-betting-design.md` and `docs/Design/2026-08-25-nfl-fifth-sport-autobetting-design.md` (the 2026-04-21 spec cited above was deleted 2026-06-08 — git history retains it).
