---
tags: [type/architecture]
related: []
---
# OddsIQ — Map of Content

Async Python pipeline for detecting +EV moneyline betting opportunities across five profiled sports — NBA/NHL/MLB/WNBA/NFL (WNBA added 2026-06-11, NFL 2026-08-25) — of which **MLB and NFL are enabled**. Compares Tipsport odds (the sole soft book) against sharp Pinnacle lines via The Odds API. **Evaluation mode, with the auto-betting engine live**: detection still accumulates CLV evidence, but since 2026-08-09 qualified bets are also placed for real money (see [[P5 - Auto-Betting]] for the superseded plan and the `docs/Design/2026-08-09-p1-auto-betting-design.md` spec for what shipped). The multi-sport refactor (2026-06-02) shipped: sport-axis fan-out (NBA/NHL/MLB via [[SportProfile]]), Tipsport-only, Odds API schedule, pipeline decomposition into [[Pipeline Services]], [[Budget Governor]], and live-first design (retired: the live-edge subsystem was fully removed 2026-08-27 — post-tipoff games are unpollable; see [[Live-First]]). P4 is partially shipped (Docker + Odds-API backoff shipped 2026-06-09; the Telegram heartbeat was removed 2026-08-10 for the `HEARTBEAT_PING_URL` dead-man ping; recovery / log-rotation monitoring / season boundaries deferred); the P5/P6 roadmap pages are unbuilt as written. The pipeline runs 24/7 accumulating CLV evidence plus, now, realized `placed_bets` P&L — that data decides whether to commit further. The legacy roadmap's "200 manually placed bets" gate is a known dead criterion (overnight CET tipoffs are unreachable for the user) and was overtaken by the auto-betting engine. Kelly staking ([[P6 - Kelly Staking]]) is still unbuilt and ships last.

---

## Architecture

- [[System Overview]] — What OddsIQ is, current status, tech stack
- [[Data Flow]] — End-to-end pipeline diagram from schedule fetch to bet resolution
- [[Polling Tiers]] — Tiered polling intervals by time-to-tipoff; far/warm/hot cadence (pregame-only — post-tipoff games are unpollable since 2026-08-27)
- [[Budget Governor]] — Odds API quota tracking, throttle model, reserve floor
- [[Architecture Decisions]] — SQLite, The Odds API (odds + schedule), Tipsport sole-book, sport axis, pipeline decomposition, data-driven cadence, live-first (reversed 2026-08-27), detection-only (superseded 2026-08-09 by the auto-betting engine)
- [[Failure Modes]] — Scraper, database, network, logic, and process failure tables

---

## Components

### Sports
- [[SportProfile]] — Registry of NBA/NHL/MLB/WNBA/NFL profiles; per-sport competition IDs, team sets, moneyline market rules, lifecycle backstop hours, far-shadow detection flag

### Scrapers
- [[BaseScraper]] — Abstract contract every odds scraper implements; shared raw-response + retention helpers
- [[PinnacleScraper]] — Sharp reference odds + schedule/scores via The Odds API (20k req/month)
- [[TipsportScraper]] — Reverse-engineered Tipsport REST API with Cloudflare bypass (sole soft book)
- [[CloudflareSession]] — Playwright headless Chromium with anti-detection patches
- [[Team Name Normalization]] — Three-tier matching: canonical, bookmaker mapping, fuzzy fallback

### Pipeline
- [[Pipeline Services]] — Orchestrator + focused services: ScheduleService, PollCoordinator, EdgeService, SettlementService, BudgetGovernor, CoverageMonitor, SessionStats, tiers

### Engine
- [[Vig Removal]] — Multiplicative method for fair odds estimation from Pinnacle lines
- [[Edge Detection]] — Compares Tipsport odds against fair odds, 3% threshold, warm + hot tiers (≤2h to tipoff)
- [[CLV Calculation]] — Closing Line Value tracking, bet resolution, selection swap logic

### Data
- [[SQLite Store]] — WAL-mode database operations, batch inserts, closing line marks
- [[Data Model]] — Schema for games, odds_snapshots, value_bets tables with indexes
- [[Game Lifecycle]] — scheduled → live → final state transitions
- [[OddsSnapshot Lifecycle]] — From API response through game matching to closing mark
- [[ValueBet Lifecycle]] — From edge detection through CLV enrichment to result resolution

### Orchestration & Notifications
- [[Scheduler]] — The central orchestrator: polling, edge detection, CLV, game transitions
- [[Telegram Notifications]] — Best-effort sends (5s timeout, never raises), **bettor-facing only** since the 2026-08-10 rebrand: bet placed, bet rejected/failed, won/lost (real placements only), auto-betting stopped, Tipsport login lost/restored, and the not-logged-in skip alert. Detections, CLV, and session summaries are log-only
- [[CLI Entry Point]] — Startup validation, signal handlers, graceful shutdown
- [[Dashboard]] — P3: local FastAPI + htmx web UI on `oddsiq.db` and `logs/oddsiq.log`

### Resilience
- [[Backup]] — Automated WAL-safe database backup on every pipeline startup and shutdown, integrity-checked, retention-pruned
- [[Migrations]] — Forward-only schema-migration runner; applies pending `NNN_*.py` files on every CLI startup inside per-migration transactions

---

## Operations

- [[Runbook]] — Starting a game night session, what to watch, healthy vs warning indicators
- [[Health Monitoring]] — Per-night checklist, weekly trends, degradation signals, and the Coverage-Gap Alerts subsystem (WARNING log when a scraper goes silent during an active polling window)
- [[Database Inspection]] — SQLite queries for games, value bets, odds history, CLV distribution
- [[Backup and Retention]] — Manual/automated backup, verification, storage projections, retention policy
- [[Troubleshooting]] — Diagnosing no value bets, duplicate alerts, CLV failures, common symptoms

---

## Decisions

- [[Markets]] — Pinnacle scope reduction to moneyline by default
- [[Live-First]] — The retired live-first thesis: original rationale, why the live-edge subsystem was removed 2026-08-27, and the lifecycle facts that survive. (The companion Live Tier Scout decision note was deleted with the subsystem; the pre-release history retains it.)
- [[Multi-Sport Expansion]] — Tipsport coverage map + deferred sports (NFL/NCAAF/NCAAB) with competition IDs

---

## Production

- [[First Game Night]] — 2026-04-10: 15 games, 5 value bets, four issues discovered and fixed
- [[Config Constants]] — Every production-tuned constant with its rationale
- [[Production Lessons]] — Timeouts, throttling, proactive sessions, early edges are noise, CLV matters

---

## Validation

- [[Validation Guide]] — Game night procedures, health checks, when to stop collecting
- [[CLV Analysis Framework]] — 7-step analysis: raw data → CLV verdict → edge accuracy → go/no-go
- [[Useful Queries]] — SQL queries for CLV analysis, health monitoring, edge tracking

---

## Known Issues

### Open
- [[Fixed - Basketball MatchView Data Contamination]] — ALL NBA `value_bets` + `live_edge_candidates` from the start of collection (2026-04-14) until the fix deploy carried inflated edge/CLV (regulation 3-way legs scraped as moneyline). WNBA unaffected (shipped fixed). Fully resolved: code fixed (`a1669a4`), reporting filter + `mark-excluded` shipped, VM stamping executed 2026-07-10 (56 bets + 96 live candidates).
- [[Open - Stake Field Always None]] — `value_bets.stake` is always NULL until P6 Kelly staking

### Fixed
- [[Fixed - Moneyline Tie Not Handled]] — draw resolution shipped in the 2026-06-02 refactor (`resolve_bet_result()` returns `"draw"` on equal final scores)

(The P1 known-issue post-mortems have been archived. See git history for details on `_home_team`/`_away_team` dataclass fields, edge-persistence dedup, CLV selection swap, duplicate value bets, zero-match log, and multi-date polling.)

### Mitigated
- [[Mitigated - Cloudflare Session Lifecycle]] — Proactive 20-min refresh + 60s Playwright timeout
- [[Mitigated - State Persistence]] — _handled_live restored from DB, _alerted_edges eliminated
- [[Mitigated - Pinnacle Budget In-Memory]] — Startup probe + `meta` table fallback replace the misleading 500 default

---

## Roadmap

- [[Phase Overview]] — Component status table, what's built vs planned
- [[P0 - Telegram Alerts]] — Complete, then narrowed by the 2026-08-10 rebrand: the original value-bet / CLV / session-summary alerts were deleted; Telegram now carries real-money bet events only
- [[P1 - State Persistence and Bug Fixes]] — Mostly complete: 9/10 criteria met
- [[P3 - Dashboard and Reporting]] — Shipped 2026-05-28: local FastAPI dashboard at `oddsiq dashboard`, five pages (`/upcoming`, `/live`, `/betting`, `/analysis`, `/settings`) plus the per-game `/game/{id}` drill-down. See [[Dashboard]] for current state.
- [[P4 - Resilience and Deployment]] — In progress: Docker container + Odds-API backoff SHIPPED 2026-06-09; the Telegram heartbeat shipped then was removed 2026-08-10 (superseded by the `HEARTBEAT_PING_URL` dead-man ping); recovery / log-rotation monitoring / season-boundary handling still deferred
- [[P5 - Auto-Betting]] — **Superseded.** Auto-betting actually shipped 2026-08-09 as the "P1" engine (decision ledger, qualification chain, circuit breakers, WebSocket bridge, Chrome-extension actuator) and runs live; this page documents the older plan and its dead 200-manual-bets gate
- [[P6 - Kelly Staking]] — Planned (last): fractional Kelly, bankroll tracker, stake in alerts

---

## Legal

- [[Legal Risk Analysis]] — Czech legal context, Tipsport ToS, risk assessment, conclusion: low risk

---

## Testing

- [[Test Suite Overview]] — canonical test count and coverage breakdown; integration test gap documented

---
