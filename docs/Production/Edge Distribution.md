---
title: Edge Distribution
date: 2026-04-10
tags:
  - type/production-data
  - phase/p1
  - layer/analysis
---

# Edge Distribution by Market Type

One game night isn't enough data to draw conclusions, but it's enough to set up the tracking framework. This table will be populated as more game nights accumulate.

## Current Data (DB snapshot, 2026-04-28)

> **Superseded:** This is a frozen 2026-04-28 snapshot. Single-book Tipsport CLV reached ~+5.87% over ~50 bets as of 2026-05-25. For up-to-date figures, re-run the [Tracking Query](#tracking-query) below rather than trusting this table.

> **Caveat (current scope):** Since `PINNACLE_MARKETS=h2h` (default since 2026-04-22; see [[Markets]]), the per-market table currently shows only moneyline value bets. Spread/total edges are not absent from the market -- they're not being scraped from Pinnacle in this period. Do not infer "spread/total are unprofitable" from this snapshot. To restore spread/total scope, widen `PINNACLE_MARKETS` to `h2h,spreads,totals` and re-run after a few game nights.

| Market Type | Edges | Edges w/ CLV | Avg Edge % | Avg CLV % | Wins | Losses |
| ----------- | ----- | ------------ | ---------- | --------- | ---- | ------ |
| moneyline   | 20    | 20           | 5.8%       | 5.8%      | 8    | 12     |

## Key Question This Data Will Answer

**Should spread line interpolation be prioritized, or is moneyline-only coverage sufficient?**

Moneyline edges require exact line matching (which always works -- moneylines have no line value). Spread and total edges require exact line matching too, but [[TipsportScraper]] and [[PinnacleScraper]] frequently offer different lines, producing zero comparisons for those markets.

### Three Scenarios

1. **> 80% of edges come from moneylines:** The exact line matching limitation is acceptable. Spread interpolation drops in priority. The multi-book expansion (Betano/Allwyn/Fortuna) should focus on moneyline odds -- broader coverage beats deeper coverage of less-available markets.

2. **Spread/total edges are frequent but missed due to line mismatch:** The `Line mismatch` log messages (at DEBUG level) will quantify how often this happens. If Pinnacle has -4.5 and Tipsport has -5.5 regularly, there's potentially uncaptured value. Line interpolation moves up in priority.

3. **Spread/total edges are rare even when lines match:** The market is efficient for these types on Tipsport, or Tipsport's lines are too close to Pinnacle's. Focus elsewhere.

## Tracking Query

```sql
SELECT market_type,
       count(*) as edges,
       count(clv_pct) as edges_with_clv,
       round(avg(edge_pct), 1) as avg_edge,
       round(avg(case when clv_pct is not null then clv_pct end), 1) as avg_clv,
       sum(case when result = 'win' then 1 else 0 end) as wins,
       sum(case when result = 'loss' then 1 else 0 end) as losses
FROM value_bets
GROUP BY market_type;
```

**Run this after every 5 game nights. Update the table above with real numbers.**

## See Also

- [[Edge Detection]] -- the algorithm that flags edges
- [[Vig Removal]] -- fair odds estimation that edges are measured against
- [[TipsportScraper]] -- soft book odds source
- [[PinnacleScraper]] -- sharp reference odds source
- [[Architecture Decisions]] -- line matching design choices and exact matching tradeoffs
