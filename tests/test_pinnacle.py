import asyncio
import importlib
from datetime import date, datetime, timezone

import pytest
import httpx

from oddsiq import config
from oddsiq.scrapers.pinnacle import PinnacleScraper
from oddsiq.sports.profiles import NBA


def test_parse_commence_time_coerces_naive_to_utc():
    # Must guarantee a tz-aware UTC datetime even if the API ever returns a
    # naive ISO string (no Z/offset) — callers subtract it from now(UTC).
    dt = PinnacleScraper._parse_commence_time("2026-06-01T23:00:00")
    assert dt is not None and dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0
    assert PinnacleScraper._parse_commence_time("2026-06-01T23:00:00Z").utcoffset().total_seconds() == 0


def test_parse_source_updated_at_requires_trustworthy_provider_time():
    observed = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    assert PinnacleScraper._parse_source_updated_at(
        "2026-09-06T11:59:30Z", observed_at=observed,
    ) == datetime(2026, 9, 6, 11, 59, 30, tzinfo=timezone.utc)
    assert PinnacleScraper._parse_source_updated_at(
        "2026-09-06T12:02:00Z", observed_at=observed,
    ) is None
    assert PinnacleScraper._parse_source_updated_at(
        "2026-09-06T11:59:30", observed_at=observed,
    ) is None
    assert PinnacleScraper._parse_source_updated_at(
        "garbage", observed_at=observed,
    ) is None


def test_parse_threads_event_id_and_market_source_timestamp():
    observed = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    payload = [{
        "id": "exact-event-1",
        "home_team": "Boston Celtics",
        "away_team": "Los Angeles Lakers",
        "commence_time": "2026-09-07T00:00:00Z",
        "bookmakers": [{
            "key": "pinnacle", "last_update": "2026-09-06T11:58:00Z",
            "markets": [{
                "key": "h2h", "last_update": "2026-09-06T11:59:20Z",
                "outcomes": [
                    {"name": "Boston Celtics", "price": 1.8},
                    {"name": "Los Angeles Lakers", "price": 2.1},
                ],
            }],
        }],
    }]
    scraper = PinnacleScraper(api_key="k")
    snaps = scraper.parse_response(payload, captured_at=observed)
    assert len(snaps) == 2
    assert {s._source_event_id for s in snaps} == {"exact-event-1"}
    assert {s.source_updated_at for s in snaps} == {
        datetime(2026, 9, 6, 11, 59, 20, tzinfo=timezone.utc)}
    assert all(not s._source_freshness_missing for s in snaps)
    assert scraper.last_parse_missing_source_updates == 0


def test_parse_missing_event_id_drops_event_and_counts_it():
    scraper = PinnacleScraper(api_key="k")
    payload = [{
        "home_team": "Boston Celtics", "away_team": "Los Angeles Lakers",
        "bookmakers": [],
    }]
    assert scraper.parse_response(payload) == []
    assert scraper.last_parse_missing_event_ids == 1


SAMPLE_ODDS_RESPONSE = [
    {
        "id": "abc123",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": "2026-04-11T00:00:00Z",
        "home_team": "Boston Celtics",
        "away_team": "Los Angeles Lakers",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Boston Celtics", "price": 1.65},
                            {"name": "Los Angeles Lakers", "price": 2.35},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Boston Celtics", "price": 1.91, "point": -5.5},
                            {"name": "Los Angeles Lakers", "price": 1.93, "point": 5.5},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": 1.95, "point": 218.5},
                            {"name": "Under", "price": 1.88, "point": 218.5},
                        ],
                    },
                ],
            }
        ],
    }
]


@pytest.fixture
def scraper():
    return PinnacleScraper(api_key="test_key")


@pytest.fixture(autouse=True)
def _raw_responses_to_tmp(monkeypatch, tmp_path):
    """Redirect _save_raw_response writes away from the real ./data/raw.

    fetch_odds persists every payload via _save_raw_response, which reads
    config.RAW_DATA_DIR at call time — without this patch, a local pytest run
    drops fixture files into the operator's raw-response archive. config loads
    .env at import time, so the config attribute must be patched, not the env
    var.
    """
    monkeypatch.setattr("oddsiq.config.RAW_DATA_DIR", tmp_path)


@pytest.mark.asyncio
async def test_parse_odds_response(scraper: PinnacleScraper):
    snapshots = scraper.parse_response(SAMPLE_ODDS_RESPONSE)
    assert len(snapshots) == 6
    ml_home = [s for s in snapshots if s.market_type == "moneyline" and s.selection == "home"]
    assert len(ml_home) == 1
    assert ml_home[0].odds == 1.65
    assert ml_home[0].line is None
    assert ml_home[0]._commence_time == datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc)
    spread_home = [s for s in snapshots if s.market_type == "spread" and s.selection == "home"]
    assert len(spread_home) == 1
    assert spread_home[0].odds == 1.91
    assert spread_home[0].line == -5.5
    total_over = [s for s in snapshots if s.market_type == "total" and s.selection == "over"]
    assert len(total_over) == 1
    assert total_over[0].odds == 1.95
    assert total_over[0].line == 218.5


@pytest.mark.asyncio
async def test_parse_skips_malformed_event(scraper: PinnacleScraper):
    # A single malformed event (missing required keys) must not blow away the
    # entire Pinnacle poll — Pinnacle is the sharp reference book, so one bad
    # record would otherwise zero out edge detection for every book that cycle.
    malformed = {"id": "bad", "bookmakers": [{"key": "pinnacle", "markets": []}]}
    outcome_missing_price = {
        "id": "bad2",
        "home_team": "Phoenix Suns",
        "away_team": "Denver Nuggets",
        "bookmakers": [
            {"key": "pinnacle", "markets": [
                {"key": "h2h", "outcomes": [{"name": "Phoenix Suns"}]},  # no price
            ]},
        ],
    }
    data = [malformed, outcome_missing_price] + SAMPLE_ODDS_RESPONSE
    snapshots = scraper.parse_response(data)
    assert len(snapshots) == 6  # only the well-formed event survives


@pytest.mark.asyncio
async def test_parse_normalizes_team_names_for_matching(scraper: PinnacleScraper):
    # The snapshot match key (_home_team/_away_team) must be the CANONICAL name,
    # not the raw Odds API name. The schedule source stores normalized names on
    # the game row, and _matches_game compares by exact equality — so if a name
    # the schedule source normalizes (alias/fuzzy) is left raw here, the Pinnacle
    # odds silently never match that game and edge detection is blacked out.
    # "LA Lakers" is an alias for "Los Angeles Lakers" in the NBA mapping.
    data = [{
        "id": "e1",
        "home_team": "LA Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-04-11T00:00:00Z",
        "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "LA Lakers", "price": 2.1},
            {"name": "Boston Celtics", "price": 1.8},
        ]}]}],
    }]
    snaps = scraper.parse_response(data, sport="nba")
    assert snaps, "expected moneyline snapshots"
    home = next(s for s in snaps if s.selection == "home")
    assert home._home_team == "Los Angeles Lakers"  # normalized, not "LA Lakers"
    assert home._away_team == "Boston Celtics"


@pytest.mark.asyncio
async def test_budget_exhausted(scraper: PinnacleScraper):
    scraper._budget_remaining = 0
    assert scraper.should_skip_request() is True


@pytest.mark.asyncio
async def test_fetch_odds_with_mock(scraper: PinnacleScraper, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/odds",
            params={
                "apiKey": "test_key",
                "regions": "us",
                "markets": "h2h",
                "bookmakers": "pinnacle",
            },
        ),
        json=SAMPLE_ODDS_RESPONSE,
        headers={"x-requests-remaining": "495", "x-requests-used": "5"},
    )
    snapshots = await scraper.fetch_odds(date(2026, 4, 10), NBA)
    assert len(snapshots) == 6
    assert scraper.budget_remaining == 495
    assert scraper.budget_used == 5
    assert scraper.budget_total == 500


def test_pinnacle_markets_default_is_h2h(monkeypatch):
    monkeypatch.delenv("PINNACLE_MARKETS", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.PINNACLE_MARKETS == "h2h"
    finally:
        importlib.reload(config)


@pytest.mark.asyncio
async def test_fetch_odds_uses_configured_markets(httpx_mock, monkeypatch):
    monkeypatch.setattr(config, "PINNACLE_MARKETS", "h2h,spreads")
    scraper = PinnacleScraper(api_key="test_key")
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/odds",
            params={
                "apiKey": "test_key",
                "regions": "us",
                "markets": "h2h,spreads",
                "bookmakers": "pinnacle",
            },
        ),
        json=SAMPLE_ODDS_RESPONSE,
        headers={"x-requests-remaining": "495", "x-requests-used": "5"},
    )
    await scraper.fetch_odds(date(2026, 4, 10), NBA)


@pytest.mark.asyncio
async def test_captured_at_stamped_at_request_initiation_not_parse_time(
    scraper: PinnacleScraper, monkeypatch, tmp_path
):
    """Regression: captured_at must reflect when the HTTP request was sent,
    not when parsing finished. Same bug pattern as Tipsport — invisible at
    Pinnacle's typical ~500ms latency but symmetric with the Tipsport fix
    so both scrapers behave consistently.
    """
    monkeypatch.setattr("oddsiq.config.RAW_DATA_DIR", tmp_path)

    class FakeResponse:
        status_code = 200
        headers = {"x-requests-remaining": "499", "x-requests-used": "1"}

        def raise_for_status(self):
            pass

        def json(self):
            return SAMPLE_ODDS_RESPONSE

    class FakeClient:
        async def get(self, url, **kwargs):
            await asyncio.sleep(1.5)  # simulate slow API
            return FakeResponse()

        async def aclose(self):
            pass

    async def fake_get_client(self):
        return FakeClient()

    monkeypatch.setattr(
        "oddsiq.scrapers.pinnacle.PinnacleScraper._get_client", fake_get_client
    )

    pre_call = datetime.now(timezone.utc)
    snapshots = await scraper.fetch_odds(date(2026, 4, 10), NBA)
    post_call = datetime.now(timezone.utc)

    assert len(snapshots) > 0
    elapsed = (post_call - pre_call).total_seconds()
    assert elapsed >= 1.0, "Test setup invalid: HTTP delay didn't fire"

    for snap in snapshots:
        delta_from_pre = abs((snap.captured_at - pre_call).total_seconds())
        delta_from_post = abs((snap.captured_at - post_call).total_seconds())
        assert delta_from_pre < delta_from_post, (
            f"captured_at {snap.captured_at} is closer to post-call {post_call} "
            f"than pre-call {pre_call}; should reflect request initiation, "
            f"deltas: pre={delta_from_pre:.3f}s, post={delta_from_post:.3f}s"
        )


@pytest.mark.asyncio
async def test_raw_response_path_propagates_to_snapshots(
    scraper: PinnacleScraper, httpx_mock, monkeypatch, tmp_path
):
    """Regression: raw_response_path must be set on every returned snapshot,
    pointing at the JSON file actually written to disk."""
    monkeypatch.setattr("oddsiq.config.RAW_DATA_DIR", tmp_path)
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/odds",
            params={
                "apiKey": "test_key",
                "regions": "us",
                "markets": "h2h",
                "bookmakers": "pinnacle",
            },
        ),
        json=SAMPLE_ODDS_RESPONSE,
        headers={"x-requests-remaining": "495", "x-requests-used": "5"},
    )
    snapshots = await scraper.fetch_odds(date(2026, 4, 10), NBA)
    assert len(snapshots) > 0
    from pathlib import Path
    for snap in snapshots:
        assert snap.raw_response_path is not None, (
            f"raw_response_path is None on {snap.market_type}/{snap.selection}"
        )
        assert Path(snap.raw_response_path).exists(), (
            f"raw_response_path {snap.raw_response_path} does not exist on disk"
        )


@pytest.mark.asyncio
async def test_probe_budget_populates_used_and_remaining(
    scraper: PinnacleScraper, httpx_mock
):
    """The free /v4/sports probe must populate both quota fields."""
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": "test_key"},
        ),
        json=[{"key": "basketball_nba"}],
        headers={"x-requests-used": "42", "x-requests-remaining": "458"},
    )
    result = await scraper.probe_budget()
    assert result == {"used": 42, "remaining": 458}
    assert scraper.budget_used == 42
    assert scraper.budget_remaining == 458
    assert scraper.budget_total == 500


@pytest.mark.asyncio
async def test_probe_budget_missing_headers_returns_none(
    scraper: PinnacleScraper, httpx_mock
):
    """Without quota headers the probe is inconclusive — return None."""
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": "test_key"},
        ),
        json=[],
        headers={},
    )
    result = await scraper.probe_budget()
    assert result is None
    # State untouched — caller will fall back to DB or defaults.
    assert scraper.budget_used == 0
    assert scraper.budget_remaining == 20000


@pytest.mark.asyncio
async def test_probe_budget_http_error_returns_none(
    scraper: PinnacleScraper, httpx_mock
):
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": "test_key"},
        ),
        status_code=503,
    )
    result = await scraper.probe_budget()
    assert result is None


@pytest.mark.asyncio
async def test_probe_budget_malformed_headers_returns_none(
    scraper: PinnacleScraper, httpx_mock
):
    """A 200 response with non-numeric quota headers (whitespace, garbage,
    or a typo from the upstream API) used to ValueError out of probe_budget
    and crash the scheduler before the main loop started — the surrounding
    HTTPError guard does NOT catch ValueError. Now it must fail soft to
    None so the caller falls back to the DB snapshot."""
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": "test_key"},
        ),
        json=[],
        headers={"x-requests-used": "not a number", "x-requests-remaining": "458"},
    )
    result = await scraper.probe_budget()
    assert result is None
    # Budget state untouched; caller will fall back to DB or defaults.
    assert scraper.budget_used == 0
    assert scraper.budget_remaining == 20000


# ---------------------------------------------------------------------------
# Single-key behavior (multi-key pool rotation removed)
# ---------------------------------------------------------------------------


def test_single_key_construction():
    """The scraper holds a single Odds API key (config.ODDS_API_KEY by default)."""
    sc = PinnacleScraper(api_key="legacy_key")
    assert sc._api_key == "legacy_key"


@pytest.mark.asyncio
async def test_fetch_odds_skips_when_budget_drained():
    """No HTTP request fires when the key is out of monthly budget."""
    sc = PinnacleScraper(api_key="k")
    sc._budget_remaining = 0
    snapshots = await sc.fetch_odds(date(2026, 4, 11), NBA)
    assert snapshots == []
    await sc.close()


@pytest.mark.asyncio
async def test_fetch_odds_returns_empty_on_http_error(httpx_mock, monkeypatch):
    """A 429/401/5xx on the single key fails soft to [] (no rotation) while the
    quota headers from the response are still applied before the raise. A 429 is
    now retried with backoff (instant here) and still ends as [] once exhausted."""
    async def _noop_sleep(_d):
        return None
    monkeypatch.setattr("oddsiq.scrapers.retry.asyncio.sleep", _noop_sleep)
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/odds",
            params={"apiKey": "k", "regions": "us", "markets": "h2h", "bookmakers": "pinnacle"},
        ),
        status_code=429,
        headers={"x-requests-used": "20000", "x-requests-remaining": "0"},
        is_reusable=True,  # retried across attempts now
    )
    sc = PinnacleScraper(api_key="k")
    snapshots = await sc.fetch_odds(date(2026, 4, 11), NBA)
    assert snapshots == []
    assert sc.budget_remaining == 0
    # The swallowed failure must surface in the health line (errors=1), not
    # report a fully failed poll as errors=0.
    assert sc.last_fetch_error_count == 1
    await sc.close()


@pytest.mark.asyncio
async def test_fetch_error_count_resets_and_skips_budget_path():
    """The per-fetch error counter mirrors Tipsport: reset on every call, and
    the deliberate budget-exhausted skip is NOT counted as an error."""
    sc = PinnacleScraper(api_key="k")
    sc.last_fetch_error_count = 1  # stale from a previous failed poll
    sc._budget_remaining = 0
    assert await sc.fetch_odds(date(2026, 4, 11), NBA) == []
    assert sc.last_fetch_error_count == 0
    await sc.close()


@pytest.mark.asyncio
async def test_parse_counters_reset_on_a_fetch_that_never_parses(httpx_mock, monkeypatch):
    """The parse counters ride the scraper health line, so a fetch that returns
    [] without parsing (budget skip, HTTP failure) must not republish the
    PREVIOUS cycle's integrity counts as if they were this cycle's."""
    async def _noop_sleep(_d):
        return None
    monkeypatch.setattr("oddsiq.scrapers.retry.asyncio.sleep", _noop_sleep)

    sc = PinnacleScraper(api_key="k")
    # A real parse that recorded both integrity problems.
    assert sc.parse_response([{
        "home_team": "Boston Celtics", "away_team": "Los Angeles Lakers",
        "bookmakers": [],
    }, {
        "id": "no-timestamps",
        "home_team": "Miami Heat", "away_team": "Chicago Bulls",
        "commence_time": "2026-09-07T00:00:00Z",
        "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Miami Heat", "price": 1.9},
            {"name": "Chicago Bulls", "price": 2.0},
        ]}]}],
    }]) != []
    assert sc.last_parse_missing_event_ids == 1
    assert sc.last_parse_missing_source_updates == 1

    # Budget-exhausted short circuit: no parse runs.
    sc._budget_remaining = 0
    assert await sc.fetch_odds(date(2026, 4, 11), NBA) == []
    assert sc.last_parse_missing_event_ids == 0
    assert sc.last_parse_missing_source_updates == 0
    await sc.close()

    # Same for an HTTP failure that exhausts its retries before any parse.
    sc2 = PinnacleScraper(api_key="k")
    sc2.last_parse_missing_event_ids = 4
    sc2.last_parse_missing_source_updates = 7
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/odds",
            params={"apiKey": "k", "regions": "us", "markets": "h2h", "bookmakers": "pinnacle"},
        ),
        status_code=500,
        is_reusable=True,
    )
    assert await sc2.fetch_odds(date(2026, 4, 11), NBA) == []
    assert sc2.last_fetch_error_count == 1
    assert sc2.last_parse_missing_event_ids == 0
    assert sc2.last_parse_missing_source_updates == 0
    await sc2.close()


@pytest.mark.asyncio
async def test_fetch_uses_profile_odds_api_key_and_stamps_sport(httpx_mock):
    httpx_mock.add_response(
        url="https://api.the-odds-api.com/v4/sports/basketball_nba/odds?apiKey=k&regions=us&markets=h2h&bookmakers=pinnacle",
        json=[{
            "id": "nba-profile-test",
            "home_team": "Boston Celtics", "away_team": "Miami Heat",
            "commence_time": "2026-06-04T00:00:00Z",
            "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Boston Celtics", "price": 1.8},
                {"name": "Miami Heat", "price": 2.1},
            ]}]}],
        }],
    )
    scraper = PinnacleScraper(api_key="k")
    snaps = await scraper.fetch_odds(date.today(), NBA)
    assert snaps and all(s.sport == "nba" for s in snaps)
    await scraper.close()


@pytest.mark.asyncio
async def test_fetch_nhl_uses_icehockey_url_and_stamps_sport(httpx_mock):
    from oddsiq.sports.profiles import NHL
    httpx_mock.add_response(
        url="https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds?apiKey=k&regions=us&markets=h2h&bookmakers=pinnacle",
        json=[{
            "id": "nhl-profile-test",
            "home_team": "Carolina Hurricanes", "away_team": "Vegas Golden Knights",
            "commence_time": "2026-06-05T00:00:00Z",
            "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Carolina Hurricanes", "price": 1.65},
                {"name": "Vegas Golden Knights", "price": 2.37},
            ]}]}],
        }],
    )
    scraper = PinnacleScraper(api_key="k")
    snaps = await scraper.fetch_odds(date.today(), NHL)
    assert all(s.sport == "nhl" for s in snaps)
    assert {s.selection for s in snaps} == {"home", "away"}
    await scraper.close()


@pytest.mark.asyncio
async def test_parse_skips_nonpositive_or_garbage_price(scraper: PinnacleScraper):
    """A zero/negative/non-numeric price must be dropped at parse time — it
    would otherwise flow into remove_vig()/find_edges() and raise mid-poll.
    Only the bad OUTCOME is dropped; the valid sibling survives."""
    event = {
        "id": "badprice",
        "home_team": "Phoenix Suns",
        "away_team": "Denver Nuggets",
        "commence_time": "2026-04-11T00:00:00Z",
        "bookmakers": [
            {"key": "pinnacle", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Phoenix Suns", "price": 0.0},        # nonsense
                    {"name": "Denver Nuggets", "price": 2.0},      # valid
                ]},
            ]},
        ],
    }
    garbage = {
        "id": "strprice",
        "home_team": "Phoenix Suns",
        "away_team": "Denver Nuggets",
        "commence_time": "2026-04-11T00:00:00Z",
        "bookmakers": [
            {"key": "pinnacle", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Phoenix Suns", "price": "abc"},      # garbage type
                    {"name": "Denver Nuggets", "price": 1.0},      # <=1.0 nonsense
                ]},
            ]},
        ],
    }
    snapshots = scraper.parse_response([event, garbage])
    assert [(s.selection, s.odds) for s in snapshots] == [("away", 2.0)]
