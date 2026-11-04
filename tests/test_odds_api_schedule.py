import re
import pytest

from oddsiq.scrapers.odds_api_schedule import (
    OddsApiScheduleSource,
    ScheduleFetchError,
    _parse_iso,
)
from oddsiq import config

BASE = config.ODDS_API_BASE_URL


@pytest.mark.asyncio
async def test_final_and_upcoming_from_scores(httpx_mock):
    # /scores alone yields the whole slate: a completed game (with scores) and
    # an upcoming game (completed=false, scores=null).
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{BASE}/sports/basketball_nba/scores")),
        json=[
            {
                "id": "g1",
                "commence_time": "2026-06-01T23:40:00Z",
                "completed": True,
                "home_team": "Boston Celtics",
                "away_team": "Miami Heat",
                "scores": [
                    {"name": "Boston Celtics", "score": "110"},
                    {"name": "Miami Heat", "score": "104"},
                ],
            },
            {
                "id": "g2",
                "commence_time": "2026-06-02T23:00:00Z",
                "completed": False,
                "home_team": "Denver Nuggets",
                "away_team": "Phoenix Suns",
                "scores": None,
            },
        ],
    )
    src = OddsApiScheduleSource(api_key="k")
    games = {g.external_id: g for g in await src.fetch_games("basketball_nba")}
    await src.close()
    assert games["g1"].status == "final"
    assert games["g1"].home_score == 110 and games["g1"].away_score == 104
    assert games["g1"].sport == "nba"
    assert games["g2"].status == "scheduled"
    assert games["g2"].home_score is None


@pytest.mark.asyncio
async def test_live_game_from_scores(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{BASE}/sports/basketball_nba/scores")),
        json=[
            {
                "id": "g3",
                "commence_time": "2026-06-01T23:40:00Z",
                "completed": False,
                "home_team": "New York Knicks",
                "away_team": "Chicago Bulls",
                "scores": [
                    {"name": "New York Knicks", "score": "40"},
                    {"name": "Chicago Bulls", "score": "38"},
                ],
            }
        ],
    )
    src = OddsApiScheduleSource(api_key="k")
    games = await src.fetch_games("basketball_nba")
    await src.close()
    assert len(games) == 1 and games[0].status == "live"
    assert games[0].home_score == 40


@pytest.mark.asyncio
async def test_4xx_raises_schedule_fetch_error(httpx_mock):
    # A hard 4xx (e.g. 401 on a bad key) raises ScheduleFetchError.
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{BASE}/sports/basketball_nba/scores")),
        status_code=401,
    )
    src = OddsApiScheduleSource(api_key="bad")
    with pytest.raises(ScheduleFetchError):
        await src.fetch_games("basketball_nba")
    await src.close()


@pytest.mark.asyncio
async def test_5xx_retried_once_then_succeeds(httpx_mock, monkeypatch):
    # A 5xx is retried with backoff (instant here); the second attempt's 200
    # body is parsed normally.
    async def _noop_sleep(_d):
        return None

    monkeypatch.setattr("oddsiq.scrapers.retry.asyncio.sleep", _noop_sleep)
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{BASE}/sports/basketball_nba/scores")),
        status_code=503,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{BASE}/sports/basketball_nba/scores")),
        json=[
            {
                "id": "g5",
                "commence_time": "2026-06-02T23:00:00Z",
                "completed": False,
                "home_team": "Denver Nuggets",
                "away_team": "Phoenix Suns",
                "scores": None,
            }
        ],
    )
    src = OddsApiScheduleSource(api_key="k")
    games = await src.fetch_games("basketball_nba")
    await src.close()
    assert len(games) == 1 and games[0].external_id == "g5"


@pytest.mark.asyncio
async def test_health_check_ok(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{BASE}/sports")),
        json=[{"key": "basketball_nba"}],
    )
    src = OddsApiScheduleSource(api_key="k")
    assert await src.health_check() is True
    await src.close()


@pytest.mark.asyncio
async def test_malformed_json_body_raises_schedule_fetch_error(httpx_mock):
    # A 200 response whose body isn't valid JSON must surface as a
    # ScheduleFetchError, not a raw json.JSONDecodeError. The startup
    # refresh() runs OUTSIDE the main loop's try/except, so an unguarded
    # decode error there crashes the scheduler before it starts polling.
    httpx_mock.add_response(
        url=re.compile(re.escape(f"{BASE}/sports/basketball_nba/scores")),
        status_code=200,
        content=b"<html>upstream maintenance</html>",
    )
    src = OddsApiScheduleSource(api_key="k")
    with pytest.raises(ScheduleFetchError):
        await src.fetch_games("basketball_nba")
    await src.close()


def test_parse_iso_coerces_naive_to_utc():
    # A naive ISO string (no Z, no offset) must still yield a tz-AWARE UTC
    # datetime. Downstream tier math subtracts it from datetime.now(UTC); a
    # naive value would raise TypeError in the unguarded startup refresh().
    dt = _parse_iso("2026-06-01T23:00:00")
    assert dt is not None and dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0
    # A Z-suffixed string stays UTC-aware as before.
    assert _parse_iso("2026-06-01T23:00:00Z").utcoffset().total_seconds() == 0
