---
tags:
  - component/engine
  - layer/tracking
  - type/component
source-files:
  - oddsiq/engine/clv.py
  - oddsiq/pipeline/settlement_service.py
  - oddsiq/db/store.py
  - tests/test_clv.py
related:
  - "[[Vig Removal]]"
  - "[[Edge Detection]]"
  - "[[Scheduler]]"
  - "[[SQLite Store]]"
  - "[[Polling Tiers]]"
  - "[[Production Lessons]]"
  - "[[First Game Night]]"
---

# CLV Calculation

## Purpose

Measures Closing Line Value -- compares the odds at flag time (entry) against the fair market price at tipoff (closing). Positive CLV means the bettor got better-than-fair value. Over a large sample, consistently positive CLV is the strongest indicator of a profitable bettor. This is the validation mechanism for the entire [[Edge Detection]] pipeline.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/engine/clv.py` | `calculate_clv()` | `(entry_odds / closing_fair_odds) - 1.0` |
| `oddsiq/engine/clv.py` | `resolve_bet_result()` | Final-score → `"win"` / `"loss"` / `"push"` / `"draw"` for moneyline / spread / total (`"draw"` on equal scores) |
| `oddsiq/pipeline/settlement_service.py` | `_OPPOSITE_SELECTION`, `_opposite_selection()` | Module dict `{"home":"away", "away":"home", "over":"under", "under":"over"}` and the wrapper function (`_opposite_selection(selection)`) used by the CLV path |
| `oddsiq/pipeline/settlement_service.py` | `SettlementService.handle_game_live()` | Orchestrates closing-snapshot marking + per-bet CLV + bet-result resolution at tipoff |
| `oddsiq/pipeline/settlement_service.py` | `SettlementService.recover_finalized_game()` | Restart-recovery path: when the schedule refresh finds a game already `final`, marks closing snapshots and runs `calculate_clv_for_pending_bets()` for any value bets that never got CLV |
| `oddsiq/pipeline/settlement_service.py` | `SettlementService.calculate_clv_for_pending_bets()` | Shared per-bet CLV worker used by both `handle_game_live()` and `recover_finalized_game()` |
| `oddsiq/db/store.py` | `Store.mark_closing_snapshots()` | Per-game freshness-gated `is_closing` mark via `ROW_NUMBER()` window |
| `tests/test_clv.py` | — | 15 tests: CLV math, all bet-result branches (incl. draw/tie), ValueError on unknown market |

## Inputs

### For CLV Calculation
- `entry_odds` (float) -- Czech book odds at the time the value bet was flagged
- `closing_fair_odds` (float) -- Pinnacle fair (vig-removed) closing odds for the same selection

### For Bet Resolution
- `market_type` (str) -- "moneyline", "spread", or "total"
- `selection` (str) -- "home", "away", "over", or "under"
- `line` (float | None) -- spread or total line; None for moneyline
- `home_score`, `away_score` (int) -- final game scores

## Outputs

### CLV
- `clv` (float) -- closing line value as a decimal (e.g., 0.05 = 5% CLV)
- Positive = better-than-fair value; negative = worse-than-fair

### Bet Resolution
- `result` (str) -- "win", "loss", "push", or "draw"

## Interactions

- Uses [[Vig Removal]] to convert Pinnacle closing pair into fair closing odds
- Triggered by `SettlementService.handle_game_live()` in `oddsiq/pipeline/settlement_service.py` when game goes live
- Also triggered by `SettlementService.recover_finalized_game()` during schedule refresh — if the pipeline was down at tipoff and a game is already `final`, the recovery path marks closing snapshots and computes CLV for any still-pending value bets via the shared `calculate_clv_for_pending_bets()` worker
- Reads closing snapshots from [[SQLite Store]] (`get_closing_odds()`)
- Writes CLV results back to [[SQLite Store]] (`update_value_bet_closing()`)
- Bet result resolution (win/loss/draw) is handled by `SettlementService` when a game reaches `final` status (the schedule-refresh / `recover_finalized_game()` path), not by a `Scheduler` method
- Operates on [[ValueBet Lifecycle|ValueBet]] records created by [[Edge Detection]]

## Internal Logic

### CLV Formula

```python
clv = (entry_odds / closing_fair_odds) - 1.0
```

Positive CLV = got better odds than the closing fair line (the bet had value). Closing fair odds are **Pinnacle closing** after [[Vig Removal|vig removal]], not Tipsport closing.

### CLV Pipeline

Executes in three stages:

**1. At tipoff** (`SettlementService.handle_game_live()` in `oddsiq/pipeline/settlement_service.py`): fires once per game when `time_to_tipoff <= 0`.

**2. Mark closing snapshots** (`Store.mark_closing_snapshots`): marks the **last captured snapshot** per (bookmaker, market, selection) as the closing line, **only if** `captured_at` is within `CLOSING_FRESHNESS_MINUTES` (default 30) of the game's effective tipoff (`commence_time` when Pinnacle supplied it, otherwise `scheduled_at`). Stale rows stay at `is_closing = 0` so CLV is skipped rather than computed against a fake closing line. The SQL uses a `ROW_NUMBER()` window function partitioned by `(bookmaker, market_type, selection)` ordered by `captured_at DESC`, with the cutoff derived from `commence_time or scheduled_at`.

**3. Calculate CLV for each pending value bet** (continuing in `handle_game_live`):

1. Get the Tipsport closing odds for the bet's selection
2. Get the Pinnacle closing odds for the bet's selection
3. Get the Pinnacle closing odds for the **opposite** selection
4. Remove vig from the Pinnacle pair via [[Vig Removal]] to get `closing_fair`
5. Calculate `clv = (entry_odds / closing_fair) - 1.0`

### Selection Swap Logic

The opposite selection for vig removal is determined by an explicit mapping dict:

```python
_OPPOSITE_SELECTION = {"home": "away", "away": "home", "over": "under", "under": "over"}
```

The bet's selection is always passed as the first argument to `remove_vig()`, and the first return value (`fair_sel`) is used as the closing fair price. Both the selection mapping and the argument ordering are explicit -- no implicit dependency on coincidental argument order.

**Remaining improvement:** `remove_vig()` still returns a positional tuple. Proposed change to keyed dict return:

```python
# Before (current):
fair_sel, _fair_opp = remove_vig(pin_sel.odds, pin_opp.odds)

# After (proposed):
fair = remove_vig_named(
    {vb.selection: pin_sel.odds, opposite: pin_opp.odds}
)
closing_fair = fair[vb.selection]
```

### Bet Resolution

`resolve_bet_result()` in `oddsiq/engine/clv.py`:

Determines win/loss/push/draw based on final scores:

- **Moneyline:** Direct win/loss based on final score. `selection == "home"` wins if `home_score > away_score`; equal scores resolve to `"draw"` for both sides (defensive — NBA/NHL/MLB/WNBA rarely tie after OT/extra innings, but the branch exists).
- **Spread:** `margin = my_score - opp_score + line`. Win if `margin > 0`, loss if `margin < 0`, push if `margin == 0`.
- **Total:** Compare `home_score + away_score` to line. Over wins if `total > line`, under wins if `total < line`, push if equal.

Implementation is a flat market-type switch — read the source for the full branches. Key invariants:

- Moneyline `home` wins iff `home_score > away_score`; `home_score == away_score` returns `"draw"` for both sides (shipped with the multi-sport refactor — defensive against ties).
- Spread margin: `(my_score - opp_score) + line`. `> 0` win, `< 0` loss, `== 0` push.
- Total: compare `home_score + away_score` to `line`.
- Unknown `market_type` raises `ValueError`.

## Configuration

No dedicated configuration constants. CLV uses `EDGE_THRESHOLD` indirectly (only bets exceeding the threshold in [[Edge Detection]] enter the CLV pipeline).

## Error Handling

- Missing closing Pinnacle odds for a selection: CLV not calculated, `clv_pct` remains NULL on the [[ValueBet Lifecycle|ValueBet]] record
- Missing opposite selection for vig removal: same -- cannot remove vig without both sides
- Game scores unavailable at resolution time: bet result stays NULL until `SettlementService` finds final scores (on a later schedule refresh / recovery pass)

## Known Issues

### Accuracy Limitations

- **Polling gap:** The "closing" snapshot is the last one captured before tipoff, not the actual last available odds. With a 90-second poll interval (hot tier), the real closing line could differ by up to 90 seconds of line movement.
- **Pinnacle-only reference:** CLV is measured against Pinnacle's closing line. If Pinnacle's line is inefficient for a particular game (unlikely but possible), CLV will be misleading.
- **No line-adjusted CLV:** For spreads, if the line moves from -4.5 to -5.5 at close, the current system can't capture this since it uses exact line matching. The closing odds might not exist for the same line the bet was placed at. (Latent — the pipeline is moneyline-only in practice.)

## Tests

`tests/test_clv.py` -- 15 tests covering:

- CLV calculation (positive, negative, zero)
- Bet resolution: moneyline (home win, away win, draw/tie)
- Bet resolution: spread (win, loss, push)
- Bet resolution: total (over win, under win, push)
- Unknown market type raises `ValueError`
