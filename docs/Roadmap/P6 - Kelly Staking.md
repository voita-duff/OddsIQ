---
tags:
  - type/roadmap
  - status/planned
  - phase/p6
---
# P6 -- Kelly Staking

## Goal

Suggest optimal bet sizing for each value bet using fractional Kelly criterion. The `stake` field exists on `ValueBet` but nothing populates it -- see [[Open - Stake Field Always None]].

## Formula

```
stake = (edge / (odds - 1)) * fraction * bankroll
```

Where:
- `edge` = detected edge as a decimal (e.g., 0.05 for 5%)
- `odds` = decimal odds at the Czech bookmaker
- `fraction` = Kelly fraction (default: quarter-Kelly = 0.25)
- `bankroll` = current bankroll balance

## Design

- **Configurable fraction:** Default quarter-Kelly (0.25). Half-Kelly and eighth-Kelly available. Start conservative -- edge estimates have uncertainty from vig removal error (~0.5%), manual placement delay (odds may move), and small sample size early in the season.
- **Running bankroll tracker:** New `bankroll_log` table in SQLite. Tracks deposits, withdrawals, and bet outcomes. Bankroll survives restarts.
- **Hard cap:** No bet exceeds 5% of bankroll regardless of Kelly recommendation (model uncertainty safeguard).
- **Display:** Suggested stake shown in Telegram alerts and console output.
- **Prerequisite (legacy design intent):** 200+ bets of CLV data before activating. Under current evaluation mode, the 200-manual-bets gate is unreachable — the actual P6 commit gate will be revised at commit time. Current pacing: see `HANDBOOK.md` § "CLV gating".

## Success Criteria

- [ ] Fractional Kelly formula: `stake = (edge / (odds - 1)) * fraction * bankroll`
- [ ] Configurable Kelly fraction (default: quarter-Kelly = 0.25)
- [ ] Running bankroll tracker in SQLite (new `bankroll_log` table)
- [ ] Stake displayed in Telegram alerts and console output
- [ ] `stake` field populated on `ValueBet` rows
- [ ] Bankroll survives restarts

## Components Affected

- [[SQLite Store]] -- new `bankroll_log` table
- [[Data Model]] -- `stake` field populated on `ValueBet`
- [[Edge Detection]] -- passes edge data to staking calculator
- [[Telegram Notifications]] -- displays suggested stake
- [[Scheduler]] -- integrates staking into the edge detection flow

## Dependencies

- Should only be activated after sufficient positive-CLV evidence — see `HANDBOOK.md` § "CLV gating" (legacy 200-bet gate is dead under evaluation mode and will be revised at P6 commit)
- Requires [[Open - Stake Field Always None]] to be resolved

## Open Questions

- Kelly fraction selection (what fraction to use, edge uncertainty) — open question, deferred to P6 commit
- Drawdown thresholds and stop-loss triggers — to be decided at P6 commit; `HANDBOOK.md` § "CLV gating" has the current decision criteria

## Related

- [[Phase Overview]]
