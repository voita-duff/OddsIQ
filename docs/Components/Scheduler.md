---
tags:
  - component/orchestration
  - status/complete
  - phase/p0
  - layer/orchestration
  - type/component
source-files:
  - oddsiq/pipeline/scheduler.py
related:
  - "[[Pipeline Services]]"
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[Team Name Normalization]]"
  - "[[CloudflareSession]]"
  - "[[Edge Detection]]"
  - "[[CLV Calculation]]"
  - "[[Vig Removal]]"
  - "[[SQLite Store]]"
  - "[[Telegram Notifications]]"
  - "[[Dashboard]]"
  - "[[CLI Entry Point]]"
  - "[[Polling Tiers]]"
  - "[[SportProfile]]"
  - "[[Budget Governor]]"
  - "[[Game Lifecycle]]"
  - "[[OddsSnapshot Lifecycle]]"
  - "[[ValueBet Lifecycle]]"
  - "[[Data Model]]"
  - "[[Config Constants]]"
---

# Scheduler

The slim async orchestrator that drives the OddsIQ pipeline. Lives at `oddsiq/pipeline/scheduler.py`. The former monolithic `oddsiq/scheduler.py` was decomposed in the 2026-06-02 multi-sport refactor (Architecture Decision D5) into `Scheduler` plus focused services — see [[Pipeline Services]] for the service descriptions.

## What the orchestrator owns

`Scheduler` has one responsibility: **run the main loop and delegate**. It owns no domain logic beyond heartbeat emission and the browser preflight check.

On **startup** it:
1. Loads previously-handled live game IDs from the DB (so a restart doesn't re-fire CLV on games already processed)
2. Runs `SettlementService.recover_historical_bets()` for any stranded final-game bets
3. Checks `OddsApiScheduleSource.health_check()`
4. Calls `_check_browser_preflight()` — one-shot Chromium availability check
5. Probes `BudgetGovernor` for the current Pinnacle quota
6. Runs an initial schedule refresh

**Main loop** (30 s tick, wakes immediately on `stop()`):
1. Emits a `heartbeat:` INFO line on `SCHEDULER_HEARTBEAT_SECONDS` cadence (default 60 s) — the [[Dashboard]] reads this to derive pipeline liveness even during idle periods
2. Calls `ScheduleService.maybe_refresh(now)` — throttled schedule update
3. Calls `ScheduleService.process_live_transitions(now)` — once-per-game tipoff handler
4. Computes **pollable games** (pregame only — post-tipoff games are unpollable since the live tier's removal, 2026-08-27) and the governor-throttled minimum interval across active tiers
5. When the minimum interval has elapsed and there are pollable games: iterates `enabled_profiles()` and calls `PollCoordinator.poll(profile, sport_pollable, sport_slate, now)` for each sport with at least one pollable game
6. Persists the budget snapshot
7. Calls `ScheduleService.check_completed_games()`
8. Calls `CoverageMonitor.check_gaps()`

On **shutdown**: forces a final budget persist, calls `print_summary()` (console/log only), closes the Telegram client, closes all scraper sessions.

## Services (see `[[Pipeline Services]]`)

| Service | Responsibility |
|---------|---------------|
| `ScheduleService` | Multi-sport game discovery via `OddsApiScheduleSource`; tipoff + completion transitions |
| `PollCoordinator` | Tier decisions; concurrent scraper fetches; match/distribute; health log |
| `EdgeService` | Pregame +EV detection → `value_bets` (log-only `value_bet:`); collects clean detections and dispatches them to `BettingService` |
| `SettlementService` | Closing snapshots, CLV calculation, result resolution, startup recovery |
| `BudgetGovernor` | Pinnacle quota projection; governor-throttled tier intervals; meta persistence |
| `CoverageMonitor` | Stale-scraper gap WARNINGs (log-only) for warm/hot-tier games |

For detailed logic on any service, see [[Pipeline Services]].

## Per-sport poll loop

The orchestrator iterates `enabled_profiles()` and scopes each `PollCoordinator.poll()` call to the games belonging to that sport. This makes the pipeline sport-agnostic at the orchestrator level: adding a new sport requires a [[SportProfile]] entry and no changes here.

```python
for profile in enabled_profiles():
    sport_pollable = [g for g in pollable if g.sport == profile.key]
    if sport_pollable:
        sport_slate = [g for g in self._schedule.games if g.sport == profile.key]
        await self._poll_coordinator.poll(profile, sport_pollable, sport_slate, now_poll)
```

## Browser preflight

`_check_browser_preflight()` runs once at boot when Tipsport is in the scraper list. It calls `chromium_available()` from `oddsiq/scrapers/preflight.py`:

- On success: logs `"Browser preflight OK — Playwright Chromium launches"`
- On failure: logs a WARNING with the `playwright install chromium` remediation command, then continues without aborting — REST books (Pinnacle) are unaffected

This probe closed the silent gap that caused the 2026-05-30 ~18h Tipsport outage when a Playwright package bump left the browser binary missing.

## Heartbeat

`_maybe_emit_heartbeat(now)` emits `heartbeat: state=<s> games_tracked=<n> seconds_since_last_poll=<n> budget_throttle=<n>` on `SCHEDULER_HEARTBEAT_SECONDS` cadence. The [[Dashboard]] reads the most recent matching line to derive pipeline liveness; without periodic emission, an idle scheduler would falsely appear stale after 2 minutes.

State values: `polling`, `between-game-windows`, `idle-no-games`. Only pregame games count as "in a polling tier" — a slate whose games have all tipped off reads `between-game-windows` (in-progress games are lifecycle-tracked, not polled, since the live tier's removal 2026-08-27).

## Session summary (`print_summary`)

Prints to console on shutdown — **no phone message** (2026-08-10 rebrand): games tracked, value bets flagged, average edge, CLV, per-book breakdowns, Odds API budget, and per-scraper poll/error/latency stats.

## Telegram

The orchestrator sends exactly one pair of messages: `_on_login_change(event)` — the [[Pipeline Services|BettingBridge]] `on_login_change` hook — fires `format_login_lost()` on `"lost"` and `format_login_restored()` on `"restored"`. Deliberately not gated on `betting_armed`: the operator wants to know a Tipsport session died *before* arming, and the bridge only exists when `BETTING_ENABLED=1`. Every other scheduler-side event (detections, CLV, results, coverage gaps, budget, session summary) is log-only. See [[Telegram Notifications]] for the complete message list.

## Exception handling

```python
except Exception:
    logger.exception("Error in scheduler main loop")
    # 10 s recovery sleep, then continues
```

The main loop logs all exceptions and continues. Any single scraper failure, DB error, or network hiccup does not kill the pipeline.

## Data flow (summary)

```
OddsApiScheduleSource → ScheduleService → games table
                                │
                         PollCoordinator (per sport)
                         PinnacleScraper ─┐ asyncio.gather
                         TipsportScraper ─┘
                                │ ordered after gather
                     EdgeService (warm/hot) → value_bets → log value_bet:
                                │                        └→ PendingBet → BettingService → placed_bets
                                │
                         (at tipoff)
                     SettlementService → mark_closing_snapshots
                                       → calculate_clv → log clv:
                                │
                          (at game final)
                     SettlementService → resolve_bets → update value_bets.result → log result:
                                                      └→ Telegram won/lost (only if a placed_bets row settled)
```

## See Also

- [[Pipeline Services]] — the service classes that do all the domain work
- [[CLI Entry Point]] — starts the scheduler and handles shutdown signals
- [[Polling Tiers]] — tier boundary values and rationale
- [[Game Lifecycle]] — game status state machine
- [[OddsSnapshot Lifecycle]] — snapshot flow from API to closing line
- [[ValueBet Lifecycle]] — edge → tracked bet → CLV + result
- [[Budget Governor]] — Pinnacle quota projection and interval throttling
- [[Data Model]] — database schema
