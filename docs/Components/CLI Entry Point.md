---
tags:
  - component/cli
  - status/complete
  - phase/p0
  - layer/orchestration
  - type/component
source-files:
  - oddsiq/cli.py
related:
  - "[[Pipeline Services]]"
  - "[[SQLite Store]]"
  - "[[Telegram Notifications]]"
  - "[[Runbook]]"
---

# CLI Entry Point

Entry point for the OddsIQ pipeline. Argparse-driven dispatch over a default run, a budget-probe flag, and 7 subcommands (data hygiene, data-quality exclusion, zombie-game deletion, auto-betting control, a per-book audit, a smoke pre-flight, and the dashboard). Wires startup/shutdown backups, runs pending migrations, validates env, and hands off to [[Pipeline Services]]. (The `live-scout-report` and `live-discover` subcommands were deleted 2026-08-27 with the live-edge subsystem — `oddsiq/audit/live_scout.py` went with them; code archived at git tag `archive/live-edges-2026-08-27`.)

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/cli.py` | `main()` | Argparse entry point; dispatches to `cleanup_closing_flags`, `mark_excluded`, `delete_zombie_games`, the betting control, `audit_bookmakers`, `tipsport_smoke`, `run_dashboard`, `check_budget`, or `async_main` |
| `oddsiq/cli.py` | `_parse_args()` | Defines `--check-budget` plus 7 subcommands. `betting` supports arm/disarm/status and evidence-required reconciliation of a pending/unknown placement. |
| `oddsiq/cli.py` | `async_main()` | Main pipeline path: env validation → startup backup → raw-response pruning → migrations → store init → missed-gameday audit → scheduler |
| `oddsiq/cli.py` | `check_budget()` | Standalone Odds API quota probe; exits without starting the scheduler |
| `oddsiq/cli.py` | `cleanup_closing_flags(dry_run)` | One-shot hygiene: clears + re-marks `is_closing` flags under the current freshness gate |
| `oddsiq/cli.py` | `mark_excluded(*, sports, from_iso, until_iso, reason, dry_run)` | One-shot data-quality flag: normalizes the window to UTC, validates it, runs migrations, calls `Store.mark_rows_excluded` to stamp `excluded_reason` on `value_bets` for the given sports (value_bets only since the 2026-08-27 live-edge removal). `--dry-run` reports counts without writing |
| `oddsiq/cli.py` | `audit_bookmakers(*, bookmaker, since, as_json)` | Per-book audit: reads the DB and prints the per-book report via `oddsiq/audit/bookmakers.py`. Pure read-only. |
| `oddsiq/cli.py` | `tipsport_smoke(sport)` | Tipsport pre-flight: runs `TipsportScraper.fetch_odds(today, profile)` once outside the scheduler, prints market counts + sample moneyline events. Never opens the DB. |
| `oddsiq/cli.py` | `audit_missed_gamedays(store)` | Surfaces final games with zero snapshots; baselines known misses in `meta` and WARNs only about newly discovered ones. Logged only — no phone alert |
| `oddsiq/cli.py` | `setup_logging()` | INFO-level format with rotating file handler at `logs/oddsiq.log`; secrets-redacting filter; idempotent |
| `tests/test_cli.py` | — | 50+ tests: arg parsing (per subcommand), env validation, budget probe path, cleanup dry-run, missed-gameday audit, audit-bookmakers happy path + filter round-trip + JSON, tipsport-smoke healthy/degraded exits, logging hygiene, and secret redaction |

## Invocations

```bash
oddsiq                                                            # default — full pipeline
oddsiq --check-budget                                             # standalone Odds API quota probe
oddsiq cleanup-closing-flags                                      # one-shot is_closing cleanup
oddsiq cleanup-closing-flags --dry-run                            # preview without writes
oddsiq mark-excluded --sports nba,wnba --from 2026-06-02T00:00:00+00:00 --until 2026-06-12T00:00:00+00:00 --reason basketball_3way_matchview_2026_06           # stamp known-bad rows
oddsiq mark-excluded --sports nba --from ... --until ... --reason ... --dry-run                                                                                # preview counts without writes
oddsiq delete-zombie-games --dry-run                              # preview stuck-'live' game deletions
oddsiq delete-zombie-games [--grace-days 3]                       # DELETE zombie games + dependent rows
oddsiq betting [--arm | --disarm]                                 # auto-betting engine control; bare = status
oddsiq betting --reconcile 42 --as-placed --accepted-odds 2.08 --receipt-id T123 --reason verified_in_tipsport_history
oddsiq betting --reconcile 42 --as-rejected --reason absent_from_tipsport_history
oddsiq audit-bookmakers                                           # per-book audit report
oddsiq audit-bookmakers --bookmaker tipsport --since YYYY-MM-DD  # filtered audit
oddsiq audit-bookmakers --json                                    # machine-readable JSON
oddsiq tipsport-smoke                                             # Tipsport pre-flight; no DB writes (default: nba)
oddsiq tipsport-smoke --sport nhl                                 # Tipsport pre-flight for a specific sport
oddsiq dashboard [--host 127.0.0.1] [--port 8080]                # P3 read-only FastAPI dashboard (separate process)
```

The `--sport` flag for `tipsport-smoke` accepts `nba`, `nhl`, `mlb`, `wnba`, or `nfl`. An invalid value prints the valid list to stderr and exits 1. (The `live-scout-report` and `live-discover` invocations were removed 2026-08-27 with the live-edge subsystem.)

Reconciliation is deliberately manual. First inspect Tipsport bet history, then resolve exactly one `placed_bets.id`. Only `pending`/`unknown` rows are eligible; `--as-placed` requires accepted odds and receipt id, while `--as-rejected` requires a reason. The command never retries the wager and leaves the engine disarmed until a separate explicit `--arm`.

## Startup Sequence (`async_main`)

1. **Logging setup** — INFO level, timestamped console format + rotating file handler at `logs/oddsiq.log`. Secrets-redacting filter attached to both root logger and file handler.
2. **Env validation** — exits non-zero if `ODDS_API_KEY` is not set in `.env`.
3. **`RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)`** — ensures raw response directory exists before scrapers fire.
4. **Startup backup** (if `BACKUP_ENABLED`) — `backup.backup_database(..., "startup", ...)` via `asyncio.to_thread` BEFORE opening any aiosqlite connection. WAL-safe; captures the prior session's final state. See [[Backup]].
5. **Raw-response pruning** — `scraper_base.prune_raw_responses()` removes dated subdirs older than `RAW_RESPONSE_KEEP_DAYS` (default 14). Non-fatal — errors are logged and startup continues.
6. **Migrations** — `migrations.run(DATABASE_PATH)` via `asyncio.to_thread` applies pending forward-only schema migrations. See [[Migrations]].
7. **Store initialize** — `Store(DATABASE_PATH).initialize()` opens aiosqlite, sets per-connection PRAGMAs (no `CREATE TABLE` here — that is owned by migrations).
8. **Missed-gameday audit** — `audit_missed_gamedays(store)` checks final games with zero snapshots (visibility for pipeline downtime; no retry). Existing misses are recorded in `meta`, so the `Pipeline missed N new final game(s) with zero snapshots` WARNING fires only for newly discovered ones. Logged only; no phone alert since the 2026-08-10 rebrand.
9. **Scheduler construction + signal handlers** — `signal.SIGINT`/`SIGTERM` bound to `scheduler.stop()`.
10. **`scheduler.run()`** — hands off to the async event loop.

## Shutdown Sequence (the `finally` block)

1. `store.close()` — flushes pending aiosqlite writes, closes the connection.
2. **Shutdown backup** (if `BACKUP_ENABLED`) — captures this session's clean state.
3. **Raw-response pruning** — repeated at shutdown (belt-and-suspenders).
4. `telegram.close()` — closes the shared httpx client (idempotent — no-op if already closed by scheduler shutdown).
5. Process exits.

A failed backup never blocks shutdown — `backup_database()` swallows errors and returns `None`.

## `--check-budget` Path

Bypasses migrations, store init, and the scheduler. Calls `PinnacleScraper.probe_budget()` against the free `/v4/sports` endpoint and prints `X used / Y remaining (Z total)`. Exits `0` on success, `1` if no Odds API key is configured or the probe fails.

## `cleanup-closing-flags` Path

One-shot data-hygiene tool. For each `final` game, clears all `is_closing=1` flags then re-marks under the current freshness gate (`CLOSING_FRESHNESS_MINUTES`). Per-game work is two transactions; mid-game failure leaves `is_closing=0` across all rows for that game — a safe state, recoverable by re-running. `--dry-run` computes counts without writes.

## `mark-excluded` Path

One-shot data-quality tool. Runs migrations, opens `Store`, calls `Store.mark_rows_excluded(sports=..., from_iso=..., until_iso=..., reason=..., dry_run=...)`, which stamps `excluded_reason` on `value_bets` (by `flagged_at`) for the named sports inside the half-open `[from, until)` window. (It stamps `value_bets` **only** since 2026-08-27 — the `live_edge_candidates` table it used to stamp as well was dropped with the live-edge subsystem.) Rows already excluded are skipped, so re-runs are no-ops. The window bounds are **required** arguments (no defaults) and are normalized to UTC — a naive ISO value is assumed UTC, an explicit offset (e.g. a CET deploy time) is converted to its UTC instant — then validated (`--until` must be after `--from`). `--dry-run` reports per-table counts without writing.

Built for the 2026-06 basketball matchView incident (Tipsport's regulation 3-way prices were scraped as the NBA moneyline, fixed in `a1669a4`). Stamped rows vanish from every reporting surface (dashboard queries, per-book audit) but are preserved for audit and remain inert in the pipeline lifecycle. **Operationally:** the `--until` bound is the fix's *deploy* time on the host running the pipeline, not the commit date — pin it on that host before stamping.

## `delete-zombie-games` Path

One-shot destructive data-hygiene tool. Runs migrations, opens `Store`, selects games permanently stuck `status='live'` — tipoff + the sport's `live_backstop_hours` (the game-lifecycle window on the [[SportProfile]]) + `--grace-days` (default 3, the Odds API scores window) has passed, so the final can never arrive (game finalized during a multi-day outage, or a suspended game dropped from the feed) — and calls `Store.delete_games_with_children(ids)`, which deletes `placed_bets`, `value_bets`, `odds_snapshots`, then the `games` rows (children first, one transaction; `live_edge_candidates` left the cascade when migration 011 dropped it, 2026-08-27). Logs one `zombie game_id=...` line per selected game plus per-table totals. `--dry-run` reports the same selection and counts without writing.

Deletion (not annotate-and-exclude) was the operator decision of 2026-07-10: these games' bets can never settle, so removing them keeps reporting denominators honest. Built for the 25 games stuck `live` after the 2026-06-19..21 outage plus a few earlier rain-suspended MLB games. **Operationally:** run `--dry-run` first and sanity-check the listed games; only `status='live'` games are ever selected.

## `audit-bookmakers` Path

Per-book audit. Runs migrations, opens `Store`, calls `oddsiq.audit.bookmakers.run_audit(store, bookmaker=..., since=...)`, prints the report (snapshot volumes, value bets, CLV by edge bucket, closing quality, coverage-gap proxy, multi-book overlap), closes the store, exits 0. **Pure read-only** — no `INSERT`/`UPDATE`/`DELETE` anywhere in `oddsiq/audit/`. Safe to run mid-game-night.

Flags: `--bookmaker NAME` restricts to one book; `--since YYYY-MM-DD` restricts the audit window; `--json` emits machine-readable JSON.

## `tipsport-smoke` Path

Tipsport pre-flight. Takes `--sport` (default `nba`). Instantiates a fresh `TipsportScraper`, runs `fetch_odds(date.today(), profile)` once, prints elapsed time + snapshot count + fetch error count + per-market counts + the first moneyline events, calls `scraper.close()`, and exits 0 only when Tipsport returns non-empty sane moneyline data within the elapsed threshold. Degraded exits cover scraper internal errors, zero snapshots, zero moneyline rows, odd moneyline count, out-of-range odds, and elapsed time above the smoke threshold. **Never opens the DB**.

## Configuration

| Env var | Default | Required for default run |
| --- | --- | --- |
| `ODDS_API_KEY` | — | **Yes** (also for `--check-budget`) |
| `BOOKMAKER_TIPSPORT_ENABLED` | `1` | Czech soft-book selection |
| `BACKUP_ENABLED` | `1` | Disables backup hooks if set to `"0"` |
| `BACKUP_DIR`, `BACKUP_KEEP_DAILY`, `BACKUP_KEEP_MONTHLY` | see [[Backup]] | Backup tuning |
| `RAW_RESPONSE_RETENTION_ENABLED` | `1` | Prune raw response dirs at startup/shutdown |
| `RAW_RESPONSE_KEEP_DAYS` | `14` | Days to retain raw response dirs |

## See Also

- [[Pipeline Services]] — main loop started by the CLI
- [[Backup]] — startup/shutdown hooks wired here
- [[Migrations]] — schema migration runner invoked here
- [[SQLite Store]] — opened after migrations, closed in `finally`
- [[Telegram Notifications]] — the shared httpx client the `finally` block closes; nothing the CLI itself does is messaged (the missed-gameday audit is log-only)
- [[Runbook]] — operational guide for running game night sessions
