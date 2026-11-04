# P1 Auto-Betting — Design (approved 2026-08-09)

Decisions from the design discussion: engine actuates through a **Chrome extension in a real Chrome on the VM** (anti-detection: real browser, persistent logged-in profile, placement executed from page context with the site's own cookies); **real money from day one** at Tipsport-minimum stakes (no shadow phase); **flat stake now, Kelly later** (dashboard-adjustable); scope at launch **pregame hot tier, MLB, guards-passed, edge 3–8%**. Dashboard gains a `/betting` tab replacing `/bets`; its write controls ship only behind the user's nginx basic auth (user-owned prerequisite). Known and accepted: automated placement violates Tipsport ToS — small stakes and human-ish pacing are the mitigation, account limiting is the expected eventual cost.

## Architecture

```
Pipeline container (existing)                  Chrome container (Phase 2)
┌──────────────────────────────┐              ┌─────────────────────────────┐
│ EdgeService (clean bet)      │              │ Chrome + Xvfb               │
│   ↓ same process             │  WebSocket   │ persistent profile (volume) │
│ BettingService               │◄────────────►│ MV3 extension:              │
│  qualify → stake → command   │  token auth  │  page-context placement,    │
│   ↓                          │  compose-    │  min-odds re-check at       │
│ placed_bets (migration 010)  │  internal    │  confirm, receipt/balance   │
│   ↓                          │              │  report, list_bets          │
│ /betting dashboard tab       │              └─────────────────────────────┘
└──────────────────────────────┘
```

Latency budget: detection is bounded by the hot-tier poll cadence (90s un-throttled); detect→placed adds ~1–3s (in-process hook ~0ms, WS command ~50ms, page-context placement ~1s). Backtest context: 72% of entry prices alive at T+60s, 64% still ≥3% edge at T+120s.

## Components

- **BettingService** (`oddsiq/pipeline/betting_service.py`): invoked synchronously by EdgeService on every CLEAN value-bet insert. Ordered qualification chain; first failure records a `skipped` decision row with its reason. On pass: `pending` row → executor command → `placed`/`rejected`/`failed`/`unknown`. Circuit breaker auto-disarms (human re-arm only).
- **BettingBridge** (`oddsiq/pipeline/betting_bridge.py`): WS server inside the pipeline; single extension client; token auth; ping/pong liveness; correlation-id request/response; `PlacementTimeout` → `unknown`.
- **Chrome extension** (Phase 2): connects out to the bridge; executes the slip from page context; **re-checks the live price against `min_odds` at confirm time** (last-line freshness guard); reports accepted odds, receipt id, balance; `list_bets` for reconciliation.
- **placed_bets** (migration 010): one row per DECISION per value bet (`value_bet_id UNIQUE` — double-bet impossible by schema; no retries in Phase 1). Statuses: `skipped | pending | placed | rejected | failed | unknown | settled`.
- **Dashboard `/betting`** (Phase 3): balance / at-risk / staked / P&L KPIs, engine card (armed toggle, extension status, kill switch), stake + cap controls (auth-gated), placed-bets table with requested-vs-accepted slippage and latencies, detections feed with skip reasons.

## Qualification chain (launch)

`sport=mlb` → `tier=hot` → tipsport ids present → armed → extension connected + logged in → Pinnacle fair age ≤ 120s → tipoff in future → stake ≤ hard ceiling (clamp) → daily bet cap → open-stakes cap → daily loss limit. `min_odds = fair × (1 + EDGE_THRESHOLD)`.

## Protocol v1 (JSON over WS)

`hello{token, extension_version, logged_in}` → `hello_ack` (bad token → close) · `ping`/`pong` 20s · `place_bet{id, bet_id, match_id, opp_id, url, stake_czk, min_odds}` → `result{id, status: placed|rejected|error, accepted_odds?, receipt_id?, balance_czk?, message?}` · `list_bets` (reserved, Phase 2 reconciliation).

## Failure matrix

| Failure | Detection | Response |
| --- | --- | --- |
| Placement timeout | no `result` in 10s | status `unknown` → **auto-disarm** (never blind-retry) |
| 2 consecutive placement errors | breaker counter | auto-disarm |
| Daily loss limit hit | settled P&L aggregate | auto-disarm |
| Extension gone | 2 missed pongs / socket close | state `disconnected` → skip bets, alert |
| Logged out | extension `logged_in=false` | skip bets (`not_logged_in`), alert |
| Stale sharp reference | fair age > 120s | skip bet (`stale_fair`) |
| Pipeline death | healthchecks.io dead-man ping | operator paged (existing) |

Every auto-disarm: `betting_armed=0` + `betting_disarm_reason` in meta + Telegram + `betting_disarmed:` log. Re-arm via CLI/dashboard only.

## Phases

1. **Engine core** (this spec's first implementation): migration 010, BettingService, bridge, CLI arm/disarm, alerts — executor mockable, ships inert (`BETTING_ENABLED=0`).
2. Chrome container + extension + real placement; VM integration disarmed.
3. `/betting` dashboard tab + auth-gated controls.
4. Login, arm at minimum stake, first supervised placements.
Later: live-edge betting, fractional-Kelly staking, NFL.
