---
tags:
  - type/decision-record
  - status/superseded
  - layer/architecture
source-files:
  - oddsiq/pipeline/tiers.py
  - oddsiq/config.py
related:
  - "[[Polling Tiers]]"
  - "[[Pipeline Services]]"
  - "[[Budget Governor]]"
  - "[[SportProfile]]"
  - "[[Architecture Decisions]]"
---

# Live-First

> Decision date: 2026-06-02. Status: **RETIRED 2026-08-27** — the live-edge subsystem was fully removed.

## Retirement Note (2026-08-27)

The live-edge subsystem is gone in its entirety: the post-tipoff "live" polling tier was deleted from `tiers.py` (tiers are far/warm/hot; post-tipoff games are unpollable), `LiveScoutService` and markout classification were deleted, live-tier Pinnacle polling was deleted (freeing ~150–225 Odds API credits/day), the `live_edge_candidates` table was dropped by migration 011, the `SportProfile.live_tipsport_coverage` field was deleted, the live config knobs (`POLL_TIER_LIVE`, `LIVE_EDGE_THRESHOLD`, `LIVE_SCOUT_MAX_HOURS`, `LIVE_EDGE_ALERT_THRESHOLD`, the five `LIVE_MARKOUT_*` knobs) were deleted, and the `live-discover` / `live-scout-report` CLI commands were removed. Rationale: there is no live betting, the live tier's quota cost was real, and the accumulated data went unused. The dataset is archived at `~/dev/oddsiq-live-edge-archive/` and the pre-removal code at git tag `archive/live-edges-2026-08-27`.

**What survives is the game lifecycle, unchanged:** `status='live'` transitions with `game_live:`/`game_final:` logs, closing snapshots marked at tipoff (clock-driven, not poll-driven), finals via `/scores` + settlement + CLV (provably unaffected — CLV never depended on post-tipoff polling), and `SportProfile.live_backstop_hours`, which is now purely the lifecycle window used for zombie-game detection and `/scores` refresh scoping.

The remainder of this page is the original decision narrative, kept for historical context.

## The Thesis (historical)

The starting hypothesis was that most +EV value would come from **live** (in-play) rather than pregame: Tipsport's live lines were expected to lag the sharp Pinnacle line more than pregame, because Tipsport's live pricing is less automated. The pipeline was designed around this thesis — a dedicated live polling tier (60s at launch, later 240s), a live scout recording candidates, markout confirmation, and (until the 2026-08-10 notification rebrand) rare markout-confirmed Telegram live-edge alerts.

**The thesis never validated.** Tipsport's `WINNER_FINAL_DECISION` tab turned out not to carry in-play prices for basketball/hockey (zero live candidates ever), so meaningful live signal only came from MLB; the markout-confirmed "tradeable set" showed promise in aggregate but there was no live betting to act on it, and the data went unused while the live tier kept spending quota. That imbalance is what retired the subsystem on 2026-08-27.

## How Live Worked (historical)

- Post-tipoff games entered the `live` tier in `tiers.tier_for()`.
- `PollCoordinator` called `LiveScoutService.detect_live_edges()` instead of `EdgeService.detect_pregame_edges()`.
- `LiveScoutService` compared live Tipsport moneyline vs vig-removed Pinnacle fair at `LIVE_EDGE_THRESHOLD` (3%).
- Qualifying edges wrote to `live_edge_candidates` — **never** `value_bets`, CLV, or closing snapshots.
- Each candidate was markout-classified one poll cycle later (confirmed/decayed/rejected, plus a stale-mirage plausibility cap and a frozen-offer gate).
- Games stayed live-pollable until `commence_time + live_backstop_hours`.

None of this exists anymore; post-tipoff games are simply not polled.

## Separation Invariant (the part that mattered)

The live path was deliberately isolated from the pregame path, which is exactly why its removal was clean:

- `live_edge_candidates` was a separate time series (never touched `value_bets` / CLV / `is_closing`) — dropping it removed no pregame data.
- Closing snapshots are marked at tipoff by the clock-driven lifecycle transition, not by a live poll — CLV was provably unaffected by the removal.
- The `odds_snapshots` "last seen" dedup bump never touched an `is_closing=1` row — an invariant that outlives the live tier.

## Budget Impact (resolved by removal)

Live polling was a major Odds API quota driver (the [[Budget Governor]] throttle table carried a live column and protected the hot tier by widening warm and live first). Removing live-tier Pinnacle polling freed roughly 150–225 credits/day; the throttle table no longer has a live column — hot stays protected, warm widens first.
