---
tags:
  - type/roadmap
  - status/complete
  - phase/p1
---
# P1 -- State Persistence and Bug Fixes

## Goal

Eliminate data loss across restarts and fix duplicate alert issues. After P1, the pipeline can be stopped and restarted mid-session without losing track of which edges were already alerted or which games are already live.

## Success Criteria

- [x] `_alerted_edges` eliminated -- dedup via `insert_value_bet()` return value, survives restarts
- [x] `_handled_live` restored on startup from closing snapshots -- CLV capture survives restart
- [x] Pinnacle budget persisted -- accurate budget display from first request
- [x] Value bet deduplication in DB -- `INSERT ... ON CONFLICT DO NOTHING` on `(game_id, bookmaker, market_type, selection)`
- [x] `_home_team`/`_away_team` as proper dataclass fields (excluded from DB ops)
- [x] Proactive Cloudflare session refresh before cookie expiry (20 min TTL, `SESSION_MAX_AGE = 1200`)
- [x] Playwright API request timeout (60s) to prevent pipeline freezes
- [x] `_check_completed_games` throttled to every 5 min during live games
- [x] Edge detection restricted to hot tier only (< 30 min to tipoff) — *this was the P1 scope; later widened to hot + medium (the tier since renamed **warm**; ≤ 2h to tipoff) in commit `3252c71`, see [[Edge Detection]]*
- [ ] `remove_vig()` returns keyed dict instead of positional tuple (low priority -- current code is correct but fragile on refactor)

9 of 10 criteria met. Pinnacle budget persistence landed as part of commit `58f1ce2` (startup probe + `meta` table fallback) — see [[Mitigated - Pinnacle Budget In-Memory]]. One open:

1. **`remove_vig()` keyed dict** -- low priority, current code works correctly

## Components Affected

- [[Scheduler]] -- `_handled_live` restore, throttled completion checks, hot-tier-only edge detection
- [[SQLite Store]] -- `ON CONFLICT` dedup, closing snapshot query for handled-live restore
- [[Data Model]] -- `_home_team`/`_away_team` proper dataclass fields
- [[CloudflareSession]] -- proactive session refresh, `SESSION_MAX_AGE = 1200`
- [[TipsportScraper]] -- Playwright API request timeout (60s)
- [[Vig Removal]] -- `remove_vig()` return type (deferred)
- [[Edge Detection]] -- restricted to hot tier at P1 (since widened to hot + warm — formerly "medium" — in `3252c71`)

## Related Known Issues

- [[Mitigated - Cloudflare Session Lifecycle]]
- [[Mitigated - Pinnacle Budget In-Memory]]

(Fixed-bug post-mortems for `_alerted_edges`, `_setattr`, CLV selection swap were archived. See git history for details.)

## Related

- [[Phase Overview]]
