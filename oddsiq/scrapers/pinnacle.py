from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta, timezone

import httpx

from oddsiq import config
from oddsiq.db.models import OddsSnapshot
from oddsiq.scrapers.base import BaseScraper
from oddsiq.scrapers.normalize import normalize_team
from oddsiq.scrapers.retry import RetryableError, parse_retry_after, with_backoff

logger = logging.getLogger(__name__)


class PinnacleScraper(BaseScraper):
    def __init__(self, api_key: str | None = None) -> None:
        # Single Odds API key (config.ODDS_API_KEY by default). An empty key is
        # preserved rather than rejected so a missing-key misconfig surfaces as
        # the normal 401 path through logging rather than a constructor error.
        self._api_key: str = api_key if api_key is not None else config.ODDS_API_KEY
        self._budget_remaining = config.ODDS_API_MONTHLY_BUDGET
        self._budget_used = 0
        self._client: httpx.AsyncClient | None = None
        # Number of swallowed fetch failures during the MOST RECENT fetch_odds
        # call (mirrors TipsportScraper). PollCoordinator reads it via getattr
        # for the per-scraper health log — without it a fully failed Pinnacle
        # poll reports errors=0. The deliberate budget-exhausted skip is NOT
        # counted (it is already surfaced by the warning + budget governor).
        self.last_fetch_error_count: int = 0
        # Per-parse integrity counters surfaced on the structured scraper
        # health line. They count malformed upstream records, not HTTP errors.
        self.last_parse_missing_event_ids: int = 0
        self.last_parse_missing_source_updates: int = 0

    @property
    def name(self) -> str:
        return "pinnacle"

    @property
    def budget_remaining(self) -> int:
        return self._budget_remaining

    @property
    def budget_used(self) -> int:
        return self._budget_used

    @property
    def budget_total(self) -> int:
        """Best-known total quota for the key. Uses used+remaining once the
        probe has populated both; falls back to the configured monthly budget
        otherwise."""
        if self._budget_used > 0 or self._budget_remaining != config.ODDS_API_MONTHLY_BUDGET:
            return self._budget_used + self._budget_remaining
        return config.ODDS_API_MONTHLY_BUDGET

    def should_skip_request(self) -> bool:
        """True when the key is out of monthly budget — skip the request rather
        than spend a guaranteed-429 round trip. The budget governor handles
        cadence throttling above this hard floor."""
        return self._budget_remaining <= 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def probe_budget(self) -> dict[str, int] | None:
        """Call the free /v4/sports endpoint and read the quota headers.

        This endpoint does NOT count against the monthly budget, so it's safe
        to call on every startup. Updates self._budget_{used,remaining} on
        success. Returns {"used": int, "remaining": int} on success, None on
        failure (network, HTTP error, or missing headers).
        """
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{config.ODDS_API_BASE_URL}/sports",
                params={"apiKey": self._api_key},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Odds API budget probe failed: %s", e)
            return None
        used = resp.headers.get("x-requests-used")
        remaining = resp.headers.get("x-requests-remaining")
        if used is None or remaining is None:
            logger.warning("Odds API budget probe: quota headers missing")
            return None
        # int() can raise ValueError on whitespace/garbage headers (low-prob,
        # but the probe runs once at startup and the surrounding HTTPError
        # guard does NOT catch ValueError — so a malformed header would crash
        # the scheduler before its main loop). Fail soft to the DB-fallback path.
        try:
            self._budget_used = int(used)
            self._budget_remaining = int(remaining)
        except (TypeError, ValueError):
            logger.warning(
                "Odds API budget probe: quota headers malformed: used=%r remaining=%r",
                used, remaining,
            )
            return None
        return {"used": self._budget_used, "remaining": self._budget_remaining}

    def _apply_quota_headers(self, headers: httpx.Headers) -> None:
        """Update best-known quota counters from an Odds API response.

        The Odds API sends quota headers on normal odds responses and may also
        include them on quota/auth failures. Treat malformed headers as a
        visibility issue, not a scraper crash.
        """
        remaining = headers.get("x-requests-remaining")
        used = headers.get("x-requests-used")
        try:
            if remaining is not None:
                self._budget_remaining = int(remaining)
            if used is not None:
                self._budget_used = int(used)
        except (TypeError, ValueError):
            logger.warning(
                "Odds API: malformed quota headers, leaving budget unchanged: "
                "used=%r remaining=%r",
                used, remaining,
            )
            return
        if remaining is not None:
            total = self.budget_total
            logger.info(
                "Odds API budget: %d used / %d remaining (%d total)",
                self._budget_used, self._budget_remaining, total,
            )

    async def fetch_odds(self, game_date: date, profile) -> list[OddsSnapshot]:
        self.last_fetch_error_count = 0
        # Reset the parse counters here, not only in parse_response: a budget
        # skip or an HTTP failure returns [] without parsing, and counters left
        # at the previous cycle's values would republish stale integrity
        # numbers on this cycle's scraper health line.
        self.last_parse_missing_event_ids = 0
        self.last_parse_missing_source_updates = 0
        if self.should_skip_request():
            logger.warning("Odds API budget exhausted (0 remaining) — skipping fetch")
            return []
        try:
            data, captured_at = await with_backoff(
                lambda: self._fetch_odds_once(profile), label="pinnacle fetch",
            )
        except (httpx.HTTPError, RetryableError) as e:
            self.last_fetch_error_count = 1
            logger.error("Pinnacle fetch failed: %s", e)
            return []
        raw_path = await asyncio.to_thread(self._save_raw_response, data, game_date)
        return self.parse_response(data, raw_path, captured_at, sport=profile.key)

    async def _fetch_odds_once(self, profile) -> tuple[list[dict], datetime]:
        """One Odds API /odds GET. Raises RetryableError on 429/5xx/transport
        (honoring Retry-After); raises httpx.HTTPStatusError on other non-2xx."""
        client = await self._get_client()
        # Stamp captured_at at request initiation, not parse time — symmetric
        # with Tipsport. Pinnacle's ~500ms latency makes the drift invisible.
        captured_at = datetime.now(timezone.utc)
        try:
            resp = await client.get(
                f"{config.ODDS_API_BASE_URL}/sports/{profile.odds_api_key}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": "us",
                    "markets": config.PINNACLE_MARKETS,
                    "bookmakers": "pinnacle",
                },
            )
        except httpx.RequestError as e:
            raise RetryableError(f"transport error: {e}") from e
        self._apply_quota_headers(resp.headers)
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise RetryableError(
                f"HTTP {resp.status_code}",
                retry_after=parse_retry_after(resp.headers.get("Retry-After")),
            )
        resp.raise_for_status()
        return resp.json(), captured_at

    def parse_response(
        self,
        data: list[dict],
        raw_response_path: str | None = None,
        captured_at: datetime | None = None,
        sport: str = "nba",
    ) -> list[OddsSnapshot]:
        snapshots: list[OddsSnapshot] = []
        now = captured_at if captured_at is not None else datetime.now(timezone.utc)
        self.last_parse_missing_event_ids = 0
        self.last_parse_missing_source_updates = 0
        for event in data:
            # Isolate per event: a single malformed event must not abort the
            # whole poll. Pinnacle is the sharp reference book, so dropping the
            # entire response would zero out edge detection for every book that
            # cycle. Buffer per event so a malformed one contributes nothing.
            try:
                event_snaps: list[OddsSnapshot] = []
                event_id = event.get("id")
                if not isinstance(event_id, str) or not event_id.strip():
                    self.last_parse_missing_event_ids += 1
                    logger.warning(
                        "Skipping Pinnacle event without a usable id "
                        "(home=%s away=%s commence=%s)",
                        event.get("home_team"), event.get("away_team"),
                        event.get("commence_time"),
                    )
                    continue
                home_team = event["home_team"]
                away_team = event["away_team"]
                # Normalize the match-key team names to canonical (mirrors
                # Tipsport + the schedule source). _map_market still keys off the
                # RAW names because the outcome labels come from the same raw
                # event; but the snapshot's _home_team/_away_team — what
                # _matches_game compares against the normalized game row — must
                # be canonical, or an aliased/fuzzy-mapped name silently never
                # matches and blacks out edge detection for that game.
                home_norm = normalize_team(home_team, "pinnacle", sport) or home_team
                away_norm = normalize_team(away_team, "pinnacle", sport) or away_team
                commence_time = self._parse_commence_time(event.get("commence_time"))
                for bookmaker in event.get("bookmakers", []):
                    if bookmaker["key"] != "pinnacle":
                        continue
                    for market in bookmaker.get("markets", []):
                        market_key = market["key"]
                        source_updated_at = self._parse_source_updated_at(
                            market.get("last_update"), observed_at=now)
                        if source_updated_at is None:
                            source_updated_at = self._parse_source_updated_at(
                                bookmaker.get("last_update"), observed_at=now)
                        source_freshness_missing = source_updated_at is None
                        if source_freshness_missing:
                            self.last_parse_missing_source_updates += 1
                            logger.warning(
                                "Pinnacle market missing/invalid last_update "
                                "(event=%s market=%s)",
                                event_id, market_key,
                            )
                        for outcome in market.get("outcomes", []):
                            market_type, selection = self._map_market(
                                market_key, outcome["name"], home_team, away_team
                            )
                            if market_type is None:
                                continue
                            # Defensive price floor (mirrors Tipsport's
                            # _parse_odds): a non-numeric / non-finite / <=1.0
                            # price would flow into remove_vig()/find_edges()
                            # and raise mid-poll instead of being skipped here.
                            price = outcome.get("price")
                            if (
                                not isinstance(price, (int, float))
                                or isinstance(price, bool)
                                or not math.isfinite(price)
                                or price <= 1.0
                            ):
                                logger.warning(
                                    "Skipping Pinnacle outcome with invalid price %r "
                                    "(event=%s market=%s selection=%s)",
                                    price, event.get("id"), market_key, selection,
                                )
                                continue
                            line = outcome.get("point")
                            event_snaps.append(OddsSnapshot(
                                game_id=0,
                                bookmaker="pinnacle",
                                market_type=market_type,
                                selection=selection,
                                odds=price,
                                line=line,
                                raw_response_path=raw_response_path,
                                source_updated_at=source_updated_at,
                                captured_at=now,
                                sport=sport,
                                _home_team=home_norm,
                                _away_team=away_norm,
                                _commence_time=commence_time,
                                _source_event_id=event_id,
                                _source_freshness_missing=source_freshness_missing,
                            ))
            except (KeyError, TypeError) as e:
                logger.warning(
                    "Skipping malformed Pinnacle event %s: %s", event.get("id"), e
                )
                continue
            snapshots.extend(event_snaps)
        return snapshots

    @staticmethod
    def _parse_source_updated_at(
        value: str | None, *, observed_at: datetime,
    ) -> datetime | None:
        """Parse a trustworthy, timezone-aware upstream market timestamp.

        Returns None for anything that cannot be trusted: a missing/malformed
        value, a naive timestamp (not safely comparable), or a value more than
        one minute in the future (payload/clock drift).

        None is a rejection of THIS value, not automatically the fail-closed
        path: the caller first retries with the bookmaker-level ``last_update``
        and only marks the market ``source freshness missing`` when that
        fallback is rejected too.
        """
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        parsed = parsed.astimezone(timezone.utc)
        observed = (
            observed_at.astimezone(timezone.utc)
            if observed_at.tzinfo is not None
            else observed_at.replace(tzinfo=timezone.utc)
        )
        if parsed > observed + timedelta(seconds=60):
            return None
        return parsed

    @staticmethod
    def _parse_commence_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Invalid Pinnacle commence_time: %s", value)
            return None
        # Coerce a naive value to UTC so the result is always tz-aware.
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _map_market(market_key: str, outcome_name: str, home_team: str, away_team: str) -> tuple[str | None, str | None]:
        if market_key == "h2h":
            if outcome_name == home_team:
                return "moneyline", "home"
            elif outcome_name == away_team:
                return "moneyline", "away"
        elif market_key == "spreads":
            if outcome_name == home_team:
                return "spread", "home"
            elif outcome_name == away_team:
                return "spread", "away"
        elif market_key == "totals":
            if outcome_name == "Over":
                return "total", "over"
            elif outcome_name == "Under":
                return "total", "under"
        return None, None

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
