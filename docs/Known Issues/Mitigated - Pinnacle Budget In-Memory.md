---
tags:
  - type/issue
  - status/mitigated
  - phase/p1
related:
  - "[[PinnacleScraper]]"
  - "[[SQLite Store]]"
  - "[[Data Model]]"
---

# Mitigated: Pinnacle Budget In-Memory Only

## Status

Mitigated. Startup probe + `meta` table fallback together eliminate the misleading 500-remaining display on restart.

## Original Problem

The Odds API monthly budget counter was initialized to `config.ODDS_API_MONTHLY_BUDGET` (500 at the time; a fixed 20000 since the key upgrade) on every start. Mid-month restarts displayed the full default remaining until the first live poll corrected it from the `x-requests-remaining` response header. Standalone budget checks weren't possible without starting the full scheduler.

> **2026-06-02 refactor note:** budget probe/persist/snapshot now lives in the [[Budget Governor]] (`oddsiq/pipeline/budget_governor.py`), lifted from the old Scheduler — the mechanics below are unchanged, but read "Scheduler" as "BudgetGovernor".

## Fix Applied

Commit `58f1ce2` — *feat: accurate Odds API budget tracking with startup probe and CLI check*.

1. **Startup probe**: `PinnacleScraper.probe_budget()` hits the free `/v4/sports?apiKey=...` endpoint on scheduler startup and parses both `x-requests-used` and `x-requests-remaining` headers. This endpoint does not count against quota. Populates `_budget_used` and `_budget_remaining` with real values before any paid request.
2. **DB fallback**: If the probe fails (network blip, stale key, missing headers), [[Scheduler]] reads the last-known values from the `meta` table via `store.get_meta("odds_api_budget_used")` / `get_meta("odds_api_budget_remaining")`. Better than the misleading 500 default.
3. **Persistence**: After each successful poll, [[Scheduler]] writes current budget values to `meta`, throttled to once per 5 min, and skipped until a probe has populated real values (never persists the default).
4. **CLI entry point**: `python3 -m oddsiq.cli --check-budget` runs only the probe and prints `X used / Y remaining (Z total)`. Useful for "did my new key work?" checks without starting the pipeline.

## Remaining Risk

Minimal. On first-ever start with no `meta` rows and a probe that also fails, the displayed budget is still the configured default — but `should_skip_request()` uses the same field and will self-correct after the first successful paid poll. The 5-min persistence window means up to 5 minutes of drift can be lost on a hard crash between persists, acceptable for monthly granularity. See [[Data Model]] for the `meta` table schema.
