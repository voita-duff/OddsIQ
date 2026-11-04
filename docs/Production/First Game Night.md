---
title: First Game Night
date: 2026-04-10
commit: 8bde64f
tags:
  - type/production-data
  - status/complete
  - phase/p1
  - layer/operations
---

# First Game Night (2026-04-10)

The pipeline's first real production run. 15 NBA games scheduled.

## What Worked

| Metric                                | Observed Value                                             |
| ------------------------------------- | ---------------------------------------------------------- |
| Games tracked                         | 15                                                         |
| Tipsport odds fetched (all 3 markets) | 90 across 16 matches                                       |
| Value bets flagged                    | 5                                                          |
| Average edge on flagged bets          | 5.0%                                                       |
| Odds API budget consumed              | ~30 requests (470/500 remaining after session)             |
| Cloudflare initial solve time         | 2.0 seconds                                                |
| curl_cffi fast-path latency           | ~200ms per request                                         |
| Tipsport inter-request delay          | Historical: random delay between markets, removed on 2026-05-25 after browser-session curl reuse proved safe |

## What Broke

Four issues were discovered and fixed in commit `8bde64f`:

### The 17-Minute Pipeline Freeze

A Tipsport total odds fetch hung indefinitely -- Playwright's `page.evaluate(fetch(...))` never resolved. The entire async event loop blocked for 17 minutes because there was no timeout on the `cf.post()` call. During this freeze: no polling, no edge detection, no closing line capture.

**Fix:** Added `asyncio.wait_for(cf.post(...), timeout=60.0)` to all Playwright API calls in `tipsport.py:136`. The 60-second timeout is generous -- normal Playwright fetches complete in 2-10 seconds, so 60s catches genuine hangs without false-triggering.

Affected: [[TipsportScraper]], [[CloudflareSession]]

### The 429 Rate Limit Storm

`_check_completed_games` was calling balldontlie.io every 30 seconds during live games to check if any had gone final. With 15 live games, this hammered the API and triggered 429 (Too Many Requests) responses. The 429s cascaded -- the scheduler kept retrying on the next 30-second cycle, making it worse.

**Fix:** Throttled `_check_completed_games` to run at most every 5 minutes (`scheduler.py:253-261`). Also added a guard to skip the check entirely when no games are live (`40f757d`).

Affected: [[Scheduler]], schedule scraper

### The 90-Minute Dead Cycle

Cloudflare session cookies expire after ~20-30 minutes. Before the fix, the pipeline didn't know sessions were dead until `curl_cffi` got a 403, fell back to Playwright, and Playwright also failed (because it was using the same expired cookies). It took 3 consecutive failures to trigger a session reset (`MAX_CONSECUTIVE_FAILURES = 3`). With the then-current inter-market delay and 3 markets per cycle, three failed cycles could mean a long silent outage.

**Fix:** Proactive session refresh at 20 minutes (`SESSION_MAX_AGE = 1200` in `tipsport.py:46`). The `_maybe_refresh_session()` method checks session age at the start of every `fetch_odds()` call and recreates if needed. The 3-failure safety net remains as a fallback.

Affected: [[CloudflareSession]], [[TipsportScraper]], [[Config Constants]]

### The -33% CLV Lesson

A 7.5% edge was detected on a game 3.5 hours before tipoff. It looked great on paper. By tipoff, the lines had moved significantly -- the closing line value was **-33%**, meaning the "edge" had completely evaporated and reversed. The bet would have been placed at odds substantially worse than the closing fair price.

**Root cause:** Odds 3+ hours before tipoff are soft -- lines are wide, liquidity is low, and sharp money hasn't fully shaped the market. Edges detected early reflect temporary inefficiency, not persistent value.

**Fix (at the time):** Restricted edge detection to the hot tier only (< 30 minutes to tipoff). Earlier polling tiers still collected `odds_snapshots` for closing line tracking, but `find_edges()` was not called. Enforced in `scheduler.py` in `_poll_all_games()`.

> **Successor (2026-05-24, commit `3252c71`):** The hot-tier-only restriction was relaxed once the `ODDS_API_KEYS` pool absorbed the extra Pinnacle quota. `find_edges()` now runs in hot AND medium tiers (≤2h to tipoff). The original lesson still holds — far-tier lines (>2h) are still skipped because they haven't been sharpened enough — but the medium boundary turned out to be sharp enough to compare against in practice.

**Lesson:** This single observation validated the entire [[CLV Calculation]] tracking pipeline -- it proved that CLV catches exactly the kind of false positive that would silently drain a bankroll without detection.

Affected: [[Edge Detection]], [[Polling Tiers]], [[CLV Calculation]], [[Scheduler]]

## See Also

- [[Config Constants]] -- every constant tuned by this session
- [[Production Lessons]] -- generalized takeaways
- [[Validation Guide]] -- what to check after each night
