---
tags:
  - type/roadmap
  - status/complete
  - phase/p0
---
# P0 -- Telegram Alerts

**Status: shipped 2026-04, RETIRED 2026-08-10.** This phase's alert set — value-bet notifications, CLV summaries, session summaries — shipped exactly as described below, then was retired by the **bettor-facing Telegram rebrand** of 2026-08-10: the phone now carries real-money events only (bet placed, won/lost, rejected/failed, auto-betting stopped, Tipsport login lost/restored, plus the rate-limited not-logged-in skip), and all three P0 events became log-only (`value_bet:`, `clv:`, and a console session summary). See [[Telegram Notifications]] for the message set that is actually sent today. Everything below is the historical record of the phase as delivered.

## Goal

Deliver real-time value bet notifications, CLV summaries, and session summaries via Telegram Bot API so the user can act on opportunities without watching the console.

## Success Criteria

- [x] Instant notification within 5s of edge detection
- [x] CLV notification when game tips off
- [x] Session summary on shutdown
- [x] Non-blocking -- failures never crash the pipeline
- [x] Works without configuration (silent no-op if token not set)

All five criteria met.

## Components Affected

- [[Telegram Notifications]] -- new component, sends formatted messages via Bot API
- [[Scheduler]] -- triggers alert dispatch after edge detection and CLV capture
- [[CLI Entry Point]] -- session summary sent on Ctrl+C / shutdown

## Configuration

- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env` (optional -- pipeline works without)

## Alert Types

1. **Value bet alert** -- instant notification with game, market, odds, edge%
2. **CLV summary** -- sent when a game tips off and closing line is captured
3. **Session summary** -- sent on Ctrl+C / shutdown

## Dependencies

None -- this was the first feature added after the core pipeline.

## Related

- [[Phase Overview]]
- [[First Game Night]] -- validated alerts in production
