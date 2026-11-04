# Telegram Notification Rebrand — bettor-facing (2026-08-10)

## Motivation

Real-money auto-betting is live (P4, engine armed at 10 Kč). The Telegram feed
still carries evaluation-mode analytics (value-bet detections, CLV updates,
live edges, session summaries) and operational alerts (coverage gaps, budget,
backup failures, health pings) — noise on a phone. The operator wants **only
the money events that matter**: a bet was placed, and it won or lost.

## Scope

**Keep + redesign (money-first, minimal):**
- `BET PLACED` — a real bet was placed by the engine.
- `WON` / `LOST` — a **placed** bet settled (real Kč P&L).
- `AUTO-DISARM` (slim) — the engine stopped itself (safety; kept per operator).

**Cut entirely (11 types + their formatters/send-sites):** value-bet detection,
CLV update, live-edge alert, coverage-gap, Odds-API health-fail, budget-exhausted,
budget-throttle-max, heartbeat health-ping, session summary, missed-final-games,
startup/shutdown backup-failed.

Liveness/ops monitoring is covered by the existing `HEARTBEAT_PING_URL`
dead-man ping (healthchecks.io) + the dashboard — not the phone.

## Behavioral change (important)

Today `WON/LOST` fires for **every** settled value bet, including paper bets
(clean detections that were never actually placed). After this change,
`WON/LOST` fires **only when a real `placed_bets` row exists** for that value
bet (status `placed`/`settled`), and uses the **real stake + accepted odds**
for Kč P&L — not the flat-1u evaluation metric. Paper value-bet settlements
notify nothing. This removes the largest remaining source of phone noise and
pairs exactly with "only placed + won/lost".

`BET PLACED` already fires only on a real engine placement — unchanged trigger,
redesigned body.

## Message designs (Telegram HTML: `<b>` + emoji only)

**🎫 Bet placed** (data: `placed_bets` stake/accepted-odds/balance + game + selection)
```
🎫 <b>Bet placed · 10 Kč</b>
⚾ <b>Blue Jays</b> to win — vs Red Sox
@ 2.30 → returns <b>23 Kč</b>
💰 Balance 490 Kč
```

**✅ Won** (data: placed stake+odds, game score, cumulative realized P&L)
```
✅ <b>WON +13 Kč</b>
⚾ Blue Jays beat Red Sox 5–3
10 Kč @ 2.30 → 23 Kč back
Total: +45 Kč
```

**❌ Lost**
```
❌ <b>LOST −10 Kč</b>
⚾ Blue Jays lost to Red Sox 3–5
10 Kč stake
Total: +35 Kč
```

**🛑 Auto-disarm** (slim; unchanged trigger at `betting_service`)
```
🛑 <b>Auto-betting stopped</b>
Reason: 2 consecutive failed placements
No more bets until you re-arm.
```

Design rules: lead with the money (stake / signed profit), one line for the
pick + result/score, one line for the numbers, cumulative total on results.
No book header, implied %, CLV, edge, tier, receipt id, or dividers.

## Data + components

- `oddsiq/notifications/telegram.py`:
  - Rewrite `format_bet_placed`, `format_bet_result`, `format_betting_disarmed`.
  - `format_bet_result(game, vb, *, placed, total_czk)` — new signature; real Kč
    from `placed.stake_czk` × `placed.accepted_odds`.
  - Delete `format_value_bet`, `format_clv`, `format_live_edge`,
    `format_coverage_gap`, `format_health_ping`, `format_session_summary`, and
    now-unused helpers (`_implied`, `_units`, `_unit_return`, `_tier_label`,
    `_book_header`, `_divider` if orphaned).
- `oddsiq/db/store.py`: `get_placed_bet_by_value_bet(value_bet_id)` (if absent)
  + `cumulative_realized_pl_czk()` (sum of settled `placed_bets` P&L). Reuse
  existing `betting_overview`/settle logic where possible.
- Remove send-sites: `edge_service` (value bet), `settlement_service` (CLV;
  gate result to placed-only), `live_scout_service` (live edge),
  `coverage_monitor` (gap), `scheduler` (health-check, budget-exhausted,
  budget-throttle, health-ping, session-summary), `cli` (missed-games,
  backup-fail ×2). Keep the non-Telegram logging on all of them.

## Testing (TDD)

- `test_telegram.py`: replace formatter cases with the new designs (placed /
  won / lost / auto-disarm); assert real-Kč math, cumulative total, and that
  deleted formatters are gone. Assert HTML-escaping + tag-safe truncation still
  hold on the survivors.
- Update `test_settlement_service.py`: result Telegram fires only when a placed
  row exists; paper-bet settlement sends nothing; excluded still silent.
- Update tests asserting the removed sends (edge/scheduler/coverage/live-scout/cli)
  to assert no Telegram.

## Out of scope

Deployment (operator-gated — do NOT push to the VM until told). Dashboard
unchanged. No change to detection/settlement logic beyond the notify gate.
