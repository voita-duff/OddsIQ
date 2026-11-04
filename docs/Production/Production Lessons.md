---
title: Production Lessons
date: 2026-04-10
tags:
  - type/production-data
  - status/complete
  - phase/p1
  - layer/operations
---

# Production Lessons

Things that weren't obvious before production but are obvious now. All derived from [[First Game Night]] (2026-04-10).

## 1. Timeouts on Everything

The 17-minute freeze was caused by a single missing `asyncio.wait_for`. Every external call -- HTTP, Playwright, database -- needs an explicit timeout. "It'll probably return" is not a timeout policy.

The freeze blocked the entire async event loop: no polling, no edge detection, no closing line capture. One missing timeout on a [[TipsportScraper]] Playwright call cost 17 minutes of data.

## 2. Throttle All Background Checks

Any periodic task that hits an external API needs a minimum interval. The 429 storm happened because `_check_completed_games` ran on every [[Scheduler]] loop iteration (30s) instead of being rate-limited independently.

With 15 live games, every 30-second cycle hit balldontlie.io, which triggered 429s that cascaded -- the scheduler kept retrying, making it worse. Fix was a 5-minute minimum interval (`_check_completed_games`) and a guard to skip when no games are live.

## 3. Session TTLs Must Be Proactive, Not Reactive

Waiting for failures to detect expired sessions wastes 3x the failure budget in recovery time. Refresh sessions before they expire, and keep the failure-based reset as a safety net.

The [[CloudflareSession]] cookies expire at ~20-30 minutes. Reactive detection took 3 consecutive failures (each ~30 minutes) = ~90 minutes dead. Proactive refresh at 20 minutes (`SESSION_MAX_AGE = 1200`) eliminates the dead cycles entirely.

## 4. Early Edges Are Noise

The -33% CLV observation wasn't an outlier -- it reflects a fundamental market microstructure reality. Odds far from tipoff haven't been shaped by sharp money. [[Edge Detection]] should only run in the window where lines are efficient enough for meaningful comparison.

A 7.5% edge detected 3.5 hours before tipoff had -33% CLV at closing. Original fix: restrict `find_edges()` to hot tier only (< 30 minutes to tipoff). Updated 2026-05-24 (commit `3252c71`): now hot AND medium (≤2h). Far tier (>2h, where the original blow-up happened) is still skipped — the lesson stands; the implementation just got more permissive at the tighter boundary once the `ODDS_API_KEYS` pool (see [[PinnacleScraper]]) removed the budget constraint.

## 5. CLV Is the Only Metric That Matters

Win/loss is noise over small samples. Edge percentage is an estimate. CLV measures whether you systematically got better prices than the closing market -- the one metric that predicts long-term profitability regardless of variance.

The -33% CLV catch proved the [[CLV Calculation]] pipeline works: it identified a false positive that looked like a real edge on every other metric. Without CLV tracking, that pattern would silently drain a bankroll.

## See Also

- [[First Game Night]] -- the source incidents for all 5 lessons
- [[Scheduler]] -- throttling and polling logic
- [[TipsportScraper]] -- timeout and session management
- [[CLV Calculation]] -- the metric that matters
- [[Edge Detection]] -- restricted to hot tier after lesson #4
- [[Config Constants]] -- all constants tuned by these lessons
