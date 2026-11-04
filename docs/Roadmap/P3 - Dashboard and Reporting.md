---
tags:
  - type/roadmap
  - status/complete
  - phase/p3
---
# P3 -- Dashboard and Reporting

> **Shipped 2026-05-28.** Implementation lives at [[Dashboard]]. This page is the roadmap entry that scoped the work — read [[Dashboard]] for the current surface.

## Goal

Long-term performance tracking and analysis. Query the [[SQLite Store]] data to answer the key question: is the model producing real edge?

## Metrics

- **CLV over time** -- the key metric. Are you beating closing lines? A CLV distribution histogram shows whether detected edges hold up.
- **ROI by market type** -- moneyline vs spread vs total. Which markets produce the most real edge?
- **ROI by bookmaker** -- (multi-book era archived 2026-06-02; currently Tipsport-only)
- **Hit rate** -- win% and edge accuracy (detected edge vs actual outcome)
- **Edge distribution** -- histogram of detected edges. Are they clustered at the threshold or spread across a range?
- **API budget usage tracking** -- Odds API consumption over time, remaining budget projections

## Implementation Options (historical)

1. **CLI command** (`oddsiq report`) -- simplest, generates a text/table report in the terminal
2. **Web UI** -- Flask/FastAPI + htmx for a lightweight dashboard. More visual (charts), but more code to maintain.

The web UI route shipped; the CLI report option was not pursued. See [[Dashboard]] for the implementation.

## Success Criteria

- [x] CLV distribution and trend (Chart.js dual-line: nightly raw + 7-day rolling on `/analysis`)
- [x] Edge distribution histogram and per-bucket calibration (`/analysis` Edge distribution + Edge calibration)
- [x] Hit rate, avg odds, flat P/L, flat ROI (`/analysis` Results detail)
- [x] Per-book scoreboard (snapshots, games matched, days active, first/last seen, bets, avg edge, avg CLV)
- [x] Odds API budget visible (status strip and `/settings`)
- [x] Available as web UI (`oddsiq dashboard` subcommand → uvicorn on `127.0.0.1:8080` by default)

The original "ROI by market type" success criterion is partially met: `PINNACLE_MARKETS` defaults to moneyline only, so spread/total ROI breakdowns are not surfaced yet. They can be added if `PINNACLE_MARKETS` is widened (see `docs/Decisions/Markets.md`). The "available as CLI command" alternative was deferred — the web UI ships instead.

## Components Affected

- [[CLV Calculation]] -- source of CLV data
- [[Edge Detection]] -- source of edge data
- [[SQLite Store]] -- all queries run against the existing schema
- [[Useful Queries]] -- reusable SQL for common analyses

## Dependencies

- Benefits from data accumulated during the multi-book soak (P2) and ongoing manual placement, but doesn't strictly depend on them
- Minimum ~50 resolved bets to make any reporting meaningful

## Related

- [[Phase Overview]]
- [[First Game Night]] -- initial production data
