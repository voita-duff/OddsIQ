---
tags:
  - component/dashboard
  - phase/p3
  - status/complete
  - layer/operations
source-files:
  - oddsiq/dashboard/server.py
  - oddsiq/dashboard/queries.py
  - oddsiq/dashboard/log_tail.py
  - oddsiq/dashboard/state.py
related:
  - "[[CLI Entry Point]]"
  - "[[Pipeline Services]]"
  - "[[Budget Governor]]"
  - "[[SQLite Store]]"
  - "[[Health Monitoring]]"
  - "[[CLV Calculation]]"
---

# Dashboard

Local FastAPI + htmx web dashboard for OddsIQ. Five user-facing pages: `/upcoming` (read-only 24h multi-sport schedule with bet-window badges, shipped 2026-06-08), `/live` (game-night console), `/betting` (auto-betting engine + execution ledger; replaced `/bets` 2026-08-09), `/analysis` (decision-support analytics), `/settings` (read-only config + budget-governor state). Reads `oddsiq.db` (via the existing async [[SQLite Store]] — WAL allows concurrent readers) and tails `logs/oddsiq.log` (rotating). It is read-only except for the gated `/betting` controls. Those POST endpoints require `DASHBOARD_CONTROLS_ENABLED=1`, app-level HTTP Basic credentials, and a same-origin request; missing credentials fail closed. An outer reverse-proxy auth layer remains recommended defense in depth.

## Starting

```bash
oddsiq dashboard            # localhost:8080
oddsiq dashboard --port 9000
oddsiq dashboard --host 0.0.0.0  # WARN: no auth — only on trusted networks
```

Run alongside the main `oddsiq` pipeline in a second terminal. SQLite WAL mode handles the concurrent reader cleanly.

## Sport Tab-Bar

Every page carries an **All / NBA / NHL / MLB / WNBA** tab-bar backed by a `?sport=` query parameter — **hidden entirely while only one sport is enabled** (2026-08-14: an All|MLB toggle is dead weight). Selecting a tab reloads the page with `?sport=nba|nhl|mlb|wnba` (or no param for All), and all htmx partials on that page thread the same `sport` through their `hx-get` URLs so the filter survives auto-refresh. `_norm_sport()` in `server.py` validates the value against the registered [[SportProfile]] registry — an unknown sport key is treated as All.

The sport filter maps to a `WHERE sport = ?` clause added by `_sport_clause()` in `queries.py`. For All (no `?sport=`), the clause is omitted entirely so existing queries are unchanged.

## Pages

### `/upcoming` — schedule view

A forward-looking, read-only schedule for the next 24h across every enabled [[SportProfile]]. One htmx section (5-min auto-refresh). Built entirely from the `games` table the pipeline already fills — no Odds API calls.

| Part | What it shows |
| --- | --- |
| Summary tiles (`.cellgrid`) | One tile per enabled sport (incl. zero-game sports); a TOTAL tile only with 2+ sports (single-sport it duplicated the sport tile — dropped 2026-08-14). A **Bet window** tile shows when the first warm window opens ("now" when one is open). |
| Agenda | Scheduled games (`now < tip-off ≤ now+24h`) grouped **Bet window open → Later today → Tomorrow**, with a header row (Tipoff / Sport / Matchup / In / Bet window). Rows whose window is open are highlighted (`tr.window-open`). |

**Bet-window badge** reuses `oddsiq.pipeline.tiers.tier_for`: `HOT` (<60m), `WARM` (1–2h) mean Tipsport-vs-Pinnacle [[Edge Detection|edges]] are detectable now; `opens HH:MM` (= tip-off − 2h) is when the warm window starts. The `?sport=` filter narrows the agenda; the tiles stay all-sports (the overview). Countdowns **tick client-side** every 30s off a `data-tipoff` ISO attribute (2026-08-14 — server-rendered text went up to an hour stale between refreshes).

### `/live` — game-night console

Four sections, each htmx-refreshing on its own interval. All partials accept `?sport=` and thread it through their self-refresh `hx-get`:

| Section | Refresh | Source |
| --- | --- | --- |
| Status strip (pipeline state + updated stamp, per-scraper status, budget bar with burn/day + reset date + month-end projection via `_budget_context`, detections today: clean + guarded counts) | 15s | log file + DB + `meta` |
| Coverage matrix (active warm/hot games × scrapers, tier-aware ✓ / ⚠ / ✗ — **pregame-only** since 2026-08-27) | 30s | `MAX(captured_at)` per `(game, bookmaker)` |
| Activity feed (hybrid DB + LOG, filter chips per event type incl. **guarded** for `value_bet_suspect:` events) | 8s | `value_bets` + `odds_snapshots` (DB) merged with parsed log events; value-bet rows carry matchup + resolved team + tier |
| Raw logs panel (collapsible, last 200 lines, WARNING/ERROR lines color-coded) | 8s when expanded | `logs/oddsiq.log` |

**Coverage matrix tier logic** uses `oddsiq.pipeline.tiers.tier_for` (far/warm/hot — no dashboard-local "medium" hardcode). Far-tier games (>2h to tipoff) and post-tipoff games are excluded — the matrix is **pregame-only** since the live tier's removal (2026-08-27); in-progress games surface via the status-strip live count, the activity feed's `game_live`/`game_final` events, and their `/game/{id}` pages. Staleness thresholds: hot < 5 min, warm < 15 min.

**Status strip** derives pipeline state from the log file (heartbeat freshness, scheduler running/idle/stalled), Scraper health log lines for per-scraper ✓/⚠/✗ icons, and `meta` for persisted budget counts. **With zero pollable games the scraper row is a neutral `idle · <age>`** — red is reserved for "games needed polling and didn't get it" (an 8h-stale ✗ during a normal overnight idle was a false alarm, fixed 2026-08-14). A scraper row also carries an **`unsafe start: N` chip** when the health line's `unsafe_start_snapshots` is non-zero (2026-09-06): those are Tipsport listings whose own start time was missing or untrustworthy, so they were dropped rather than attributed to a game — a silent payload change would otherwise only show as thinning coverage, so it gets its own badge. All feed timestamps render in Prague, dated (`DD.MM HH:MM`) when not today — the feed used to print raw UTC with no dates, so 4-day-old bets read as tonight's. The coverage-matrix empty state names the next game + when its bet window opens (`queries.next_game_window`).

The coverage card also shows Pinnacle upstream source age even when no game row is active, with warning chips for missing timestamps, missing provider event ids, and exact-id mismatches. This distinguishes a fast HTTP request from a genuinely fresh sharp market.

**Shadow detections** (`tier='far'`, NFL far-tier shadow since 2026-09-06) are excluded from every clean/performance figure and surfaced separately: a dim "+N shadow" strip badge, "· N shadow" in the betting overview's today line, and `n_shadow` in the calendar day detail.

### `/betting` — auto-betting engine + execution ledger (replaced `/bets` 2026-08-09)

The P1 auto-betting operator surface (renamed from `/bets`; `/bets` 307-redirects here for old bookmarks). Three htmx sections, each accepting `?sport=`:

| Section | Refresh | Source |
| --- | --- | --- |
| Engine overview | 30s | `queries.betting_overview` + heartbeat/bridge log. **Engine-state banner** (armed = green glow / disarmed amber / disconnected-off-unknown red; a stale heartbeat downgrades to `unknown` via `_engine_state`) + a **6-link safety chain** (enabled → bridge → extension → logged in → armed → confirm gate, from `config` + heartbeat + `log_tail.latest_bridge_state`; a supersede within 30 min appends a `reconnect storm` warn chip). KPI tiles: realized P&L + ROI, at-risk, staked, balance. **Caps progress** bars (bets today x/N, open stakes, day P&L vs loss limit) + today's decision funnel (clean · guarded · skipped · placed). **Write controls** render only when `DASHBOARD_CONTROLS_ENABLED=1` and both app credentials are configured; all mutations require Basic auth + same-origin checks. Stake and aggregate caps are clamped to immutable env ceilings in both the route and engine. |
| Betting decisions (last 25, **skips included** with their qualification reason — 2026-08-14) | 30s | `queries.placed_bets_table` — game (linked to `/game/{id}`), selection, stake, requested→accepted odds, placement latency, status, P/L, receipt/skip-reason/rejection-error |
| Recent value bets (last 15: tier badge, entry odds, edge, CLV, result, Tipsport deep link, game link; a **"show guarded too"** toggle adds guard-stamped rows greyed with their reason) | 30s | `queries.recent_value_bets(include_suspect=...)` joined to `games` |

(The fourth section — the "research, never bet" live-edges panel — was removed 2026-08-27 with the live-edge subsystem.)

A nav-wide **engine-state dot** (`/betting/partials/state-dot`, 60s) shows off/disconnected/disarmed/armed on every page; it renders nothing when no heartbeat ever carried a betting field.

Write endpoints: `POST /betting/arm`, `POST /betting/disarm`, `POST /betting/config`. They return 403 when controls are disabled, 503 when app credentials are missing, 401 for bad Basic auth, and 403 for a missing/cross-origin `Origin`/`Referer` (compared against `X-Forwarded-Host` when a proxy sets it, else `Host` — behind a reverse proxy that does not forward the browser's host, every POST would otherwise 403). The config route sets meta `betting_*` knobs including the sport checkboxes; unknown/disabled sports drop and empty falls back to `mlb`. These are the dashboard's only writes.

**`GET /betting` challenges too** (2026-09-06) when controls are enabled AND credentialed: the page returns 401 with `WWW-Authenticate: Basic realm="OddsIQ betting controls"`, so the browser prompts on a top-level navigation and then attaches the credentials to every same-origin htmx request from it. Without the page challenge the first Arm click would be an XHR that 401s, and htmx does not swap a non-2xx response — the button would silently do nothing. One realm serves the page and the POSTs (browsers cache Basic credentials per origin+realm). Every other GET — the other tabs, the `/betting` partials, the `/bets` redirect — stays open, and with no credentials configured `/betting` stays public read-only. `htmx:responseError` renders a banner so a rejected control POST is visible rather than silent.

### `/analysis` — decision-support

Six sections, all at 5-min refresh, all sport-filtered. The CLV-trend self-refresh carries `?days=` + `?sport=`; the merged performance self-refresh carries `?month=` (incl. the literal `all`) + `?sport=`. (The 2026-08-08 overhaul removed the redundant results-detail section and the Pinnacle scoreboard card; the 2026-08-14 overhaul merged monthly performance + live value into ONE month-selected section, merged edge distribution + calibration into one table, and added the profit curve; the 2026-09-06 overhaul added the activity calendar and made month tabs calendar-true.)

| Section | What it shows |
| --- | --- |
| Big numbers (4 tiles) | total bets, resolved, avg CLV (n=K with CLV), win rate + avg entry odds (resolved-only), overall flat-1u ROI |
| Activity calendar (new 2026-09-06) | Wall-calendar month grids from the first collected odds through today, bucketed by **Prague** day (a 23:30 UTC detection is "tonight"). Cell shade = clean detections that day (guarded-only days render faint), accent outline = a real bet placed, caution dot = a failed attempt, ★ = a system milestone. Milestones merge a curated deployment list (`oddsiq/dashboard/milestones.py`: guards, engine, rebrand, NFL, live-edge removal, wrong-game fix, login fixes…) with data-derived markers (first odds, first detection, first placement attempt, first bet placed), so "when did the app start working / when was auto-betting introduced" reads off the grid. Clicking a day loads `partials/day_detail.html` (`queries.day_detail`): every detection with its Prague time, teams, tier, odds vs fair, edge, guard reason, betting decision (placed/attempt/skip reason), result + CLV + real Kč. `queries.activity_calendar` |
| Performance (merged 2026-08-14) | ONE chip row (**All time** + one chip per calendar month from the first value bet through the CURRENT month — empty months included, current month default; before 2026-09-06 a month with zero clean bets vanished, which read as "the app stopped") drives the value-bets block: flat-1u profit hero, W-L-P, win rate, avg CLV + edge. `queries.monthly_performance(month="all")` aggregates all-time. (The live-scout tradeable-set block below it and the monthly live column were removed 2026-08-27 with the live-edge subsystem.) |
| Profit curve (new 2026-08-14) | Chart.js cumulative flat-1u equity line over the full calendar range (`queries.profit_curve`), gap-bridging, zero-origin fill |
| CLV trend | Chart.js dual line (nightly raw + 7-day calendar rolling), period selector All / 30d / 7d. Labels span the full calendar range with nulls on no-bet days: the nightly line breaks at gaps (`spanGaps:false`) so outages (e.g. 2026-06-20→07-06) render as visible holes; the rolling line bridges |
| Per-book scoreboard | SOFT books only (Tipsport), rendered as one compact strip per book (no card-in-card): snapshots, games, days active, first→last, bets, avg edge / CLV. The Pinnacle reference row was dropped 2026-08-08 |
| Edge buckets — volume & calibration (merged 2026-08-14) | one table over the same buckets that used to be two sections: volume bar + flagged count + resolved + break-even vs actual win% + colored Δ (break-even = `100 / entry_odds`), `n < 10` marked `⚠ thin` |

### `/settings`

Read-only panels (reworked 2026-08-14 to be registry-driven and truthful):

- **Budget governor** — usage bar, quota used/remaining, burn/day since reset, reset date + days left, projected total at reset (warn when over), throttle level, and the **effective cadence at the current throttle level** (`budget_governor.throttle_intervals(level)` — showing the base `hot 90s` while L2 actually polls at 120s was misinformation) alongside the base cadence.
- **System** — package version, Python version, DB schema migration, latest heartbeat state + age, Telegram configured boolean.
- **Grouped config sections** (Detection / Polling & schedule / Auto-betting / Pipeline & ops) — generated per-request from `_settings_config_sections()` so the list can't drift from `config.py`. Includes the pregame guards, all schedule cadences, every betting gate (`BETTING_ENABLED`, `BETTING_CONFIRM_ENABLED` labeled "(env)", max stake, bridge host:port, timeouts, `DASHBOARD_CONTROLS_ENABLED`), `ENABLED_SPORTS`, ping/backup/retention knobs. (The Live scout section disappeared 2026-08-27 — its config knobs were deleted with the live-edge subsystem.)
- **Paths** — DB / logs / raw responses / backups sizes, file counts. No secrets rendered (only configured-vs-not booleans).

### `/game/{id}` — per-game drill-down (new 2026-08-14)

Answers "why did this flag?" without ad-hoc SQL. Header: matchup, sport, Prague tipoff, status/score, snapshot counts. Two Chart.js panels (one per selection) plot **Tipsport vs Pinnacle moneyline odds over time** from `odds_snapshots` (unified Prague time labels, gap-bridging) with orange **flag markers** at each value-bet detection. Below: every `value_bets` row on the game — suspect rows greyed with their guard reason. Game cells across the dashboard (betting decisions, recent value bets, coverage matrix) link here. Backed by `queries.game_detail`; unknown id → 404.

## Architecture

The dashboard is a separate `uvicorn` process (started by the `oddsiq dashboard` CLI subcommand). Both processes share:

- `oddsiq.db` via SQLite WAL (one writer, many readers).
- `logs/oddsiq.log` via the rotating file handler in [[CLI Entry Point]] `setup_logging`.

The dashboard module lives at `oddsiq/dashboard/`:

- `server.py` — FastAPI app (`create_app()`), route handlers, helpers. Routes: `/` (redirect to `/live`), `/upcoming`, `/upcoming/partials/board`, `/live`, `/live/partials/*`, `/betting`, `/betting/partials/{overview,state-dot,placed-bets,value-bets}`, `POST /betting/{arm,disarm,config}` (gated), `/bets` (redirect to `/betting`), `/analysis`, `/analysis/partials/*` (incl. `calendar` and `day?date=YYYY-MM-DD`), `/settings`.
- `queries.py` — read-only async SQL functions over `Store._db`. Uses `_sport_clause()` for all sport filtering.
- `log_tail.py` — log tailing, heartbeat / scraper-health / structured-event parsers.
- `state.py` — `derive_pipeline_state(log_path, *, now=None)` combining heartbeat freshness with log recency.
- `templates/` — Jinja2 base + page templates + per-section partials.
- `static/` — vendored Chart.js + htmx + `dashboard.css`.

The scheduler emits structured log events (`heartbeat:`, `value_bet:`, `clv:`, `result:`, `game_live:`, `game_final:`, `Scraper health:`, `Coverage gap:`) that the dashboard's log parser consumes.

## See also

- [[CLI Entry Point]] — `setup_logging` (idempotent rotating file handler) and `oddsiq dashboard` subcommand
- [[Pipeline Services]] — emits the heartbeat and structured event lines
- [[Budget Governor]] — state surfaced on `/settings` via `meta` + heartbeat
- [[SQLite Store]] — the read-only target
- [[Health Monitoring]] — overlaps with `/live` coverage matrix
- [[CLV Calculation]] — source of the metrics shown on `/analysis`
