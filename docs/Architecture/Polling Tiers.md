---
tags:
  - component/scheduler
  - status/current
  - layer/orchestration
  - type/configuration
source-files:
  - oddsiq/pipeline/tiers.py
  - oddsiq/pipeline/budget_governor.py
  - oddsiq/config.py
related:
  - "[[Pipeline Services]]"
  - "[[Budget Governor]]"
  - "[[Edge Detection]]"
  - "[[Config Constants]]"
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[System Overview]]"
  - "[[Data Flow]]"
  - "[[SportProfile]]"
---

# Polling Tiers

OddsIQ polls odds on a tiered schedule that increases frequency as tipoff approaches. Cadence is data-driven: 89% of historical value bets land in the last hour, peaking at 15–30 minutes before tipoff. The [[Budget Governor]] throttles Pinnacle cadence when the monthly quota is at risk.

**All tiers are pregame.** Post-tipoff games are unpollable since 2026-08-27, when the live tier (and the live scout it fed) was removed — game lifecycle after tipoff is schedule/clock-driven: finals via `/scores`, closing snapshots marked at tipoff. See [[Live-First]].

## Tier Table

`POLL_TIER_*` constants (`oddsiq/config.py`) define the base cadence at each tier. The [[Budget Governor]] may widen warm intervals under pressure (hot is protected) — see [[Budget Governor#Throttle Model]].

| Tier | Time to Tipoff | Cadence (base) | Pinnacle? | Config Constant | Rationale |
| --- | --- | --- | --- | --- | --- |
| — | > 6 h | skip | — | `None` returned | Too far out; no useful data |
| far | 2–6 h | 30 min | **No** (NFL shadow exception, see below) | `POLL_TIER_FAR = 1800` | Lines too soft; 1 bet ever in this window |
| warm | 1–2 h | 5 min | **Yes** | `POLL_TIER_WARM = 300` | Rare early edge; Pinnacle baseline needed for detection |
| hot | < 60 m | 60 s | **Yes** | `POLL_TIER_HOT = 60` | 89% of value bets; tightened 90→60 on 2026-08-27 (funded by the live-tier removal) |
| — | after tipoff | stop | — | `None` returned | Post-tipoff games are unpollable (live tier removed 2026-08-27) |

**Previous cadences (retired):** The original tier table used far (>2 h, 1800s) / medium (30 m–2 h, 600s) / hot (<30 m, 120s) / live (240s); the 2026-06-02 refactor replaced it with far/warm/hot/live (live at 60s, later 240s). The live tier itself was removed 2026-08-27 — code referencing `POLL_TIER_MEDIUM` or `POLL_TIER_LIVE` is stale.

## Tier Logic (`oddsiq/pipeline/tiers.py`)

```python
def tier_for(seconds_to_tipoff: float) -> str | None:
    """Pregame tier, or None once the game has tipped off (unpollable)."""
    if seconds_to_tipoff <= 0:   return None
    if seconds_to_tipoff > 7200: return "far"
    if seconds_to_tipoff > 3600: return "warm"
    return "hot"

def get_poll_interval(seconds_to_tipoff: float) -> int | None:
    if seconds_to_tipoff > 21600 or seconds_to_tipoff <= 0:
        return None                                # > 6h or post-tipoff: skip
    if seconds_to_tipoff > 7200:     return POLL_TIER_FAR
    if seconds_to_tipoff > 3600:     return POLL_TIER_WARM
    return POLL_TIER_HOT
```

`get_poll_interval()` takes only `seconds_to_tipoff` (the old second argument, `live_backstop_hours`, left with the live tier) and returns `None` for any post-tipoff time.

## Pinnacle Budget Gating

Pinnacle (`GET /sports/{key}/odds`) is called once per sport per cycle and returns all that sport's games in one response — budget scales with **poll count, not game count**.

- **Far tier:** Pinnacle not called. Tipsport still polled (no budget cost). Exception: `shadow_far_detection` sports (NFL) DO call Pinnacle in the far tier — see "Edge Detection Window" below.
- **Warm and hot tiers:** Pinnacle called every cycle; detection fires in both.

The [[Budget Governor]] sits in front of every Pinnacle fetch and can widen intervals when the projected monthly burn exceeds the quota — warm widens first, hot is protected (the throttle table's live column was removed with the live tier, 2026-08-27). A hard reserve floor (`BUDGET_RESERVE_FLOOR`, default 1500) drops all tiers to conservative regardless of the projection. See [[Budget Governor]] for the throttle table.

## Edge Detection Window

`EdgeService.detect_pregame_edges()` runs for games in the **warm and hot tiers** (≤ 2 h to tipoff). The far tier is excluded because lines haven't been shaped by sharp money yet — the first game night (2026-04-10) produced a −33% CLV on a far-tier edge detection, confirming this decision. See [[First Game Night|Production Observations]].

Exception (2026-09-06): profiles with `shadow_far_detection=True` (NFL) fetch Pinnacle and detect in the far tier as SHADOW rows (`tier='far'`, `tiers.SHADOW_TIER`) — recorded with CLV, never bet (`tier_not_bettable`), excluded from every performance aggregate. Evidence for widening the betting window, not a betting tier.

## Lifecycle Backstop (`live_backstop_hours`)

Each sport has its own `live_backstop_hours` in its [[SportProfile]]. Since the live tier's removal (2026-08-27) it is **purely a game-lifecycle window**, not a polling bound: it scopes zombie-game detection (`oddsiq delete-zombie-games` treats a game still `live` past tipoff + backstop + grace as permanently stuck) and the `/scores` refresh windows.

| Sport | `live_backstop_hours` | Rationale |
| --- | --- | --- |
| NBA | 3.0 | Typical game length + OT headroom |
| NHL | 2.5 | Shorter games; OT covered |
| MLB | 4.0 | Extra innings; games can run long |
| WNBA | 2.5 | 4×10 min format runs ~2 h; NHL-style buffer |
| NFL | 3.5 | Games run ~3 h 10 m; OT buffer |

Post-tipoff games return `None` from `get_poll_interval()` immediately — the backstop no longer extends polling past tipoff.

## Scheduler Loop Mechanics

The `Scheduler` main loop sleeps 30 seconds between iterations. Each iteration:

1. Computes pollable games + the shortest governor-throttled interval among them (`_compute_pollable`).
2. If enough time has elapsed since the last poll, calls `PollCoordinator.poll()` per enabled sport for that sport's pollable games.
3. After polling, persists the budget snapshot.
4. Checks for completed games (`ScheduleService.check_completed_games`).
5. Runs `CoverageMonitor.check_gaps()`.

A poll's effective cadence can be slightly longer than the tier constant due to the 30s sleep granularity.

## Config Constants

All constants are in `config.py` and can be overridden via environment variables:

| Constant | Value | Why |
| --- | --- | --- |
| `POLL_TIER_FAR` | `1800` (30 min) | Far-tier Tipsport-only polling |
| `POLL_TIER_WARM` | `300` (5 min) | Warm-tier detection cadence |
| `POLL_TIER_HOT` | `60` (60 s) | Hot-tier closing-line capture |

(`POLL_TIER_LIVE`, `LIVE_EDGE_THRESHOLD`, `LIVE_SCOUT_MAX_HOURS`, and the `LIVE_MARKOUT_*` knobs were deleted 2026-08-27 with the live-edge subsystem.)

See [[Config Constants]] for the full table of production-tuned constants.
