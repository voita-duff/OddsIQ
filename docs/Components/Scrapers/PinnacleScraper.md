---
tags:
  - component/scraper
  - status/complete
  - phase/p0
  - layer/data-ingestion
  - type/component
source-files:
  - oddsiq/scrapers/pinnacle.py
  - oddsiq/scrapers/odds_api_schedule.py
  - tests/test_pinnacle.py
related:
  - "[[TipsportScraper]]"
  - "[[Edge Detection]]"
  - "[[Vig Removal]]"
  - "[[Scheduler]]"
  - "[[Data Model]]"
  - "[[Team Name Normalization]]"
  - "[[SportProfile]]"
---

# PinnacleScraper

## Purpose

Fetches Pinnacle odds via The Odds API — a third-party aggregator, not Pinnacle directly. Pinnacle serves as the sharp reference bookmaker whose vig-removed lines define "fair odds" for [[Edge Detection]]. One GET per poll cycle parameterised by [[SportProfile]]; returns `OddsSnapshot` objects for the sport's active games.

This file also covers `OddsApiScheduleSource` — the schedule and scores source that replaced balldontlie. Both components are backed by The Odds API and use the same single Odds API key.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/scrapers/pinnacle.py` | `PinnacleScraper` | Odds scraper; subclasses `BaseScraper` |
| `oddsiq/scrapers/pinnacle.py` | `PinnacleScraper.fetch_odds(game_date, profile)` | Single GET to `/sports/{profile.odds_api_key}/odds`; stamps `captured_at` at request init; updates budget from response headers |
| `oddsiq/scrapers/pinnacle.py` | `PinnacleScraper.probe_budget()` | Free `/v4/sports` probe — does not consume quota; populates `_budget_used` + `_budget_remaining` |
| `oddsiq/scrapers/pinnacle.py` | `PinnacleScraper.should_skip_request()` | True only when `_budget_remaining <= 0`; checked at top of `fetch_odds()` |
| `oddsiq/scrapers/pinnacle.py` | `PinnacleScraper.budget_total` | `used + remaining` once probe ran; falls back to `ODDS_API_MONTHLY_BUDGET` |
| `oddsiq/scrapers/pinnacle.py` | `PinnacleScraper.parse_response(data, ..., sport)` | Iterates events → bookmakers → markets → outcomes; per-event isolation (one malformed event skipped, rest continue); stamps `sport` from profile |
| `oddsiq/scrapers/pinnacle.py` | `PinnacleScraper._map_market()` | Static; (market_key, outcome_name, home, away) → (market_type, selection) |
| `oddsiq/scrapers/odds_api_schedule.py` | `OddsApiScheduleSource` | Schedule + scores source; replaces balldontlie |
| `oddsiq/scrapers/odds_api_schedule.py` | `OddsApiScheduleSource.fetch_games(sport_key)` | GET `/sports/{sport_key}/scores?daysFrom=3` — returns upcoming + live + recently-final games |

## Inputs — `PinnacleScraper`

- **API key**: single `ODDS_API_KEY` (defaults from `config.ODDS_API_KEY`)
- **HTTP endpoint**: `GET {ODDS_API_BASE_URL}/sports/{profile.odds_api_key}/odds`
  - Query params: `apiKey`, `regions=us`, `markets={PINNACLE_MARKETS}`, `bookmakers=pinnacle`
  - Default `PINNACLE_MARKETS=h2h` → moneyline only (1 quota credit per request)
  - Widen to `h2h,spreads,totals` via env var to restore 3-market scope; see `docs/Decisions/Markets.md`
- **`game_date`**: `date` object, used only for raw response storage path
- **`profile`**: [[SportProfile]] — supplies `odds_api_key` (e.g. `"basketball_nba"`, `"icehockey_nhl"`, `"baseball_mlb"`)

## Inputs — `OddsApiScheduleSource`

- **HTTP endpoint**: `GET {ODDS_API_BASE_URL}/sports/{sport_key}/scores?daysFrom=3&dateFormat=iso`
- `daysFrom=3` returns upcoming, live, and recently-completed games with `commence_time`
- Uses the same single `ODDS_API_KEY` as `PinnacleScraper`; `_fetch_endpoint` shares `oddsiq/scrapers/retry.with_backoff` (up to 3 total attempts / 2 retries, full-jitter exponential backoff, honoring a `Retry-After` header clamped to `MAX_DELAY=30s`), retrying on 429/5xx/transport errors and raising `ScheduleFetchError` immediately on a hard 4xx, an unexpected non-2xx, or a malformed (non-JSON) 2xx body. After retries are exhausted it raises `ScheduleFetchError`.

## Outputs

- `list[OddsSnapshot]` — one snapshot per (event, market, outcome)
  - `game_id=0` (placeholder, resolved by [[Scheduler]])
  - `bookmaker="pinnacle"`
  - `sport=profile.key`
  - `_home_team` / `_away_team` for game matching
  - `_commence_time` — Pinnacle-derived tipoff, used by [[Scheduler]] as the canonical tipoff source
  - `_source_event_id` — exact Odds API event id; `PollCoordinator` requires it to equal `games.external_id` before attribution
  - `source_updated_at` — upstream Pinnacle market `last_update` (falling back to bookmaker `last_update`), separate from local observation time
- Raw JSON saved to `./data/raw/pinnacle/{date}/{HHMMSS}.json`
- Budget counters updated from `x-requests-used` / `x-requests-remaining` headers

## Interactions

- [[Scheduler]] / [[Pipeline Services]] — `PollCoordinator` calls `fetch_odds(game_date, profile)` per sport on each poll cycle; `ScheduleService` calls `OddsApiScheduleSource.fetch_games(sport_key)` for schedule refresh
- [[Edge Detection]] — Pinnacle snapshots are the sharp reference; vig removed via [[Vig Removal]] before comparing against Czech book odds
- [[Data Model]] — produces `OddsSnapshot` instances stored in `odds_snapshots`
- [[Team Name Normalization]] — both `PinnacleScraper.parse_response` and `OddsApiScheduleSource._parse_scores_event` call `normalize_team(name, "pinnacle", sport)`. Pinnacle/Odds API names are generally canonical; aliases for `pinnacle` are empty.
- [[SportProfile]] — `odds_api_key` drives the `/sports/{key}/odds` and `/sports/{key}/scores` endpoint paths

## Internal Logic

### `captured_at` semantics

Stamped at **request initiation** in `fetch_odds()`, before the GET fires. Threaded through `parse_response()` into every `OddsSnapshot`. Matches [[TipsportScraper]] semantics so [[Scheduler]]'s closing-snapshot freshness gate sees consistent timestamps across bookmakers.

This is not proof that the upstream Pinnacle market itself is fresh. `parse_response()` also parses The Odds API's `market.last_update` (bookmaker timestamp as fallback) into `source_updated_at`. Missing, malformed, naive, or implausibly future values are marked as unknown freshness; the resulting edge is retained as excluded evidence but never bet. Events without a usable provider `id` are dropped, because schedule and odds use the same provider identity and fuzzy attribution is unnecessary risk.

### Budget management

Three in-memory fields track quota: `_budget_used`, `_budget_remaining`, and the derived `budget_total` property.

**Startup probe** (`probe_budget()`): hits the free `/v4/sports?apiKey=...` endpoint — does not consume quota. Updates both budget fields. Returns `{"used": int, "remaining": int}` on success, `None` on failure.

**Poll-time refresh**: every successful `fetch_odds()` call reads `x-requests-used` and `x-requests-remaining` from the response headers and logs `Odds API budget: N used / M remaining`.

**Single key, fail-soft with retry** (no rotation): `fetch_odds()` shares `oddsiq/scrapers/retry.with_backoff` — up to 3 total attempts (2 retries) with full-jitter exponential backoff (`BASE_DELAY=1s`, capped at `MAX_DELAY=30s`), honoring a server `Retry-After` header clamped to `MAX_DELAY`. HTTP `429` (rate-limited) and `5xx`/transport errors are **retried**; `401`/`403`/other-`4xx` raise via `raise_for_status` and are not retried. After retries are exhausted (or on a non-retryable error), `fetch_odds()` catches the exception, logs, and returns `[]` — that cycle's Pinnacle odds are simply empty. The [[Budget Governor]] throttles cadence to keep the single key under its monthly quota.

**Exhaustion guard** (`should_skip_request()`): returns `True` only when `_budget_remaining <= 0`. Checked at the top of `fetch_odds()` — the scraper guards itself without [[Scheduler]] involvement.

### Market mapping

`_map_market()` maps Odds API keys to internal types:
- `h2h` → `moneyline` (matched against `home_team` / `away_team` strings)
- `spreads` → `spread`
- `totals` → `total` (literal `"Over"` / `"Under"`)

### `OddsApiScheduleSource` — schedule and scores

`fetch_games(sport_key)` calls `/sports/{key}/scores?daysFrom=3`, which returns upcoming + live + recently-final games in a single response. Status is derived from `completed` flag and presence of `scores`:

- `completed=True` + both scores parse → `"final"`
- `completed=True` but scores absent/unparseable → `"live"` (conservative — never mark final without scores)
- `scores` present but `completed=False` → `"live"`
- No scores → `"scheduled"`

Team names are normalised via `normalize_team(name, "pinnacle", sport)` before building `Game` objects. The source uses the same single `ODDS_API_KEY` as `PinnacleScraper`; `_fetch_endpoint` shares `oddsiq/scrapers/retry.with_backoff` (up to 3 total attempts / 2 retries, full-jitter exponential backoff, `Retry-After` clamped to `MAX_DELAY=30s`), retrying on 429/5xx/transport errors and raising `ScheduleFetchError` immediately on a hard 4xx, an unexpected non-2xx, or a malformed (non-JSON) body. After retries are exhausted it raises `ScheduleFetchError`. Per-event parse failures return `None` (skipped, not retried).

## Configuration

| Constant | Value | Source |
|----------|-------|--------|
| `ODDS_API_KEY` | env var | `.env` — single Odds API credential |
| `ODDS_API_BASE_URL` | `https://api.the-odds-api.com/v4` | `config.py` |
| `ODDS_API_MONTHLY_BUDGET` | 20000 | `config.py` |
| `PINNACLE_MARKETS` | `h2h` (default) | env var via `config.py` |
| `RAW_DATA_DIR` | `./data/raw` | `config.py` |

## Error Handling

- `httpx.HTTPError` in `fetch_odds()` is caught, logged, and returns `[]` — never raised to [[Scheduler]].
- Retryable failures (HTTP `429`, `5xx`, transport errors) are retried via `with_backoff` (up to 3 total attempts, full-jitter backoff, `Retry-After` clamped to `MAX_DELAY=30s`); on exhaustion the final error is caught and returns `[]`.
- HTTP `401`/`403`/other-`4xx` raise via `raise_for_status` (not retried), are caught by the same handler, and return `[]` (fail-soft; no rotation).
- Budget exhaustion checked before every request via `should_skip_request()`; logs a warning and returns `[]` without making the HTTP call.
- Per-event `KeyError`/`TypeError` in `parse_response()`: the offending event is skipped; the rest of the response still produces snapshots.
- Missing event id: event is skipped and `missing_event_ids` increments on the scraper-health line.
- Missing/invalid upstream `last_update`: snapshots are marked freshness-unknown; any resulting edge is excluded as `sharp_source_unknown`. `missing_source_updates` increments on scraper health.
- `_parse_commence_time()` on malformed timestamps: logs a warning, returns `None` — the snapshot is still produced without a fallback tipoff.
- Raw response save failure: `OSError` caught inside `BaseScraper._save_raw_response()`, logged, does not fail the fetch.

## Known Issues

- **Single API call for all games per sport**: one failure loses data for all games of that sport in that cycle.

## Tests

`tests/test_pinnacle.py` covers parse response structure, per-event malformed-event isolation, budget tracking (header parsing, exhaustion guard), `probe_budget` success/failure, `commence_time` parsing, `captured_at` threading, and single-key fail-soft behavior.
