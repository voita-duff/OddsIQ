# Markets — Pinnacle scope reduction

> **Accumulated decision log** for choices made NOW that future-me might want to revisit. Not specs, not plans. Short, fact-heavy, reversible.

---

## TL;DR

Snížili jsme Pinnacle API call na pouze `h2h` (moneyline). Ušetřili jsme 3× budget při nulové ztrátě reálné hodnoty. Reverzibilní změnou jednoho env varu.

---

## Decision (2026-04-22)

- **Co změněno:** `oddsiq/scrapers/pinnacle.py` — parametr `markets` v Odds API callu
  - Z: `"h2h,spreads,totals"`
  - Na: `"h2h"` (default přes env var `PINNACLE_MARKETS`)
- **Dopad:** Pinnacle přestane vracet spread/total data. Tyto snapshots se přestanou zapisovat do `odds_snapshots`. `find_edges()` přestane mít Pinnacle baseline pro spread/total → žádné edge nevygeneruje pro tyto markety.
- **Tipsport scraper beze změny** — stále fetchuje všechny 3 markety (nezávislé na budgetu).

---

## Context (proč)

### Budget math

The Odds API účtuje `regions × markets` per request. Náš původní call:

```
?regions=us&markets=h2h,spreads,totals&bookmakers=pinnacle
```

= 1 × 3 = **3 quota per request**.

Po změně: 1 × 1 = **1 quota per request**.

### Pozorování z produkce (noc 2026-04-21 → 2026-04-22)

- 42 Pinnacle requestů (spočítáno z logu)
- Stará kvóta: 42 × 3 = 126 units
- Nová by byla: 42 × 1 = 42 units
- Free tier 500/měsíc: z ~3-4 nocí na ~12 nocí

### Data-driven argument pro drop

DB k datu change:

| Market    | Pinnacle snapshots (48h) | Tipsport snapshots (48h) | Value bets ever |
| --------- | ------------------------ | ------------------------ | --------------- |
| Moneyline | 56                       | 76                       | **7**           |
| Spread    | 58                       | 77                       | **0**           |
| Total     | 60                       | 67                       | **0**           |

Sbíráme spread/total data rovnoměrně, ale produkují **nula value betů** napříč 11 dny produkce. Důvody (per vault Edge Distribution + Architecture Decisions 8.4):

1. **Line mismatch** — Pinnacle a Tipsport nabízí různé lines pro spread/total. Edge detection vyžaduje exact match (po `abs(line)` normalizaci). Málokdy matchují.
2. I když matchují, málokdy edge ≥ 3%. Tipsport má na spread/total relativně úzké hodnoty.
3. Vault cite: "Most value is in moneylines where lines always match".

**Závěr:** sbíráme data, která produkují 0 signal, za cenu 2/3 našeho Pinnacle budgetu.

---

## What we gave up

1. **Historické Pinnacle spread/total odds** po datumu změny. Retrospektivní analýzy typu "co kdyby P3 P4 implementoval line interpolation" budou mít Pinnacle baseline chybějící pro období po dropu.
2. **Schopnost detekovat spread/total value bety** dokud nevrátíme. Vzhledem k tomu, že generovaly 0 bets historicky, praktická ztráta ~0.

---

## What we gained

1. **3× úspora Pinnacle kvóty okamžitě.**
2. **Praktická neexistence budget constraint** pro běžné používání:
   - 3-game night: 126 → 42 quota
   - 15-game night: ~135 → ~45 quota
   - Free tier 500 pokryje celý měsíc i při husté NBA sezoně.
3. **Cleaner mental model:** "OddsIQ Phase 1 = moneyline only" matching reality (user bets jen ML, value bets historicky všechny ML).

---

## Triggers to revisit

Zvaž vrátit `PINNACLE_MARKETS=h2h,spreads,totals` když:

1. **P2 přidá Fortuna.cz / Betano.cz** a tyto knihy mají spread/total lines které častěji matchují s Pinnacle. Multi-book coverage může změnit match rate.
2. **Implementujeme line interpolation** → i mismatched lines se dají porovnat.
3. **Upgradneme na paid plan** ($20/měsíc, 10,000 quota) → budget constraint zmizí, nemáme důvod omezovat scope.
4. **Empirické sledování:** pokud kdykoliv během Phase 1 zjistíš že user bets nebo chtěl bys betovat spread/total → to je signál že nám chybí data.

### Jak ověřit zda stojí za to vrátit

Po P2: spusť Tipsport spread/total vs Fortuna spread/total comparison. Jestli inter-book match rate pro spreads/totals > ~60%, má smysl vrátit Pinnacle fetching, protože teď máme víc chancí na match.

> **Poznámka (2026-05-31):** Trigger #1 (P2 přidá další knihy) fakticky vystřelil — Betano/Allwyn/Fortuna jsou shipped. Ale spread/total match-rate check je momentálně bezpředmětný (moot): při `PINNACLE_MARKETS=h2h` nemáme žádný Pinnacle spread/total baseline, takže inter-book match rate proti Pinnacle nelze spočítat bez nejdřív vrácení `PINNACLE_MARKETS=h2h,spreads,totals`. Kontrolu lze dělat jen Tipsport-vs-Czech (bez Pinnacle), dokud nerozšíříme scope zpět.

---

## How to reverse

1. V `.env` (nebo `.env.example` default): nastav `PINNACLE_MARKETS=h2h,spreads,totals`
2. Restart pipeline.

Žádné code changes, žádné DB migrace. Historická data z gap periodu chybí, ale nový collection resume ihned.

---

---

## Note (2026-06-02)

The multi-book era (Betano/Allwyn/Fortuna) was archived on 2026-06-02 in the multi-sport / live-first refactor (kept in the pre-release history). Tipsport is again the single soft book. The system remains moneyline-only (`PINNACLE_MARKETS=h2h`), so the inter-book spread/total match-rate question raised in "Triggers to revisit" §1 (and the 2026-05-31 note) is now fully retired — there are no other Czech books to compare spread/total match rates against. If spread/total is revisited in the future, it would be within a single-book scope.

---

## Related

- `oddsiq/scrapers/pinnacle.py` — místo změny
- `docs/Production/Edge Distribution.md` — empirical data supporting the decision
- `docs/Architecture/Architecture Decisions.md` §8.4 Exact Line Matching — design rationale for exact-match requirement
- Terminal log noc 2026-04-21 — evidence pro 42 requests × 3 quota calculation
