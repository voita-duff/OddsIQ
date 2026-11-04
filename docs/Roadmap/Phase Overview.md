---
tags:
  - type/roadmap
  - status/complete
  - phase/p0
---
# Phase Overview

OddsIQ development is organized into sequential phases. P0-P3 are complete — the pipeline detects value bets, persists state across restarts, compares Tipsport (the single soft book) against Pinnacle across the enabled sports (five are profiled — NBA/NHL/MLB/WNBA/NFL — with MLB + NFL enabled), and surfaces game-night state and decision-support analytics through the local multi-sport FastAPI dashboard. Telegram is bettor-facing only since the 2026-08-10 rebrand — see [[Telegram Notifications]]. **P4 is partially shipped** (Docker + Odds-API backoff shipped 2026-06-09; the Telegram heartbeat shipped then and was removed 2026-08-10 for the `HEARTBEAT_PING_URL` dead-man ping; recovery / log-rotation monitoring / season-boundary handling still deferred). **Auto-betting was commissioned out of order and shipped 2026-08-09** on its own three-phase track, so P5-P6 *as written on those pages* are what remains unbuilt. Phases are not opened beyond their current scope until the user explicitly does so.

## What's Built (Phase 1 -- Complete)

End-to-end async pipeline: Tipsport scraper fetches moneyline odds per sport on a tiered polling schedule, stores in SQLite, edge engine compares Tipsport odds against Pinnacle no-vig lines, flags value bets, captures closing lines at tip-off, resolves results.

| Component | Status | Notes |
| --- | --- | --- |
| Domain models (Game, OddsSnapshot, ValueBet) | Done | `sport` column added (migration 005) |
| SQLite store (async, WAL mode) | Done | |
| Pinnacle scraper (The Odds API) | Done | Budget tracking, raw response storage; also the schedule/scores source |
| Schedule source (The Odds API `/scores`) | Done | Replaced balldontlie.io in the 2026-06-02 refactor |
| Tipsport scraper | Done | Moneyline-only, per-sport profile; auto Cloudflare bypass (Playwright) |
| Team name normalization (NBA/NHL/MLB/WNBA/NFL) | Done | Per-sport canonical sets + Tipsport aliases; fuzzy fallback via rapidfuzz |
| Vig removal (multiplicative method) | Done | |
| Edge detection (exact line matching) | Done | |
| CLV calculation + bet resolution | Done | |
| Pipeline services (Scheduler, PollCoordinator, EdgeService, SettlementService, BudgetGovernor, CoverageMonitor) | Done | Decomposed in the 2026-06-02 refactor |
| Budget Governor | Done | Throttles Pinnacle cadence as monthly quota tightens |
| Live scout | Removed 2026-08-27 | The live-edge subsystem (post-tipoff polling, `live_edge_candidates`, markout) was deleted; dataset archived offline (see [[Live-First]]) |
| Bettor-facing Telegram | Done (rebranded 2026-08-10) | Bet placed / won-lost / rejected-failed / auto-disarm / login lost-restored, plus the rate-limited not-logged-in skip. The original value-bet, CLV and session-summary alerts were deleted with their formatters — those events are log-only ([[Telegram Notifications]]) |
| Auto-betting engine | Shipped 2026-08-09 | Commissioned ahead of the phase order; three phases (`placed_bets` ledger + `BettingService`, Chrome actuator + MV3 extension, `/betting` dashboard tab) |
| CLI entry point | Done | |
| Test suite | Done | See [[Test Suite Overview]] |

## Phase Index

| Phase | Title | Status |
| --- | --- | --- |
| P0 | [[P0 - Telegram Alerts]] | Complete (its alert set was retired by the 2026-08-10 bettor-facing rebrand) |
| P1 | [[P1 - State Persistence and Bug Fixes]] | Complete (not the auto-betting track's own "P1" — that numbering is separate) |
| P2 | More Czech Bookmakers | Archived (Betano/Allwyn/Fortuna shipped P2.1–P2.4 then archived 2026-06-02; multi-sport / live-first refactor superseded multi-book as the expansion axis; recoverable from the pre-release history) |
| P2.5 | Multi-Sport / Live-First Refactor | Complete (shipped 2026-06-02): NBA/NHL/MLB/WNBA, Tipsport-only, Odds API schedule, pipeline decomposition, Budget Governor, live scout promoted, multi-sport dashboard |
| P3 | [[P3 - Dashboard and Reporting]] | Complete (shipped 2026-05-28; multi-sport dashboard updated 2026-06-02; see [[Dashboard]]) |
| P4 | [[P4 - Resilience and Deployment]] | In progress (Docker + Odds-API backoff shipped; the Telegram heartbeat shipped then was removed 2026-08-10 for the `HEARTBEAT_PING_URL` dead-man ping; recovery / log-rotation monitoring / season boundaries deferred) |
| P5 | [[P5 - Auto-Betting]] | Superseded — auto-betting was commissioned early and shipped 2026-08-09 on its own three-phase track; this page's CLV/manual-bet gate never applied |
| P6 | [[P6 - Kelly Staking]] | Planned (last) |

Cross-phase open questions (Kelly fraction, bookmaker limits / ban timing, multi-sport normalization for college leagues) are deferred to phase-commit time and have no canonical doc yet — they're tracked inline in the relevant phase pages and `HANDBOOK.md`. (Live-tier full promotion is no longer an open question: the live-edge subsystem was removed 2026-08-27 — live-scout validation is not a track anymore.)

## Phase Dependencies

The phase order is value-driven, not "least-blocked first." Each phase compounds with the next:

- **P2 multi-book era -- archived 2026-06-02.** Betano/Allwyn/Fortuna shipped P2.1–P2.4 but the multi-book expansion axis was superseded by the multi-sport / live-first refactor. As of the archive, only Tipsport produced value bets (50, avg CLV +5.87%). The pre-release history preserves the multi-book codebase.
- **P2.5 multi-sport / live-first refactor -- shipped 2026-06-02.** NBA/NHL/MLB/WNBA now covered via the `SportProfile` registry. Tipsport is the single soft book. The Odds API is both the sharp reference and the schedule/scores source; balldontlie is retired. The pipeline is decomposed into focused services under `oddsiq/pipeline/`. Live scout promoted to first-class service at the time (the live-edge subsystem was later removed entirely, 2026-08-27).
- **P3 (Dashboard) -- shipped 2026-05-28, multi-sport update 2026-06-02; `/bets` + `/upcoming` tabs added 2026-06-08.** Local FastAPI process started via `oddsiq dashboard`. `/upcoming` is the read-only 24h multi-sport schedule with bet-window badges; `/live` is the game-night console (status strip, coverage matrix — pregame-only since 2026-08-27 — activity feed, raw logs); `/betting` (which replaced `/bets`) holds the auto-betting engine surface + recent value bets; `/analysis` provides decision-support analytics (big numbers, CLV trend, per-book scoreboard, edge distribution, calibration); `/settings` is read-only config. The dashboard reads `oddsiq.db` (WAL — concurrent readers OK) and tails `logs/oddsiq.log`. See [[Dashboard]] for the implementation surface.
- **P4 (Resilience) third** -- harden the foundation before automating real money flow. Unattended placement (P5) needs reliable recovery, log rotation, season-boundary handling, and graceful failure on every scraper.
- **P5 (Auto-Betting) fourth** -- the actual profit unlock. Places bets while the operator sleeps during overnight CET tipoffs. Capstone in design intent; ships before Kelly because sizing only matters once placement is reliable. **Superseded in practice:** the user commissioned auto-betting directly and it shipped 2026-08-09, ahead of P4's remaining items; real placements have been attempted since August.
- **P6 (Kelly Staking) last** -- sizing only matters when bets are reliably placed (P5) and the edge has been validated (multi-sport CLV, P3 surfaces CLV per market and sport). Sizing into a noisy or rarely-placed signal is wasted effort.

The dependency order above is the original design intent; it did not survive contact — auto-betting shipped 2026-08-09 with P4 still partial. As written, P5 strictly depends on P3 (visibility), P4 (hardening), and validated positive CLV. The old "~200 manually placed bets" gate is legacy design intent and is currently dead for this user because tipoffs happen overnight CET; current pacing decisions use passive CLV evidence from the 24/7 pipeline (`HANDBOOK.md` § "CLV gating"). Automating placement before the edge is validated just automates losses.

## Related

- [[System Overview]] -- architecture context
- [[Data Flow]] -- how data moves through the pipeline
- [[First Game Night]] -- production validation
