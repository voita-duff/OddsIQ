---
tags:
  - component/data
  - layer/tracking
  - type/component
source-files:
  - oddsiq/db/models.py
  - oddsiq/db/store.py
  - oddsiq/pipeline/edge_service.py
  - oddsiq/pipeline/settlement_service.py
  - oddsiq/engine/clv.py
related:
  - "[[Edge Detection]]"
  - "[[CLV Calculation]]"
  - "[[Vig Removal]]"
  - "[[Pipeline Services]]"
  - "[[SQLite Store]]"
  - "[[Data Model]]"
  - "[[Telegram Notifications]]"
---

# ValueBet Lifecycle

## Purpose

Documents the full lifecycle of a `ValueBet` record — from initial edge detection through CLV enrichment at tipoff to final win/loss/push/draw resolution. ValueBets are the primary output of the system: each one represents a flagged +EV opportunity.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/db/models.py` | `ValueBet` | Dataclass; `sport` stamped from parent game; `tier` records detection tier; `stake` reserved for P6 (always NULL today) |
| `oddsiq/db/store.py` | `Store.insert_value_bet()` | `INSERT ... ON CONFLICT(...) DO NOTHING`; returns `id` on insert, `None` on conflict |
| `oddsiq/db/store.py` | `Store.update_value_bet_closing()` | Sets `closing_odds`, `pinnacle_closing`, `clv_pct` at tipoff |
| `oddsiq/db/store.py` | `Store.update_value_bet_result()` | Sets `result` (`"win"`/`"loss"`/`"push"`/`"draw"`) after game final |
| `oddsiq/db/store.py` | `Store.get_value_bets_without_clv()` | Returns ValueBets with `clv_pct IS NULL` for a game — used by CLV path and recovery |
| `oddsiq/db/store.py` | `Store.get_pending_value_bets()` | Returns ValueBets with `result IS NULL` — used by result-resolution path |
| `oddsiq/pipeline/edge_service.py` | `EdgeService.detect_pregame_edges()` | Wraps `find_edges()` → ValueBet creation → `value_bet:` log; returns the cycle's clean `PendingBet` list |
| `oddsiq/pipeline/edge_service.py` | `EdgeService.dispatch_pending()` | Hands that list to `BettingService.on_value_bet()`, earliest kickoff first |
| `oddsiq/pipeline/settlement_service.py` | `SettlementService.calculate_clv_for_pending_bets()` | CLV enrichment at tipoff |
| `oddsiq/pipeline/settlement_service.py` | `SettlementService.resolve_bets()` | Result resolution at game final; settles the linked `placed_bets` row for real-money P&L |
| `oddsiq/engine/clv.py` | `calculate_clv()`, `resolve_bet_result()` | CLV math + win/loss/push/draw resolution |

## Inputs

### At Creation (Edge Detection)
- `game_id` — which game the edge was found on
- `bookmaker` — e.g. "tipsport"
- `market_type` — "moneyline", "spread", or "total"
- `selection` — "home", "away", "over", or "under"
- `line` — spread/total line (NULL for moneyline)
- `entry_odds` — Czech book odds at flag time
- `fair_odds` — Pinnacle fair (vig-removed) odds at flag time
- `edge_pct` — `(entry_odds / fair_odds - 1) * 100`
- `tier` — "hot" or "warm" (detection tier at flag time)
- `sport` — from `game.sport`

### At Tipoff (CLV Enrichment)
- `closing_odds` — Czech book closing odds for the selection
- `pinnacle_closing` — Pinnacle fair closing odds (after [[Vig Removal]])
- `clv_pct` — `(entry_odds / pinnacle_closing - 1) * 100`

### At Game Final (Resolution)
- `result` — "win", "loss", "push", or "draw" based on final scores

## Outputs

- Fully resolved `value_bets` row with edge, CLV, and result
- Log lines at every stage — `value_bet:` / `value_bet_suspect:` at flag time, `clv:` / `clv_suspect:` at tipoff, `result:` / `result_suspect:` at final. None of these reach the phone (2026-08-10 rebrand)
- For a **clean** detection: a `placed_bets` decision row — either `skipped` with the qualification-failure reason, or an execution attempt through `pending → placed | rejected | failed | unknown → settled`
- Telegram only for the real-money events on that row (placed, rejected/failed, won/lost) — see [[Telegram Notifications]]
- Data for long-term profitability analysis via [[Dashboard]] and audit CLI

## Interactions

- [[Edge Detection]] (`find_edges()`) creates the initial edge data
- `EdgeService` converts edge dicts to ValueBet records and inserts via [[SQLite Store]]
- `EdgeService.dispatch_pending()` hands clean detections to [[Pipeline Services|BettingService]], which writes the `placed_bets` decision row
- [[Telegram Notifications]] messages only real-money events (bet placed, rejected/failed, won/lost) — never the detection, CLV, or a paper settlement
- [[CLV Calculation]] enriches the record at tipoff (closing odds + CLV)
- `resolve_bet_result()` resolves the record after game final
- [[Vig Removal]] is used to derive fair closing odds from the Pinnacle pair
- References a [[Game Lifecycle|Game]] via `game_id` foreign key

## Internal Logic

### Full Lifecycle

```
find_edges() detects edge >= 3%  ->  ValueBet(entry_odds, fair_odds, edge_pct, tier, sport)
                                         |
                   Store.insert_value_bet()
                   (ON CONFLICT DO NOTHING -- dedup by game/book/market/selection/line/tier)
                                         |
             log value_bet:  (or value_bet_suspect: if guard-stamped)
                                         |
           +-----------------------------+-----------------------------+
           |                             |                             |
     At tipoff:                    At game final:              If CLEAN, same cycle:
     get_value_bets_without_clv()  resolve_bet_result()        PendingBet -> dispatch_pending()
     get_closing_odds(bet side)    result = win|loss|push|draw   -> BettingService.on_value_bet()
     get_closing_odds(opp side)    update_value_bet_result()     -> placed_bets row:
     remove_vig(pin_sel, pin_opp)  log result:                      skipped(reason)
     calculate_clv()               Telegram: format_bet_result()    | pending -> placed
     update_value_bet_closing()      ONLY if a placed_bets row         | rejected | failed
     log clv:                        reached status='settled'          | unknown -> settled
```

### Stage 1: Edge Detection and Creation

[[Edge Detection]] (`find_edges()`) runs during warm-tier polling (1–2h to tipoff) AND hot-tier polling (< 1h to tipoff). When an edge >= `EDGE_THRESHOLD` (default 3%) is found:

1. `find_edges()` returns a dict with `bookmaker`, `market_type`, `selection`, `line`, `czech_odds`, `fair_odds`, `edge`
2. `EdgeService` converts this to a `ValueBet` (tagged with the detection `tier` and `game.sport`) and calls `Store.insert_value_bet()`
3. `insert_value_bet()` uses `ON CONFLICT DO NOTHING` on the unique index `(game_id, bookmaker, market_type, selection, IFNULL(line, -999.0), tier)`. Returns `None` if the bet already exists — dedup survives restarts, no in-memory set needed.
4. If insert succeeds (new bet), `EdgeService` logs `value_bet:` (book, game, market, selection, line, edge, tier) and prints a console `VALUE BET` block — **no Telegram**. A bet stamped by a pregame guard logs `value_bet_suspect:` / prints `SUSPECT VALUE BET` and stops there.
5. A **clean** bet is appended to the cycle's `PendingBet` list. After the poll cycle closes, `EdgeService.dispatch_pending()` sorts by `(kickoff, -edge)` and awaits `BettingService.on_value_bet()` for each in turn — so every bet is qualified against the clock at *its* dispatch moment, not at detection (two-phase dispatch, 2026-09-06). Each dispatch is individually try/excepted (`bet_dispatch_failed:`).

### Stage 2: CLV Enrichment at Tipoff

When `SettlementService.handle_game_live()` fires for the game (see [[Game Lifecycle]]):

1. `mark_closing_snapshots(game_id)` marks the last odds snapshot per (bookmaker, market, selection, line) as closing
2. `calculate_clv_for_pending_bets()` uses `get_value_bets_without_clv()` (not `get_pending_value_bets()`) so a bet whose result was set before its CLV can still be reached:
   - Get the bookmaker closing odds for the bet's selection via `get_closing_odds(game_id, bookmaker, market_type, selection, line)`
   - Get Pinnacle closing odds for the bet's selection (`pin_sel`) — same line
   - Get Pinnacle closing odds for the **opposite** selection (`pin_opp`) — opposite signed line for spreads (e.g. home -4.5 pairs with away +4.5)
   - Remove vig from the Pinnacle pair via [[Vig Removal]]: `fair_sel, _fair_opp = remove_vig(pin_sel.odds, pin_opp.odds)`. **Argument order matters**: `pin_sel` is always passed first so `fair_sel` is the wanted side. Swapping args gives the wrong CLV.
   - `clv = (entry_odds / fair_sel) - 1.0`
3. `update_value_bet_closing()` sets `closing_odds`, `pinnacle_closing` (= `fair_sel`), `clv_pct`
4. Logs `clv:` — or `clv_suspect:` for a guard-stamped bet, which still writes CLV to the DB but stays out of session stats. Either way nothing is sent to the phone

### Stage 3: Resolution at Game Final

When `check_completed_games()` finds the game has gone final:

1. `SettlementService.resolve_bets()` returns immediately unless both final scores are present, then walks `get_pending_value_bets(game.id)` (result IS NULL). For each, `resolve_bet_result()` determines the outcome:
   - **Moneyline:** direct win/loss by score comparison; **draw** when scores are tied (an NFL game still tied after OT, or an NHL/MLB score the API reports level, does not double-loss)
   - **Spread:** `margin = my_score - opp_score + line`; >0 win, <0 loss, =0 push
   - **Total:** `home_score + away_score` vs line; over/under comparison; push when equal
2. `update_value_bet_result()` sets `result` to "win", "loss", "push", or "draw", then `settle_placed_bet(vb.id, result)` settles the linked auto-betting decision — a no-op unless a `placed` row exists for this value bet
3. A guard-stamped (`excluded_reason`) bet logs `result_suspect:` and stops there: the result is written for guard validation, but there is no console print and no WIN/LOSS Telegram, mirroring the suppressed detection
4. Otherwise the result prints and logs `result:`. Then `get_placed_bet_for_value_bet()` runs: if — and only if — a `placed_bets` row exists at status `settled`, [[Telegram Notifications]] sends the won/lost message with real-Kč P&L and the cumulative `Total:` from `cumulative_realized_pl_czk()`. A paper value bet settles silently

(There is no live-candidate settlement step: the live-edge subsystem — and the settle call `resolve_bets()` used to open with — was removed on 2026-08-27. See [[Live-First]].)

### Record State at Each Stage

| Field | After Creation | After Tipoff | After Final |
| --- | --- | --- | --- |
| `entry_odds` | Set | Set | Set |
| `fair_odds` | Set | Set | Set |
| `edge_pct` | Set | Set | Set |
| `tier` | Set | Set | Set |
| `sport` | Set | Set | Set |
| `closing_odds` | NULL | Set | Set |
| `pinnacle_closing` | NULL | Set | Set |
| `clv_pct` | NULL | Set | Set |
| `result` | NULL | NULL | Set |
| `stake` | NULL | NULL | NULL (always) |

### Recovery Path

`SettlementService.recover_historical_bets()` runs once at startup. It uses `get_final_games_with_incomplete_bets()` (joins by `clv_pct IS NULL OR result IS NULL`) so a bet stranded outside the 3-day Odds API window (too old for the normal refresh path) is still backfilled. The CLV sub-path uses `get_value_bets_without_clv()` rather than `get_pending_value_bets()` for the same reason: `get_pending_value_bets()` filters `result IS NULL` and cannot reach a bet whose result was already set without CLV.

## Configuration

| Constant | Value | Notes |
| --- | --- | --- |
| `EDGE_THRESHOLD` | 0.03 (3%) | Minimum edge to create a ValueBet — see [[Edge Detection]] |
| `CLOSING_FRESHNESS_MINUTES` | 30 min | Governs whether a closing snapshot exists to compute CLV against |

## Error Handling

- **Duplicate prevention:** `ON CONFLICT DO NOTHING` on `(game_id, bookmaker, market_type, selection, IFNULL(line, -999.0), tier)` — distinct spread/total lines and distinct warm/hot tier observations coexist as independent rows.
- **Missing closing data:** If Pinnacle closing odds are unavailable (poll failure near tipoff), `clv_pct` remains NULL. CLV is skipped rather than fabricated.
- **Missing opposite selection:** Cannot remove vig without both sides of the Pinnacle pair — CLV not calculated.
- **Pipeline not running at tipoff:** `handle_game_live()` never fires; closing snapshots never marked; CLV never calculated. `recover_historical_bets()` handles this at next startup.

## Known Issues

- **`stake` always NULL (Phase 1):** The `stake` field exists in the schema and dataclass but nothing writes to it. Kelly staking is a Phase 6 feature.
- **Single detection per market/selection/line/tier per game:** The unique index means the first detected edge for a given line+tier is stored. If the edge changes at the same line+tier in subsequent polls, the original entry is preserved (ON CONFLICT DO NOTHING). Line moves store as new rows.

## Tests

ValueBet lifecycle is tested through:

- `tests/test_store.py` — insert, duplicate handling, closing update, result update, `get_value_bets_without_clv` vs `get_pending_value_bets`
- `tests/test_edge.py` — edge detection that produces ValueBet candidates
- `tests/test_clv.py` — CLV calculation and bet resolution (incl. "draw" for moneyline ties)
- `tests/test_edge_service.py`, `tests/test_settlement_service.py` — service-layer orchestration
