---
title: Observed Latency
date: 2026-04-10
tags:
  - type/production-data
  - status/complete
  - phase/p1
  - layer/operations
---

# Observed Latency Profile

Real timing data from production, not benchmarks. All numbers from the first game night (2026-04-10).

## Latency Table

| Operation                            | Typical | Worst Observed | Notes                                          |
| ------------------------------------ | ------- | -------------- | ---------------------------------------------- |
| Pinnacle API call (The Odds API)     | ~500ms  | ~2s            | Single HTTP GET, all NBA games in one response |
| Tipsport via curl_cffi (fast path)   | < 200ms | ~1s            | Three market POSTs after curl has CF cookies + browser `JSESSIONID` |
| Tipsport via Playwright (fallback)   | 2-10s   | 60s (timeout)¹ | Browser in-page API call after curl still fails |
| Cloudflare initial session setup     | ~2s     | ~30s (timeout) | First solve after fresh browser launch         |
| Tipsport full poll cycle (3 markets) | < 1s steady-state | ~60s fallback | Three curl POSTs; fallback bounded by Playwright timeout |
| balldontlie.io schedule fetch        | ~300ms  | ~2s            | Simple REST call                               |
| Edge detection (in-memory)           | < 1ms   | < 1ms          | Pure math, no I/O                              |
| SQLite batch insert (50 snapshots)   | < 5ms   | ~20ms          | WAL mode, local SSD                            |
| Telegram alert send                  | ~200ms  | ~2s            | Telegram Bot API, non-blocking                 |

¹ The 60s Playwright timeout is a **hardcoded `asyncio.wait_for(...)` wrapper** around each Playwright-backed Tipsport API fetch in `tipsport.py:_post_with_playwright()` (added in commit `8bde64f` on 2026-04-13, after the first game night), not a first-night observed latency. It is distinct from `config.CF_SOLVE_TIMEOUT` (default `30.0`), which is the cap on the **initial Cloudflare challenge solve** during fresh-session setup. The other numbers in this table are first-night observed values.

## Key Insight

Tipsport's steady-state path is no longer the polling bottleneck. The current scraper lets curl_cffi keep its own Cloudflare cookies, copies only the browser-created `JSESSIONID` into that curl jar, and then performs the three market POSTs without an artificial inter-market delay.

The slow path still matters operationally: a fresh browser solve can take seconds, and a stuck Playwright fallback is capped at 60s. This is why Tipsport keeps proactive 20-minute session refresh and a consecutive-failure reset, and why `oddsiq tipsport-smoke` is the pre-flight check before relying on Tipsport for a game night.

## Component Links

- [[TipsportScraper]] -- curl_cffi fast path + browser-session sync + Playwright fallback
- [[PinnacleScraper]] -- fastest external call (~500ms)
- [[CloudflareSession]] -- initial setup is slow (~2s typical, 30s worst)
- Schedule scraper -- balldontlie.io (~300ms)
- [[Edge Detection]] -- negligible (< 1ms, pure math)
- [[SQLite Store]] -- fast batch inserts (< 5ms typical)
- [[Telegram Notifications]] -- non-blocking (~200ms)
- [[First Game Night]] -- source of all these measurements
- [[Config Constants]] -- timeout values derived from this data
