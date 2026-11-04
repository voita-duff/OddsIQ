---
tags:
  - component/pipeline
  - status/current
  - layer/orchestration
  - type/component
source-files:
  - oddsiq/pipeline/budget_governor.py
  - oddsiq/config.py
related:
  - "[[Polling Tiers]]"
  - "[[PinnacleScraper]]"
  - "[[Pipeline Services]]"
  - "[[System Overview]]"
  - "[[Config Constants]]"
---

# Budget Governor

`BudgetGovernor` (`oddsiq/pipeline/budget_governor.py`) gates every Pinnacle fetch and throttles cadence warm → hot → live when the projected monthly quota burn exceeds the budget. It reads authoritative quota values from real response headers (via `PinnacleScraper`) and persists them to the `meta` table so the dashboard and future probes can use them.

## Responsibility

- **Probe** the Odds API quota at startup (via `PinnacleScraper.probe_budget()`; falls back to last `meta` snapshot on failure).
- **Throttle** the poll interval returned to `Scheduler._compute_pollable()` based on the current throttle level.
- **Persist** used/remaining to `meta` (keys: `odds_api_budget_used`, `odds_api_budget_remaining`) every 5 min and on forced shutdown.
- **Snapshot** used/remaining/total for display in session summaries and the dashboard `/settings` page.

## Throttle Model

Four discrete levels. The reserve floor check runs first and always wins.

| Level | Trigger | warm interval | hot interval | live interval |
|---|---|---|---|---|
| 0 None | Projected ≤ total × slack (or unprobed / <1 day into period) | 300s (base) | 90s (base) | 60s (base) |
| 1 Warm | slack < ratio ≤ 1.25 | 600s | 90s (base) | 60s (base) |
| 2 Warm+Hot | 1.25 < ratio ≤ 1.6 | 900s | 180s | 60s (base) |
| 3 Conservative | ratio > 1.6 OR remaining ≤ `BUDGET_RESERVE_FLOOR` | 1800s | 300s | 120s |

**Live is protected longest:** level 3 is the only level that touches it (120s vs 60s base). Warm is throttled first, then hot, then live — because 89% of pregame value bets land in the hot window and live is the current primary research path.

The throttle interval table lives in `_THROTTLE_INTERVALS` in `budget_governor.py` and references `config.POLL_TIER_*` constants at level 0 so it stays in sync with cadence changes.

## Burn Projection

```
ratio = (budget_used / frac_elapsed) / budget_total
```

where `frac_elapsed` is the fraction of the current quota period elapsed (honors `ODDS_API_QUOTA_RESET_DAY` for non-calendar resets). The projection only activates after at least 1 day into the period; before that, level 0 is returned regardless.

The reserve floor (`BUDGET_RESERVE_FLOOR`, default 1500) is checked against the **authoritative `remaining`** value from quota response headers, not the projection. It is the primary guard against a hard stop mid-month.

## Non-Calendar Reset Days

The Odds API exposes no reset-date header. `ODDS_API_QUOTA_RESET_DAY` (default 1 = calendar-month reset) tells the governor when the quota period starts. If the quota resets on a billing-anniversary date (e.g., the 2nd), set `ODDS_API_QUOTA_RESET_DAY=2`. A wrong value mis-times the soft early-warning throttle (levels 1–2); the reserve floor uses the authoritative `remaining` and is unaffected.

## Re-Probes: Rollover and Exhaustion Recovery

Pinnacle latches dead once `remaining` hits 0 (`should_skip_request()` blocks every request, so live quota headers never refresh it), and the startup probe runs only once. `maybe_reprobe()` (called every scheduler loop iteration) clears the latch two ways, both via the quota-free `/v4/sports` probe:

- **Period rollover** — on a detected `ODDS_API_QUOTA_RESET_DAY` boundary, re-probe and refresh the budget. The tracked period only advances on a successful probe, so a failed boundary probe retries next iteration.
- **Exhaustion recovery** — while the budget reads exhausted, re-probe at most hourly (`_EXHAUSTED_REPROBE_SECONDS`) regardless of boundaries, until tokens reappear. This is the safety net for a mis-set reset day (2026-07 incident: the boundary probe fired a day early, read the pre-reset zeros, and the real refill had no boundary left to trigger on — Pinnacle stayed dead for days), for resets that land later in the day, and for mid-month top-ups, which are picked up within the hour without a restart.

## Persistence

`BudgetGovernor.persist()` writes `odds_api_budget_used` and `odds_api_budget_remaining` to the `meta` table at most once per 5 min, and always on shutdown (`force=True`). On probe failure at startup, the last persisted snapshot restores the in-memory budget state — better than displaying the misleading 20000-default.

## Config

| Key | Default | Env var | Role |
|---|---|---|---|
| `BUDGET_RESERVE_FLOOR` | `1500` | `BUDGET_RESERVE_FLOOR` | Hard floor; triggers level 3 |
| `BUDGET_THROTTLE_SLACK` | `1.10` | `BUDGET_THROTTLE_SLACK` | Multiplier on total before throttle kicks in |
| `ODDS_API_QUOTA_RESET_DAY` | `1` | `ODDS_API_QUOTA_RESET_DAY` | Day-of-month the quota resets |
| `ODDS_API_MONTHLY_BUDGET` | `20000` | — | Total quota (hardcoded; reflects the upgraded tier) |

See [[Config Constants]] for the full table.
