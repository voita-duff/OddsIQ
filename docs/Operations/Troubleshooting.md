---
tags:
  - type/runbook
  - layer/operations
  - status/complete
  - phase/p0
source-files:
  - oddsiq/pipeline/scheduler.py
  - oddsiq/pipeline/poll_coordinator.py
  - oddsiq/cli.py
  - oddsiq/engine/edge.py
  - oddsiq/engine/clv.py
  - oddsiq/notifications/telegram.py
related:
  - "[[Pipeline Services]]"
  - "[[Edge Detection]]"
  - "[[CLV Calculation]]"
  - "[[Telegram Notifications]]"
  - "[[Polling Tiers]]"
  - "[[TipsportScraper]]"
  - "[[PinnacleScraper]]"
---

# Troubleshooting

Diagnostic procedures for common OddsIQ issues.

## Diagnosing "No Value Bets Detected"

Work through this 5-step checklist in order:

1. **Are games in the polling window (< 6 hours to tipoff)?** The pipeline's `_compute_pollable` returns nothing for games > 6h out. Edge detection fires in the hot (< 60 min) AND warm (1–2h) tiers per [[Polling Tiers]]; far tier (2–6h before tipoff) collects Tipsport snapshots but skips Pinnacle and edge comparison.

2. **Are Pinnacle odds being fetched?** Check for `Scraper health: name=pinnacle` log line. If missing, Pinnacle is not being called (expected in far tier — is the game genuinely beyond 2h?). If it is warm/hot and missing, check `--check-budget` and logs for a Pinnacle fetch error.

3. **Are Tipsport odds being fetched?** Check the `Scraper health: name=tipsport snapshots=N matched_games=M` line. If `matched_games=0` during a warm/hot game window, the [[TipsportScraper]] is fetching but not matching — see the Cloudflare issues below and run `oddsiq tipsport-smoke`.

4. **Are lines matching?** Only moneyline is compared (Tipsport is moneyline-only). Enable DEBUG logging to see `Line mismatch` messages from [[Edge Detection]].

5. **Is the threshold too high?** Lower `EDGE_THRESHOLD` in `.env` (default 3%). The market may be efficient enough that edges only appear at 2%.

## Duplicate Value-Bet Detections

**Status:** Fixed.

Previously caused by pipeline restarts losing the in-memory `_alerted_edges` set, which re-announced the same bet after every restart. Now handled via database `ON CONFLICT DO NOTHING` — the `insert_value_bet()` return value tells the pipeline whether the bet was new, and a repeat detection returns `None` and is dropped before it can re-log `value_bet:` or re-enter the betting handoff. Deduplication survives restarts because it lives in [[SQLite Store]]. Note `tier` is part of the unique key, so a warm and a later hot detection of the same selection are deliberately separate rows — the double-bet there is closed by the engine's `selection_already_open` qualification rule, not by dedup. Duplicate *messages* are moot either way: detections are never sent to the phone since the 2026-08-10 rebrand ([[Telegram Notifications]]).

## CLV Not Calculated

Four possible causes:

1. **Closing-snapshot freshness gate rejected the snapshot.** The most common cause. `mark_closing_snapshots()` only sets `is_closing=1` if the latest snapshot's `captured_at` is within `CLOSING_FRESHNESS_MINUTES` (default 30) of effective tipoff. If the pipeline missed the hot-tier window for that game (crash, scraper outage), the latest snapshot is hours old and intentionally rejected. Result: `is_closing=0` across all rows for that game, CLV NULL. **This is correct behavior** — better no CLV than a fake one. Run `oddsiq cleanup-closing-flags --dry-run` to preview which games were affected; run without `--dry-run` if you want to retry under the current gate (idempotent and safe).

2. **Game went live but the live-transition handler did not fire.** Check if the pipeline was running at tipoff time. On restart, `_handled_live` is restored from `odds_snapshots` where `is_closing = 1` — games that already had closing snapshots are recognized as handled.

3. **No closing Pinnacle odds.** The [[PinnacleScraper]] poll might have failed near tipoff. Without Pinnacle closing data, [[CLV Calculation]] cannot compute fair closing odds.

4. **Missing opposite selection for vig removal.** Both sides of the market (home/away) must have closing snapshots from Pinnacle. If only one side was captured, vig removal fails and CLV cannot be calculated.

## Graceful Shutdown Sequence

See [[Runbook#Graceful Shutdown]] for the full sequence (`scheduler.stop()` → betting-bridge stop → budget persist → console summary → Telegram client close → schedule + scraper close → store close → shutdown backup → exit). The summary is printed, never messaged.

## Common Symptoms and Fixes

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Tipsport: curl_cffi 403/401 on {sport}, syncing browser API session` | Normal — Cloudflare challenge; curl_cffi resync with JSESSIONID from Playwright | Automatic — session syncs in ~5s |
| `Tipsport: session expired, re-creating` | Cloudflare cookies expired mid-run (~20 min lifecycle) | Automatic — session recreates itself via `SESSION_MAX_AGE` (1200s) |
| `Tipsport: Playwright failed to solve Cloudflare` | Cloudflare blocked headless browser | Check `playwright install chromium` was run. See [[CloudflareSession]] |
| `Browser preflight FAILED ... Fix: playwright install chromium` | Playwright binary missing or version-drifted | Run `playwright install chromium`; Playwright is pinned `~=1.60.0` |
| `Schedule refresh failed` | Odds API `/scores` unreachable | Transient — the pipeline keeps the last-known slate and retries next cycle |
| `Pinnacle fetch failed: 401` | Bad or exhausted Odds API key | Check `ODDS_API_KEY` in `.env`. See [[PinnacleScraper]] |
| `Schedule refreshed: 0 games` | No games for the enabled sports (MLB + NFL today) in the Odds API window | Check sport season dates. The pipeline uses the Odds API recent+upcoming window. |
| `Odds API budget: X used / 0 remaining` | Monthly Odds API limit hit | Wait for next month or upgrade plan. See [[PinnacleScraper]] |
| `Telegram send failed` | Bot token/chat ID wrong or network issue | Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` |
| No Telegram messages but pipeline runs fine | Token/chat ID not set — or, far more likely, nothing bettor-facing happened: since the 2026-08-10 rebrand only real placements, their results, rejected/failed attempts, auto-disarms and login transitions are sent | Confirm `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are in `.env` ([[Telegram Notifications]] silently no-ops without them), then check the log and the dashboard `/betting` tab — detections, CLV and coverage gaps live there, not on the phone |
| `Tipsport smoke FAILED — zero moneyline snapshots` | Parser shape mismatch or empty listing | Inspect the raw response under `./data/raw/tipsport/`. The session may need a fresh warm-up; restart the pipeline. |
| `Coverage gap: game_id=... scraper=tipsport ... minutes_stale=N` | Tipsport hasn't matched the watched game in >10 min | Run `oddsiq tipsport-smoke --sport <sport>` to probe without pipeline involvement; restart if it succeeds. |

## See Also

- [[Pipeline Services]] — the main loop and orchestration logic
- [[Edge Detection]] — edge calculation and line matching
- [[CLV Calculation]] — closing line value pipeline
- [[Telegram Notifications]] — bettor-facing message delivery (real placements and their outcomes)
- [[Polling Tiers]] — controls when edge detection runs
- [[TipsportScraper]] — Cloudflare bypass and session management
- [[PinnacleScraper]] — API budget and authentication
