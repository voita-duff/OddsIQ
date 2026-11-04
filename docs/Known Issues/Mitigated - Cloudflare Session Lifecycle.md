---
tags:
  - type/issue
  - status/mitigated
  - phase/p1
related:
  - "[[CloudflareSession]]"
  - "[[TipsportScraper]]"
  - "[[First Game Night]]"
---

# Mitigated: Cloudflare Session Lifecycle

## Status

Mitigated.

## Location

`oddsiq/scrapers/tipsport.py` — `_maybe_refresh_session()`, `SESSION_MAX_AGE = 1200`.
`oddsiq/scrapers/cloudflare.py` — [[CloudflareSession]] Playwright calls with `asyncio.wait_for` timeout.

## Problem

Cloudflare sessions expire silently after ~30 minutes. Before mitigation, the system relied on detecting 3 consecutive failures (`MAX_CONSECUTIVE_FAILURES`) before triggering a session reset — creating a 90-minute dead cycle where the [[TipsportScraper]] returned zero data while burning through poll windows. Observed during the [[First Game Night]].

## Root Cause

No proactive session refresh. The system was purely reactive — waiting for failures instead of preempting expiry. Additionally, Playwright `cf.post()` calls could hang indefinitely if Cloudflare stalled.

## Fix Applied

Proactive refresh at 20 minutes (`SESSION_MAX_AGE = 1200`) via `_maybe_refresh_session()` in `oddsiq/scrapers/tipsport.py`, called at the start of every `fetch_odds()`. This avoids the 90-minute dead cycle. The `MAX_CONSECUTIVE_FAILURES` mechanism remains as a safety net. Playwright `cf.post()` calls now have a 60-second `asyncio.wait_for` timeout to prevent pipeline freezes.

## Remaining Risk

Cloudflare can change their challenge mechanism at any time, breaking Playwright-based solving entirely. The proactive refresh only helps if sessions *can* be created. If [[CloudflareSession]] creation itself starts failing, the system degrades to the same dead cycle (but now detectable faster via the timeout).
