---
tags:
  - type/issue
  - status/fixed
related:
  - "[[SportProfile]]"
  - "[[Data Model]]"
  - "[[CLI Entry Point]]"
  - "[[Migrations]]"
---

# Fixed: Basketball MatchView Data Contamination (2026-06)

## Status

**Fully resolved.** Code fixed (`a1669a4`, 2026-06-12) · Reporting filter shipped (migration 007 + `oddsiq mark-excluded` + reporting-query filters, 2026-06-12) · **Production stamping EXECUTED on the VM 2026-07-10** (with the v2.3.0 deploy) and verified in the local pull: **56 `value_bets` + 96 `live_edge_candidates`** stamped `excluded_reason='basketball_3way_matchview_2026_06'` — a few more than the 55+4 local-mirror floor estimated below, as expected (the mirror lagged the 06-11/06-12 NBA activity). The runbook below is retained as the historical record of how the stamping was scoped.

## Problem

**Since NBA data collection began (first row 2026-04-14)** until the fix on 2026-06-12, the Tipsport scraper read the NBA "moneyline" from Tipsport's `WINNER_WHOLE_MATCH` tab — the legacy pre-refactor scraper hardcoded that matchView (verified against `archive/multibook-2026-06-02`: `MARKET_TAB_VIEW = {"moneyline": "WINNER_WHOLE_MATCH"}`), and the 2026-06-02 refactor carried it into the basketball [[SportProfile]]. For basketball that tab carries the **regulation 3-way** legs (home / draw / away), NOT the incl-OT 2-way moneyline. Only MLB's `WINNER_WHOLE_MATCH` is a clean 2-way (baseball cannot tie). The fix repointed both basketball profiles to `WINNER_FINAL_DECISION` (`superSportId=7`), matching how NHL already worked. **WNBA has no contaminated rows**: it was enabled in the same changeset that contains the fix, so no production deploy ever scraped WNBA from the wrong tab (verified: zero `wnba` rows in either table).

**Effect on data:** regulation entry odds are systematically **longer** than the true incl-OT moneyline (some win probability sits on the regulation draw and on OT), so every NBA/WNBA row recorded in the window carries **inflated `edge_pct` and `clv_pct`**. Some are pure artifacts — at fix time a fake +5.1% Knicks edge existed while the real moneyline had ~0%. The Pinnacle side was always the correct incl-OT fair; only the Tipsport leg is wrong.

## Scope (which rows)

- `value_bets` and `live_edge_candidates` rows with `sport = 'nba'` whose `flagged_at` / `observed_at` predates the fix deploy. All NBA rows are `bookmaker='tipsport'` (verified), so no per-book filter is needed.
- **The window starts at the beginning of collection, not at the refactor**: 54 of the 55 NBA value bets in the local mirror were flagged *before* 2026-06-02. A June-only window would stamp 1 row and leave the bulk of the contamination in every CLV aggregate.
- **WNBA, NHL, and MLB are unaffected** (WNBA shipped already-fixed and has zero rows pre-deploy; NHL always used `WINNER_FINAL_DECISION`; MLB's whole-match is a clean 2-way).
- Only the **`--until` bound is deploy-time-based**: it must be the instant the `a1669a4` fix image started running on the VM (the registry deploy lags the commit — pin it from `docker inspect`/compose logs). The `--from` bound just needs to predate the first NBA row (2026-04-14) — `2026-04-01T00:00:00` is safely early.
- **Markout confirmation does NOT protect against this.** `classify_markout` re-checks the *Pinnacle* fair for staleness ("mirage" rejection); the contamination is in the *Tipsport* price, so contaminated live candidates can be `markout_status='confirmed'` and still be artifacts.

## Local-DB damage snapshot (analysis only — VM is source of truth)

The local `./oddsiq.db` is overwritten daily by the `com.oddsiq.dbpull` launchd job and lagged to **2026-06-10 23:09** at analysis time, so it MISSES 2026-06-11/06-12 NBA activity (including the fake +5.1% Knicks edge cited at fix time). Treat the local figures as a **floor**, and re-measure on the VM. `edge_pct` / `clv_pct` are stored as percentages (e.g. `3.02` = 3.02%).

`value_bets`, window 2026-06-02 .. 2026-06-12 (by `flagged_at`):

| sport | n | avg edge% | max edge% | avg CLV% | W–L–D | note |
|-------|---|-----------|-----------|----------|-------|------|
| nba | 1 | 3.02 | 3.02 | 3.02 | 1–0–0 | contaminated (regulation legs) |
| nhl | 0 | — | — | — | — | none flagged in window |
| mlb | 24 | 8.83 | 27.49 | 5.42 | 10–12–0 | **clean** (whole-match is 2-way for baseball) |

`live_edge_candidates`, `sport='nba'`, same window: **4 rows, 1 game (id 11608)**, avg edge **16.95%** (10.5–23.4%), 3 `confirmed` + 1 `pending` by markout, **0 win / 4 loss**. Double-digit "edges" that all lost — consistent with fake edges from longer regulation prices, and a concrete demonstration that markout-`confirmed` ≠ real here.

So the windowed comparison is underpowered (1 NBA vs 0 NHL vs 24 MLB) — because the June window itself was wrong. **Full-history figures (local mirror, 2026-06-10 sync): 55 NBA value bets, all `bookmaker='tipsport'`, 54 flagged before 2026-06-02, 54 with CLV averaging +5.82%** — i.e. most of the project's accumulated "positive CLV evidence" sits on contaminated NBA rows. Plus the 4 `live_edge_candidates` above. Final counts must be re-measured on the VM (the mirror lags ~a day).

## Remediation (chosen: annotate + exclude)

Reversible, audit-preserving. No rows are deleted.

1. **Migration 007** (`007_add_excluded_reason.py`) adds a nullable `excluded_reason TEXT` to `value_bets` and `live_edge_candidates`. NULL = part of the evaluation dataset.
2. **`Store.mark_rows_excluded(...)`** stamps a reason slug on the affected sports+window. Already-stamped rows are skipped (re-runnable).
3. **Reporting surfaces filter `excluded_reason IS NULL`**: dashboard queries (`VALUE_BETS_DEDUPED`, status strip, recent lists, activity feed, recent live edges), the per-book audit (`_VALUE_BETS_DEDUPED`), and `Store.get_live_edge_candidates`. Pipeline-lifecycle queries (settlement, CLV recovery, markout resolution) deliberately do NOT filter — excluded rows are inert there.
4. **CLI:** `oddsiq mark-excluded --sports … --from … --until … --reason … [--dry-run]` (see [[CLI Entry Point]]).

Tests: `tests/test_migration_007.py` (3) + `tests/test_excluded_rows.py` (11).

## VM execution runbook (historical — executed 2026-07-10)

Retained as the record of how the stamping was scoped and run. On the production VM, in the pipeline container/venv:

```bash
# 1. Pin ONE timestamp on the VM: UNTIL = the instant the a1669a4 fix image
#    started running (docker inspect / compose logs / image pull timestamps).
#    FROM needs no pinning — anything predating the first NBA row (2026-04-14)
#    works; use 2026-04-01T00:00:00.

# 2. Take a manual backup first (startup backup also fires, but be explicit).

# 3. Preview (expect roughly 55 value_bets + 4 live_edge_candidates — the
#    local-mirror floor; the VM may have a few more from 06-11/06-12):
oddsiq mark-excluded --sports nba \
  --from 2026-04-01T00:00:00 --until <UNTIL_ISO_UTC> \
  --reason basketball_3way_matchview_2026_06 --dry-run

# 4. Inspect the printed per-table counts, then drop --dry-run to apply.

# 5. Verify the dashboard / `oddsiq audit-bookmakers` / `oddsiq live-scout-report`
#    no longer show the contaminated NBA rows.
```

The next `com.oddsiq.dbpull` will then bring the stamped DB local. (Stamping on the VM is correct; do not stamp the local copy — it would be overwritten by the next pull.)

## See Also

- [[SportProfile]] — the matchView fix and the per-sport price-sanity guidance
- [[Data Model]] — `excluded_reason` column semantics
- [[CLI Entry Point]] — the `mark-excluded` subcommand
