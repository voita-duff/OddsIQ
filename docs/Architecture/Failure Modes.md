---
tags:
  - type/failure-analysis
  - status/current
  - layer/operations
  - component/system
source-files:
  - oddsiq/scrapers/pinnacle.py
  - oddsiq/scrapers/tipsport.py
  - oddsiq/scrapers/odds_api_schedule.py
  - oddsiq/scrapers/cloudflare.py
  - oddsiq/db/store.py
  - oddsiq/pipeline/scheduler.py
  - oddsiq/pipeline/budget_governor.py
  - oddsiq/cli.py
related:
  - "[[System Overview]]"
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[CloudflareSession]]"
  - "[[SQLite Store]]"
  - "[[Pipeline Services]]"
  - "[[Budget Governor]]"
  - "[[CLI Entry Point]]"
  - "[[Edge Detection]]"
  - "[[Team Name Normalization]]"
  - "[[Architecture Decisions]]"
---

# Failure Modes

Every known failure mode across scrapers, database, network, logic, and process layers. Each entry includes detection mechanism, impact, and recovery path.

---

## 9.1 Scraper Failures

| Component | Failure | Detection | Impact | Recovery |
| --- | --- | --- | --- | --- |
| **[[PinnacleScraper\|Pinnacle]]** | Key invalid / blocked (401/403/other-4xx) | `fetch_odds` raises via `raise_for_status` (not retried), caught + logged, returns `[]` | That cycle's Pinnacle odds empty (no edge detection); next cycle retries | Fix/replace `ODDS_API_KEY` in `.env`, restart |
| **[[PinnacleScraper\|Pinnacle]]** | Rate-limited (429) | `with_backoff` retries (up to 3 attempts, full-jitter, `Retry-After` clamped to 30s); on exhaustion caught + returns `[]` | Brief in-cycle wait, then odds or `[]` | Automatic; [[Budget Governor]] throttles cadence to avoid 429 |
| **[[PinnacleScraper\|Pinnacle]]** | Budget exhausted (`remaining ≤ 0`) | `should_skip_request()` returns True; fetch skipped before spending | Polls return `[]` | Wait for the monthly quota reset (the [[Budget Governor]] throttles to avoid hitting 0) |
| **[[PinnacleScraper\|Pinnacle]]** | API down (5xx) / transport error | `with_backoff` retries (up to 3 attempts, full-jitter); on exhaustion `httpx.HTTPError` caught | Missing poll cycle, retries next interval | Automatic on next cycle |
| **[[TipsportScraper\|Tipsport]]** | curl_cffi 4xx (any status ≥ 400) | `resp.status_code >= 400` in `_fetch_market()` | Falls back to JSESSIONID-sync from [[CloudflareSession\|Playwright]] cookie jar, then Playwright | Automatic |
| **[[TipsportScraper\|Tipsport]]** | [[CloudflareSession\|Playwright]] CF solve fails | `CloudflareSolveError` → `ConnectionError` | `_consecutive_failures` increments | After 3 failures: full session reset |
| **[[TipsportScraper\|Tipsport]]** | [[CloudflareSession\|Playwright]] CF solve timeout | Cookie poll exhausted at 30s | Same as above | Same as above |
| **[[TipsportScraper\|Tipsport]]** | [[CloudflareSession\|Playwright]] API request timeout | `asyncio.wait_for` exceeds 60s | `ConnectionError`, increments `_consecutive_failures` | After 3 failures: full session reset |
| **[[TipsportScraper\|Tipsport]]** | API structure changed | `KeyError`/`IndexError` in `parse_response` | Caught in `fetch_odds` error handler; returns `[]` | Manual code fix required |
| **[[TipsportScraper\|Tipsport]]** | NHL 3-way regulation market returned | Row text does not match 2-way `WINNER_FINAL_DECISION` filter | Rows dropped (fail-closed); `last_fetch_error_count` surfaced in health log | Confirm NHL profile's `tipsport_moneyline_matchview` + `superSportId=23` |
| **[[TipsportScraper\|Tipsport]]** | All 3 markets fail (consecutive) | `_consecutive_failures >= 3` | Full session reset, one cycle lost | Automatic (new session) |
| **[[TipsportScraper\|Tipsport]]** / Chromium | Chromium missing / version-drifted | Startup `chromium_available()` preflight WARNING with `playwright install chromium` remediation | Tipsport silently down (per-poll launch failure storm, no other signal) | `playwright install chromium`; Playwright pinned `~=1.60.0` |
| **Odds API schedule** | Transient 429 / 5xx / transport error | `OddsApiScheduleSource._fetch_endpoint()` retries via shared `with_backoff` (up to 3 attempts, full-jitter, `Retry-After` clamped to 30s), then raises `ScheduleFetchError`; `ScheduleService.refresh()` catches and preserves prior cache | No schedule refresh; uses stale slate (does NOT zero out coverage) | Automatic retry at next refresh interval |
| **Odds API schedule** | Hard 4xx (401/403/other) or malformed-JSON 2xx body | Source raises `ScheduleFetchError` immediately (no retry); `ScheduleService` keeps the prior slate | Same as transient | Fix `ODDS_API_KEY` in `.env`; restart |
| **Odds API schedule** | Game finalizes outside the `/scores?daysFrom=3` window (pipeline down >3 days, or a suspended game drops from the feed) | Startup stranded-game WARNING (`Store.get_stranded_games`: still non-final >96h past tipoff) | Finals never arrive: game sits `live`/`scheduled` forever, its value_bets result-less, polluting reporting denominators | Manual one-shot `oddsiq delete-zombie-games [--dry-run]` — deletes stuck-`live` games AND their snapshots/bets (children first, one transaction); deletion over annotation decided 2026-07-10 |

### Scraper Failure Cascades

The most dangerous failure pattern is **silent degradation** — the pipeline continues running but produces no useful data:

1. **Pinnacle budget exhausted:** Every poll returns `[]`; edge detection never fires; no alerts. The pipeline looks alive but is functionally dead. Detection requires monitoring the `Odds API budget: NNN remaining` log line. The [[Budget Governor]] reserve floor (`BUDGET_RESERVE_FLOOR`, default 1500) is the primary guard — it throttles cadence well before the budget reaches zero.

2. **Tipsport session death spiral (mitigated):** Before `SESSION_MAX_AGE`, expired Cloudflare cookies caused a 90-minute dead cycle (3 failures × 30 min each). Now mitigated by proactive refresh at 20 min. See [[First Game Night|Production Observations]].

3. **Tipsport API structure change:** Returns `[]` with no explicit error. The pipeline keeps polling Pinnacle but has nothing to compare against. Detected by `"fetched 0 odds"` in health logs (also normal if Tipsport has no live games in a sport).

4. **Browser version drift (mitigated):** An unpinned Playwright bump on 2026-05-30 left the bundled Chromium binary missing, taking Tipsport silently down for ~18 h (per-poll `BrowserType.launch` failure storm, no other signal). Mitigated: Playwright is pinned `~=1.60.0` and `Scheduler` runs a startup `chromium_available()` preflight (`oddsiq/scrapers/preflight.py`) that logs a loud WARNING with the `playwright install chromium` remediation when Tipsport is enabled. Pinnacle is pure REST and is unaffected.

**Mitigation — Coverage-Gap Alerts:** All three silent-degradation patterns are caught by `CoverageMonitor.check_gaps()`, which tracks per-game matched-coverage timestamps and emits a `Coverage gap:` WARNING log when a scraper has not matched a warm/hot-tier game for >10 min (log + dashboard feed only — the phone carries real-money events only since the 2026-08-10 rebrand). The check reads in-memory match timestamps (not DB rows) — so snapshot dedup (stable odds → no new rows) doesn't mask a stuck scraper. See [[Health Monitoring#Coverage-Gap Alerts]].

---

## 9.2 Database Failures

| Failure | Detection | Impact | Recovery |
| --- | --- | --- | --- |
| Disk full | `aiosqlite` raises `OperationalError` | Uncaught → crashes main loop → `except Exception` catches, logs, continues | Free disk space; data loss for that cycle |
| Corrupt database | Various SQLite errors | Pipeline crashes repeatedly | Delete `oddsiq.db`, restart (loses history) |
| Locked database (external reader) | WAL handles most cases | Extremely unlikely with single-writer | Kill conflicting process |
| Migration `apply()` raises mid-run | Exception propagates from `_apply_one()` after transaction rollback | Startup aborts with non-zero exit; no partially-applied `schema_version` row | Restore from `./backups/oddsiq-*-startup.db` (taken before migrations ran), fix the migration code, retry |
| Migration file missing `apply()` symbol | `RuntimeError` raised from `_discover()` with the offending filename | Startup aborts before any DB write | Add the `apply(conn: sqlite3.Connection) -> None` function and retry |

### Schema Migrations

Fourteen forward-only migrations have shipped: `000_baseline.py` through `013_scope_value_bet_dedup_by_exclusion.py`. See [[Migrations]] for the full list and runner internals, and [[Backup]] for the safety net.

---

## 9.3 Network Failures

| Failure | Detection | Impact | Recovery |
| --- | --- | --- | --- |
| Total internet outage | All HTTP calls fail | Pipeline logs errors, sleeps, retries | Automatic when connectivity returns |
| DNS failure | `httpx` timeout | Same as outage | Automatic |
| [[TipsportScraper\|Tipsport]] IP ban | Persistent 403 even from [[CloudflareSession\|Playwright]] | Infinite session reset loop (3 failures → reset → 3 failures → reset) | VPN or IP change required |

### The IP Ban Loop

The most operationally dangerous network failure is a Tipsport IP ban. The scraper cannot distinguish between a transient Cloudflare challenge (~2s to solve) and a reputation-based block (persistent; no amount of retrying helps). Both manifest as 403 responses.

**Detection:** Repeated `Tipsport: Playwright failed to solve Cloudflare` in logs with no successful fetches between resets.

**Recovery:** Manual intervention — VPN, IP change, or waiting for Cloudflare to de-list the IP.

---

## 9.4 Logic Failures

| Failure | Detection | Impact | Recovery |
| --- | --- | --- | --- |
| Team name not normalized | `normalize_team` returns `None` | Match skipped silently (debug log) | Add mapping to `team_mappings` in the sport's [[SportProfile]] |
| Fuzzy match wrong team | No automated detection | Wrong odds matched to wrong game → false edge | Review fuzzy match warnings in logs; convert to explicit mapping |
| Pinnacle odds stale | No staleness detection | Edge based on outdated fair line | Accepted; `CLOSING_FRESHNESS_MINUTES` gate protects CLV from stale closing snapshots |
| NHL 3-way market misfetched | `parse_response` drops rows not matching 2-way filter; `last_fetch_error_count` | Zero or partial NHL moneyline coverage | Confirm `WINNER_FINAL_DECISION` + `superSportId=23` in NHL profile; health log shows `structural-fallback rows: K` |

### Team Name Normalization Risks

[[Team Name Normalization]] uses a 3-tier matching system (per-sport canonical set from `SportProfile`):

1. **Exact canonical match** — full team names in `profile.canonical_teams` (NBA 30, NHL 32, MLB 30 teams).
2. **Bookmaker-specific mapping** — e.g., `"LA Lakers"` → `"Los Angeles Lakers"` (from `profile.team_mappings`).
3. **Fuzzy fallback** — `rapidfuzz.process.extractOne` with `WRatio` scorer, threshold 85%.

The fuzzy fallback is the dangerous tier — a wrong match produces false edges. Watch `Fuzzy matched ... (score: NN)` log messages; any score below 90 should be investigated and converted to an explicit alias.

### Pinnacle Staleness

For [[CLV Calculation]], the closing-snapshot freshness gate (`CLOSING_FRESHNESS_MINUTES`, default 30) protects against the worst case: if no Pinnacle snapshot for a game lands within `freshness_minutes` of effective tipoff, `mark_closing_snapshots()` leaves all rows at `is_closing=0` and CLV is skipped rather than computed against stale data.

---

## 9.5 Process Failures

| Failure | Detection | Impact | Recovery |
| --- | --- | --- | --- |
| `SIGKILL` / crash | No detection | Unclean shutdown, no session summary, connections left open | Restart; lose in-memory state (see below) |
| `SIGINT` / `SIGTERM` | Signal handler in `cli.async_main()` calls `scheduler.stop()` | Clean shutdown: summary printed to console, connections closed (incl. the shared [[Telegram Notifications\|Telegram]] client) | Manual restart |
| Python exception in main loop | `except Exception` in `Scheduler.run()` | Logged, 10s sleep, loop continues | Automatic |
| Out of memory | OS kills process | Same as `SIGKILL` | Investigate memory leak (likely Playwright) |

### In-Memory State Lost on Crash

On unclean shutdown, the following in-memory state is lost:

| State | Impact | Mitigation |
| --- | --- | --- |
| `_handled_live` | Restored on startup from `odds_snapshots` where `is_closing = 1` (`Store.get_handled_live_game_ids()`) | Fully mitigated |
| `_last_poll_time` | Immediate poll on restart | Harmless |
| `_last_schedule_refresh` | Immediate schedule refresh on restart | Harmless |
| `_coverage_gap_alerted` | Cleared on restart; re-alerts if scraper still silent | Harmless — correct re-detection |
| `SessionStats` | Summary covers only post-restart period | Cosmetic loss |
| Pinnacle `_budget_remaining` | Falls back to `meta` table on probe failure; self-corrects after first successful poll | One wasted call worst-case |

### Graceful Shutdown Sequence

On `SIGINT` (Ctrl+C) or `SIGTERM`:

1. Calls `scheduler.stop()` and wakes the sleep loop.
2. Stops the betting bridge — the **first** step inside `Scheduler._shutdown()`, and only when `BETTING_ENABLED=1` constructed one; no placement command is left in flight while the rest tears down.
3. Forces a final [[Budget Governor]] persist.
4. Prints session summary to console.
5. Closes the shared [[Telegram Notifications|Telegram]] HTTP client — the summary is console/log only; no message is sent (2026-08-10 bettor-facing rebrand).
6. Closes the schedule source (The Odds API httpx client).
7. Closes all scraper sessions (Playwright, httpx).
8. Closes the [[SQLite Store|database]] connection.
9. Writes the shutdown backup and prunes raw responses.
10. Exits.

Steps 2–7 are each isolated in their own `try`/`except` inside `Scheduler._shutdown()`, so an early failure (e.g. a Telegram timeout) can't skip the later scraper/browser cleanup and leak a Chromium process past Docker's stop grace period.

Steps 8–9 run in the [[CLI Entry Point]] `finally` block, outside `Scheduler._shutdown()`, so they fire even if `scheduler.run()` raised.

If shutdown hangs (e.g., Playwright cleanup stalls), a second `SIGINT` will force-kill. There is no timeout on the shutdown sequence itself.
