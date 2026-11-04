---
title: Useful Queries
date: 2026-04-10
tags:
  - type/reference
  - status/active
  - phase/p1
  - layer/data
---

# Useful Queries

SQL queries for manual inspection, health monitoring, and analysis. Run against `oddsiq.db` with `sqlite3`.

> **Canonical home for analysis/CLV queries.** This doc owns the reusable SQL for CLV, value-bet health, and edge inspection. [[Database Inspection]] covers how to open and browse the DB and defers here for the queries themselves — don't duplicate the SQL there.

## Manual Inspection Queries

### All Bets with Full Data

```sql
SELECT g.away_team || ' @ ' || g.home_team as game,
       vb.market_type, vb.selection,
       vb.entry_odds, vb.fair_odds, vb.edge_pct,
       vb.clv_pct, vb.result, vb.flagged_at
FROM value_bets vb
JOIN games g ON vb.game_id = g.id
WHERE vb.result IS NOT NULL
ORDER BY vb.flagged_at;
```

### Average CLV

```sql
SELECT round(avg(clv_pct), 2) as avg_clv,
       count(*) as total_bets,
       sum(case when result='win' then 1 else 0 end) as wins,
       sum(case when result='loss' then 1 else 0 end) as losses
FROM value_bets
WHERE clv_pct IS NOT NULL;
```

### CLV by Market Type

```sql
SELECT market_type,
       round(avg(clv_pct), 2) as avg_clv,
       count(*) as bets
FROM value_bets
WHERE clv_pct IS NOT NULL
GROUP BY market_type;
```

### Odds API Budget Usage per Night

```sql
SELECT date(captured_at) as night,
       count(distinct captured_at) as polls
FROM odds_snapshots
WHERE bookmaker = 'pinnacle'
GROUP BY date(captured_at);
```

## Nightly Health Dashboard

From the [[Runbook]] -- run after each game night to confirm the pipeline is healthy.

### Snapshot Volume by Night

```sql
SELECT
    date(captured_at) as night,
    count(*) as snapshots,
    count(distinct game_id) as games_tracked
FROM odds_snapshots
GROUP BY date(captured_at)
ORDER BY night DESC LIMIT 7;
```

**Note:** raw row volume is no longer a clean health signal. Snapshot dedup (post-2026-04-27) bumps `captured_at` on rows whose `(odds, line)` match the previous poll for the same `(game_id, bookmaker, market_type, selection)` — stable-line nights add far fewer rows than volatile-line nights even when polling is healthy. For coverage health, prefer the in-memory `_last_game_coverage_success[(game_id, scraper_name)]` timestamps surfaced by the [[Pipeline Services|CoverageMonitor]]'s `check_gaps()` (a `Coverage gap:` WARNING log — no phone alert — when a scraper stops matching a warm/hot-tier game). This query is still useful as a coarse "did anything land at all" check.

### Value Bet Health by Night

```sql
SELECT
    date(flagged_at) as night,
    count(*) as bets_flagged,
    sum(case when clv_pct IS NOT NULL then 1 else 0 end) as clv_populated,
    sum(case when result IS NOT NULL then 1 else 0 end) as results_resolved,
    round(avg(clv_pct), 2) as avg_clv
FROM value_bets
GROUP BY date(flagged_at)
ORDER BY night DESC LIMIT 7;
```

Check: CLV should be populated on bets whose closing snapshot was captured within `CLOSING_FRESHNESS_MINUTES` (default 30) **before** effective tipoff (the gate rejects snapshots both too old and post-tipoff). **NULL `clv_pct` is not always failure** — the freshness gate intentionally leaves `is_closing=0` (and therefore `clv_pct` NULL) when the latest snapshot is too stale or was captured after tipoff, so the bet shows up here as "missing CLV" but that's correct behavior. To distinguish capture failure from gate rejection, query `is_closing` on the bet's game: if no snapshot was marked closing for the bookmaker/market/selection but snapshots exist near tipoff, the gate ran and rejected; if no snapshots exist near tipoff at all, polling actually missed the window. NULL `result` on final games still means the resolver didn't run (clean failure).

## See Also

- [[SQLite Store]] -- database engine and WAL mode details
- [[Data Model]] -- schema for `games`, `odds_snapshots`, `value_bets`
- [[Database Inspection]] -- how to open and browse the database
- [[CLV Analysis Framework]] -- analysis process that uses these queries
