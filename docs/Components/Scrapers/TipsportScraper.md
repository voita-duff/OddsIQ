---
tags:
  - component/scraper
  - status/complete
  - phase/p0
  - layer/data-ingestion
  - type/component
source-files:
  - oddsiq/scrapers/tipsport.py
  - tests/test_tipsport.py
related:
  - "[[CloudflareSession]]"
  - "[[PinnacleScraper]]"
  - "[[Edge Detection]]"
  - "[[Scheduler]]"
  - "[[Team Name Normalization]]"
  - "[[SportProfile]]"
  - "[[Config Constants]]"
---

# TipsportScraper

## Purpose

Reverse-engineered Tipsport.cz REST API scraper with Cloudflare bypass. Tipsport is the primary Czech bookmaker whose odds are compared against [[PinnacleScraper]] fair lines in [[Edge Detection]]. Fetches **moneyline only** — one POST per poll cycle, parameterised by the [[SportProfile]] for the active sport.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/scrapers/tipsport.py` | `TipsportScraper` | Full scraper; subclasses `BaseScraper` |
| `oddsiq/scrapers/tipsport.py` | `_warmup_url(profile)` | Builds the sport-specific Tipsport page URL `/kurzy/{sport_slug}/{sport_slug}-{gender_slug}/{league_slug}-{competition_id}` from `profile.tipsport_sport_slug`, `tipsport_gender_slug` (default `muzi`; WNBA `zeny`), `tipsport_league_slug`, `tipsport_competition_id` |
| `oddsiq/scrapers/tipsport.py` | `_moneyline_filters(profile)` | Returns `[]` only for the `WINNER_WHOLE_MATCH` sport (MLB); returns the explicit `matchView`+`superSportId` filter for NHL/NBA/WNBA (`WINNER_FINAL_DECISION`) |
| `oddsiq/scrapers/tipsport.py` | `TipsportScraper.fetch_odds(game_date, profile)` | Entry point; one moneyline fetch per poll; non-raising; counts consecutive failures |
| `oddsiq/scrapers/tipsport.py` | `TipsportScraper._fetch_moneyline(game_date, profile)` | curl_cffi fast path → JSESSIONID sync on rejection → Playwright last-resort fallback |
| `oddsiq/scrapers/tipsport.py` | `TipsportScraper._maybe_refresh_session()` | Proactive 20-min session refresh (`SESSION_MAX_AGE = 1200.0`) |
| `oddsiq/scrapers/tipsport.py` | `TipsportScraper._reset_session()` | Destroys curl_cffi + Playwright sessions on `MAX_CONSECUTIVE_FAILURES` |
| `oddsiq/scrapers/tipsport.py` | `TipsportScraper._copy_browser_api_session_to_client()` | Copies only browser-created `JSESSIONID` into the curl cookie jar |
| `oddsiq/scrapers/tipsport.py` | `TipsportScraper.parse_response(data, profile, ...)` | Walks `offerSuperSports[0].tabs` → selects the tab matching `profile.tipsport_moneyline_matchview` (fail-closed); iterates matches → oppRows[0].oppsTab |
| `oddsiq/scrapers/tipsport.py` | `TipsportScraper._map_opp(opp_type)` | Maps opp types `"1"` → home, `"2"` → away only (moneyline-only; draw/OT opp types are skipped) |

## Inputs

- **HTTP endpoint**: `POST https://www.tipsport.cz/rest/offer/v2/offer?limit=75`
- **Request body**: JSON with `type: "COMPETITION"`, `id: profile.tipsport_competition_id`, `matchViewFilters` from `_moneyline_filters(profile)`, `withLive: True`
- **`game_date`**: `date` object, used for raw response storage path
- **`profile`**: [[SportProfile]] — carries all sport-specific constants (competition ID, slugs, moneyline match view, super sport ID)

### Per-sport moneyline market

| Sport | `tipsport_moneyline_matchview` | `matchViewFilters` sent | Notes |
|-------|-------------------------------|------------------------|-------|
| NBA | `WINNER_FINAL_DECISION` | `[{"matchView": "WINNER_FINAL_DECISION", "id": {"superSportId": 7}}]` | 2-way incl-OT. Basketball's default `WINNER_WHOLE_MATCH` tab carries the **regulation 3-way legs** (`1/1x/x/x2/2`) whose 1/2 prices are NOT the moneyline (A/B price-verified 2026-06-12) — corrected from `WINNER_WHOLE_MATCH`, which the scraper had read since April 2026. |
| WNBA | `WINNER_FINAL_DECISION` | `[{"matchView": "WINNER_FINAL_DECISION", "id": {"superSportId": 7}}]` | Same basketball engine as NBA; Tipsport labels carry a `" Ž"` suffix handled by `WNBA_TIPSPORT_ALIASES`. |
| MLB | `WINNER_WHOLE_MATCH` | `[]` (default tab) | 2-way (baseball cannot tie) — the only sport on the default tab |
| NHL | `WINNER_FINAL_DECISION` | `[{"matchView": "WINNER_FINAL_DECISION", "id": {"superSportId": 23}}]` | 2-way incl-OT. Default `WINNER_WHOLE_MATCH` for NHL is the **3-way regulation market** — reading it would fabricate edges and is excluded. |

## Outputs

- `list[OddsSnapshot]` — moneyline snapshots only
  - `game_id=0` (resolved by [[Scheduler]])
  - `bookmaker="tipsport"`
  - `market_type="moneyline"`, `line=None`
  - `sport=profile.key`
  - `_home_team` / `_away_team` set after [[Team Name Normalization]]
- Raw JSON saved to `./data/raw/tipsport/{date}/{HHMMSS}.json` (one file per poll)

## Interactions

- [[CloudflareSession]] — Playwright creates the Tipsport API session cookie; last-resort in-page fallback when curl still fails after JSESSIONID sync
- [[Scheduler]] / [[Pipeline Services]] — `PollCoordinator` calls `fetch_odds(game_date, profile)` on tiered intervals, matches snapshots to games
- [[Team Name Normalization]] — `normalize_team(raw_name, "tipsport", profile.key)` handles Tipsport abbreviations
- [[SportProfile]] — all sport-specific constants injected via `profile`
- [[Config Constants]] — `MAX_CONSECUTIVE_FAILURES`, `CF_SOLVE_TIMEOUT`

## Internal Logic

### `captured_at` semantics

`captured_at` is stamped at **request initiation** inside `_fetch_moneyline()`, before the POST fires. It is re-taken before the curl retry after JSESSIONID sync, and again before the Playwright fallback if curl still fails.

### HTTP strategy (three-step fast path)

Each moneyline fetch uses a three-step path:

**1. `curl_cffi` with Chrome 146 impersonation** (fast path):

```python
client = await self._get_client(profile)  # AsyncSession(impersonate="chrome146")
resp = await client.post(api_url, json=body, headers=headers)
```

curl_cffi owns the TLS/UA bundle for `chrome146`. A manual warmup GET to `_warmup_url(profile)` is issued on first `_get_client()` call to let Cloudflare set cookies.

**2. On any curl HTTP >= 400 — browser JSESSIONID sync, then retry curl**:

```python
if resp.status_code >= 400:
    await self._copy_browser_api_session_to_client(client, profile)
    resp = await client.post(api_url, json=body, headers=headers)
```

Tipsport's `/rest/*` API requires a browser-created `JSESSIONID`. `_copy_browser_api_session_to_client()` copies **only** `JSESSIONID` from [[CloudflareSession]] into the curl cookie jar. Playwright's Cloudflare cookies are deliberately not copied — those are tied to a different TLS fingerprint.

**3. If curl still returns HTTP >= 400 — [[CloudflareSession]] in-page fallback**:

```python
if resp.status_code >= 400:
    cf = await self._get_cf_session(profile)
    data = await asyncio.wait_for(cf.post(api_url, json=body), timeout=60)
```

Runs `fetch()` inside the live browser context, sharing the browser's full TLS fingerprint and cookies. `asyncio.wait_for` with 60s timeout prevents event-loop freezes.

### Fail-closed tab selection

`parse_response()` selects the response tab matching `profile.tipsport_moneyline_matchview`:

```python
tab = next((t for t in tabs if t.get("matchView") == target_view), None)
if tab is None:
    raise TipsportMarketUnavailable(
        f"Tipsport response missing {target_view} tab for {profile.key}"
    )
```

There is **no fallback to another tab**. For hockey AND basketball, the default `WINNER_WHOLE_MATCH` tab carries the 3-way regulation legs — falling back to it would read regulation prices as the moneyline and fabricate edges (this is exactly the bug fixed 2026-06-12 for NBA/WNBA). The raise propagates to `fetch_odds()`, which treats `TipsportMarketUnavailable` as "market not offered this cycle": it returns `[]` but does **NOT** increment `_consecutive_failures` (a Cloudflare session reset can't conjure a market, and the session is shared across sports, so a sport whose market isn't posted yet must not churn it). A *structurally* malformed envelope — `offerSuperSports` missing/empty/non-dict — still raises `KeyError`/`IndexError`/`TypeError`, which IS counted as a fetch failure.

### Response parsing

`parse_response()` navigates the Tipsport JSON:

```
offerSuperSports[0].tabs
  -> find tab where matchView == profile.tipsport_moneyline_matchview
    -> offerCompetitionAnnuals
      -> matches
        -> oppRows[0].oppsTab
          -> individual opp objects (opp_type 1=home, 2=away)
```

For each match:
1. Extracts `participantHome` and `participantVisiting`
2. Parses the listing's own start (`dateClosed`, then `dateStartTv`/per-opp fallback) into transient `_commence_time`
3. Normalises via [[Team Name Normalization]]: `normalize_team(raw_name, "tipsport", profile.key)`
4. Skips the match if either team returns `None`
5. Iterates `oppRows[0].oppsTab`; skips disabled bets (`bettingEnabled=False`)
6. Maps opp types via `_map_opp()`: `"1"` → home, `"2"` → away; any other type is skipped

Snapshots carry `sport=profile.key` so the DB knows which sport each row belongs to. `PollCoordinator` requires a parseable Tipsport `_commence_time` and matches it within ±2h of the game. Missing/malformed listing time fails closed and increments `unsafe_start_snapshots` in the structured health log; it never falls back to pair-only attribution because Tipsport commonly lists the next game of a series with the same teams.

### Session lifecycle

**Proactive refresh** (`_maybe_refresh_session()`): checks session age at the start of every `fetch_odds()`. Resets if age > `SESSION_MAX_AGE` (1200s / 20 min). Cloudflare cookies expire at ~20-30 min; refreshing at 20 min avoids the dead cycle.

**Failure recovery** (`_reset_session()`): after `MAX_CONSECUTIVE_FAILURES` (3) consecutive errors, destroys both the curl_cffi client and the Playwright CF session. The next `fetch_odds()` call triggers a fresh warmup.

## Configuration

| Constant | Value | Source |
|----------|-------|--------|
| `MAX_CONSECUTIVE_FAILURES` | 3 | `config.py` |
| `SESSION_MAX_AGE` | 1200s (20 min) | `tipsport.py` |
| `CF_SOLVE_TIMEOUT` | 30.0s | `config.py` |
| `TIPSPORT_API_URL` | `https://www.tipsport.cz/rest/offer/v2/offer` | `config.py` |
| Sport-specific constants | competition ID, slugs, moneyline matchview | [[SportProfile]] |

## Error Handling

- **`TipsportMarketUnavailable`** in `fetch_odds()` (well-formed response, sport's moneyline matchView tab absent): logged at INFO, returns `[]`, and is **NOT** counted toward `_consecutive_failures` — "market not offered" is not a session problem. Caught by a dedicated `except` clause *before* the broad one below.
- **`(ConnectionError, OSError, KeyError, IndexError, TypeError, ValueError)`** in `fetch_odds()`: caught, logged, `last_fetch_error_count = 1`, `_consecutive_failures` incremented. Returns `[]`. After `>= MAX_CONSECUTIVE_FAILURES` failures in a row, `_reset_session()` is called.
- **Playwright timeout (60s)**: `asyncio.TimeoutError` raised by `wait_for`; converted to `ConnectionError`.
- **`CloudflareSolveError`**: wrapped as `ConnectionError` by `_get_cf_session()`.
- **Tab missing** (well-formed envelope, target moneyline matchView absent): `parse_response()` raises `TipsportMarketUnavailable` (fail-closed — never reads another tab); returns `[]`, NOT counted as a fetch failure. A structurally malformed envelope instead raises `KeyError`/`IndexError`/`TypeError` and IS counted.
- **Raw response save failure**: `OSError` caught inside `BaseScraper._save_raw_response()`, logged, does not fail the fetch.

## Tests

`tests/test_tipsport.py` covers moneyline/time parsing, 401/403 JSESSIONID sync and curl retry, Playwright fallback, session recovery after consecutive failures, session warm-up flow, team name attachment, and browser reuse across requests. `tests/test_poll_coordinator.py` pins the fail-closed attribution rule and its health counter.
