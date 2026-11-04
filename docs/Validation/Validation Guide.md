---
title: Validation Guide
date: 2026-04-10
tags:
  - type/guide
  - status/active
  - phase/p1
  - layer/operations
---

# Validation Guide

How to collect data, verify the pipeline is healthy, and know when you have enough to analyze.

## Phase: Data Collection (Ongoing — Evaluation Mode)

### Before Each Game Night

```bash
cd ~/dev/OddsIQ          # or wherever you cloned the repo
source .venv/bin/activate
oddsiq                   # installed entry point (alternative: python3 -m oddsiq.cli)
```

Start anytime -- the pipeline idles until games enter the 6-hour polling window. Keep laptop plugged in, lid open, no sleep.

### During the Game Night

Leave it running. Nothing to do. To watch detections, grep the log for `value_bet:` or open the dashboard `/betting` tab — value bets are not messaged; [[Telegram Notifications|Telegram]] carries real-money events only (2026-08-10 rebrand).

If it crashes (rare), restart immediately:

```bash
oddsiq
```

**Known limitation:** CLV is lost only in the narrow window where the freshness gate fails — specifically, if restart happens after the latest snapshot's `captured_at` falls outside `CLOSING_FRESHNESS_MINUTES` (default 30) of effective tipoff, or if the game has already gone final without `_handle_game_live()` ever firing. A just-tipped game whose latest snapshot is still inside the freshness window can still mark closing snapshots correctly on restart. The [[CLV Calculation]] pipeline benefits from continuous data through tipoff but can recover on a fast restart.

### After Each Game Night

Once all games are final (check the log for `RESULT:` lines or wait until ~5:00 local), stop the pipeline:

**Ctrl+C** -- wait for the session summary. The shutdown hook writes a fresh backup to `./backups/` automatically; no manual `cp` step needed. See [[Backup and Retention]] for retention, config, and verification.

### Quick Health Check After Each Night

```bash
sqlite3 oddsiq.db "SELECT count(*) FROM value_bets WHERE result IS NOT NULL;"
```

This number should grow each night. If it's 0 after a full game night, something broke.

### What Counts as a Successful Night

1. Pipeline ran from before first tipoff through last game final
2. At least some value bets flagged (depends on the night -- 0 is possible but rare)
3. CLV calculated for flagged bets (check for non-NULL `clv_pct`)
4. Results resolved (check for non-NULL `result`)

### When to Stop Collecting

When `value_bets` has **50+ rows with CLV data**:

```bash
sqlite3 oddsiq.db "SELECT count(*) FROM value_bets WHERE clv_pct IS NOT NULL;"
```

With the pipeline running 24/7 in evaluation mode, validation accumulates passively. Aim for a sample size large enough to detect signal — see [`HANDBOOK.md`](../../HANDBOOK.md) § CLV gating for the current threshold (50 resolved bets) and decision tree.

## Per-Night Validation Checklist

Use this end-to-end check on any game night to confirm the pipeline ran clean:

- [ ] Pipeline starts without errors
- [ ] Pinnacle odds are fetched (budget tracking works)
- [ ] Tipsport (and any other enabled book) odds are fetched for all enabled markets
- [ ] Edge detection flags value bets (0 is possible on a quiet night)
- [ ] CLV is calculated when a game tips off
- [ ] Bet results resolve after games finish (win/loss/push)
- [ ] Session summary prints correct stats on Ctrl+C

The original end-to-end confirmation from the first real game night (2026-04-10: 15 games / 90 odds / 5 flagged bets) lives in [[First Game Night]] — see it for the reference production run.

## See Also

- [[Runbook]] -- operational procedures and troubleshooting
- [[CLV Calculation]] -- how closing line value is captured
- [[Telegram Notifications]] -- bettor-facing message set and configuration
- [[Backup and Retention]] -- database backup strategy
- [[CLV Analysis Framework]] -- what to do once you have enough data
- [[First Game Night]] -- reference production run
