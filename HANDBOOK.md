# HANDBOOK

Personal cheat sheet for OddsIQ. Read this when you sit down with the project after a break. For deep dives, the Obsidian vault is at `docs/00 - Map of Content.md`.

---

## What this is, in two sentences

Async Python pipeline (multi-sport, live-first) that detects +EV moneyline opportunities by comparing **Tipsport** (the sole soft book) against **Pinnacle** sharp lines via The Odds API, across **NBA, NHL, MLB, WNBA, and NFL** (MLB + NFL enabled since 2026-08-25). The 2026-06-02 refactor shipped: sport-axis fan-out via `SportProfile`, The Odds API as both sharp reference and schedule/scores source (balldontlie retired), pipeline decomposed into focused services, and data-driven far/warm/hot tiers with a budget governor. (The live scout that also shipped then was FULLY REMOVED 2026-08-27 — no post-tipoff polling; dataset archived offline.)

## Project mode — evaluation, not commitment

You are collecting CLV data passively. **P3 (dashboard) has shipped. P4 (resilience) is partially shipped — Docker, Telegram heartbeat, and Odds-API backoff landed 2026-06-09; recovery, log-rotation monitoring, and season-boundary handling are still deferred. You will decide whether to build the remaining P4 items + P5-P6 (auto-betting, Kelly staking) based on accumulated CLV alone.** Bug fixes are fair game; new feature work for still-open phases is not, unless you decide otherwise.

The roadmap's "200 manually placed bets" gate in P5 is a known dead gate — you sleep through 3 AM CET tipoffs. When you actually commit to P5, revise that gate.

---

## Run it

```bash
cd ~/dev/OddsIQ
source .venv/bin/activate
oddsiq                              # run the pipeline (Ctrl+C for session summary)
oddsiq --check-budget               # standalone Odds API budget probe
oddsiq cleanup-closing-flags        # one-shot is_closing reset (rare; --dry-run to preview)
oddsiq mark-excluded --sports nba,wnba --from <ISO> --until <ISO> --reason <slug>   # stamp known-bad rows out of reporting (--dry-run to preview)
oddsiq audit-bookmakers             # per-bookmaker audit report (snapshots, CLV, gaps, overlap)
oddsiq audit-bookmakers --bookmaker tipsport --since YYYY-MM-DD --json  # filtered/machine-readable
oddsiq tipsport-smoke               # Tipsport pre-flight; no DB writes (--sport nba|nhl|mlb|wnba|nfl)
oddsiq dashboard                    # P3 read-only dashboard at http://127.0.0.1:8080
```

Pipeline idles when no games are within the polling window (>6h to tipoff). Leave it running. Restart on crash — only the in-flight game's CLV is lost.

## Key paths

| What               | Where                                           |
| ------------------ | ----------------------------------------------- |
| Database           | `./oddsiq.db` (override: `DATABASE_PATH`)       |
| Backups            | `./backups/` (auto on every startup + shutdown) |
| Raw odds responses | `./data/raw/` (override: `RAW_DATA_DIR`)        |
| Env config         | `.env` (template: `.env.example`)               |
| Vault              | `docs/00 - Map of Content.md`                   |

---

## What's running right now

24/7 passive collection. The scheduler tier-gates polling by time-to-tipoff:

| Tier   | Window         | Pinnacle?            | Edge detection?       | Cadence    |
| ------ | -------------- | -------------------- | --------------------- | ---------- |
| (none) | >6h            | no                   | no                    | not polled |
| far    | 2–6h           | no (NFL shadow: yes) | no (NFL: shadow rows) | 1800s      |
| warm   | 1–2h           | yes                  | yes                   | 300s       |
| hot    | <1h pre-tipoff | yes                  | yes                   | 60s        |

Post-tipoff games are NOT polled (the live tier + live scout were removed
2026-08-27); settlement and CLV run off the schedule clock and `/scores`.

Edge detection (warm + hot) stamps value bets with their tier; warm and hot observations of the same selection coexist as separate DB rows, and since 2026-08-30 BOTH tiers auto-bet (the `selection_already_open` rule prevents the cross-tier double-bet). Tipsport listings are matched to games by team names + a ±2h window on the listing's own start (`dateClosed`) — a next-day series listing must never attach to today's game. NFL additionally runs far-tier (2–6h) SHADOW detection (2026-09-06) — recorded as `tier='far'`, excluded from performance aggregates, never bet — to learn its edge horizon; the edge→betting handoff is two-phase (collected per poll cycle, dispatched earliest kickoff first, re-qualified at dispatch). The `BudgetGovernor` throttles Pinnacle cadence (warm first, hot protected) as the 20,000-request monthly quota tightens.

What's worth glancing at:

- **Telegram** — real-money events ONLY (2026-08-10 rebrand): bet placed, won/lost, rejected/failed attempts, auto-disarm, and (2026-08-30) Tipsport login lost/restored + the rate-limited not-logged-in skip (an armed engine with a dead session is a silent outage, not an ops detail). Everything else is log/dashboard-only.
- **Logs** — `INFO` lines should show schedule refreshes, Pinnacle/Tipsport polls, budget burn-down, heartbeat. `WARNING` is rare and worth reading.
- **Coverage gap warnings** — WARNING log (no Telegram since the 2026-08-10 rebrand) if a scraper goes silent during an active warm/hot window.
- **Dashboard** — `oddsiq dashboard` for game-night console (`/live`), decision-support analytics (`/analysis`), and read-only config (`/settings`).

## Health checks (copy-paste)

```bash
# How many bets resolved? Should grow each game night.
sqlite3 oddsiq.db "SELECT count(*) FROM value_bets WHERE result IS NOT NULL;"

# Average CLV across resolved bets — the metric that matters
sqlite3 oddsiq.db "SELECT round(avg(clv_pct),2) avg_clv, count(*) n FROM value_bets WHERE clv_pct IS NOT NULL;"

# Odds API monthly budget (live probe — works without restarting the pipeline)
oddsiq --check-budget

# Persisted budget snapshot from the meta table
sqlite3 oddsiq.db "SELECT key, value, updated_at FROM meta;"

# Latest game-night summary
sqlite3 oddsiq.db "SELECT date(flagged_at) night, count(*) bets, round(avg(clv_pct),2) avg_clv FROM value_bets WHERE clv_pct IS NOT NULL GROUP BY night ORDER BY night DESC LIMIT 7;"

```

For deeper analysis: `docs/Validation/Useful Queries.md`.

---

## Money and limits

- **The Odds API: 20,000 requests/month.** The `BudgetGovernor` tracks quota burn and throttles Pinnacle cadence (4 discrete levels) as the reserve floor approaches. Keep `PINNACLE_MARKETS=h2h` (1 quota/req) — widening to `h2h,spreads,totals` triples it (and Tipsport is moneyline-only, so spread/total Pinnacle calls produce zero edges anyway). A single `ODDS_API_KEY` (no multi-key pool) — the governor throttles cadence to fit the quota. Decision log: `docs/Decisions/Markets.md`.
- **Schedule source: The Odds API** (`/scores?daysFrom=3`). balldontlie is retired; no `BALLDONTLIE_API_KEY` needed.
- **Tipsport: no auth, no API key.** Cloudflare bypass via Playwright (auto). **One moneyline POST per sport per poll cycle** (not per game). Cookies expire ~20 min — proactive refresh.
- **Telegram: free.** Each recipient is independent — a notification goes to whichever of the primary (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`) and optional second (`TELEGRAM_BOT_TOKEN_2`/`TELEGRAM_CHAT_ID_2`) pairs is fully set; an account-2-only setup works, and with neither set it's skipped silently.

If `--check-budget` shows budget burn is significantly ahead of the monthly pace, the governor's warm-tier throttle kicks in automatically; for extreme depletion the reserve floor (`BUDGET_RESERVE_FLOOR`, default 1500) triggers conservative mode.

---

## CLV gating — when to make the next decision

Stop and analyze when `value_bets` has **50+ rows with CLV data**. After that, the decision tree:

| Signal                                            | Action                                                      |
| ------------------------------------------------- | ----------------------------------------------------------- |
| Avg CLV > +1%, edge accuracy matches expectations | **Go.** Consider P4+ or expanded sport coverage.            |
| Avg CLV > +1% but small sample (<30 bets clean)   | **Continue collecting.**                                    |
| Avg CLV ≈ 0%, data quality good                   | **Pause.** Consider lowering threshold to 2%, collect more. |
| Avg CLV ≈ 0%, Tipsport coverage spotty            | **Fix Tipsport reliability first**, then re-collect.        |
| Avg CLV negative                                  | **Stop.** Vig model or Tipsport feed isn't suitable.        |

Full analysis playbook: `docs/Validation/CLV Analysis Framework.md`. Game-night procedure: `docs/Validation/Validation Guide.md`.

---

## When something breaks

| Symptom                                                 | Cause                                        | Fix                                                                                  |
| ------------------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------ |
| `Tipsport: launching Playwright to solve Cloudflare`    | Normal — JS challenge                        | Auto-solves in 10-30s                                                                |
| `Tipsport: Playwright failed to solve Cloudflare`       | Headless blocked or binary missing           | `playwright install chromium`; check `docs/Components/Scrapers/CloudflareSession.md` |
| WARNING: Chromium binary not found at startup           | Playwright version bump without reinstall    | `playwright install chromium`                                                        |
| `Tipsport: session expired, re-creating`                | 20-min cookie expiry                         | Auto-recreates, no action                                                            |
| `Pinnacle fetch failed: 401`                            | Bad Odds API key                             | Check `ODDS_API_KEY` in `.env`; fails soft to `[]` that cycle.                       |
| `Odds API budget: X used / 0 remaining`                 | Monthly cap hit                              | Wait for the monthly reset; the Budget Governor auto-throttles cadence               |
| `BudgetGovernor: throttle level 3 (CONSERVATIVE)`       | Quota running low                            | Normal; governor widens cadence automatically                                        |
| `Schedule refreshed: 0 games today`                     | No games on any sport tonight                | Not a bug                                                                            |
| No snapshots from a sport for multiple cycles           | Tipsport returned no rows for that sport     | Check `tipsport-smoke --sport <key>`; may be off-season or Tipsport layout change    |
| Coverage gap WARNING in logs                            | Scraper silent during active warm/hot window | Check `docs/Operations/Troubleshooting.md`                                           |
| Pipeline crashes                                        | Various                                      | Restart `oddsiq`. In-flight game CLV is lost.                                        |

Deeper: `docs/Operations/Troubleshooting.md`, `docs/Architecture/Failure Modes.md`.

---

## Open issues to know about

- **Stake field always None** (`docs/Known Issues/Open - Stake Field Always None.md`) — `value_bets.stake` stays NULL until P6 Kelly. By design.

---

## Gotchas (the ones that will bite you again)

- **Everything is async.** Forgetting `await` is the #1 mistake.
- **`remove_vig()` argument order matters** — returns `(fair_a, fair_b)` matching input. Swap = wrong CLV.
- **Edge detection is tier-gated.** Fires in warm (1–2h) + hot (<1h). Far tier (2–6h) is silent — lines aren't sharp yet. Exception: NFL's far tier records SHADOW detections (never bet).
- **Tipsport is moneyline-only.** One POST per sport per cycle. `edge.py` contains latent spread/total comparison code, but nothing feeds it in production.
- **Games must exist before odds.** FK constraint — `upsert_game()` first.
- **`captured_at` semantic shift on 2026-04-27.** Post-fix is request-init time, bumped on dedup hits ("last seen"). Pre-fix rows have parse-time or first-observed semantics. Don't aggregate timestamps across the boundary.
- **Scraper fetches are concurrent per sport; DB writes are ordered.** `PollCoordinator` uses `asyncio.gather()` to fan out Tipsport + Pinnacle per sport, then persists and runs edge detection in deterministic order.
- **Coverage-gap dedup: one alert per (game, scraper) until the scraper's next successful match clears it.** The gap-check runs at most once per 2 min — that is a run throttle, not a per-alert re-fire.

---

## Pointers into the vault

When you need depth:

- **Full index:** `docs/00 - Map of Content.md`
- **System overview:** `docs/Architecture/System Overview.md`
- **Pipeline services:** `docs/Components/Pipeline/Pipeline Services.md`
- **Sport profiles:** `docs/Components/Sports/SportProfile.md`
- **Budget governor:** `docs/Architecture/Budget Governor.md`
- **Polling tiers:** `docs/Architecture/Polling Tiers.md`
- **Live-first rationale:** `docs/Decisions/Live-First.md`
- **Decision history:** `docs/Architecture/Architecture Decisions.md`
- **Failure modes:** `docs/Architecture/Failure Modes.md`
- **Operations runbook:** `docs/Operations/Runbook.md`
- **Markets scope decision:** `docs/Decisions/Markets.md`

---

## Status

Multi-sport / live-first refactor shipped 2026-06-02. Pipeline running 24/7.

**For current numbers (final games, value bets, CLV, etc.), query the DB directly:**

```bash
sqlite3 oddsiq.db "
SELECT 'final_games',      COUNT(*)            FROM games       WHERE status='final';
SELECT 'value_bets_total', COUNT(*)            FROM value_bets;
SELECT 'with_clv',         COUNT(*)            FROM value_bets  WHERE clv_pct IS NOT NULL;
SELECT 'with_result',      COUNT(*)            FROM value_bets  WHERE result  IS NOT NULL;
SELECT 'with_both',        COUNT(*)            FROM value_bets  WHERE clv_pct IS NOT NULL AND result IS NOT NULL;
SELECT 'avg_clv_pct',      ROUND(AVG(clv_pct), 2) FROM value_bets WHERE clv_pct IS NOT NULL;
SELECT 'snapshot_range',   MIN(captured_at), MAX(captured_at) FROM odds_snapshots;
"
```
