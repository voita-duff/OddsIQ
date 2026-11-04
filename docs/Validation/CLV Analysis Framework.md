---
title: CLV Analysis Framework
date: 2026-04-10
tags:
  - type/guide
  - status/active
  - phase/p1
  - layer/analysis
---

# CLV Analysis Framework

Once you have 50+ bets with CLV data, run through these steps in order against the database.

> **Status (2026-05-25):** the single-book CLV gate has already cleared — Tipsport reached a "Go" at **+5.87% avg CLV** over 48 resolved bets. The steps below remain the standing procedure for re-running the analysis on fresh data.

## Step 1: Raw Data Dump

> Read oddsiq.db and show me all value bets with their edge, CLV, and result. Sort by date.

This gives you the full picture of every bet the system flagged. Look for patterns: are edges clustered around certain game times, market types, or teams?

## Step 2: CLV Verdict (The Key Metric)

> What's the average CLV across all resolved bets? Break it down by market type. Is CLV consistently positive?

**Interpreting results:**

| Average CLV   | Meaning                                                               |
| ------------- | --------------------------------------------------------------------- |
| **> +1%**     | The model finds real value. Worth continuing.                         |
| **Around 0%** | Edges are noise. The model isn't beating closing lines.               |
| **< -1%**     | The model is systematically wrong. Stop.                              |

This is the single most important output of the entire validation process. Everything else is supporting evidence. See [[CLV Calculation]] for how CLV is computed.

## Step 3: Edge Accuracy

> Compare the detected edge % to actual win rate. For bets with 5% edge, is the win rate roughly what you'd expect from the fair odds?

Example: if fair odds are 2.00 (50% implied), bets at 5% edge should win ~50% of the time. If they win 35%, the fair odds estimate is off.

This tests the [[Edge Detection]] algorithm and [[Vig Removal]] accuracy independently of CLV.

## Step 4: Win/Loss Breakdown

> Show me win rate, total profit at flat stakes, and profit at proportional-to-edge stakes. Break down by market type.

This tells you if flat betting the edges would have been profitable. Note: over small samples (< 100 bets), win/loss variance is high. CLV is more reliable than win rate for small samples.

## Step 5: Per-Book Data Quality

> For each enabled book, how many polls captured odds vs how many failed? What percentage of poll cycles had complete data from that book and Pinnacle?

If any enabled scraper was down >50% of the time, edge detection on that book is unreliable. Check for patterns: does a book fail more during hot-tier polling (higher frequency = more Cloudflare pressure for the browser-based books)? The canonical per-book health/coverage tool is `oddsiq audit-bookmakers` (`--bookmaker NAME`, `--since YYYY-MM-DD`, `--json`); Tipsport is the sole soft book (the multi-book era — Betano, Allwyn, Fortuna — was archived 2026-06-02).

## Step 6: Edge Distribution

> Show me a distribution of detected edges -- how many at 3-5%, 5-7%, 7-10%, 10%+? Do larger edges correlate with better CLV?

If large edges have worse CLV, the model overestimates edge on extreme lines. This indicates a [[Vig Removal]] bias on wide-spread markets. See [[Edge Distribution]] for the tracking framework.

## Step 7: Go/No-Go Decision

> Based on all of the above, give me a go/no-go recommendation on continuing to build OddsIQ. Consider CLV, edge accuracy, data quality, and sample size.

Use the CLV gating table in `HANDBOOK.md` § "CLV gating" to map the CLV result to a concrete action.

## See Also

- [[CLV Calculation]] -- how CLV is computed from closing snapshots
- [[Edge Detection]] -- the algorithm being validated
- [[TipsportScraper]] -- primary data quality concern
- `HANDBOOK.md` § "CLV gating" — maps CLV results to actions (canonical decision framework under evaluation mode)
- [[Edge Distribution]] -- per-market breakdown
- [[Useful Queries]] -- SQL queries for manual inspection
