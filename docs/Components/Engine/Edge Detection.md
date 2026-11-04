---
tags:
  - component/engine
  - layer/detection
  - type/component
source-files:
  - oddsiq/engine/edge.py
  - tests/test_edge.py
related:
  - "[[Vig Removal]]"
  - "[[Scheduler]]"
  - "[[TipsportScraper]]"
  - "[[PinnacleScraper]]"
  - "[[Data Model]]"
  - "[[First Game Night]]"
  - "[[Production Lessons]]"
  - "[[Config Constants]]"
  - "[[Polling Tiers]]"
  - "[[Production Lessons]]"
---

# Edge Detection

## Purpose

Compares Czech bookmaker odds ([[TipsportScraper|Tipsport]]) against vig-free Pinnacle fair odds to identify +EV betting opportunities. An edge means the Czech book is paying more than fair value for a given outcome.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/engine/edge.py` | `calculate_edge()` | `(czech_odds / fair_odds) - 1.0` |
| `oddsiq/engine/edge.py` | `find_edges()` | Pairs Pinnacle by `(market_type, abs(line))`, vig-removes, returns edge dicts ≥ threshold |
| `tests/test_edge.py` | — | 6 tests: math, line normalization, mismatch handling, spread strict-match |

## Inputs

- `pinnacle_odds` (list[OddsSnapshot]) -- all Pinnacle snapshots for the current poll cycle
- `czech_odds` (list[OddsSnapshot]) -- all Tipsport snapshots for the current poll cycle
- `threshold` (float, optional) -- minimum edge to flag, defaults to `config.EDGE_THRESHOLD` (3%)

## Outputs

- List of edge dicts containing the price comparison plus exact sharp evidence: local pair observation time, upstream source-update time, and whether freshness was missing.
- Each dict represents a value bet candidate with edge >= threshold

## Interactions

- Calls [[Vig Removal]] (`remove_vig()`) on each Pinnacle pair to get fair odds
- Receives odds snapshots matched to games by `PollCoordinator.poll()` in `oddsiq/pipeline/poll_coordinator.py`
- [[PinnacleScraper]] provides the sharp reference odds
- [[TipsportScraper]] provides the Czech book odds
- Output is consumed by `PollCoordinator` to create [[ValueBet Lifecycle|ValueBet]] records in [[SQLite Store]]
- Runs during **warm (1–2h to tipoff) and hot (< 60m)** tiers — enforced by `EdgeService.detect_pregame_edges()` in `oddsiq/pipeline/edge_service.py`, called from `PollCoordinator.poll()`

## Internal Logic

### Edge Formula

```python
edge = (czech_odds / fair_odds) - 1.0
```

A 3% edge means the Czech book is paying 3% more than fair value.

### `find_edges()` algorithm

1. **Group Pinnacle by `(market_type, abs(line))`** so spread home `-4.5` and away `+4.5` end up in the same group for vig removal. The grouped dict is `{(market_type, abs_line): {selection: snapshot}}`.
2. **For each Czech snapshot**, look up the Pinnacle pair by exact `(market_type, abs(line))` match. Skip if missing — log `Line mismatch` at DEBUG level for spread/total only.
3. **Strict spread same-side check** (added in `a11e01f`): for spreads, additionally verify Pinnacle's snapshot for the **same selection** exists AND its raw `line` equals Czech's raw `line`. Catches edge cases where `abs(line)` collides but the signed lines disagree (e.g., a buggy bookmaker payload). Logs `Spread side/line mismatch` at DEBUG.
4. **Remove vig** via `remove_vig()` ([[Vig Removal]]) on the Pinnacle pair — uses fixed argument order `(home, away)` for non-totals, `(over, under)` for totals.
5. **Map fair odds back to selection** via `fair_map = {side_a: fair_a, side_b: fair_b}; fair = fair_map[czech_snap.selection]` — robust to refactoring (no positional dependency).
6. **Calculate edge**, filter by threshold (default 3%), build output dict.
7. **Freeze provenance for the exact Pinnacle pair**: the conservative older local observation and older upstream `last_update` travel with the candidate into `value_bets`. Parser-marked missing freshness fails closed.

### Integrity guards

`EdgeService._suspect_reason()` records but excludes candidates, returning the FIRST match in this fixed order:

1. `in_play_pregame` — the Tipsport match entry sits under `/live/` (checked first: it's the root cause the other guards were catching indirectly).
2. `sharp_source_unknown` — the parser flagged the Pinnacle market's upstream `last_update` as missing/naive/unparseable/>60 s in the future.
3. `stale_sharp_source` — `observed_at − source_updated_at` > `PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS` (300 s). `<=0` disables THIS check only; an unknown timestamp still fails closed at step 2.
4. `implausible_edge_pregame` — edge ≥ `PREGAME_MAX_PLAUSIBLE_EDGE`.
5. `incomplete_book_market` — the Tipsport listing carries an execution id but only one side of its two-way market. Gated by `PREGAME_REQUIRE_COMPLETE_BOOK_MARKET` (default `1`), its own off-switch: `PREGAME_MIN_BOOK_OVERROUND<=0` disables only the coherence floor below, not this.
6. `incoherent_book_pregame` — Tipsport's own two-way implied sum (paired within ONE `tipsport_match_id`) < `PREGAME_MIN_BOOK_OVERROUND`.

Excluded rows are recorded and settle (so the guards stay validatable) but never reach betting. Migration 013 includes normalized `excluded_reason` in the dedup identity so a temporary bad observation cannot suppress a later clean candidate in the same tier — expect a stamped selection to become bettable again once the feed recovers. `stale_sharp_source` is ALSO re-checked at dispatch by `BettingService._qualify` as a `placed_bets` skip reason, there measured `now − source_updated_at` so it includes dispatch-queue latency.

### Line Matching

**Why `abs(line)`?** Spreads have opposite signs for each side. Home `-4.5` and Away `+4.5` are the same market. Normalizing to `abs(line)` groups them for vig removal. Moneyline uses `None` as the line key (always pairs trivially). Totals use `abs()` of the literal total (e.g., `215.5`).

**Exact matching only.** If Tipsport has spread `-4.5` and Pinnacle has `-5.5`, the comparison is skipped. This is intentional — comparing mismatched lines would produce a false edge. Future improvement (deferred): interpolate fair odds between adjacent lines.

**Where exact matching fails:**

- If Tipsport offers a different line than Pinnacle (common for spreads), no edge is detected even if there IS value. A future improvement would be to interpolate fair odds between lines.
- Moneyline uses `None` as the line key, which works correctly since moneylines are always paired.
- Totals use the absolute line value (e.g., 215.5). If bookmakers disagree on the total number, the match fails.

### Edge Calculation Example

`czech_odds=2.10`, `fair_odds=2.04`:

```
edge = (2.10 / 2.04) - 1 = 0.029 (2.9%)
```

Below the 3% threshold -- not flagged.

### When Edges Are Detected

Edge detection runs in the **warm tier (1–2h to tipoff)** and the **hot tier (< 60m to tipoff)**. Far-tier polls (> 2h) collect `odds_snapshots` for [[CLV Calculation|closing-line tracking]] but skip edge comparison — soft lines that far out produce noisy detections. Enforced via `EdgeService.detect_pregame_edges()` in `oddsiq/pipeline/edge_service.py`, called from `PollCoordinator.poll()` in `oddsiq/pipeline/poll_coordinator.py`.

Each detection carries the tier it was observed in. Warm and hot observations of the same `(game, bookmaker, market, selection, line)` coexist as separate `value_bets` rows — `tier` is part of the unique index (migration 003). This lets the operator compare warm-tier CLV against hot-tier CLV: same closing line, different entry odds, so the gap shows whether earlier detection actually held up.

**Why far tier is still skipped:** [[First Game Night]] proved a 7.5% "edge" detected 3.5 hours before tipoff produced **-33% CLV**. Odds that far out are wide, liquid, and sharp money hasn't shaped them yet. The warm and hot tiers (≤ 2h) sit inside the "sharpened enough to compare" window; far tier doesn't.

## Configuration

| Constant | Value | Source | Notes |
| --- | --- | --- | --- |
| `EDGE_THRESHOLD` | 0.03 (3%) | `config.py` / env var | ~1% model uncertainty + ~0.5% execution slippage + buffer |
| `PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS` | 300 s | `config.py` / env var | Rejects an upstream Pinnacle feed stall even when the HTTP response itself arrived now; `<=0` disables the age cap only (`sharp_source_unknown` still fails closed) |
| `PREGAME_REQUIRE_COMPLETE_BOOK_MARKET` | `1` (on) | `config.py` / env var | Off-switch for the completeness guard alone — set `0` to stop stamping `incomplete_book_market` without a rebuild |

See [[Config Constants]] for full tuning rationale.

**Why 3%?** Covers model uncertainty (~1%), execution slippage (~0.5%), and provides a small buffer. Can be lowered to 2% for aggressive strategies or raised to 5% for conservative ones. Configurable via `EDGE_THRESHOLD` env var. [[First Game Night]] average edge was 5.0%, suggesting 3% isn't too tight.

## Error Handling

- Missing Pinnacle pair for a market: silently skips the Czech snapshot. `Line mismatch` logged at DEBUG for spread/total markets where Pinnacle line doesn't match.
- Missing one side of a Pinnacle pair (e.g., only "home" without "away"): skips — cannot remove vig from a single side.
- Spread same-side line disagreement (after `abs()` matched): skips, logs `Spread side/line mismatch` at DEBUG.
- `remove_vig()` raises `ValueError` on non-positive odds — propagates up; would indicate corrupt Pinnacle data, not handled here.

## Known Issues

### "fetched X odds from 0 matches" (historical)

The Tipsport spread and total markets frequently return `"fetched 0 odds from 0 matches"`. This is **not a parsing bug** — Tipsport often doesn't offer Asian handicap or Asian total lines for NBA games, or only offers them close to tipoff. The `_count_matches` helper in `tipsport.py` previously counted matches in the first tab only, which differed from the tab being parsed — this mismatch was fixed, making the helper tab-aware.

## Tests

`tests/test_edge.py` -- 9 tests covering:

- Edge calculation (basic math)
- Value detection against threshold
- `find_edges()` with matching lines
- `find_edges()` with mismatched lines
- Line normalization (`abs()`)
- Spread strict same-side line check (sign disagreement rejected)
- Missing Pinnacle pair handling
- Threshold filtering
