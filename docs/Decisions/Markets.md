# Markets — Pinnacle scope reduction

> **Accumulated decision log** for choices made now that might be worth revisiting later. Not specs, not plans. Short, fact-heavy, reversible.

---

## TL;DR

The Pinnacle API call was reduced to `h2h` (moneyline) only. That cut the quota cost per request by 3× with no loss of real value, and it is reversible by changing one environment variable.

---

## Decision (2026-04-22)

- **What changed:** `oddsiq/scrapers/pinnacle.py`, the `markets` parameter of the Odds API call
  - From: `"h2h,spreads,totals"`
  - To: `"h2h"` (default, via the `PINNACLE_MARKETS` env var)
- **Effect:** Pinnacle stops returning spread/total data, so those snapshots are no longer written to `odds_snapshots`. `find_edges()` then has no Pinnacle baseline for spread/total and generates no edges for those markets.
- **Tipsport scraper unchanged at the time:** it still fetched all three markets, since its requests do not count against the budget. (It has since become moneyline-only.)

---

## Context (why)

### Budget math

The Odds API charges `regions × markets` per request. The original call:

```
?regions=us&markets=h2h,spreads,totals&bookmakers=pinnacle
```

= 1 × 3 = **3 quota per request**.

After the change: 1 × 1 = **1 quota per request**.

### Production observation (night of 2026-04-21 → 2026-04-22)

- 42 Pinnacle requests (counted from the log)
- Old quota cost: 42 × 3 = 126 units
- New cost would be: 42 × 1 = 42 units
- Free tier of 500/month: from ~3–4 nights to ~12 nights

### Data-driven argument for the drop

Database state at the time of the change:

| Market    | Pinnacle snapshots (48h) | Tipsport snapshots (48h) | Value bets ever |
| --------- | ------------------------ | ------------------------ | --------------- |
| Moneyline | 56                       | 76                       | **7**           |
| Spread    | 58                       | 77                       | **0**           |
| Total     | 60                       | 67                       | **0**           |

Spread/total data was collected at the same rate but produced **zero value bets** across 11 days of production. The reasons (see Architecture Decisions §8.4):

1. **Line mismatch:** Pinnacle and Tipsport offer different lines for spreads and totals. Edge detection requires an exact match (after `abs(line)` normalization), and they rarely match.
2. Even when they match, the edge is rarely ≥ 3%. Tipsport's spread/total prices are relatively tight.
3. Most value is in moneylines, where lines always match.

**Conclusion:** the system was collecting data that produced no signal, at the cost of two thirds of the Pinnacle budget.

---

## What we gave up

1. **Historical Pinnacle spread/total odds** after the change. Retrospective analyses such as "what if line interpolation had been implemented" have no Pinnacle baseline for the period after the drop.
2. **The ability to detect spread/total value bets** until the change is reversed. Since those markets had produced no bets, the practical loss was close to zero.

---

## What we gained

1. **An immediate 3× saving in Pinnacle quota.**
2. **Effectively no budget constraint** for normal use:
   - 3-game night: 126 → 42 quota
   - 15-game night: ~135 → ~45 quota
   - The free tier of 500 would cover a whole month even in a busy NBA season.
3. **A cleaner mental model:** "moneyline only" matches reality. The operator only bets moneylines, and every value bet so far had been a moneyline.

---

## Triggers to revisit

Consider restoring `PINNACLE_MARKETS=h2h,spreads,totals` when:

1. **More Czech bookmakers are added** and their spread/total lines match Pinnacle more often. Multi-book coverage could change the match rate.
2. **Line interpolation is implemented**, so mismatched lines can still be compared.
3. **The Odds API plan is upgraded** ($20/month, 10,000 quota). The budget constraint then disappears and there is no reason to limit scope.
4. **Empirical signal:** if it ever turns out that spread/total bets are wanted, that is a sign the data is missing.

### How to check whether restoring is worth it

After adding another bookmaker, compare its spread/total lines with Tipsport's. If the inter-book match rate for spreads/totals exceeds ~60%, restoring Pinnacle fetching makes sense, because there are more chances of a match.

> **Note (2026-05-31):** Trigger #1 effectively fired when Betano, Allwyn and Fortuna shipped. The match-rate check against Pinnacle is moot for now, though: with `PINNACLE_MARKETS=h2h` there is no Pinnacle spread/total baseline, so it cannot be computed without first restoring `PINNACLE_MARKETS=h2h,spreads,totals`. Until the scope is widened again, the check can only be done Tipsport-versus-Czech-books, without Pinnacle.

---

## How to reverse

1. In `.env` (or the `.env.example` default), set `PINNACLE_MARKETS=h2h,spreads,totals`.
2. Restart the pipeline.

No code changes and no database migrations are needed. Data from the gap period is missing, but collection resumes immediately.

---

## Note (2026-06-02)

The multi-book era (Betano/Allwyn/Fortuna) was archived on 2026-06-02 in the multi-sport / live-first refactor (kept in the pre-release history). Tipsport is again the single soft book. The system remains moneyline-only (`PINNACLE_MARKETS=h2h`), so the inter-book spread/total match-rate question raised in "Triggers to revisit" §1 (and the 2026-05-31 note) is now fully retired — there are no other Czech books to compare spread/total match rates against. If spread/total is revisited in the future, it would be within a single-book scope.

---

## Related

- `oddsiq/scrapers/pinnacle.py` — where the change lives
- `docs/Architecture/Architecture Decisions.md` §8.4 Exact Line Matching — design rationale for the exact-match requirement
- Terminal log of the night of 2026-04-21 — evidence for the 42 requests × 3 quota calculation
