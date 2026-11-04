---
tags: [type/spec, phase/evaluation]
related:
  - "[[Dashboard]]"
  - "[[SportProfile]]"
  - "[[Scheduler]]"
  - "[[Edge Detection]]"
---

# Upcoming Dashboard Tab — Design

> Status: design, approved 2026-06-08. Adds a read-only **Upcoming** tab to the P3 dashboard: a 24-hour, multi-sport schedule view answering "when should I be active to manually bet" by surfacing each game's **bet window** (the warm/hot tier when Tipsport-vs-Pinnacle edges become detectable), not just its tipoff. Pure read on the `games` table the pipeline already fills. Stays in evaluation mode: NO new scrapers, NO network calls, NO writes, NO new config, NO auto-betting.

## Problem

The operator bets manually on [[TipsportScraper|Tipsport]] vs Pinnacle moneyline edges, but the dashboard has no forward-looking view. `/live` shows in-progress games, `/bets` shows flagged value bets + confirmed live edges, `/analysis` shows CLV history — nothing says *"here is what's coming in the next 24 hours, across all the sports I run, and when I need to be at the keyboard."* The actionable moment is not tipoff: pregame [[Edge Detection|edge detection]] only runs in the **warm** (1–2h to tipoff) and **hot** (<60m) tiers, so value bets begin surfacing ~2h before tipoff and intensify in the final hour. A schedule view that ignores this would point the operator at the wrong time.

## Goals

1. **A 24-hour schedule view** of upcoming games across every implemented sport.
2. **Surface the bet window** per game — derive `warm`/`hot`/`opens-HH:MM` from the same `pipeline_tiers.tier_for()` the pipeline uses — so the operator sees *when edges go live*, not just when the ball tips.
3. **Forward-compatible with new sports** — render a tile for every enabled sport, including ones with zero games right now, so the layout auto-extends when sports are added to the registry.
4. **Auto-refresh hourly + a manual refresh button**, consistent with the existing htmx partial pattern.

## Non-Goals (project-mode guardrails)

- **No live API fetch.** The tab reads the `games` table that `OddsApiScheduleSource` already fills via the 24/7 pipeline. No Odds API quota is spent; the dashboard's read-only invariant is preserved. (Rejected alternative: dashboard re-fetching the schedule on each refresh — spends quota, breaks read-only, double-fetches, and cannot see further ahead than the API already returns.)
- **No per-game odds or edge values.** This is a schedule/clock view, not a price view. Pregame Tipsport odds aren't reliably present until the warm tier anyway.
- **No client-side live countdown ticking.** Countdowns/badges are server-rendered (accurate as of last refresh); the manual button gives an instant update. Keeps the JS surface at zero — it is a local dashboard.
- **No change to the root redirect** (`/` still → `/live`) and no pipeline changes.
- No new bookmakers, no new sports, no new env/config vars, no schema changes.

## Design

### Data flow

Read-only, identical in shape to every other dashboard partial: one new query function → one page route → one partial route → two templates → one nav link. The pipeline's `OddsApiScheduleSource` already upserts every upcoming game (`status='scheduled'`, with `commence_time`, `home_team`, `away_team`, `sport`) into `games`; the tab reads it.

### Query — `queries.upcoming_games(store, *, sport=None, now=None)`

Follows the `coverage_matrix` / `active_game_tiers` precedent (fetch rows, parse + window in Python) rather than filtering datetimes in SQL — SQLite's `datetime('now')` (space-separated, no offset) does not string-compare reliably against stored ISO `commence_time` (`...T...+00:00`), and Python-side windowing also cleanly drops the known stale `scheduled` rows whose `commence_time` is already in the past.

- **Input:** `sport` (optional `?sport=` filter, applies to the agenda only); `now` injectable (defaults to `datetime.now(timezone.utc)`) for hermetic tests.
- **Fetch:** `SELECT ... FROM games WHERE status='scheduled'`, ordered by `COALESCE(commence_time, scheduled_at)`. The `sport` filter is applied in Python to the agenda only, never to the tiles (the strip is the cross-sport overview).
- **Window:** keep games with `now < tipoff ≤ now + 24h` **and** `sport ∈ ENABLED_SPORTS` (so a leftover row for a since-removed sport can never appear in the agenda; tiles iterate `ENABLED_SPORTS` directly, so they're already bounded).
- **Tier + window label** per game via `pipeline_tiers.tier_for(ttip_seconds)`:
  - `far` (>7200s): `window_label = "opens HH:MM"` (HH:MM = `tipoff − 2h`, Prague), muted.
  - `warm` (3600–7200s): `window_label = "WARM"`, `.warn`.
  - `hot` (0–3600s): `window_label = "HOT"`, `.ok`.
- **Grouping** (ordered, only non-empty groups returned):
  - `open` — tier ∈ {`warm`, `hot`} (tipoff ≤ 2h, "edges live now").
  - `today` — remaining games whose tipoff is today in Prague.
  - `tomorrow` — remaining games (within a 24h horizon, non-today is at most tomorrow Prague).
  - Within each group, sort by tipoff ascending.
- **Times:** formatted in `Europe/Prague` (a module-level `_DISPLAY_TZ` mirroring `server.py`'s).

**Return contract:**

```python
{
  "tiles": [                       # ALWAYS one per ENABLED sport (count incl. zero), + a TOTAL tile
    {"sport": "nba", "display": "NBA", "count": 2, "next_local": "19:00"},  # next_local None -> "—"
    ...,
    {"sport": "total", "display": "TOTAL", "count": 6, "next_local": None},
  ],
  "groups": [                      # non-empty only, ordered open -> today -> tomorrow
    {"key": "open", "label": "Bet window open", "games": [game, ...]},
    ...
  ],
  "total": 6,
}
# game = {"sport", "display", "away", "home",
#         "tipoff_local": "19:00", "countdown": "in 6h 22m",
#         "tier": "hot|warm|far", "window_label": "HOT"|"WARM"|"opens 22:40"}
```

Tiles are built over **all enabled sports regardless of `sport`** (the strip is the cross-sport overview); the agenda `groups` respect the `sport` filter. `display` = `get_profile(sport).display_name`. Countdown format: `"in {m}m"` when <1h, else `"in {h}h {m}m"`.

### Server (`server.py`)

- `GET /upcoming` → `templates/upcoming.html` with `{page:"upcoming", sport:_norm_sport(sport), enabled_sports:ENABLED_SPORTS}` (mirrors the other tab routes).
- `GET /upcoming/partials/board` → `templates/partials/upcoming_board.html` with the `upcoming_games(...)` result + `sport`.
- No emoji in server code; emoji is a presentation concern handled in the template.

### Templates

Layout C, reusing the existing 36-line CSS (`.grid`, `.card`, `.section`, `.big`, `.small`, `.ok`/`.warn`) — **no new CSS**.

- **`upcoming.html`** extends `base.html`; one placeholder section `#upcoming-board` (see Refresh).
- **`partials/upcoming_board.html`** is itself the `<section id="upcoming-board">` (swapped via `outerHTML`, matching `recent_value_bets.html`):
  - **Summary strip:** a `.grid` of `.card` tiles, one per `tiles` entry. Emoji **only here**, via an inline Jinja map `{"nba":"🏀","nhl":"🏒","mlb":"⚾"}` with a generic fallback (e.g. `"🏟"`) for future sports. Each tile: emoji + `display` + `count` games + `next HH:MM` (or `—`). Active sport tile highlighted when `sport` matches.
  - **Agenda:** for each group, a sub-heading (`label`) then a plain text row per game (no emoji): `tipoff_local` · `display` · `away @ home` · `countdown` · `window_label` (colored span by tier). Empty overall → "No games scheduled in the next 24h."
  - **Manual refresh button** "⟳ Refresh upcoming": `hx-get="/upcoming/partials/board{?sport}"`, `hx-target="#upcoming-board"`, `hx-swap="outerHTML"` — works on demand even when auto-refresh is off.

### Refresh

The page-shell section carries `hx-trigger="load, every 3600s from:body[data-autorefresh='1'], refreshAll from:body"` and `hx-swap="outerHTML"` (hourly auto, gated by the global autorefresh toggle; responds to the global "Refresh all"; the `every` clause self-heals a failed initial `load`, per the existing regression test). The swapped-in partial carries the recurring `every 3600s` + `refreshAll` triggers (no `load`), matching the established two-part pattern.

### Nav + sport filter

Add `Upcoming` as the **first** nav link in `base.html` (`Upcoming → Live → Bets → Analysis`); root redirect unchanged. The existing `?sport=` sport tabs already render on every page; the Upcoming partial threads `sport` through its htmx URLs exactly like the other tabs.

## Edge cases

- **Stale `scheduled` rows** with past `commence_time` (observed: 14 NBA) — excluded by the `now < tipoff` window.
- **`live` / `final` games** — excluded (status filter); they belong to `/live` and `/bets`.
- **A `scheduled` row that is really stale-but-future** (pipeline hasn't transitioned it) — may appear; acceptable for a schedule view, and the pipeline transitions it. Not cleaned here.
- **All sports empty in 24h** — strip renders all-zero tiles; agenda shows the empty-state line.
- **Future sport in the registry with no emoji** — generic fallback emoji; everything else (count, agenda) works unchanged.

## Testing

- **`test_dashboard_queries.py`** (ephemeral SQLite + injected `now`): seed `scheduled` games at +0.5h (hot), +1.5h (warm), +5h (far/today), +26h (excluded), −1h (excluded stale), plus a `live` and a `final` (excluded), across ≥2 sports. Assert: tile per enabled sport incl. a zero-count one, tile `count`/`next_local`, group membership (`open`/`today`/`tomorrow`) + ascending order within groups, `window_label` per tier, total, and that `sport=` filters the agenda but not the tiles.
- **`test_dashboard_server.py`** (`TestClient`): `/upcoming` → 200 + `data-page="upcoming"`; the `#upcoming-board` section's `hx-trigger` contains `every` (self-heal); nav contains the `Upcoming` link; `/upcoming/partials/board` → 200.

## Docs to update

- `docs/Components/Dashboard.md` — document the Upcoming tab.
- the project notes — add `/upcoming` to the tab list (Status section + the `oddsiq dashboard` CLI line).
- Any hard-coded test/route counts; then `make audit` to clear drift.

## File-by-file change list

| File | Change |
|---|---|
| `oddsiq/dashboard/queries.py` | + `upcoming_games(...)`, + `_DISPLAY_TZ` (mirrors server) |
| `oddsiq/dashboard/server.py` | + `/upcoming` route, + `/upcoming/partials/board` route |
| `oddsiq/dashboard/templates/upcoming.html` | new page shell |
| `oddsiq/dashboard/templates/partials/upcoming_board.html` | new partial (strip + agenda + refresh button) |
| `oddsiq/dashboard/templates/base.html` | + `Upcoming` nav link |
| `tests/test_dashboard_queries.py` | + `upcoming_games` tests |
| `tests/test_dashboard_server.py` | + `/upcoming` route + nav tests |
| `docs/Components/Dashboard.md`, the project notes | doc updates |
