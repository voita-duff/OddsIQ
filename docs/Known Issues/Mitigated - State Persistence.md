---
tags:
  - type/issue
  - status/mitigated
  - phase/p1
related:
  - "[[Scheduler]]"
  - "[[SQLite Store]]"
  - "[[PinnacleScraper]]"
---

# Mitigated: No State Persistence Across Restarts

## Status

Mitigated. Two critical items now persisted; remaining items harmless.

## Location

`oddsiq/pipeline/scheduler.py` (and its services, e.g. `schedule_service.py` for the `_handled_live` set) — in-memory state fields.
`oddsiq/db/store.py` — database-backed dedup and closing snapshot queries.
`oddsiq/scrapers/pinnacle.py` — `_budget_remaining`.

## Problem

Multiple pieces of in-memory state were lost on restart, causing duplicate alerts, re-processing of already-handled live games, and incorrect budget counts.

## Root Cause

Rapid Phase 1 implementation stored working state in instance variables with no persistence strategy.

## In-Memory State Inventory

| State Item | Status | Recovery Behavior |
|---|---|---|
| `_alerted_edges` | **REMOVED** | Dedup now via database `ON CONFLICT DO NOTHING`. |
| `_handled_live` | **RESTORED on startup** | Rebuilt from `odds_snapshots` where `is_closing = 1` in [[SQLite Store]]. Games with closing snapshots are recognized as handled. No new table needed — state derived from existing data. |
| `_last_poll_time` | Not persisted | Immediate poll on restart (harmless — correct recovery behavior). |
| `_last_schedule_refresh` | Not persisted | Immediate schedule refresh on restart (harmless — correct recovery behavior). |
| `SessionStats` | Not persisted | Summary only covers post-restart period (cosmetic). |
| Pinnacle `_budget_used` / `_budget_remaining` | Persisted to `meta` table, probed on startup | Free `/v4/sports` probe populates real values; `meta` fallback kicks in if the probe fails. See [[Mitigated - Pinnacle Budget In-Memory]]. |
| Live-scout `_alerted_bucket` (2026-06+) | Not persisted | Live-edge alert dedup resets on restart — one re-alert per (game, selection) is acceptable by design; pruned per cycle so it can't grow unbounded. |
| Betting `betting_armed` flag (P1, 2026-08) | Persisted in `meta` | Deliberate: arm/disarm survives restarts; the circuit breaker disarms by writing the same flag. |

## Fix Applied

The two critical items (`_alerted_edges` and `_handled_live`) are now persisted or derived from the database. `_alerted_edges` was removed entirely in favor of database-level dedup. `_handled_live` is rebuilt on startup by querying closing snapshots from [[SQLite Store]].

## Remaining Risk

The four non-persisted items are all harmless on restart: immediate poll and schedule refresh are correct recovery behavior, `SessionStats` is cosmetic, and the [[PinnacleScraper]] budget self-corrects after one API call.
