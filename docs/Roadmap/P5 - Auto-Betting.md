---
tags:
  - type/roadmap
  - status/planned
  - phase/p5
---
# P5 -- Auto-Betting

## Goal

Automate bet placement for OddsIQ-detected moneyline edges via a Chrome extension running in the user's own logged-in browser. Close the gap between detected edges and realized profit that currently exists because edges decay in minutes and the user is asleep during overnight CET tipoffs.

> **Note (2026-06-02):** The multi-book prerequisite changed — Betano/Allwyn/Fortuna were archived and Tipsport is the single soft book (kept in the pre-release history). The per-bookmaker content script references and "all 3 books supported" decision (#8) below predate that and will be revisited when P5 is committed. No design decisions are invalidated — they are scoped to whatever books are active at P5 commit time.

**Capstone feature in design intent** -- the actual profit unlock that closes the gap between detected edges and realized money. Now ships before [[P6 - Kelly Staking|P6 Kelly]] (no longer last numerically) because sizing only matters once placement is reliable. Depends on P3-P4 being complete plus a CLV validation period. **The legacy "≥200 manually placed bets" gate is currently dead** — the user can't manually place into overnight CET tipoffs, so the project is in evaluation mode using passive CLV from the 24/7 pipeline as the validation criterion (`HANDBOOK.md` § "CLV gating", MOC). The actual P5 commit gate will be revised at commit time.

Shipped engine design: `docs/Design/2026-08-09-p1-auto-betting-design.md`.

## Design Summary

Three new pieces, one changed existing flow:

- **AutoBet Gate** -- new Python module between [[Edge Detection]] and the notification layer. Runs circuit breakers, applies take-best dedup, computes Kelly stake, decides auto-place / flag-manual / skip.
- **Chrome Extension** -- Manifest V3 extension in user's own Chrome. Background service worker connects to OddsIQ; per-bookmaker content scripts (Tipsport, Fortuna, Betano) navigate the bet slip, re-verify the edge on the live slip, place the bet, report back.
- **Local bridge** -- low-latency authenticated channel between OddsIQ and the extension. Transport TBD at build time.
- **Audit log table** -- new DB table logging every placement attempt (placed / aborted / failed) for CLV reconciliation and forensic review.

## Locked Decisions

Nine brainstormed decisions, each with rationale preserved in the design spec:

1. **Pure auto-place, 24/7** -- no tap-to-confirm. Sleep problem drives this; circuit breakers make manual oversight redundant.
2. **Edge > 15% suppressed** -- routes to Telegram "INVESTIGATE" flag instead of auto-placing. Huge edges are overwhelmingly data bugs.
3. **Kill switches:** daily stop-loss (-3%), active exposure cap (10% of bankroll), Pinnacle freshness guard (> 90s old), scheduler heartbeat (no poll in > 10min).
4. **Chrome extension in user's real browser** -- lowest detection risk vs headless/API alternatives. Protects unbanned accounts.
5. **Moneyline only** -- spread/total stay manual Telegram. Cleanest market, lowest false-edge surface.
6. **Re-verify edge on bet slip** -- abort if edge drops below 3% threshold after slip renders.
7. **Stake = Kelly + hard max-stake cap** -- same calculator as manual flow; cap is belt-and-suspenders.
8. **All 3 books supported; take-best-only on collisions** -- for same-selection same-game edges across books, auto-place only on the highest-edge book. Per-book enable flags for staged rollout.
9. **Fail loud + pre-flight heartbeat + audit log** -- any failure Telegrams user with full context + deep link; pre-night login check at 22:00 CET; every attempt logged.

(A tenth brainstorming question re-confirmed #1 after the circuit breakers were stacked -- it did not produce a distinct decision.)

## Prerequisites

- P2 More Czech Bookmakers complete (Fortuna + Betano in DB) — already shipped
- [[P3 - Dashboard and Reporting|P3 Dashboard and Reporting]] complete -- visibility into edge distribution, CLV per market, CLV per book
- [[P4 - Resilience and Deployment|P4 Resilience and Deployment]] -- hardened error handling matters more once money flows without human supervision.
- **CLV validation period** (legacy design intent): a ~200-bet manual-placement gate with proven positive CLV. **Note:** the user is currently in evaluation mode and that legacy gate is unreachable (overnight CET tipoffs); it will be revised when P5 is actually committed. Current pacing decisions live in `HANDBOOK.md` § "CLV gating".
- A stake mechanism in place -- initially a fixed-fraction or operator-config approach is sufficient. Formal Kelly sizing layers on later in [[P6 - Kelly Staking|P6 Kelly Staking]] (which ships after this phase).

## Success Criteria

> The 50 / 200 bet counts below inherit the legacy dead gate (overnight CET tipoffs make manual placement unreachable) and will be revised at P5 commit time — same reframing as **Prerequisites** above.

- [ ] 50 auto-placed bets with zero placement bugs (wrong stake / selection / market / book)
- [ ] Auto-placement CLV >= manual-placement CLV over the same period
- [ ] Zero accounts banned during the first 200 auto-placed bets
- [ ] Pre-flight heartbeat catches >= 95% of session-expiry scenarios before edges fire
- [ ] Edge > 15% suppression gate fires correctly every time (zero auto-placements above 15%)
- [ ] Daily stop-loss correctly pauses auto-place within 1 game cycle of threshold being hit

## Components Affected

- [[Scheduler]] -- new AutoBet Gate hook between edge detection and notifications
- [[Edge Detection]] -- no change to logic; gate consumes its output
- [[Telegram Notifications]] -- new message types (auto-place success / abort / failure / night digest / INVESTIGATE flag / auto-pause)
- [[SQLite Store]] -- new `auto_bets` table schema, linked to `value_bets`
- [[Failure Modes]] -- new failure modes (extension disconnect, slip UI change, session expiry, bet rejection)
- New: Chrome extension (separate artifact, likely in `extension/` subdir or separate repo)
- New: local bridge server (runs inside OddsIQ Python process)

## Open Questions (Defer to Build Time)

Deliberately not decided now -- the right answer depends on codebase state and Chrome ecosystem at implementation:

- Bridge transport (WebSocket vs Native Messaging vs localhost HTTP)
- Audit log schema specifics (model after then-current patterns)
- Threshold tuning (15% / 3% / 10% / 90s / 10min are starting points)
- Bankroll integration -- initial fixed-fraction stake at this phase; formal Kelly sizing layers on later in [[P6 - Kelly Staking|P6]]
- Extension auth mechanism
- Extension distribution (unpacked vs Chrome Web Store)

## Risks Accepted

- **Bookmaker ToS violation.** Lowest-risk implementation possible (own browser, own session), but not zero risk. Ban timeline estimated in months.
- **Account asset loss.** Multi-book diversification + take-best rule preserves fallback books if one is lost.
- **Legal.** Unchanged from [[Legal Risk Analysis]] -- civil ToS violation, not criminal.
- **Maintenance.** Content scripts drift with bookmaker UI changes. Expect ~1-2 repair jobs per book per year. Failures surface in audit log.
- **Data-bug blast radius.** Subtle bugs (wrong team assignment) could still produce losses within thresholds. Mitigated by 200+ manual CLV prereq and audit log review cadence.

## Related

- Shipped engine design: `docs/Design/2026-08-09-p1-auto-betting-design.md`
- P2 More Czech Bookmakers (complete) -- target books for placement
- [[P3 - Dashboard and Reporting]] -- visibility prerequisite
- [[P4 - Resilience and Deployment]] -- hardening prerequisite
- [[P6 - Kelly Staking]] -- formal stake sizing layered on after this phase ships
- [[Legal Risk Analysis]] -- ToS / legal context
- `HANDBOOK.md` § "CLV gating" — current validation framework (legacy 200-bet gate is dead under evaluation mode)
- Tipsport limits / ban timing — open question, deferred to P5 commit; no canonical doc yet
- [[Phase Overview]]
