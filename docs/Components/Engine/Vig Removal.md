---
tags:
  - component/engine
  - layer/detection
  - type/component
source-files:
  - oddsiq/engine/vig.py
  - tests/test_vig.py
related:
  - "[[Edge Detection]]"
  - "[[CLV Calculation]]"
  - "[[PinnacleScraper]]"
  - "[[Architecture Decisions]]"
---

# Vig Removal

## Purpose

Converts bookmaker odds (which include vig/overround) into fair odds (zero-sum market) using the multiplicative method. This is the mathematical foundation for both [[Edge Detection]] and [[CLV Calculation]] -- every edge and CLV number in the system depends on fair odds produced here.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/engine/vig.py` | `implied_probability()` | `1.0 / decimal_odds`; raises `ValueError` if odds ≤ 0 |
| `oddsiq/engine/vig.py` | `remove_vig()` | Multiplicative method on a 2-way market; raises `ValueError` if either side ≤ 0; returns positional `(fair_a, fair_b)` |
| `tests/test_vig.py` | — | 5 tests: implied prob, balanced odds, NBA-typical, heavy favorite, invalid odds |

## Inputs

- `odds_a` (float) -- decimal odds for side A (e.g., 1.90)
- `odds_b` (float) -- decimal odds for side B (e.g., 2.00)

Both sides of a two-way market from [[PinnacleScraper|Pinnacle]].

## Outputs

- `fair_odds_a` (float) -- vig-free decimal odds for side A
- `fair_odds_b` (float) -- vig-free decimal odds for side B

Returns a positional tuple `(fair_a, fair_b)`. Caller must track which argument corresponds to which selection -- see [[CLV Calculation#Selection Swap Logic]] for how this is handled.

## Interactions

- Called by [[Edge Detection]] (`find_edges()`) to convert Pinnacle pairs into fair odds before comparing against Czech odds
- Called by [[CLV Calculation]] pipeline (`SettlementService.handle_game_live()` in `oddsiq/pipeline/settlement_service.py`) to get closing fair odds from Pinnacle closing pair
- Receives Pinnacle odds fetched by [[PinnacleScraper]]
- Operates on in-memory values only -- no database interaction

## Internal Logic

### Multiplicative Method

The standard approach for two-way markets. Assumes the bookmaker charges proportional vig on each side -- a reasonable assumption for Pinnacle, which has balanced books and tight margins (~2-4% overround on NBA).

The full implementation in `oddsiq/engine/vig.py` is short — read the source. The shape:

```python
imp_a = 1 / odds_a    # implied probability side A
imp_b = 1 / odds_b    # implied probability side B
overround = imp_a + imp_b  # > 1.0 due to vig
fair_prob_a = imp_a / overround  # normalize to sum=1
fair_prob_b = imp_b / overround
fair_odds_a = 1 / fair_prob_a
fair_odds_b = 1 / fair_prob_b
```

Inline example:

```python
imp_a = 1 / odds_a        # e.g., 1/1.90 = 0.5263
imp_b = 1 / odds_b        # e.g., 1/2.00 = 0.5000
overround = imp_a + imp_b  # e.g., 1.0263 (2.63% vig)
fair_a = 1 / (imp_a / overround)  # e.g., 1.952
fair_b = 1 / (imp_b / overround)  # e.g., 2.053
```

### Guarantee

`1/fair_a + 1/fair_b = 1.0` -- the resulting fair odds form a zero-sum market where implied probabilities sum to exactly 1.

### Limitations

- **Assumes symmetric vig.** If Pinnacle shades one side (e.g., public money on favorites), the fair line estimate is biased. Pinnacle does shade lines, but less than soft books.
- **Only works for two-way markets.** Three-way markets (e.g., soccer 1X2) would need the power method or Shin's model.
- **No margin scaling with odds.** Pinnacle charges slightly more vig on longer odds. The multiplicative method treats all odds equally.

## Configuration

No configuration constants. The method is pure math with no tunable parameters.

## Error Handling

- **Non-positive odds raise `ValueError`** from both `implied_probability()` and `remove_vig()`. Message: `"Odds must be positive, got X"` (single odds) or `"Odds must be positive, got (a, b)"` (pair). Callers are expected to provide valid decimal odds > 1.0; corrupt data from Pinnacle would surface here as a `ValueError` and propagate up.

## Known Issues

**Proposed improvement:** `remove_vig()` returns a positional tuple. If it returned values keyed by selection name, callers couldn't get the mapping wrong even with refactoring:

```python
# Before (current):
fair_sel, _fair_opp = remove_vig(pin_sel.odds, pin_opp.odds)

# After (proposed):
fair = remove_vig_named(
    {vb.selection: pin_sel.odds, opposite: pin_opp.odds}
)
closing_fair = fair[vb.selection]
```

Low priority since the current code is correct and explicit, but worth doing when `remove_vig` is next touched.

### Alternatives Considered and Rejected

- **Additive method:** Splits overround evenly. Biased toward the underdog. Rejected because it doesn't preserve the odds ratio.
- **Power method (Shin's model):** More sophisticated, accounts for insider trading probability. Rejected as overkill for NBA moneylines where Pinnacle markets are deep and efficient.
- **Using Pinnacle closing line directly as fair:** Would require no vig removal at all, but Pinnacle still has ~2% vig. Using raw Pinnacle odds as "fair" would underestimate edge by ~1%. See [[Architecture Decisions]] for full decision rationale.

## Tests

`tests/test_vig.py` -- 5 tests covering:

- Implied probability calculation
- Vig removal with balanced odds
- Vig removal with typical NBA odds
- Vig removal with heavy favorite
- Invalid odds handling
