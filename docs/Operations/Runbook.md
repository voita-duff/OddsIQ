---
tags:
  - type/runbook
  - layer/operations
  - status/complete
  - phase/p0
source-files:
  - oddsiq/cli.py
  - oddsiq/pipeline/scheduler.py
related:
  - "[[Pipeline Services]]"
  - "[[CLI Entry Point]]"
  - "[[Troubleshooting]]"
  - "[[Health Monitoring]]"
---

# Runbook

Operational guide for running OddsIQ during NBA/NHL/MLB/WNBA game nights.

## Starting a Session

```bash
cd ~/dev/OddsIQ          # repo root
source .venv/bin/activate

# Verify config
cat .env | grep -E "ODDS_API_KEY|TELEGRAM"

# Sanity-check your Odds API key without starting the pipeline
oddsiq --check-budget
# Prints: "Odds API budget: X used / Y remaining (Z total)" and exits.

# Start the pipeline (entry point installed by `pip install -e .`)
oddsiq
# Equivalent: python3 -m oddsiq.cli
```

To launch the pipeline + dashboard + browser together in one step, use the `make start` launcher.

## Operational subcommands

```bash
oddsiq --check-budget                                              # standalone budget probe; exits without starting the scheduler
oddsiq cleanup-closing-flags                                       # one-shot: clear is_closing flags + re-mark under current freshness gate
oddsiq cleanup-closing-flags --dry-run                             # preview the cleanup without writing
oddsiq mark-excluded --sports nba,wnba --from <ISO> --until <ISO> --reason <slug>   # stamp known-bad rows out of reporting
oddsiq mark-excluded --sports nba --from <ISO> --until <ISO> --reason <slug> --dry-run   # preview per-table counts without writing
oddsiq audit-bookmakers                                            # per-book audit (snapshot volumes, value bets, CLV, gaps)
oddsiq audit-bookmakers --bookmaker tipsport --since YYYY-MM-DD    # filtered audit for a single book / window
oddsiq audit-bookmakers --json                                     # machine-readable output
oddsiq tipsport-smoke                                              # pre-flight for Tipsport — runs the scraper once, no DB writes (default sport: nba)
oddsiq tipsport-smoke --sport nhl                                  # same, for NHL
oddsiq tipsport-smoke --sport mlb                                  # same, for MLB
```

(The `live-discover` and `live-scout-report` subcommands were removed 2026-08-27 with the live-edge subsystem; the dataset they reported on is archived offline.)

`cleanup-closing-flags` is a data-hygiene tool from the closing-snapshot integrity track. Use it once if you have historical rows where `is_closing=1` was set under the old (no-freshness) logic and you want them re-evaluated under the current `CLOSING_FRESHNESS_MINUTES` gate. Per-game work is two transactions; mid-game failure leaves all rows for that game at `is_closing=0` (a SAFE state — recoverable by re-running).

`tipsport-smoke` is a standing pre-flight tool. It runs the TipsportScraper once outside the scheduler, without touching the DB. Use it before relying on Tipsport after an outage or a Playwright update. Accepts `--sport nba|nhl|mlb|wnba` (default `nba`); an unknown sport key prints the valid list and exits 1.

`audit-bookmakers` reports snapshot volumes, value bets, CLV, gaps, and coverage quality; `--bookmaker <name> --since <date>` is the per-book follow-up tool.

`mark-excluded` is a one-shot data-quality tool. It stamps `excluded_reason` on `value_bets` (by `flagged_at`) for the named sports inside a half-open `[--from, --until)` UTC window, hiding known-bad rows from every reporting surface (dashboard, audit) without deleting them. (It stamps `value_bets` only since 2026-08-27 — the `live_edge_candidates` table it also used to stamp was dropped with the live-edge subsystem.) All four of `--sports`, `--from`, `--until`, `--reason` are required; bounds are normalized to UTC (naive = UTC; an explicit offset is converted). Re-running is a no-op (already-excluded rows are skipped). `--dry-run` prints per-table counts without writing. Built for the 2026-06 basketball matchView incident — set `--until` to the *deploy* time of fix `a1669a4` on the host running the pipeline, not the commit date. Take a backup first (the startup backup in `BACKUP_DIR` is the safety net).

**Live scout — removed 2026-08-27.** There is no post-tipoff polling: in-progress games are lifecycle-tracked only (`game_live:`/`game_final:` logs, finals via `/scores`, closing snapshots marked at tipoff). The live-scout table/commands are gone; the dataset is archived offline.

## Expected Startup Output

```
HH:MM:SS [INFO] oddsiq: Backup saved: ./backups/oddsiq-YYYYMMDD-HHMMSS-startup.db
HH:MM:SS [INFO] oddsiq.db.migrations: Schema: version N (up to date)
HH:MM:SS [INFO] oddsiq.pipeline.scheduler: Scheduler starting...
HH:MM:SS [INFO] oddsiq.pipeline.scheduler: Odds API budget: X used / Y remaining (Z total)
HH:MM:SS [INFO] oddsiq.pipeline.schedule_service: Schedule refreshed: N games (Odds API recent + upcoming)
```

If `N = 0`: no NBA/NHL/MLB/WNBA games in the Odds API recent+upcoming window (yesterday through a few days out). Check the sport schedules and current season dates.

**Watch for the missed-gameday audit warning** at startup:

```
WARNING oddsiq: Pipeline has N historical final game(s) with zero snapshots: external_id (away @ home, YYYY-MM-DD), ... (recorded as known; Telegram suppressed)
WARNING oddsiq: Pipeline missed N new final game(s) with zero snapshots: external_id (away @ home, YYYY-MM-DD), ...
```

The historical form appears when the current zero-snapshot backlog is baselined. The "new" form means pipeline downtime spanned a real tipoff — those games will never have CLV. Existing historical misses are remembered in `meta`, so only newly discovered misses WARN rather than the same stale list on every restart. Neither form sends Telegram (the missed-gameday alert was deleted in the 2026-08-10 rebrand; the "Telegram suppressed" wording survives verbatim inside the log strings).

## What to Watch

### Healthy Indicators

- `Schedule refreshed: N games (Odds API recent + upcoming)` appears every hour (or every 5 min during active game windows)
- `Odds API budget: X used / Y remaining (Z total)` on the heartbeat log and after each Pinnacle probe
- `Tipsport: session warm-up complete` once per session init (~every 20 min)
- `Scraper health: name=tipsport duration_ms=... snapshots=... matched_games=...` once per poll cycle per enabled scraper
- `heartbeat: state=polling games_tracked=N seconds_since_last_poll=N budget_throttle=0` every 60s
- `value_bet: ...` in the log (console prints a `VALUE BET` block) when a clean edge is detected; `value_bet_suspect:` / `SUSPECT VALUE BET` when a pregame guard stamps it
- `clv: ...` at tipoff and `result: ...` at game final, per bet

**On the phone.** Telegram is bettor-facing only since the 2026-08-10 rebrand — expect a message ONLY for: a bet placed, a bet REJECTED/FAILED, a placed bet won/lost (real Kč + running `Total:`), auto-betting stopped by the circuit breaker or loss cap, Tipsport login lost/restored, and the rate-limited "skipped — not logged in" alert. Detections, CLV, results on paper bets, coverage gaps, budget warnings, and the session summary send **nothing** — those live in the log and on the [[Dashboard]]. A quiet phone on a night with detections is normal, not a fault. See [[Telegram Notifications]] for the complete list.

### Warning Signs

| Log Message | Meaning |
| --- | --- |
| `Tipsport: curl_cffi 403/401 on {sport}, syncing browser API session` | Normal occasionally, concerning if every poll |
| `Tipsport: session expired, re-creating` | Session died, ~30s recovery incoming |
| `Tipsport: Playwright failed to solve Cloudflare` | Serious — Cloudflare may have changed |
| `Odds API budget probe failed` (at startup) | Probe couldn't reach `/v4/sports`; pipeline falls back to last persisted values from the `meta` table |
| `Budget preflight FAILED` | Odds API budget is gone — read the budget line before starting |
| `heartbeat: budget_throttle=1` (or 2, 3) | Budget governor has throttled Pinnacle cadence; see [[Budget Governor]] |
| `Coverage gap: game_id=... scraper=... minutes_stale=...` | A scraper hasn't matched a warm/hot-tier game in >10 min — WARNING log only, no Telegram |
| `Could not normalize team name` | New abbreviation, needs mapping added to [[Team Name Normalization]] |

### Error Signs

| Log Message | Meaning |
| --- | --- |
| `Error in scheduler main loop` | Exception in core loop, auto-recovered but investigate |
| `Pinnacle fetch failed` | Odds API issue, transient |
| Repeated `Tipsport fetch failed (consecutive: N)` | Cloudflare winning — see [[Troubleshooting]] |
| `Browser preflight FAILED — Tipsport cannot run this session. Fix: playwright install chromium` | Playwright binary missing or version-drifted; Pinnacle still runs |

## Graceful Shutdown

Press **Ctrl+C** (or send `SIGTERM`). The [[CLI Entry Point]] shutdown sequence runs:

1. `signal.SIGINT` handler calls `scheduler.stop()` — sets `_stop_event`, wakes the [[Pipeline Services]] sleep loop
2. Betting bridge stopped — the **first** step inside `Scheduler._shutdown()`, and only when `BETTING_ENABLED=1` constructed one; no placement command is left in flight while the rest tears down
3. Final [[Budget Governor]] persist (forced)
4. `Scheduler.print_summary()` prints the session summary to console/log — no phone message (2026-08-10 rebrand)
5. Shared [[Telegram Notifications|Telegram]] HTTP client closed
6. Schedule source closed (The Odds API httpx client)
7. Scraper sessions closed (Playwright, httpx)
8. Database connection closed (`store.close()`)
9. **Shutdown backup** fires, then raw responses are pruned — captures this session's clean final state
10. Exit

Steps 2–7 each sit in their own `try`/`except` inside `Scheduler._shutdown()`, so an early failure (e.g. a Telegram timeout) can't skip the later scraper/browser cleanup and leak a Chromium process past Docker's stop grace period. Steps 8–9 run in the [[CLI Entry Point]] `finally` block, outside `Scheduler._shutdown()`, so they fire even if `scheduler.run()` raised. See [[Failure Modes]] for the same sequence with the failure-isolation detail.

## Dashboard

Start the read-only web dashboard in a second terminal:

```bash
oddsiq dashboard            # localhost:8080
oddsiq dashboard --port 9000
```

Open http://localhost:8080 (or your chosen port). Five user-facing pages — `/upcoming` (24h multi-sport schedule with bet-window badges), `/live` (game-night console; its coverage matrix is pregame-only since 2026-08-27), `/betting` (auto-betting engine + execution ledger; `/bets` redirects here), `/analysis` (decision-support), `/settings` (read-only config including Budget Governor panel). The dashboard reads `oddsiq.db` (WAL — concurrent readers OK) and tails `logs/oddsiq.log`.

When dashboard betting controls are enabled, set `DASHBOARD_CONTROLS_USERNAME` and a long unique `DASHBOARD_CONTROLS_PASSWORD`; empty credentials make all control POSTs return 503. The app requires HTTP Basic auth and same-origin form submissions. Keep reverse-proxy authentication as defense in depth when binding publicly.

After an extension/pipeline timeout, leave the engine disarmed and inspect Tipsport bet history. Reconcile the exact decision only after proving the outcome:

```bash
oddsiq betting --reconcile PLACED_BET_ID --as-placed --accepted-odds 2.08 --receipt-id TICKET_ID --reason verified_in_tipsport_history
oddsiq betting --reconcile PLACED_BET_ID --as-rejected --reason absent_from_tipsport_history
```

The command never retries and never re-arms. Re-arm separately only after the ambiguity is resolved.

Stop with Ctrl+C; the main pipeline is unaffected.

For the full design and implementation details see [[Dashboard]].

## See Also

- [[CLI Entry Point]] — the entry point that starts the pipeline
- [[Pipeline Services]] — the main loop and orchestration logic
- [[Troubleshooting]] — diagnosing specific issues
- [[Health Monitoring]] — longer-term health metrics and checks
- [[Budget Governor]] — Odds API quota throttling
