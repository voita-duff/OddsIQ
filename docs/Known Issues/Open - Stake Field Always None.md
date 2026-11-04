---
tags:
  - type/issue
  - status/open
  - phase/p6
related:
  - "[[Data Model]]"
  - "[[ValueBet Lifecycle]]"
  - "[[P6 - Kelly Staking]]"
---

# Open: `stake` Field Always None

## Status

Open (by design — Phase 6 feature).

## Location

`oddsiq/db/models.py` — `ValueBet.stake` field

```python
stake: float | None = None
```

## Problem

The `stake` column exists in the database schema, the field exists on `ValueBet` in the [[Data Model]], but nothing ever writes to it. The field is carried through the entire pipeline doing nothing.

## Root Cause

Kelly staking is a [[P6 - Kelly Staking|Phase 6]] feature. The field was added to the schema in anticipation but no implementation exists yet.

## Fix Applied

None — intentional placeholder.

## P1 auto-betting note (2026-08-09)

The P1 auto-betting engine did NOT adopt this column: real-money stakes live in **`placed_bets.stake_czk`** (migration 010) — one decision row per value bet — with the runtime flat stake in the meta key `betting_flat_stake_czk`. Verified 2026-08-10: `value_bets.stake` is still NULL on every row. If [[P6 - Kelly Staking]] ships, the natural home for a computed stake is the `placed_bets` ledger (or the meta config the engine reads), so this column is now more likely to be *removed* than populated — keep that in mind before building anything on it.

## Remaining Risk

None functionally. The `None` value is harmless dead weight in every `ValueBet` row.
