---
tags: [type/spec, phase/evaluation]
related:
  - "[[Live Tier Scout]]"
  - "[[Edge Detection]]"
  - "[[Scheduler]]"
  - "[[Pipeline Services]]"
---

# Live-Edge Validation + Unified Bets Tab — Design

> Status: design, approved 2026-06-08. Enhances the existing **live scout** (research instrument) with a **markout confirmation** pass so live edges can be trusted before alerting, decouples the live-edge **alert** threshold from the **recording** threshold to kill Telegram spam, and adds a dedicated dashboard **Bets** tab unifying pregame value bets + live edges. Stays in evaluation mode: NO auto-betting, NO CLV merge, NO new books/sports.

## Problem

Live edges are recorded to `live_edge_candidates` at a 3% threshold and Telegram-alerted on the first bucket crossing (≥3%) per (game, selection) plus every bucket upgrade. In practice this floods Telegram, because during live games one of the two feeds is frequently stale:

- **The Odds API serves polled Pinnacle snapshots, not a real-time stream.** During live play Pinnacle lags the true market by tens of seconds.
- **Tipsport live prices also lag / freeze / suspend.**

So most large live "edges" are **measurement artifacts**, not bettable value. Evidence from the live DB (184 candidates, all MLB, 0 settled at time of writing):

- Every ≥50% edge had simultaneous captures (`tip_minus_pin_secs ≈ 0`) — not a clock-skew bug, a **feed-staleness** problem.
- Game 11653 caught it red-handed: Tipsport away moved 2.25 → 4.0 when the game shifted; **Pinnacle sat frozen at 1.68 for 7 minutes**, producing a phantom +138% edge, then snapped to 3.02 and the edge collapsed. The stale side was **Pinnacle**, not Tipsport.
- The ≥50% bucket showed Tipsport implying ~33% win prob vs Pinnacle fair ~55% — a 22-point gulf two actively-trading markets never sustain.

A residual *real* edge may exist under the artifact (in 11653, Tipsport's ~3.9 vs the settled ~2.9 fair is still ~30%), but **there is currently no instrument to separate the real residual from the feed-lag mirage**. That instrument is markout — described in the [[Live Tier Scout]] decision doc's validation model but never built.

## Goals

1. **Separate real live edges from feed-lag artifacts** via a markout confirmation pass.
2. **Stop the Telegram spam** by decoupling the alert threshold from the recording threshold and gating alerts on markout confirmation.
3. **Unify value bets + live edges** into a dedicated dashboard tab, moved off the main `/live` page — without merging storage or polluting the CLV dataset.

## Non-Goals (project-mode guardrails)

- No auto-betting, no Kelly staking, no live fair-price model.
- No merge of `live_edge_candidates` into `value_bets`; live edges never get CLV (no closing line exists for them).
- No new bookmakers or sports.
- Recording everything ≥3% continues unchanged — we do not lose research data; only **alerting** and **presentation** change.

---

## Section 1 — Decouple alert threshold from recording threshold

- **Recording** threshold stays `LIVE_EDGE_THRESHOLD` (default `0.03`). All edges ≥3% are still written to `live_edge_candidates` and markout-scored, so the dashboard/`live-scout-report` keep the full distribution.
- **Alerting** gets a new knob `LIVE_EDGE_ALERT_THRESHOLD` (default `1.0` = 100%, env-overridable). A live edge alerts only when **both**:
  1. the instant displayed edge `candidate.edge_pct ≥ LIVE_EDGE_ALERT_THRESHOLD * 100`, AND
  2. it reaches `markout_status = 'confirmed'` (Section 2).
- The current alert-on-insert in `LiveScoutService.detect_live_edges` is **removed**. Alerts now fire from the markout-resolution path only.
- In-memory dedup (first crossing per (game, selection) + bucket upgrades) is preserved, but keyed on confirmed alerts.

**Units note (footgun):** to stay consistent with the sibling fraction knobs (`EDGE_THRESHOLD=0.03`, `LIVE_EDGE_THRESHOLD=0.03`), `LIVE_EDGE_ALERT_THRESHOLD` is a **fraction**: `1.0` = 100%. `.env.example` must carry a loud comment so a user does not type `100` and get 10000%.

**Consequence (by design):** with the default 100% bar + markout gating, Telegram alerts become rare — only genuine, confirmed monsters. The dashboard is where edges of all sizes are studied; Telegram is where you act.

---

## Section 2 — Markout validation (the core: separate real from noise)

### Idea

When an edge is flagged at time `T`, re-check it against a **fresh** Pinnacle fair one live poll cycle later (~60s). This reuses the existing 60s live cadence — **no extra fetches**. If the edge survives against a stabilized Pinnacle, it is real; if it collapses because Pinnacle was merely catching up, it was a mirage.

### Schema (migration `006_add_markout_to_live_edge_candidates.py`, forward-only)

Add to `live_edge_candidates`:

| Column | Type | Meaning |
|---|---|---|
| `markout_fair_odds` | REAL NULL | Pinnacle fair for the same selection at markout time |
| `markout_edge_pct` | REAL NULL | `(entry_odds / markout_fair_odds - 1) * 100` |
| `markout_captured_at` | TIMESTAMP NULL | when the markout Pinnacle fair was captured |
| `markout_status` | TEXT NOT NULL DEFAULT `'pending'` | `pending` / `confirmed` / `decayed` / `rejected` |
| `offer_stale` | INTEGER NULL | 1 if Tipsport's price was unchanged across the markout window (suspected frozen/suspended), else 0; NULL until resolved |

Existing rows are backfilled to `markout_status = 'legacy'` (pre-feature, no markout data) so they are distinguishable from genuinely-pending new rows. The unique index is unchanged (`game_id, bookmaker, market_type, selection, observed_at`).

### Resolution logic (in `LiveScoutService`)

New method `resolve_pending_markouts(game, game_snapshots)`, called from `PollCoordinator`'s live branch on the same `game_snapshots` it already passes to `detect_live_edges`. Order per live cycle: **resolve prior pending markouts first, then detect new edges** (a candidate inserted this cycle has `observed_at = now` and cannot resolve until `LIVE_MARKOUT_MIN_DELAY_SECONDS` has elapsed, so there is no self-markout).

For each pending candidate of this game whose `observed_at` is older than `LIVE_MARKOUT_MIN_DELAY_SECONDS`, and for which this cycle has a Pinnacle pair for the selection:

```
markout_fair  = remove_vig(pin_sel, pin_opp)[0]      # same vig math as detection
markout_edge  = (entry_odds / markout_fair - 1) * 100
fair_drift    = abs(markout_fair / fair_odds - 1)     # how far our reference moved
offer_stale   = (current Tipsport price for selection == entry_odds)  # unchanged across window

if fair_drift > LIVE_MARKOUT_MAX_FAIR_DRIFT or markout_edge <= 0:
    status = 'rejected'        # Pinnacle was mid-move/stale, or edge gone -> mirage (e.g. game 11653)
elif markout_edge >= LIVE_MARKOUT_CONFIRM_FLOOR * 100:
    status = 'confirmed'       # Pinnacle stable AND edge persists -> real
else:
    status = 'decayed'         # Pinnacle stable but edge faded below the confirm floor
```

If Pinnacle is absent this cycle or the min delay has not elapsed, the candidate stays `pending` (re-attempted next cycle). If the game goes final while still `pending`, it stays `pending` (terminal); the dashboard treats `pending` as "unverified".

`offer_stale` is a **displayed annotation only** in v1 — it does not block `confirmed`, because the scraper exposes no reliable "suspended" flag and a price can be legitimately stable when Pinnacle is also stable. A confirmed edge with `offer_stale = 1` is shown with a "may be frozen" caveat in the UI. Tightening this (true suspension detection) is a documented future refinement.

### Why the classifier is decoupled from the alert threshold

`LIVE_MARKOUT_CONFIRM_FLOOR` (the confirmed/decayed boundary) is a **separate** knob from `LIVE_EDGE_ALERT_THRESHOLD`. The classifier scores **all** recorded candidates at all edge sizes, so the dashboard shows the true confirmed-edge distribution (including the common 10–30% band). The 100% alert knob only silences Telegram. This is how the user eventually learns whether smaller edges are real and decides to lower the alert bar.

### New store methods

- `get_pending_markout_candidates(game_id, older_than_seconds)` → candidates with `markout_status='pending'` and `observed_at` older than the delay.
- `update_live_candidate_markout(cand_id, *, markout_fair_odds, markout_edge_pct, markout_captured_at, markout_status, offer_stale)`.

---

## Section 3 — Unified "Bets" tab (move off the main page)

"Unified view, separate plumbing" — no storage merge.

### Navigation

`base.html` nav becomes **Live | Bets | Analysis | ⚙ Settings** (`Bets` between Live and Analysis). Tab name "Bets" (alt "Opportunities" if preferred).

### Routing / templates (`server.py`)

- New `@app.get("/bets")` → `bets.html` with `page="bets"`, sport filter + `enabled_sports` (mirrors the `/live` handler).
- **Move** the `recent-value-bets` and `live-edges` `<section>`s out of `live.html` into `bets.html`. `/live` no longer renders them.
- Re-home the two partial endpoints to the new namespace: `/bets/partials/value-bets` and `/bets/partials/live-edges` (renamed from `/live/partials/recent-value-bets` and `/live/partials/live-edges`); update the `hx-get` URLs in the moved sections. Remove the old endpoints. **Sweep all references** (templates, tests, docs) per the repo's check-all-references rule.

### Page layout (`bets.html`)

Two clearly-labeled sections on one page:

1. **PREGAME VALUE BETS** — existing `recent_value_bets` partial (bookmaker, tier, selection, entry/fair odds, edge, CLV, result). Tagged `PREGAME`.
2. **LIVE EDGES** — `live_edges` partial extended with: `markout_status` badge (`confirmed`/`decayed`/`rejected`/`pending`/`legacy`), `markout_edge_pct`, `minutes_into_game`, and an `offer_stale` "may be frozen" caveat. Tagged `LIVE`. Default filter shows **confirmed** prominently; a toggle (query param `?show=all`) reveals decayed/rejected/pending for research.

### Queries (`queries.py`)

- Extend the live-edges query to select the markout columns.
- Add a `confirmed`-only filter (default) plus an all-statuses mode for the toggle.
- Value-bets query unchanged (just relocated).

---

## Section 4 — Config knobs

Add to `config.py` and document in `.env.example` (all fractions, matching existing convention):

| Knob | Default | Meaning |
|---|---|---|
| `LIVE_EDGE_ALERT_THRESHOLD` | `1.0` | Min instant edge to alert (fraction; `1.0` = 100%). Alert also requires `confirmed`. |
| `LIVE_MARKOUT_MIN_DELAY_SECONDS` | `60` | Min age before a candidate is markout-resolved (≈ one live cycle). |
| `LIVE_MARKOUT_MAX_FAIR_DRIFT` | `0.10` | Max \|Δ Pinnacle fair\| over the window to treat the reference as stable; above this → `rejected`. |
| `LIVE_MARKOUT_CONFIRM_FLOOR` | `0.08` | Min surviving markout edge to be `confirmed` (fraction); below → `decayed`. Matches the doc's 8–10% Go bar. |

`LIVE_EDGE_THRESHOLD` (recording, `0.03`) is unchanged.

---

## Section 5 — Telegram

- `format_live_edge` aligned with `format_value_bet` styling so the two feel like one first-class feed.
- Live-edge alerts now include `markout` context (confirmed, surviving edge, minutes-in) and fire only from the markout-resolution path, gated as in Section 1.
- `decayed` edges: **silent on Telegram, visible on the Bets tab** (design decision). `rejected`/`pending`: silent.

---

## Sequencing (for the implementation plan)

Two independently-shippable phases:

- **Phase 1 — Backend validation + spam fix.** Migration 006; markout logic + store methods in `LiveScoutService`/`store`; `PollCoordinator` live-branch wiring; config knobs + `.env.example`; remove alert-on-insert and re-gate alerts on confirmed; `format_live_edge` update; `live-scout-report`/`audit/live_scout.py` markout breakdown. Delivers immediate spam relief + trustworthy data.
- **Phase 2 — Bets tab.** New route/template; move the two sections off `/live`; re-home partials; nav entry; markout display + confirmed filter; query changes. Delivers the unified view.

## Testing

Hermetic, following existing per-file fixture patterns (`tmp_path` SQLite, pytest-asyncio strict):

- **Markout classifier:** synthetic snapshot sequences for `confirmed` (Pinnacle stable, edge persists), `rejected` (a game-11653 replay: Pinnacle drifts up, edge collapses), `decayed` (stable, edge fades below floor), `pending` (within delay / no Pinnacle this cycle).
- **Alert gating:** confirmed + ≥100% instant → alert; confirmed + <100% → no alert; unconfirmed ≥100% → no alert; dedup holds.
- **Migration 006:** applies cleanly, columns exist, existing rows → `legacy`.
- **Store methods:** `get_pending_markout_candidates` respects the delay; `update_live_candidate_markout` round-trips.
- **Dashboard:** `/bets` renders both sections; `/live` no longer renders them; relocated partials return markout fields; confirmed filter + `?show=all` toggle behave; old `/live/partials/...` endpoints removed.

## Docs to update (check-all-references sweep)

- the project notes — fix the stale "zero candidates recorded" line; update the live-scout gotcha (alerts now markout-gated; new knobs; recording vs alert threshold split).
- `docs/Decisions/Live Tier Scout.md` — markout is now built; record the as-built classifier + thresholds.
- `docs/Components/Pipeline/Pipeline Services.md` (or the live-scout component doc) — markout resolution path.
- `.env.example` — the four new knobs with the units comment.
- Any dashboard doc enumerating routes/tabs — add `/bets`, note the moved sections.
- Test-count references if the canonical count is cited.

## Open risks / future refinements

- **`offer_stale` is heuristic** (price-unchanged ≠ definitely suspended). True suspension detection from the Tipsport feed is future work.
- **60s markout granularity** is coarse; a real edge may decay within the window. Acceptable for a scout; a tighter dedicated re-sample is a later option.
- **0 settled outcomes today** — markout is a *pre-settlement* validity signal. The ultimate proof is realized win rate once games settle; the Bets tab + `live-scout-report` accumulate that.
