from __future__ import annotations

from datetime import date

import pytest

from oddsiq.scrapers.pinnacle import PinnacleScraper
from oddsiq.sports.profiles import get_profile

_PIN_EVENT = {
    "id": "e1",
    "home_team": "Boston Celtics",
    "away_team": "Los Angeles Lakers",
    "commence_time": "2026-06-09T00:00:00Z",
    "bookmakers": [{
        "key": "pinnacle",
        "markets": [{"key": "h2h", "outcomes": [
            {"name": "Boston Celtics", "price": 1.8},
            {"name": "Los Angeles Lakers", "price": 2.1},
        ]}],
    }],
}


async def _noop_sleep(_d):
    return None


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
async def test_pinnacle_retries_then_succeeds(httpx_mock, monkeypatch):
    monkeypatch.setattr("oddsiq.scrapers.retry.asyncio.sleep", _noop_sleep)
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "0"})
    httpx_mock.add_response(status_code=200, json=[_PIN_EVENT])
    scraper = PinnacleScraper(api_key="k")
    try:
        snaps = await scraper.fetch_odds(date(2026, 6, 9), get_profile("nba"))
    finally:
        await scraper.close()
    assert len(snaps) == 2  # home + away moneyline


@pytest.mark.asyncio
async def test_pinnacle_returns_empty_on_persistent_5xx(httpx_mock, monkeypatch):
    monkeypatch.setattr("oddsiq.scrapers.retry.asyncio.sleep", _noop_sleep)
    for _ in range(3):
        httpx_mock.add_response(status_code=503)
    scraper = PinnacleScraper(api_key="k")
    try:
        snaps = await scraper.fetch_odds(date(2026, 6, 9), get_profile("nba"))
    finally:
        await scraper.close()
    assert snaps == []


from oddsiq.scrapers.odds_api_schedule import OddsApiScheduleSource, ScheduleFetchError

# fetch_games only ever hits the /scores endpoint, so unconstrained
# add_response matching (in registration order) is unambiguous here.


@pytest.mark.asyncio
async def test_schedule_retries_429_then_succeeds(httpx_mock, monkeypatch):
    monkeypatch.setattr("oddsiq.scrapers.retry.asyncio.sleep", _noop_sleep)
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "0"})
    httpx_mock.add_response(status_code=200, json=[])
    src = OddsApiScheduleSource(api_key="k")
    try:
        games = await src.fetch_games("basketball_nba")
    finally:
        await src.close()
    assert games == []


@pytest.mark.asyncio
async def test_schedule_raises_after_persistent_429(httpx_mock, monkeypatch):
    monkeypatch.setattr("oddsiq.scrapers.retry.asyncio.sleep", _noop_sleep)
    for _ in range(3):
        httpx_mock.add_response(status_code=429)
    src = OddsApiScheduleSource(api_key="k")
    try:
        with pytest.raises(ScheduleFetchError):
            await src.fetch_games("basketball_nba")
    finally:
        await src.close()


@pytest.mark.asyncio
async def test_schedule_4xx_is_fatal_no_retry(httpx_mock):
    httpx_mock.add_response(status_code=404)
    src = OddsApiScheduleSource(api_key="k")
    try:
        with pytest.raises(ScheduleFetchError):
            await src.fetch_games("basketball_nba")
    finally:
        await src.close()
