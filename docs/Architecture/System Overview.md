---
tags:
  - component/system
  - status/current
  - phase/evaluation
  - layer/orchestration
  - type/overview
source-files:
  - oddsiq/pipeline/scheduler.py
  - oddsiq/cli.py
  - oddsiq/sports/profiles.py
related:
  - "[[Data Flow]]"
  - "[[Polling Tiers]]"
  - "[[Budget Governor]]"
  - "[[SportProfile]]"
  - "[[Pipeline Services]]"
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[Edge Detection]]"
  - "[[CLV Calculation]]"
  - "[[Architecture Decisions]]"
  - "[[Failure Modes]]"
  - "[[Live-First]]"
---

# System Overview

OddsIQ is an async Python pipeline that detects +EV betting opportunities by comparing Tipsport odds against sharp consensus lines from Pinnacle. The primary fan-out axis is **sport**: five sports are profiled (NBA, NHL, MLB, WNBA, NFL) and the enabled ones — MLB + NFL since 2026-08-25 — run concurrently, using a [[SportProfile]] registry to parameterize every sport-specific constant. Pinnacle (via The Odds API) is both the sharp reference and the schedule/scores source; balldontlie is retired.

## Current Status

**Evaluation mode — no longer detection-only.** One soft book ([[TipsportScraper|Tipsport]]) compared against [[PinnacleScraper|Pinnacle]] across five profiled sports (NBA / NHL / MLB / WNBA / NFL — the fourth added 2026-06-11, the fifth 2026-08-25); MLB + NFL are enabled, NBA/NHL are dark until their October preseasons and WNBA indefinitely. The system flags pregame +EV moneyline edges and **since 2026-08-09 hands the clean ones to the auto-betting engine**, which places real minimum-stake bets for the sports listed in the meta `betting_sports` key — see [[Architecture Decisions]] for rationale. **The live-edge subsystem was fully removed 2026-08-27**: no post-tipoff polling of any kind — the live scout, markout classification, and the `live_edge_candidates` table are gone (dataset archived at `~/dev/oddsiq-live-edge-archive/`, code at git tag `archive/live-edges-2026-08-27`; see [[Live-First]]). P4 is partially shipped (Docker + Odds-API backoff shipped 2026-06-09; the Telegram heartbeat shipped then too and was removed 2026-08-10, superseded by the `HEARTBEAT_PING_URL` dead-man ping; recovery / log-rotation monitoring / season-boundary handling still deferred); P5-P6 as written are unbuilt; the pipeline runs 24/7 to accumulate passive CLV evidence alongside realized P&L.

## What It Does

1. **Fetches the game slate per sport** — [[Pipeline Services#ScheduleService|ScheduleService]] calls The Odds API `/scores?daysFrom=3` for each enabled sport (MLB + NFL today), returning upcoming + live + recently-final games with `commence_time` in a single uniform response shape.
2. **Polls odds on a tiered schedule** — [[Polling Tiers]] define how frequently odds are fetched based on time-to-tipoff (skip >6h; far 2–6h 1800s, no Pinnacle except NFL's far-tier shadow detection; warm 1–2h 300s; hot <60m 60s). Polling is pregame-only: post-tipoff games are unpollable (the live tier was removed 2026-08-27). The [[Budget Governor]] throttles Pinnacle cadence when the monthly quota is at risk.
3. **Compares odds** — [[Edge Detection]] removes vig from Pinnacle lines via multiplicative vig removal, then calculates edge as `(tipsport_odds / fair_odds) - 1.0`.
4. **Flags pregame edges ≥ 3%** — Logs a `value_bet:` line when edge exceeds `EDGE_THRESHOLD` (default 3%), stamps the value bet with its polling tier (warm or hot; `far` for NFL shadow rows), and hands the clean detections to the betting engine. Detections themselves are never messaged — [[Telegram Notifications|Telegram]] is bettor-facing only since the 2026-08-10 rebrand.
5. **Tracks closing line value** — At tipoff, the CLV pipeline marks closing snapshots, removes vig from the Pinnacle closing pair, and calculates `clv = (entry_odds / closing_fair) - 1.0`. Closing marks are clock-driven at the tipoff transition, so CLV is unaffected by the absence of post-tipoff polling.
6. **Resolves bets** — After game final (detected via the Odds API `/scores` completion check), `resolve_bet_result()` marks each value bet win/loss/push/draw based on final scores.
7. **Monitors coverage** — [[Pipeline Services#CoverageMonitor|CoverageMonitor]] emits a `Coverage gap:` WARNING log when a scraper has not matched a warm/hot-tier game for >10 min (log + dashboard feed only; no phone alert).

(The former step "runs the live scout" — post-tipoff Tipsport-vs-Pinnacle comparison into `live_edge_candidates` with markout confirmation — was removed 2026-08-27 with the live-edge subsystem.)

## Architecture at a Glance

The full pipeline flows through these components (see [[Data Flow]] for the complete diagram):

- **Sport registry:** [[SportProfile]] (`oddsiq/sports/profiles.py`) — NBA / NHL / MLB / WNBA / NFL profiles, each bundling competition IDs, team sets, the game-lifecycle backstop hours, and the 2-way moneyline market rule.
- **Data sources:** The Odds API `/scores` (schedule + game status/scores) and The Odds API `/odds` (Pinnacle sharp odds via [[PinnacleScraper]]), plus Tipsport REST via [[TipsportScraper]] (the sole soft book).
- **Pipeline:** `oddsiq/pipeline/` — slim orchestrator (`Scheduler`) + focused services: [[Pipeline Services|ScheduleService, PollCoordinator, EdgeService, SettlementService, BettingService, BettingBridge, BudgetGovernor, CoverageMonitor, SessionStats]].
- **Storage:** [[SQLite Store|SQLite]] WAL — six tables: `games`, `odds_snapshots`, `value_bets`, `placed_bets`, `meta`, `schema_version` (the `live_edge_candidates` table was dropped by migration 011, 2026-08-27). The data tables (`games`, `odds_snapshots`, `value_bets`) carry a `sport` column (migration 005) for per-sport queries and dashboard tabs; `sport` is NOT in any unique/dedup key (game_id scopes it).
- **Engine:** [[Vig Removal]] (`oddsiq/engine/vig.py`), [[Edge Detection]] (`oddsiq/engine/edge.py`), [[CLV Calculation]] (`oddsiq/engine/clv.py`).
- **Notifications:** [[Telegram Notifications]] — best-effort sends (5s timeout, never raises), **bettor-facing only** since the 2026-08-10 rebrand: bet placed, won/lost, rejected/failed, auto-betting stopped, Tipsport login lost/restored, and the rate-limited not-logged-in skip. Detections, CLV, coverage gaps, budget and session summaries are log-only.
- **Dashboard:** [[Dashboard]] (P3) — separate read-only FastAPI process over `oddsiq.db` and `logs/oddsiq.log`.
- **Entry point:** [[CLI Entry Point]] (`oddsiq/cli.py`) — validates config, runs migrations, starts scheduler with signal handlers.

## Tech Stack

- **Language:** Python 3.12+, fully async (`asyncio`)
- **HTTP:** `httpx` (Pinnacle, The Odds API schedule), `curl_cffi` (Tipsport fast path), Playwright (Tipsport fallback via [[CloudflareSession]])
- **Database:** [[SQLite Store|SQLite]] via `aiosqlite` with WAL mode
- **Team matching:** [[Team Name Normalization]] via `rapidfuzz` (3-tier: canonical, bookmaker mapping, fuzzy fallback) — scoped per sport via `SportProfile`
- **Notifications:** Telegram Bot API
- **Testing:** See [[Test Suite Overview]]

## Phase Roadmap (Summary)

| Phase | Status | Description |
| --- | --- | --- |
| P0 | Complete | [[Telegram Notifications]] — the original alert set (value bets, CLV, session summary) was retired by the 2026-08-10 bettor-facing rebrand |
| P1 | Complete | State persistence, bug fixes, polling tuning |
| P2 | Archived | Multi-book era (Betano/Allwyn/Fortuna) — deleted; recoverable via git tag `archive/multibook-2026-06-02` |
| P3 | Shipped 2026-05-28 | [[Dashboard]] — local FastAPI (`oddsiq dashboard`), five pages (`/upcoming`, `/live`, `/betting`, `/analysis`, `/settings`; `/bets` 307-redirects to `/betting`) plus the per-game `/game/{id}` drill-down |
| Multi-sport refactor | Shipped 2026-06-02 | Sport axis (NBA/NHL/MLB), Tipsport-only, Odds API schedule, pipeline decomposition, budget governor, live promotion |
| WNBA fourth sport | Shipped 2026-06-11 | Profile-only add (competition 10, `basketbal-zeny`, Ž-suffix aliases) + `tipsport_gender_slug` field |
| Auto-betting engine | Shipped 2026-08-09 | Its own three-phase track: `placed_bets` decision ledger + `BettingService`, the Chrome actuator + MV3 extension, the `/betting` dashboard tab |
| NFL fifth sport | Shipped 2026-08-25, fully activated 2026-09-06 | Profile-only add (competition 5748, `superSportId=2`) + the meta `betting_sports` gate, far-tier shadow detection, two-phase edge→betting dispatch |
| P4 | In progress | Resilience and deployment (Docker + Odds-API backoff shipped 2026-06-09; the Telegram heartbeat shipped then and was removed 2026-08-10 for the `HEARTBEAT_PING_URL` dead-man ping; recovery / log-rotation monitoring / season boundaries deferred) |
| P5 | Superseded | [[P5 - Auto-Betting|Auto-Betting]] was commissioned early and shipped on the track above (2026-08-09), not against this page's CLV gate |
| P6 | Planned | [[P6 - Kelly Staking|Kelly Staking]] (last) |

See [[Phase Overview]] for full criteria per phase.
