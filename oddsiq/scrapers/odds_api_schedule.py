"""Schedule + scores source backed by The Odds API.

Builds the game slate from ``/sports/{key}/scores?daysFrom=3`` alone — that
endpoint returns upcoming + live + recently-final games, each carrying
``commence_time``. Verified 2026-06-03 against the live API: the ``/scores``
upcoming set and its lookahead match ``/sports/{key}/odds`` exactly, so a
separate ``/odds`` fetch here would be redundant. (The actual Pinnacle odds come
from ``PinnacleScraper``'s own ``/odds`` poll, not from this source.)

Note: The Odds API exposes no ``postponed`` status — postponed/cancelled
fixtures drop from the feed and surface here as ``scheduled`` until they age out
of the slate. The scheduler's ``postponed`` exclusion branches are therefore
inert for this source (kept as defensive no-ops).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from oddsiq import config
from oddsiq.db.models import Game
from oddsiq.scrapers.normalize import normalize_team
from oddsiq.scrapers.retry import RetryableError, parse_retry_after, with_backoff
from oddsiq.sports.profiles import SPORTS

logger = logging.getLogger(__name__)

# Map Odds API sport_key → internal sport key (e.g. "basketball_nba" → "nba").
_ODDS_API_KEY_TO_SPORT: dict[str, str] = {
    p.odds_api_key: p.key for p in SPORTS.values()
}


class ScheduleFetchError(Exception):
    """Raised when the Odds API schedule/scores data could not be fetched."""


class OddsApiScheduleSource:
    """Schedule + scores source backed by The Odds API ``/scores`` endpoint.

    ``/scores?daysFrom=3`` returns upcoming + live + recently-final games (each
    with ``commence_time``), so it alone yields the whole slate keyed by Odds
    API event id. Replaces the former balldontlie schedule scraper.
    """

    def __init__(self, api_key: str | None = None) -> None:
        # Single Odds API key (config.ODDS_API_KEY by default). An empty key is
        # preserved so a missing-key misconfig surfaces as the normal 401 path.
        self._api_key: str = api_key if api_key is not None else config.ODDS_API_KEY
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def fetch_games(self, sport_key: str = "basketball_nba") -> list[Game]:
        """Fetch the full slate (upcoming + live + recently-final) from /scores.

        ``/sports/{sport_key}/scores?daysFrom=3`` returns upcoming, live, and
        recently-completed games — each with ``commence_time`` — so it alone
        yields the whole slate. Other 4xx (and a malformed JSON body) raise
        ScheduleFetchError immediately; 429/5xx/transport errors are retried with
        full-jitter backoff (up to 3 attempts, Retry-After clamped to MAX_DELAY)
        before raising ScheduleFetchError. See ``_fetch_endpoint``.
        """
        internal_sport = _ODDS_API_KEY_TO_SPORT.get(sport_key, sport_key.split("_")[-1])

        scores_events = await self._fetch_endpoint(
            f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/scores",
            {"daysFrom": "3", "dateFormat": "iso"},
        )

        games_by_id: dict[str, Game] = {}
        for event in scores_events:
            game = self._parse_scores_event(event, internal_sport)
            if game is not None:
                games_by_id[game.external_id] = game

        return list(games_by_id.values())

    async def _fetch_endpoint(self, url: str, params: dict) -> list[dict]:
        """GET ``url`` with the API key injected. 429/5xx/transport retry with
        backoff (honoring Retry-After); other 4xx and malformed-JSON 2xx raise
        ScheduleFetchError immediately. Callers never see a raw httpx/json
        exception — the startup ``refresh()`` runs outside the main loop's
        try/except, so an unguarded error there would crash the scheduler.
        """
        call_params = {**params, "apiKey": self._api_key}

        async def _attempt() -> list[dict]:
            client = self._get_client()
            try:
                resp = await client.get(url, params=call_params)
            except httpx.RequestError as e:
                raise RetryableError(f"request error: {e}") from e
            status = resp.status_code
            if status == 429:
                raise RetryableError(
                    f"HTTP 429 for {url}",
                    retry_after=parse_retry_after(resp.headers.get("Retry-After")),
                )
            if 400 <= status < 500:
                raise ScheduleFetchError(f"Odds API returned {status} for {url}")
            if 500 <= status < 600:
                raise RetryableError(f"HTTP {status} for {url}")
            if not 200 <= status < 300:
                # Defensive: an unexpected non-2xx (e.g. a 3xx the client didn't
                # follow) has no usable JSON body.
                raise ScheduleFetchError(f"Odds API returned {status} for {url}")
            try:
                return resp.json()
            except ValueError as e:
                # A 2xx with a non-JSON body (CDN/maintenance/captive-portal
                # page) — JSONDecodeError is a ValueError. Surface it cleanly.
                raise ScheduleFetchError(f"malformed JSON body from {url}") from e

        try:
            return await with_backoff(_attempt, label=f"odds schedule {url}")
        except RetryableError as e:
            logger.error("Odds API schedule fetch failed after retries: %s", e)
            raise ScheduleFetchError(f"failed to fetch {url}") from e

    def _parse_scores_event(self, event: dict, sport: str) -> Game | None:
        """Parse a /scores event into a Game with status + scores."""
        event_id = event.get("id", "")
        home_raw = event.get("home_team", "")
        away_raw = event.get("away_team", "")
        commence_raw = event.get("commence_time", "")
        if not (event_id and home_raw and away_raw and commence_raw):
            return None

        home = normalize_team(home_raw, "pinnacle", sport)
        away = normalize_team(away_raw, "pinnacle", sport)
        if home is None or away is None:
            logger.debug(
                "OddsApiScheduleSource: skipping scores event %s — team normalization failed "
                "(%r, %r)", event_id, home_raw, away_raw,
            )
            return None

        commence = _parse_iso(commence_raw)
        if commence is None:
            return None

        completed = event.get("completed", False)
        scores_raw = event.get("scores")

        home_score: int | None = None
        away_score: int | None = None
        if scores_raw:
            home_score = _parse_score(scores_raw, home_raw)
            away_score = _parse_score(scores_raw, away_raw)

        if completed:
            # Only mark final when both scores parse cleanly.
            if home_score is not None and away_score is not None:
                status = "final"
            else:
                # scores present but unparseable — treat conservatively as live.
                status = "live"
        elif scores_raw:
            # scores present but game not completed → live.
            status = "live"
        else:
            status = "scheduled"

        return Game(
            external_id=event_id,
            home_team=home,
            away_team=away,
            scheduled_at=commence,
            commence_time=commence,
            status=status,
            home_score=home_score,
            away_score=away_score,
            sport=sport,
        )

    async def health_check(self) -> bool:
        """GET the free /sports endpoint; True on 200, False otherwise."""
        try:
            client = self._get_client()
            resp = await client.get(
                f"{config.ODDS_API_BASE_URL}/sports",
                params={"apiKey": self._api_key},
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error("Odds API schedule health check failed: %s", e)
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO 8601 datetime string to a UTC-aware datetime, or None on failure.

    A naive input (no ``Z``/offset) is coerced to UTC so the result is always
    tz-aware — callers subtract it from ``datetime.now(UTC)`` and a naive value
    would raise ``TypeError`` in the unguarded startup ``refresh()``.
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_score(scores: list[dict], team_name: str) -> int | None:
    """Extract and parse the integer score for team_name from a /scores scores array."""
    for entry in scores:
        if entry.get("name") == team_name:
            try:
                return int(entry["score"])
            except (ValueError, KeyError, TypeError):
                return None
    return None
