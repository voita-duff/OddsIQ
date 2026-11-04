# NFL Fifth Sport + Auto-Betting — Design

**Date:** 2026-08-25 · **Status:** approved (user, 2026-08-25) · **Mode:** NFL joins detection AND the auto-betting engine from the first posted slate.

## Decisions (user-confirmed)

1. **NFL auto-betting arms for the first slate** (week 1, opener ~Sep 10 00:15 UTC). Detection runs from deploy; placement starts once the operator ticks NFL into the betting-sports gate (deliberate dashboard action before Sep 10).
2. **Shared caps** — no per-sport caps/stakes. MLB and NFL share `betting_max_bets_per_day` / `betting_max_open_stakes_czk` / `betting_daily_loss_limit_czk`; numbers are tuned at runtime from the dashboard. The shared −300 Kč breaker deliberately bounds *total* daily downside across sports.
3. **Telegram alerts for `rejected` + `failed` placement attempts** — amends the 2026-08-10 rebrand rule from "money moved" to "money moved or an attempted stake failed". `skipped` qualification decisions stay log/dashboard-only.
4. **Betting gate = meta key** `betting_sports` (option A): CSV of sport keys, dashboard-editable checkboxes behind `DASHBOARD_CONTROLS_ENABLED`, CLI-visible. Absent/malformed key defaults to `"mlb"` (conservative).

## Recon evidence (2026-08-25, all live — nothing guessed)

- Tipsport competition **5748** (`/kurzy/americky-fotbal/americky-fotbal-muzi/nfl-5748`) verified in-browser (warmup URL incl. `-muzi` segment works).
- **`superSportId = 2`** for Americký fotbal, captured from Tipsport's own `/rest/offer/v2/offer` response.
- American football's `WINNER_WHOLE_MATCH` tab = **regulation 3-way legs** (`1/1x/x/x2/2`, draw ~14.6) — same trap as basketball. The true 2-way moneyline is **`WINNER_FINAL_DECISION`** ("Vítěz do rozhodnutí", `countOpp=2`), which only populates when requested with `superSportId=2`.
- **Price-verified on real NCAAF games** (same sport engine): FD rows carry 105.8–109.4% two-way implied (proper standalone vig); A/B coherent vs regulation legs (TCU 1.28 FD vs 1.32 regulation-1). Satisfies the mandatory price-verify step from the 2026-06-12 basketball incident.
- NFL game markets **not posted yet** (futures only); the missing-tab response shape confirmed to hit the graceful `TipsportMarketUnavailable` path.
- The Odds API: `americanfootball_nfl` active, **272 events with Pinnacle h2h already posted**; preseason is a separate key (`americanfootball_nfl_preseason`) — no contamination. Week 1: opener Sep 10 00:15 UTC, TNF Sep 11 00:35 UTC, Sunday slate Sep 13 17:00 UTC.
- Tipsport NFL team labels are full English names matching Odds API canonicals → alias dict starts empty; smoke verifies all 32 at market-post time.
- NFL ties (real, unlike MLB) settle via the existing `draw` → push (P&L 0) path — verified in `resolve_bet_result` + flat-P&L conventions.

## Components

### 1. Sport profile + teams

```python
NFL = SportProfile(
    key="nfl", display_name="NFL",
    odds_api_key="americanfootball_nfl",
    tipsport_competition_id=5748,
    tipsport_sport_slug="americky-fotbal",
    tipsport_super_sport_id=2,
    canonical_teams=NFL_TEAMS, team_mappings=NFL_TIPSPORT_ALIASES,
    live_backstop_hours=3.5,
    market_rule="winner_incl_ot_2way",
    tipsport_league_slug="nfl",
    tipsport_moneyline_matchview="WINNER_FINAL_DECISION",
    tipsport_gender_slug="muzi",
    live_tipsport_coverage=False,   # unknown until a week-1 live-discover probe
)
```

`NFL_TEAMS` = 32 canonical names **extracted from The Odds API event data** (1-credit probe, not typed from memory). `NFL_TIPSPORT_ALIASES = {}`. Append `"nfl"` to `ENABLED_SPORTS`. Re-export in `oddsiq/sports/__init__.py`. 🏈 in `telegram._SPORT_EMOJI`.

### 2. Betting gate (meta `betting_sports`)

`BettingService._qualify` first rule becomes: parse the meta key `betting_sports` (CSV → set of keys, absent/blank/malformed → `{"mlb"}`), skip with the existing `sport_not_enabled` reason when `vb.sport` is not in the set. One meta read per decision (same cost class as the existing cap reads). Dashboard `/betting/config` POST accepts per-sport checkboxes rendered from `ENABLED_SPORTS`; unchecked-all falls back to conservative default (`mlb`) rather than empty — an empty gate would silently disable betting, which is what the arm switch is for. CLI `oddsiq betting` (bare status) prints the active betting sports; the dashboard engine banner/safety chain shows them.

### 3. Reject/failed placement Telegram

New `telegram.format_bet_attempt_failed(game, vb, stake, status, reason)` — slim: status word (REJECTED/FAILED), matchup, selection, requested odds, stake, reason. Sent from the two existing result branches in `BettingService` (`rejected`, `failed`) and from the `unknown` timeout branch's existing disarm message (unchanged — disarm already alerts). Best-effort like every send.

### 4. Dashboard touches

- Config form: betting-sports checkboxes (gated by `DASHBOARD_CONTROLS_ENABLED`).
- Engine banner + safety chain: show bet-enabled sports.
- Fix the "All sports" fallback in `queries.py` (`get_profile(sport or "nba")` for backstop/live-coverage): use max `live_backstop_hours` across enabled profiles and `any()` of live coverage when `sport is None` — NFL's 3.5h games would otherwise clip out of the All-view live window.
- Everything else (NFL tab, upcoming tiles, filters) renders automatically from the registry.

### 5. Smoke overround output

`tipsport-smoke` prints a per-game two-way implied sum (overround) line — the hold measurement used at market-post time (~Sep 3–8) to set NFL edge expectations before money moves. Sport-agnostic (useful for every future sport).

### 6. CLI text

`--sport` help strings gain `nfl`; `mark-excluded` help example untouched (it documents a historical action). Smoke/live-discover keep `nba` defaults (any profiled sport is addressable).

## Non-goals

No `EDGE_THRESHOLD` change (July audit: sub-3% bands unreliable; ≥8% capped). No NCAAF (college normalization unsolved; observed 5.8–9.4% hold). No live NFL betting (live tier is data-only). No per-sport caps/stakes. No extension/Chrome/compose changes (actuator verified sport-agnostic; NFL offer rows carry the same `id`/`eventId`/`url` fields feeding migration-009 capture).

## Accepted behaviors / risks

- **Serial placement**: simultaneous Sunday qualifiers dispatch one at a time (the bet slip is a singleton; parallel placement on one Chrome profile is a correctness hazard). Later-in-queue bets may reject on min-odds — visible in the ledger + new alerts.
- **Shared breaker** stands down both sports together (deliberate).
- **September quota** runs near the sustainable line (NFL ≈ +80–100 credits/day from Sep 10, MLB through ~Sep 27); the budget governor widens warm cadence first, hot protected. Under L3 (hot 180s > `BETTING_FAIR_MAX_AGE_SECONDS` 120) bets skip as `stale_fair` — existing, now likelier on burst days.
- Tipsport NFL hold is unknown until markets post; if it lands ≥6% (NCAAF-like), qualifying edges will be scarce — the smoke overround output makes this visible before week 1.

## Rollout

1. **Now:** implement → full test suite + ruff + docs-audit → `tipsport-smoke --sport nfl` (expect graceful market-unavailable) → commit → deploy `CHROME=0` → VM verify (heartbeat, dashboard NFL tab, smoke in container). Idle cost ~16 credits/day.
2. **~Sep 3–8** (markets post): re-run smoke → read the overround lines → report hold + expectations.
3. **Before Sep 10:** operator ticks NFL in `betting_sports` (dashboard) + reviews shared caps for Sunday bursts.
4. **Week 1:** `live-discover --sport nfl` during a live game → decide `live_tipsport_coverage` (follow-up change only if `True`).

## Testing

Profile registry (5 sports, field pins), NFL parser fixture (FD tab, superSportId 2, fail-closed on missing tab), normalize (canonical passthrough + no-alias), betting gate (absent key → mlb-only; `"mlb,nfl"` → NFL qualifies; malformed → conservative; skip rows keep `sport_not_enabled`), reject/failed alert formatter + send-site, dashboard config POST mutates `betting_sports` + renders checkboxes gated, All-view backstop fix, smoke overround print. Docs-audit-enforced counts updated.
