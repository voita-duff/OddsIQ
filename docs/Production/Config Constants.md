---
title: Config Constants
date: 2026-06-04
tags:
  - type/production-data
  - status/complete
  - layer/configuration
---

# Configuration Constants

Canonical reference for every module-level constant in `oddsiq/config.py`. Non-obvious values carry their rationale inline — several were set or adjusted based on observed behavior from [[First Game Night]].

## API Key and Endpoints

A single Odds API key serves BOTH Pinnacle odds and the schedule/scores source (balldontlie is retired). There is no multi-key pool / rotation — one strong key under the 20k/month budget is enough, and the [[Budget Governor]] throttles Pinnacle cadence to fit the quota.

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `ODDS_API_KEY` | `""` | env var | The Odds API credential. The CLI exits if it is unset. Used by `PinnacleScraper` (odds) and `OddsApiScheduleSource` (schedule/scores). |
| `ODDS_API_BASE_URL` | `https://api.the-odds-api.com/v4` | literal | The Odds API root |

## Telegram Notifications

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | `""` | env var | Optional — alerts silently no-op if empty |
| `TELEGRAM_CHAT_ID` | `""` | env var | Optional — same behavior |
| `TELEGRAM_BOT_TOKEN_2` | `""` | env var | Optional second recipient. Every notification is mirrored to both accounts; fully independent of account 1 (set both `_2` vars to enable; a half-filled pair is ignored). |
| `TELEGRAM_CHAT_ID_2` | `""` | env var | Optional — chat id for the second recipient (see `TELEGRAM_BOT_TOKEN_2`). |

## Database

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `DATABASE_PATH` | `./oddsiq.db` | env var | SQLite file path |

## Edge Detection

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `EDGE_THRESHOLD` | `0.03` (3%) | env var | Covers ~1% model uncertainty + ~0.5% execution slippage + buffer. [[First Game Night]] avg edge was 5.0%, suggesting 3% isn't too tight. Lower to 2% for aggressive strategies, raise to 5% for conservative. |
| `PREGAME_MAX_PLAUSIBLE_EDGE` | `0.08` (8%) | env var | Pregame plausibility cap — a stale-price sanity cap. Calibrated 2026-07-10 on settled MLB data: pregame edges ≥8% went 2W–9L (−63% flat ROI) while showing INFLATED CLV (+5 to +26) — CLV grades the same stale entry price against the close, so it cannot catch the mirage. Bets at/above the cap are still recorded and settle (the cap stays validatable) but are stamped `excluded_reason='implausible_edge_pregame'`: hidden from every reporting surface, never counted in session stats, never alerted. `<=0` disables. |
| `PREGAME_MIN_BOOK_OVERROUND` | `1.0` | env var | Soft-book coherence floor: if Tipsport's OWN two-way implied sum (1/home + 1/away) is below this, the book is arbing against itself — impossible on a fresh coherent book (Tipsport pregame overround runs ~1.04–1.07), so one side must be stale. Every edge from that market is stamped `excluded_reason='incoherent_book_pregame'` (recorded, not alerted), even sides individually below the plausibility cap. Would have caught all 3 historical both-sides-+EV games, incl. the 37% "edge" that lost. `<=0` disables. |
| `PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS` | `300` s | env var | Maximum age of Pinnacle's upstream `last_update` at observation (`observed_at − source_updated_at`), so a response that ARRIVES now but carries a minutes-old feed snapshot is caught. **Calibration provenance:** the author's 14-day VM raw-archive replay (ad hoc, not in the repo — p99 ≈ 54 s, isolated 10–15 min stalls); an independent check on the local June archive agreed on the shape — pregame p99 ≤ 31 s (hot-tier p99 283 s), post-tipoff stalls up to 898 s. 300 s therefore sits ~6–10× the pregame p99 while still rejecting a real stall. Stamped `stale_sharp_source`; missing/invalid timestamps become `sharp_source_unknown`. `<=0` disables the age cap, but unknown timestamps still fail closed. The same slug is re-checked at dispatch as a `placed_bets` skip reason (`now − source_updated_at`, which adds queue latency). |
| `PREGAME_REQUIRE_COMPLETE_BOOK_MARKET` | `1` (on) | env var | Completeness guard: a Tipsport listing carrying an execution id must also carry BOTH sides of its two-way market, else every edge from it is stamped `excluded_reason='incomplete_book_market'` — without both sides there is neither a fair comparison nor a self-coherence check to trust. Set `0` to stop stamping it without a rebuild. Independent of the coherence floor: `PREGAME_MIN_BOOK_OVERROUND<=0` disables only the floor, not this. |

## CLV Closing Snapshot Freshness

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `CLOSING_FRESHNESS_MINUTES` | `30` | env var | `Store.mark_closing_snapshots(game_id, freshness_minutes)` refuses to mark a snapshot as `is_closing=1` if its `captured_at` is older than this many minutes before effective tipoff. Default matches the hot-tier polling boundary so any cleanly-running game has a fresh closing snapshot. Stale snapshots stay `is_closing=0` and CLV is skipped — see [[OddsSnapshot Lifecycle]]. |

## Raw Data Storage

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `RAW_DATA_DIR` | `./data/raw` | env var | Where per-poll JSON responses are saved |

## Bookmaker + Sport Selection

Tipsport is the single soft book. Enabled sports (NBA / NHL / MLB / WNBA) are code-level (`ENABLED_SPORTS` in `oddsiq/sports/profiles.py`), not env vars.

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `BOOKMAKER_TIPSPORT_ENABLED` | `1` (True) | env var | The only remaining bookmaker flag; Tipsport is the sole soft book. |

## Tipsport API

Per-sport competition id / superSportId / moneyline matchView now live on each [[SportProfile]] (`oddsiq/sports/profiles.py`), not here.

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `TIPSPORT_API_URL` | `https://www.tipsport.cz/rest/offer/v2/offer` | literal | Reverse-engineered offer endpoint |

## Scraper Tuning

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `MAX_CONSECUTIVE_FAILURES` | `3` | literal | Balances quick recovery (1 would be too aggressive with transient errors) against dead cycles (5 would waste many minutes) |
| `CF_SOLVE_TIMEOUT` | `30.0` s | env var | Observed Cloudflare solves complete in 2-10 s; 30 s catches genuine blocks without premature timeout |

## Odds API Budget

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `ODDS_API_MONTHLY_BUDGET` | `20000` | literal | Current monthly cap (upgraded from the 500 free tier). First-ever-start default only — replaced by real values from `probe_budget()` or the `meta` table fallback on every subsequent restart. See [[PinnacleScraper]] and [[Budget Governor]]. |
| `BUDGET_RESERVE_FLOOR` | `1500` | env var | Hard reserve. The [[Budget Governor]] drops to its most conservative cadence (level 3) once `remaining` falls at or below this value, regardless of the burn projection — the primary guard against overspend near month-end. |
| `BUDGET_THROTTLE_SLACK` | `1.10` | env var | Projected-overshoot slack: the governor only begins throttling once projected month-end spend exceeds `ODDS_API_MONTHLY_BUDGET × this` (10% headroom above the naive projection). |
| `ODDS_API_QUOTA_RESET_DAY` | `1` | env var | Day-of-month the Odds API monthly quota resets. The API exposes no reset-date header, so it is configured here. Default 1 = calendar-month reset. Override if your billing/signup date differs — wrong only mis-times the soft early-warning throttle (levels 1-2); the reserve floor uses the authoritative `remaining` counter. |

## Pinnacle Markets Scope

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `PINNACLE_MARKETS` | `h2h` | env var | Moneyline only; 1 quota per request. The system is moneyline-only in practice (Tipsport emits only moneyline). Widening to `h2h,spreads,totals` yields Pinnacle-only spread/total snapshots with no Czech counterpart. See `docs/Decisions/Markets.md`. |

## Polling Tiers

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `POLL_TIER_FAR` | `1800` s (30 min) | literal | Time-to-tipoff 2-6h — Tipsport-only, no Pinnacle; lines too soft. (>6h is not polled at all.) |
| `POLL_TIER_WARM` | `300` s (5 min) | literal | Time-to-tipoff 1-2h — every 5 min; rare early edges; cheap baseline. [[Edge Detection]] runs in warm AND hot tiers (≤2h to tipoff). |
| `POLL_TIER_HOT` | `60` s | literal | Time-to-tipoff < 60m — every 60s (tightened 90→60 on 2026-08-27, funded by the live-tier removal); 89% of historical value bets land here. [[Edge Detection]] runs in hot AND warm tiers. |

## Live Scout (REMOVED 2026-08-27)

The live-edge subsystem — post-tipoff polling, `LiveScoutService`, markout
classification, the `live_edge_candidates` table, and all nine `POLL_TIER_LIVE`/
`LIVE_*` knobs — was fully removed 2026-08-27. Post-tipoff games are unpollable;
game lifecycle is schedule/clock-driven. Dataset, code and a re-implementation guide are archived offline.

## Schedule Refresh

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `SCHEDULE_REFRESH_INTERVAL` | `10800` s (3 h) | env var | Full-sweep / idle cadence — the all-enabled-sports `/scores` sweep that discovers new games in sports with nothing pollable (and the refresh interval when no game is active). Raised `3600 → 10800` 2026-08-08: slates post days ahead, and each sweep costs ~2 credits per enabled sport |
| `SCHEDULE_GAME_WINDOW_REFRESH` | `900` s (15 min) | env var | Active-window cadence — fires when at least one game is in a polling tier. Raised `300 → 900` 2026-07-22 (schedule calls were ~half the monthly quota burn) |
| `SCHEDULE_COMPLETION_CHECK_INTERVAL` | `900` s (15 min) | env var | Game-final detection cadence (`check_completed_games`), scoped to sports with a live game, 2 credits per check per sport. Was a hardcoded `300`s until 2026-08-08 — measured as the largest single Odds API burn (MLB live 10–14 h/day → ~250–350 credits/day). Only delays settlement + result Telegrams; CLV is unaffected (closing snapshots are marked at tipoff) |

## Auto-betting (P1 engine — inert unless enabled)

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `BETTING_ENABLED` | `0` | env var | Master gate: off = BettingService/bridge not constructed. On requires `BETTING_BRIDGE_TOKEN` (scheduler refuses to start without it) |
| `BETTING_BRIDGE_HOST` | `127.0.0.1` | env var | WebSocket bridge bind address (compose overrides for the Chrome container) |
| `BETTING_BRIDGE_PORT` | `8765` | env var | Bridge port the extension dials into |
| `BETTING_BRIDGE_TOKEN` | `""` | env var | Shared secret in the extension's `hello`; bad token → close 4401 |
| `BETTING_CONFIRM_ENABLED` | `0` | env var | Display-only mirror of the money gate for the dashboard safety chain. The AUTHORITATIVE value is baked into the Chrome container's `config.js` at ITS start (`docker/chrome/entrypoint.sh` reads the same `.env`), so this can drift from the running extension if one container restarted against an edited `.env` — the dashboard labels it "(env)" |
| `BETTING_MAX_STAKE_CZK` | `500` | env var | Hard per-bet ceiling; the runtime meta stake is CLAMPED to it |
| `BETTING_MAX_BETS_PER_DAY` | `10` | env var | Immutable daily decision-count ceiling. The meta knob may lower it but is clamped here if edited higher. |
| `BETTING_MAX_OPEN_STAKES_CZK` | `500` | env var | Immutable ceiling for total pending/placed/unknown stakes across all sports; bounds the matching meta knob. |
| `BETTING_MAX_DAILY_LOSS_CZK` | `300` | env var | Immutable daily realized-loss ceiling; bounds the matching meta knob. |
| `BETTING_PLACEMENT_TIMEOUT_SECONDS` | `60` | env var | No extension result within it → status `unknown` → auto-disarm (never blind-retry). Must exceed the extension's worst-case successful path (enforced caps nav 5 + odd 20 + slip 5 + stake read-back 2 + ticket 12 = 44 s plus an uncapped receipt-read tail — this timeout is the only backstop on a hung extension) — raised from 10 on 2026-09-06 |
| `BETTING_FAIR_MAX_AGE_SECONDS` | `120` | env var | Stale-fair interlock: never bet against a sharp reference older than this |
| `BETTING_TIPOFF_BUFFER_SECONDS` | `120` | env var | Never start a placement this close to first pitch — Tipsport flips the offer in-play around the scheduled start (skip reason `too_close_to_tipoff`; `<=0` disables) |

Runtime money knobs live in the **meta table** (CLI `oddsiq betting`, or the `/betting` dashboard controls): `betting_armed`, `betting_flat_stake_czk` (20), `betting_max_bets_per_day` (10), `betting_max_open_stakes_czk` (500), `betting_daily_loss_limit_czk` (300), `betting_disarm_reason`. The engine validates finite/non-negative values and clamps all four exposure knobs to their immutable env ceilings, including when meta is edited outside the dashboard.

| `DASHBOARD_CONTROLS_ENABLED` | `0` | env var | Gates the `/betting` dashboard write affordances. Off = hidden + POST endpoints 403. On still requires the two credentials below; empty credentials fail closed with 503. |
| `DASHBOARD_CONTROLS_USERNAME` | `""` | env var | App-level HTTP Basic-auth username for the three money-affecting POST endpoints. With controls enabled AND both credentials set, `GET /betting` itself challenges (realm `OddsIQ betting controls`) so the browser holds credentials before the first htmx POST; the other tabs stay public |
| `DASHBOARD_CONTROLS_PASSWORD` | `""` | env var | App-level HTTP Basic-auth password (compared with `secrets.compare_digest`). Use a long unique value. Mutations also require a same-origin `Origin` or `Referer` — matched against `X-Forwarded-Host` when present, else `Host`, so a reverse proxy must forward the browser's host — which blocks cross-site form submits. Basic auth over plain HTTP is not a substitute for TLS |

## Monitoring

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `HEARTBEAT_PING_URL` | `""` (disabled) | env var | Dead-man ping: the scheduler GETs this URL (healthchecks.io-style) on every heartbeat (~60s); the service pages the operator when pings STOP. Best-effort — failures never disturb the poll loop (WARNING at most once per 10 min) |

## Backup

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `BACKUP_INTERVAL_HOURS` | `24` h (`<=0` disables) | env var | Periodic in-process backup cadence — closes the "container never restarts → never backs up" hole (Jul 22→Aug 8 2026 ran 17 days with zero backups). Reuses the WAL-safe startup routine; files named `...-periodic.db` |
| `BACKUP_ENABLED` | `1` (True) | env var | Master switch; `0` disables both startup and shutdown hooks |
| `BACKUP_DIR` | `./backups` | env var | Destination directory for snapshot files |
| `BACKUP_KEEP_DAILY` | `7` | env var | Number of most-recent distinct days to keep |
| `BACKUP_KEEP_MONTHLY` | `6` | env var | Number of most-recent distinct months to keep |

## Raw Response Retention

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `RAW_RESPONSE_RETENTION_ENABLED` | `1` (True) | env var | Master switch; `0` skips both startup and shutdown pruner hooks |
| `RAW_RESPONSE_KEEP_DAYS` | `14` | env var | Delete dated subdirectories under `RAW_DATA_DIR/{scraper}/` whose date is older than this many days from today (UTC). Prevents the `data/raw/` tree from growing unbounded during 24/7 operation. |

## Logging and Heartbeat

Rotating file logging written by `setup_logging()` in `cli.py`. The P3 dashboard tails this file for its activity feed and pipeline-liveness signal.

| Constant | Default | Source | Notes |
| --- | --- | --- | --- |
| `LOG_FILE` | `./logs/oddsiq.log` | env var | Rotating log path; the dashboard tails it. |
| `LOG_FILE_MAX_BYTES` | `10485760` (10 MB) | env var | `RotatingFileHandler` rotation size. |
| `LOG_FILE_BACKUP_COUNT` | `5` | env var | Number of rotated log files kept. |
| `LOG_FILE_ENABLED` | `1` (True) | env var | Master switch for file logging. |
| `SCHEDULER_HEARTBEAT_SECONDS` | `60` | env var | Cadence of the scheduler `heartbeat:` log line so the dashboard's liveness check works even with no games tracked. |

## Related Non-config.py Constants

Some tuning values live on the consumer class rather than in `config.py`. They're documented here for reference but should be edited in their source file.

| Constant | Value | Location | Notes |
| --- | --- | --- | --- |
| `SESSION_MAX_AGE` | `1200.0` s (20 min) | `TipsportScraper` class attribute (`oddsiq/scrapers/tipsport.py`) | Proactive Cloudflare session refresh threshold. Cookies observed to expire at ~20-30 min; refreshing at 20 min provides margin. Mitigates the long dead cycle from [[First Game Night]]. |

## Rationale Notes

**`MAX_CONSECUTIVE_FAILURES` (3):** Threshold before the [[CloudflareSession]] is destroyed and recreated from scratch. Setting it to 1 would cause unnecessary session resets on transient network errors; 5 would mean 5 failed full poll cycles before recovery. 3 is the compromise, with `SESSION_MAX_AGE` preventing the old long dead-cycle behavior.

**`EDGE_THRESHOLD` (0.03):** The minimum detected edge to flag a value bet. Breakdown: ~1% model uncertainty (vig removal is an estimate), ~0.5% execution slippage (odds may move between detection and manual placement), plus buffer. The first game night's 5.0% average edge across 5 flagged bets confirms 3% isn't filtering out real value.

## See Also

- [[TipsportScraper]] — uses `MAX_CONSECUTIVE_FAILURES`, `TIPSPORT_API_URL`, and its own `SESSION_MAX_AGE`; per-sport ids live on [[SportProfile]]
- [[CloudflareSession]] — uses `CF_SOLVE_TIMEOUT`
- [[Edge Detection]] — uses `EDGE_THRESHOLD`
- [[Polling Tiers]] — defines tier ranges bound to `POLL_TIER_FAR`, `POLL_TIER_WARM`, `POLL_TIER_HOT`
- [[Budget Governor]] — uses `ODDS_API_MONTHLY_BUDGET`, `BUDGET_RESERVE_FLOOR`, `BUDGET_THROTTLE_SLACK`, `ODDS_API_QUOTA_RESET_DAY`
- [[Scheduler]] — enforces polling intervals and throttling
- [[Backup and Retention]] — operational view of the `BACKUP_*` constants
- [[PinnacleScraper]] — uses `ODDS_API_KEY`, `ODDS_API_BASE_URL`, `PINNACLE_MARKETS`, `ODDS_API_MONTHLY_BUDGET`
- [[First Game Night]] — source of most tuning observations
