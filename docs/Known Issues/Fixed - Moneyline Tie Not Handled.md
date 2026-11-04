---
tags:
  - type/issue
  - status/fixed
related:
  - "[[CLV Calculation]]"
---

# Fixed: Moneyline Tie Not Handled

## Status

Fixed — shipped with the multi-sport / live-first refactor (2026-06-02).

## Location

`oddsiq/engine/clv.py` — `resolve_bet_result()`; `oddsiq/db/store.py` — `resolve_live_candidate_results()`

## Problem (historical)

The outcome resolution had no concept of a draw. If `home_score == away_score`, the `else` branch resolved both "home" and "away" selections as "loss" — logically impossible for moneyline (one side wins, or it's a tie).

## Root Cause

NBA games cannot tie (overtime continues until a winner), so the draw case was never needed; the code was written for NBA-only operation. When the system expanded to NHL/MLB, the missing branch became a latent correctness risk worth closing defensively.

## Fix Applied

`resolve_bet_result()` now returns `"draw"` when `home_score == away_score`, and the live-scout settler `resolve_live_candidate_results()` does the same in SQL. The `"draw"` outcome is carried through CLV/result aggregation and rendered with a dedicated Telegram bet-result label. Covered by `tests/test_clv.py` (draw/tie resolution cases).

## Remaining Risk

None. Ties are handled defensively across both the value-bet and live-candidate settlement paths.
