---
tags:
  - component/notification
  - status/complete
  - phase/p0
  - layer/notification
  - type/component
source-files:
  - oddsiq/notifications/telegram.py
related:
  - "[[Pipeline Services]]"
  - "[[Edge Detection]]"
  - "[[CLV Calculation]]"
  - "[[CLI Entry Point]]"
---

# Telegram Notifications

Best-effort one-way Telegram messages via the Bot API: each send is `await`ed with a 5s timeout and any failure is logged, never raised — a Telegram outage cannot crash the pipeline. Entirely optional: with no recipient configured, every send silently no-ops.

**Bettor-facing only (2026-08-10 rebrand, authoritative).** Telegram carries real-money events and nothing else. All evaluation-mode analytics (value-bet detections, CLV updates, live edges, session summaries) and operational alerts (coverage gaps, budget, backups, health pings) were deleted with their formatters and send-sites — liveness/ops live on the dashboard and the `HEARTBEAT_PING_URL` dead-man ping. Any reference to `format_value_bet`, `format_clv`, `format_coverage_gap`, `format_session_summary`, or a `LIVE EDGE` message is historical.

## Message types (the complete list)

| Message | Formatter | Sent by | When |
| --- | --- | --- | --- |
| 🎫 Bet placed | `format_bet_placed` | [[Pipeline Services\|BettingService]] | A real placement succeeded (stake, requested → accepted price when changed, payout, balance when parsed) |
| ⚠️/❗ Bet REJECTED / FAILED | `format_bet_attempt_failed` | BettingService | A real-money attempt didn't stick (2026-08-25 — the 6/6 silent offer_gone lesson). `skipped` qualification decisions stay silent, with one exception below |
| ✅/❌/➖/🤝 Won / lost / push / draw | `format_bet_result` | SettlementService | A PLACED bet settled — real-Kč P&L + cumulative `Total:`. Paper value-bet settlements notify nothing |
| 🛑 Auto-betting stopped | `format_betting_disarmed` | BettingService | Circuit breaker / daily loss cap auto-disarmed the engine |
| 🔐 Tipsport login LOST | `format_login_lost` | Scheduler via the [[Pipeline Services\|BettingBridge]] `on_login_change` hook | The extension navigate-confirmed the session died on the pinned `/kurzy` probe page, and the loss survived the bridge's ~10-min debounce (cancel-on-restore; 2026-08-31 — drifted live pages flapped the probe) — the Aug 28 expiry originally sat silent 32h and skipped the week's only clean bet |
| 🔓 Tipsport login restored | `format_login_restored` | same hook | Paired all-clear; only ever sent after a loss alert |
| 🔐 Bet SKIPPED — not logged in | `format_bet_skipped_not_logged_in` | BettingService | The ONE skip reason that alerts: a qualified bet lost purely to a dead session (rate-limited to one per 6h window; belt-and-braces with the bridge's 30-min startup watchdog for sessions already dead at pipeline start) |

Formatters are pure functions; money figures are whole-Kč (`_czk`/`_signed_czk`), odds two-decimal, dynamic text `html.escape`d.

## Reliability design

- **Non-raising sends** — exceptions caught and logged inside `send_telegram()`.
- **Dual recipients** — `send_telegram()` fans out to every fully-configured pair (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` and optional `*_2`), each sent independently so one failing doesn't block the other. A half-filled pair is ignored.
- **HTML-safe truncation** at Telegram's 4096-char limit (`_truncate_for_telegram` never cuts inside a tag or leaves `<b>` open).
- **Delivery audit trail** — every successful send logs `telegram_sent: chat_id=… chars=…`.
- Shared `httpx.AsyncClient`, lazily created, closed at shutdown via `telegram.close()`.
- The bridge fires login notifications as detached tasks — the WS receive loop never blocks on Telegram (a stalled send would push placement results into `PlacementTimeout`).

## Configuration

| Variable | Required | Notes |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | No | Primary recipient |
| `TELEGRAM_BOT_TOKEN_2` / `TELEGRAM_CHAT_ID_2` | No | Independent second recipient (mirror) |

## Test coverage

`tests/test_telegram.py`: formatter content for every message type (money-first placed/result lines, NFL emoji, disarm humanization, login lost/restored, not-logged-in skip), HTML escaping, truncation, recipient fan-out (single/dual/half-configured), non-200 and exception swallowing, success audit log. `tests/test_betting_service.py` covers the send-sites (placed/rejected/failed/disarm alerts, the rate-limited not-logged-in skip alert, and that other skips stay silent); `tests/test_betting_bridge.py` covers the login-transition state machine.

## See Also

- [[Pipeline Services]] — BettingService/BettingBridge send-sites and the login state machine
- [[Edge Detection]] — where clean detections (the engine's input) come from
- [[CLV Calculation]] — settlement data behind bet results
- [[CLI Entry Point]] — closes the shared client at shutdown
