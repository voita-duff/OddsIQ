---
tags:
  - type/runbook
  - layer/operations
  - status/complete
  - phase/p0
source-files:
  - oddsiq/pipeline/scheduler.py
  - oddsiq/pipeline/coverage_monitor.py
  - oddsiq/pipeline/budget_governor.py
  - oddsiq/cli.py
related:
  - "[[Pipeline Services]]"
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[CLV Calculation]]"
  - "[[Telegram Notifications]]"
  - "[[Budget Governor]]"
---

# Health Monitoring

Day-to-day operation confirms the pipeline is running. These metrics confirm it is running **correctly** over weeks and months.

## Per-Night Checklist

| Metric | Healthy | Investigate |
| --- | --- | --- |
| `odds_snapshots` rows added | Variable — snapshot dedup bumps `captured_at` on unchanged rows. Stable-line nights add far fewer rows than volatile-line nights. Better signal: the per-scraper `Scraper health:` log line (`matched_games` field) tells you whether the scraper is actually matching the watched game each cycle. | `matched_games=0` on consecutive polls during warm/hot tier — a coverage-gap alert fires automatically (see below). |
| `value_bets` flagged | 0–10 per night (varies by sport and market conditions) | > 20 suggests threshold is too low or data quality issue |
| CLV populated on flagged bets | All bets from completed games whose closing snapshot was within `CLOSING_FRESHNESS_MINUTES` of tipoff | NULL `clv_pct` on resolved bets when the freshness gate did NOT reject the closing snapshot means CLV capture failed (different from the gate intentionally skipping). |
| Results resolved | All bets from final games | NULL `result` on final games means resolver didn't run |
| Pinnacle budget consumed | Governed by [[Budget Governor]] (`ODDS_API_MONTHLY_BUDGET`=20000). Heartbeat reports `budget_throttle=N` (0=nominal, 1–3=throttling). | `budget_throttle=3` means the governor is near the reserve floor (`BUDGET_RESERVE_FLOOR`=1500); Pinnacle cadence is maximally throttled |
| `heartbeat:` log line | Emitted every `SCHEDULER_HEARTBEAT_SECONDS` (default 60s) — a steady cadence confirms the scheduler loop is alive (it also drives dashboard liveness) | A gap longer than the cadence means the loop is stalled or the process died |
| Missed-gameday warnings at startup | None | A first-run `Pipeline has N historical final game(s)` warning records the current baseline. Later `Pipeline missed N new final game(s) with zero snapshots` warnings indicate pipeline downtime during a tipoff. Known misses are remembered in `meta`, so only newly discovered ones produce a fresh warning. Logged only — no phone alert (2026-08-10 rebrand). |

### Heartbeat Format

Each heartbeat line carries: `state=` (`polling` / `between-game-windows` / `idle-no-games`), `games_tracked=N`, `seconds_since_last_poll=N`, and `budget_throttle=N` (0–3). Log it to a file and `grep heartbeat` to trace pipeline liveness.

```
2026-06-01 03:14:00 +0000 [INFO] oddsiq.pipeline.scheduler: heartbeat: state=polling games_tracked=6 seconds_since_last_poll=87 budget_throttle=0
```

### Per-Scraper Health Format

Each poll cycle emits one `Scraper health:` line per scraper (Pinnacle + Tipsport):

```
Scraper health: name=pinnacle duration_ms=412 snapshots=18 matched_snapshots=18 matched_games=9 other_games=0 unmatched_snapshots=0 errors=0 unsafe_start_snapshots=0 sharp_source_age_max_ms=54000 missing_source_updates=0 missing_event_ids=0 event_id_mismatches=0
```

`matched_games` shows whether the watched games received prices. `unsafe_start_snapshots>0` means Tipsport listings were rejected because their start was unsafe. For Pinnacle, `sharp_source_age_max_ms` is the upstream feed age (not request latency); values above `PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS × 1000`, any `missing_source_updates`, any `missing_event_ids`, or unexpected `event_id_mismatches` need investigation. These conditions fail closed for betting.

### Nightly Health Dashboard SQL

```sql
-- Snapshot volume and game coverage by night (all sports)
SELECT
    date(captured_at) as night,
    sport,
    count(*) as snapshots,
    count(distinct game_id) as games_tracked
FROM odds_snapshots
GROUP BY date(captured_at), sport
ORDER BY night DESC, sport LIMIT 21;
```

```sql
-- Value bet flagging, CLV population, and result resolution by night
SELECT
    date(flagged_at) as night,
    bookmaker,
    count(*) as bets_flagged,
    sum(case when clv_pct IS NOT NULL then 1 else 0 end) as clv_populated,
    sum(case when result IS NOT NULL then 1 else 0 end) as results_resolved,
    round(avg(clv_pct), 2) as avg_clv
FROM value_bets
GROUP BY date(flagged_at), bookmaker
ORDER BY night DESC LIMIT 14;
```

## Weekly/Monthly Trends

| Metric | What to Watch | Action |
| --- | --- | --- |
| Average CLV trend | Should be stable or positive over time | Declining CLV may mean Tipsport is adjusting lines faster |
| Tipsport curl_cffi failure rate | Occasional 4xx on a POST is normal (synced via browser session); concerning if the full fallback-to-Playwright path fires every poll | Consistent browser fallback means curl_cffi impersonation is stale — check Cloudflare challenge type |
| Playwright fallback frequency | Should be a minority of requests | If > 50% of requests need Playwright, `TIPSPORT_IMPERSONATE` may need updating |
| Odds API budget pace | `budget_throttle=0` for most of the month | `budget_throttle=1` or higher mid-month means current cadence overshoots — the governor compensates automatically, but investigate if it reaches level 3 |
| Database file size | ~1 MB/month at current single-book volume | Sudden jumps mean a scraper is returning unexpected data volumes |
| Raw JSON storage | ~5–10 MB/month in `./data/raw/` | Review and compress old months quarterly; auto-pruned after `RAW_RESPONSE_KEEP_DAYS` (default 14) |

## Degradation Signals

These indicate the system is technically running but producing unreliable data:

- **CLV consistently near 0% over 30+ bets:** [[Edge Detection]] is not finding real value. May need to lower threshold or the market has become more efficient.
- **CLV consistently negative:** The [[Vig Removal]] model is systematically biased, or Pinnacle data is stale. Stop and investigate.
- **Tipsport returning 0 matched_games during warm/hot tier:** The API structure may have changed or the session is broken. Run `oddsiq tipsport-smoke` to diagnose without touching the pipeline. See [[TipsportScraper]].
- **Session resets every cycle:** Cloudflare has likely changed their challenge. May need to update the Playwright browser version or anti-detection patches. See [[CloudflareSession]].

## Coverage-Gap Alerts

Active monitoring for the most dangerous silent failure mode: a scraper missing the watched game during warm/hot tier, where [[Edge Detection]] fires and closing lines are captured.

**Trigger:** Any scraper's last matched coverage for a watched game is more than 10 minutes old while that game is in warm or hot tier (0–2h to tipoff, not yet final/live).

**Why a matched-coverage timestamp and not `odds_snapshots`:** Snapshot dedup skips rows whose `(odds, line)` match the previous poll. "No new rows in 10 min" does NOT mean the pipeline is broken — stable odds are normal. The check reads `CoverageMonitor._last_game_coverage_success[(game_id, scraper_name)]`, which updates only when returned odds actually match the watched game. A scraper that returns odds for a future game is fetch-healthy but still a coverage gap for the active game.

**Alert format — a WARNING log line, nothing else.** The phone is reserved for real-money events since the 2026-08-10 rebrand ([[Telegram Notifications]]); this line is what you `grep` for and what the dashboard activity feed parses:

```
WARNING oddsiq.pipeline.coverage_monitor: Coverage gap: game_id=649 scraper=pinnacle tier=hot minutes_stale=12
```

**Dedup:** One alert per `(game_id, scraper_name)` gap. The dedup key is cleared on the scraper's next successful match for that game, so a recurring gap alerts again. Across five failed polls in a row, you get exactly one WARNING line, not five.

**Throttle:** The check runs at most once every 2 minutes.

**Scope:** Warm + hot tier only (0 < time-to-tipoff ≤ 2h). Far-tier games (>2h) are intentionally silent — Pinnacle is not polled there and edge detection doesn't run, so staleness is expected.

### Response Runbook

| Scraper | First response | If gap persists > 20 min |
| --- | --- | --- |
| `pinnacle` | Run `oddsiq --check-budget` to confirm the active Odds API key still works. If `remaining=0`, the key's monthly cap is hit — wait for the monthly quota reset (the [[Budget Governor]] throttles cadence to avoid hitting 0). | Check Odds API status page. If healthy, restart the pipeline — no in-memory Pinnacle state is process-sticky. |
| `tipsport` | Check logs for `Tipsport: Playwright failed to solve Cloudflare`. If yes, Cloudflare has likely rotated challenges — restart to force a fresh session. Run `oddsiq tipsport-smoke` for a targeted probe. | See [[CloudflareSession]] troubleshooting and [[TipsportScraper]] Cloudflare notes. |

## See Also

- [[Pipeline Services]] — the main loop whose health is being monitored
- [[Budget Governor]] — Odds API quota throttling and throttle levels
- [[PinnacleScraper]] — budget tracking and API health
- [[TipsportScraper]] — Cloudflare bypass health
- [[CLV Calculation]] — the key metric for model validation
- [[Telegram Notifications]] — bettor-facing only (real placements and their results); none of the health signals on this page are messaged
