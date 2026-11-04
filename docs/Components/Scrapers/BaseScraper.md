---
tags:
  - component/scraper
  - status/complete
  - phase/p0
  - layer/data-ingestion
  - type/component
source-files:
  - oddsiq/scrapers/base.py
related:
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[Scheduler]]"
  - "[[SportProfile]]"
  - "[[Team Name Normalization]]"
---

# BaseScraper

## Purpose

Abstract base class that defines the contract every odds scraper in OddsIQ must implement. [[Scheduler]] / [[Pipeline Services]] holds scrapers as a `list[BaseScraper]` and polls them uniformly — the orchestrator does not know whether it is calling [[PinnacleScraper|Pinnacle]] via The Odds API or [[TipsportScraper|Tipsport]]'s reverse-engineered endpoint. This makes adding a new bookmaker a contained change: a new `BaseScraper` subclass plus entries in [[Team Name Normalization]], with no engine changes required.

Note: `OddsApiScheduleSource` (the schedule/scores source) does **not** subclass `BaseScraper` — it fetches game schedules, not odds, and has its own interface. Only odds scrapers inherit `BaseScraper`.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/scrapers/base.py` | `BaseScraper` | ABC defining the scraper contract (`name`, `fetch_odds`, `close`) |
| `oddsiq/scrapers/base.py` | `BaseScraper._save_raw_response(data, game_date)` | Concrete helper — writes raw JSON to `RAW_DATA_DIR/<name>/<date>/<HHMMSS_microseconds>.json`; returns path string or `None` on `OSError` |
| `oddsiq/scrapers/base.py` | `prune_raw_responses(root, keep_days)` | Module-level retention pruner: deletes dated subdirs under `RAW_DATA_DIR` older than `keep_days` |

## Interface

```python
class BaseScraper(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def fetch_odds(self, game_date: date, profile) -> list[OddsSnapshot]: ...

    async def close(self) -> None:
        pass
```

Three members, two abstract:

- **`name` (property)** — a short identifier used in logs, health metrics, and coverage tracking. Current values: `"pinnacle"`, `"tipsport"`. Deleted soft-book scrapers (`betano`, `allwyn`, `fortuna`) followed the same pattern — archived at git tag `archive/multibook-2026-06-02`.
- **`fetch_odds(game_date, profile)` (async)** — returns a list of `OddsSnapshot` instances for the given date and sport profile. Implementations set `game_id=0` (placeholder — [[Scheduler]] resolves real IDs via [[Team Name Normalization]]) and attach `_home_team` / `_away_team`. Must be **non-raising for operational errors** — return `[]` on HTTP failure, API-structure changes, or empty markets. Raising is reserved for programmer bugs.
- **`close()` (async, concrete)** — default no-op. Override when the subclass owns resources needing explicit teardown (HTTP client, Playwright browser, etc.). [[Scheduler]] calls this during graceful shutdown.

## Shared Helpers

- **`_save_raw_response(data, game_date)`** — concrete method on `BaseScraper`. Writes JSON to `config.RAW_DATA_DIR / self.name / game_date.isoformat() / <HHMMSS_microseconds>.json` and returns the absolute path string, or `None` on `OSError` (logged, never raised). Filenames include microseconds to avoid collisions when two markets land in the same second. Concrete scrapers must not reimplement raw-response persistence — call this helper and thread the returned path into `OddsSnapshot.raw_response_path`.

- **`prune_raw_responses(root, keep_days)`** — module-level function (not a method on the ABC). Walks `<root>/<scraper>/<date>/` directories and removes any whose date is older than `keep_days` from today. Ignores directories whose name does not match `YYYY-MM-DD`. Empty scraper-name parents are left in place. [[CLI Entry Point]] calls this at startup and shutdown when `RAW_RESPONSE_RETENTION_ENABLED` is set (default `1`); retention window is `RAW_RESPONSE_KEEP_DAYS` (default `14`).

## Interactions

- [[Scheduler]] / [[Pipeline Services]] — `PollCoordinator` holds `scrapers: list[BaseScraper]`, iterates them per sport per poll, and keys fetch diagnostics and coverage tracking by `scraper.name`.
- [[PinnacleScraper]], [[TipsportScraper]] — existing concrete implementations.
- [[SportProfile]] — `profile` is passed through `fetch_odds(game_date, profile)` so each scraper can read sport-specific constants without being NBA-only.

## Implementation Notes

### What concrete scrapers must do

1. Return `OddsSnapshot` objects with `game_id=0`, `bookmaker=<self.name>`, `sport=profile.key`, and `_home_team` / `_away_team` populated.
2. Call `self._save_raw_response(data, game_date)` and thread the returned path into `OddsSnapshot.raw_response_path`.
3. Expose `last_fetch_error_count: int` — set to 0 at the start of every `fetch_odds()` call, incremented for each swallowed internal exception. The per-cycle health log in [[Pipeline Services]] reads this field to surface silent degradation.
4. Handle rate limiting and session errors internally — return `[]` on failure; never raise to the caller.

### What concrete scrapers must NOT do

- Write to [[SQLite Store]] directly. Persistence is the pipeline's job.
- Mutate game records — scrapers only produce `OddsSnapshot` instances.
- Call [[Edge Detection]] or [[CLV Calculation]] — engine logic is strictly downstream.

## Adding a New Scraper

A new Czech soft-book scraper needs to:

1. Subclass `BaseScraper` and implement the three members above.
2. Add alias entries for the sport in `oddsiq/sports/teams.py` (or confirm names are canonical) and document in [[Team Name Normalization]].
3. Register via `oddsiq/scrapers/registry.py` (gated by a `BOOKMAKER_*_ENABLED` env flag).
4. Ship unit tests following the [[PinnacleScraper]] / [[TipsportScraper]] patterns.

No changes are required in [[Edge Detection]], [[Vig Removal]], [[CLV Calculation]], [[SQLite Store]], or [[Telegram Notifications]] — all of those are bookmaker-agnostic.

## Tests

There are no direct tests of `BaseScraper` — it is an ABC with no runtime behaviour to verify. The contract is enforced by subclass tests (`tests/test_pinnacle.py`, `tests/test_tipsport.py`).
