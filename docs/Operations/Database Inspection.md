---
tags:
  - type/runbook
  - layer/operations
  - status/complete
  - phase/p0
source-files:
  - oddsiq/db/store.py
  - oddsiq/db/migrations/__init__.py
  - oddsiq/db/migrations/000_baseline.py
  - oddsiq/db/migrations/005_add_sport_to_tables.py
related:
  - "[[SQLite Store]]"
  - "[[Data Model]]"
  - "[[Migrations]]"
---

# Database Inspection

SQLite queries for inspecting the OddsIQ database during and after game night sessions. Open the database with:

```bash
sqlite3 oddsiq.db
```

The schema has 6 tables: `games`, `odds_snapshots`, `value_bets`, `placed_bets`, `meta`, `schema_version`. (The `live_edge_candidates` table was dropped by migration 011 on 2026-08-27 with the live-edge subsystem removal; the dataset is archived offline.) The data tables carry a `sport` column (added in migration `005_add_sport_to_tables.py`) — filter or group by it to separate NBA, NHL, MLB, and WNBA data. See [[Data Model]] for the full schema.

## Games Today (all sports)

```sql
SELECT sport, home_team, away_team, status, home_score, away_score
FROM games
WHERE date(scheduled_at) = date('now')
ORDER BY sport, scheduled_at;
```

Shows all games for the current date with their status (`scheduled`, `live`, `final`, `postponed`) and final scores. Useful for confirming the pipeline picked up tonight's slate.

## Games by Sport Summary

```sql
SELECT sport, status, count(*) as cnt
FROM games
GROUP BY sport, status
ORDER BY sport, status;
```

## Value Bets This Session

```sql
SELECT g.sport,
       g.away_team || ' @ ' || g.home_team as game,
       vb.market_type, vb.selection, vb.entry_odds, vb.fair_odds,
       vb.edge_pct, vb.clv_pct, vb.result, vb.tier
FROM value_bets vb JOIN games g ON vb.game_id = g.id
ORDER BY vb.flagged_at DESC;
```

Lists all flagged value bets with their [[Edge Detection]] data, [[CLV Calculation]] result, and win/loss outcome. The `clv_pct` column is NULL until the game tips off; `result` is NULL until the game goes final. The `tier` column records whether the bet was flagged in `warm` (1–2h) or `hot` (<60 min) tier.

## Odds History for a Game

```sql
SELECT bookmaker, market_type, selection, line, odds, is_closing,
       source_updated_at, captured_at, raw_response_path
FROM odds_snapshots
WHERE game_id = ?
ORDER BY captured_at;
```

Replace `?` with the game ID from the games query. Shows the full time series of odds observations from Pinnacle and Tipsport. Rows with `is_closing = 1` are the closing snapshots used for CLV calculation.

## CLV Distribution

```sql
SELECT g.sport,
       round(vb.clv_pct, 1) as clv,
       count(*) as cnt
FROM value_bets vb JOIN games g ON vb.game_id = g.id
WHERE vb.clv_pct IS NOT NULL
GROUP BY g.sport, round(vb.clv_pct, 1)
ORDER BY g.sport, clv;
```

Histogram of CLV values grouped by sport. Consistently positive CLV indicates the model is finding real value. See [[Health Monitoring]] for interpretation guidance.

## Snapshot Volume by Night and Sport

```sql
SELECT date(captured_at) as night, sport, count(*) as snapshots, count(distinct game_id) as games
FROM odds_snapshots
GROUP BY date(captured_at), sport
ORDER BY night DESC, sport
LIMIT 21;
```

## Live Edge Candidates — table dropped 2026-08-27

The `live_edge_candidates` table (the live scout's time series) was dropped by migration 011 with the live-edge subsystem removal. Query the offline archived copy if the historical dataset is needed; backups predating 2026-08-27 also still contain it.

## Schema Version Check

```sql
SELECT version FROM schema_version ORDER BY version DESC LIMIT 1;
```

Current migrations are `000` through `013`. Migration `012` added sharp-source timestamps to snapshots/value bets; migration `013` lets a clean detection coexist with an earlier excluded observation in the same tier.

## Quick Health Check

```sql
SELECT 'final_games',          count(*)               FROM games            WHERE status='final'
UNION ALL
SELECT 'value_bets_total',     count(*)               FROM value_bets
UNION ALL
SELECT 'with_clv',             count(*)               FROM value_bets       WHERE clv_pct IS NOT NULL
UNION ALL
SELECT 'with_result',          count(*)               FROM value_bets       WHERE result  IS NOT NULL
UNION ALL
SELECT 'avg_clv_pct',          round(avg(clv_pct), 2) FROM value_bets       WHERE clv_pct IS NOT NULL
UNION ALL
SELECT 'snapshot_range',       min(captured_at) || ' to ' || max(captured_at) FROM odds_snapshots;
```

## See Also

- [[SQLite Store]] — database implementation details (WAL mode, async, schema)
- [[Data Model]] — full schema definition for all six tables (`games`, `odds_snapshots`, `value_bets`, `placed_bets`, `meta`, `schema_version`)
- [[Migrations]] — forward-only migration runner (`000`–`013`)
