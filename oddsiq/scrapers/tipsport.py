from __future__ import annotations

import logging
import math
import asyncio
import time
from datetime import date, datetime, timezone

from curl_cffi.requests import AsyncSession
from playwright.async_api import Error as PlaywrightError

from oddsiq import config
from oddsiq.db.models import OddsSnapshot
from oddsiq.scrapers.base import BaseScraper
from oddsiq.scrapers.cloudflare import CloudflareSession, CloudflareSolveError
from oddsiq.scrapers.normalize import normalize_team

logger = logging.getLogger(__name__)

TIPSPORT_IMPERSONATE = "chrome146"
API_SESSION_COOKIE_NAMES = {"JSESSIONID"}

# Headers applied only on the JSON POST to /rest/offer/v2/offer.
# Deliberately NOT set at AsyncSession level so the warmup GET inherits
# chrome146's native Accept/Sec-Fetch defaults for HTML navigation rather
# than this JSON-flavored set.
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json;charset=utf-8",
}


def _warmup_url(profile) -> str:
    s = profile.tipsport_sport_slug
    return (
        f"https://www.tipsport.cz/kurzy/{s}/{s}-{profile.tipsport_gender_slug}/"
        f"{profile.tipsport_league_slug}-{profile.tipsport_competition_id}"
    )


def _moneyline_filters(profile) -> list:
    # The default ([]) request returns the WINNER_WHOLE_MATCH tab. That tab is a
    # true 2-way only for MLB (baseball cannot tie); for basketball and hockey it
    # carries the regulation 3-way legs (1/1x/x/x2/2), whose 1/2 prices are NOT
    # the moneyline. Those sports use WINNER_FINAL_DECISION (2-way incl OT),
    # which only populates when requested explicitly with the superSportId.
    if profile.tipsport_moneyline_matchview == "WINNER_WHOLE_MATCH":
        return []
    return [{
        "matchView": profile.tipsport_moneyline_matchview,
        "id": {"superSportId": profile.tipsport_super_sport_id},
    }]


class TipsportMarketUnavailable(Exception):
    """A well-formed Tipsport response that doesn't contain the sport's moneyline
    matchView tab (the market isn't currently offered — e.g. the 2-way tab not
    yet posted). Distinct from a transport/parse failure so the fetch layer does
    NOT treat it as a reason to reset the shared Cloudflare session: a reset
    can't make Tipsport post a market, and the session is shared across sports."""


class TipsportScraper(BaseScraper):
    """Fetch odds from Tipsport.cz via reverse-engineered API."""

    SESSION_MAX_AGE: float = 1200.0  # 20 minutes — refresh before CF cookies expire (~30 min)

    def __init__(self) -> None:
        self._client: AsyncSession | None = None
        self._cf_session: CloudflareSession | None = None
        # Monotonic time of the most recent session warmup of EITHER transport
        # (curl `_get_client` or Playwright `_get_cf_session`). _maybe_refresh_session
        # uses it to proactively recycle before Cloudflare cookies (~30 min) expire.
        self._session_warmed_at: float = 0
        self._consecutive_failures: int = 0
        # Number of swallowed internal exceptions during the MOST RECENT
        # fetch_odds call. Resets to 0 at the start of every fetch. Surfaced
        # by Scheduler in the per-scraper health log so a poll that succeeded
        # on some markets and failed silently on others is visible to the
        # operator during the soak. See HIGH 2 in the P2.2 feedback.
        self.last_fetch_error_count: int = 0

    @property
    def name(self) -> str:
        return "tipsport"

    async def _get_client(self, profile) -> AsyncSession:
        if self._client is None:
            self._client = AsyncSession(impersonate=TIPSPORT_IMPERSONATE, timeout=15)
            resp = await self._client.get(_warmup_url(profile))
            self._session_warmed_at = time.monotonic()
            if resp.status_code == 403:
                logger.info("Tipsport: warmup blocked; will retry POST and may fall back")
            else:
                logger.info("Tipsport: session warm-up complete (status %d)", resp.status_code)
        return self._client

    @staticmethod
    def _client_has_api_session(client: AsyncSession) -> bool:
        return any(name in API_SESSION_COOKIE_NAMES for name in client.cookies.keys())

    async def _get_cf_session(self, profile) -> CloudflareSession:
        """Get or create a Playwright-backed Cloudflare session."""
        if self._cf_session is None:
            self._cf_session = CloudflareSession(_warmup_url(profile))
            try:
                await self._cf_session.start()
                self._session_warmed_at = time.monotonic()
            except CloudflareSolveError as e:
                self._cf_session = None
                raise ConnectionError(
                    f"Tipsport: Playwright failed to solve Cloudflare — {e}"
                ) from e
        return self._cf_session

    async def _copy_browser_api_session_to_client(
        self, client: AsyncSession, profile, *, force_new_browser: bool = False
    ) -> int:
        """Copy only Tipsport API-session cookies from Playwright to curl_cffi.

        curl_cffi keeps its own Cloudflare cookies and TLS fingerprint. Copying
        Playwright's Cloudflare cookies into a different TLS stack gets blocked,
        but the browser-created JSESSIONID is enough to satisfy the REST API.
        """
        if force_new_browser and self._cf_session is not None:
            await self._cf_session.close()
            self._cf_session = None

        cf = await self._get_cf_session(profile)
        cookies = await cf.export_cookies(API_SESSION_COOKIE_NAMES)
        copied = 0
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            client.cookies.set(
                name,
                value,
                domain=cookie.get("domain") or "",
                path=cookie.get("path") or "/",
                secure=bool(cookie.get("secure")),
            )
            copied += 1

        if copied == 0:
            raise ConnectionError("Tipsport: browser session did not expose JSESSIONID")
        logger.info("Tipsport: copied browser API session cookie into curl_cffi")
        return copied

    @staticmethod
    async def _safe_close(closeable) -> None:
        """Close a curl/Playwright session, swallowing+logging any error so one
        failing close can't skip the other (a leaked Chromium on shutdown)."""
        if closeable is None:
            return
        try:
            await closeable.close()
        except Exception as e:  # defensive: best-effort teardown
            logger.warning("Tipsport: error closing %s: %s", type(closeable).__name__, e)

    async def _reset_session(self) -> None:
        """Destroy current sessions so next call creates fresh ones."""
        await self._safe_close(self._client)
        await self._safe_close(self._cf_session)
        self._client = None
        self._cf_session = None
        self._consecutive_failures = 0
        self._session_warmed_at = 0
        logger.warning("Tipsport: session expired, re-creating with fresh warm-up")

    async def _maybe_refresh_session(self) -> None:
        """Proactively refresh sessions before Cloudflare cookies expire."""
        if self._cf_session is None and self._client is None:
            return
        age = time.monotonic() - self._session_warmed_at
        if age > self.SESSION_MAX_AGE:
            logger.info("Tipsport: proactive session refresh (age: %.0fs)", age)
            await self._reset_session()

    async def fetch_odds(self, game_date: date, profile) -> list[OddsSnapshot]:
        # Moneyline-only sole scraper. Non-raising: returns [] on failure and
        # surfaces degradation via last_fetch_error_count.
        self.last_fetch_error_count = 0
        await self._maybe_refresh_session()
        try:
            snapshots = await self._fetch_moneyline(game_date, profile)
        except TipsportMarketUnavailable as e:
            # Well-formed response, market simply not offered this cycle. NOT a
            # session failure — don't count it toward MAX_CONSECUTIVE_FAILURES or
            # churn the shared Cloudflare session the other sports are using.
            logger.info("Tipsport %s: no moneyline market offered this cycle (%s)", profile.key, e)
            return []
        except (ConnectionError, OSError, KeyError, IndexError, TypeError, ValueError,
                PlaywrightError) as e:
            # PlaywrightError covers the Cloudflare fallback path (page.evaluate,
            # context.cookies) raising on a destroyed/closed browser context — it
            # subclasses Exception, not OSError, so without this it would escape
            # fetch_odds uncounted and the session would never reset.
            self.last_fetch_error_count = 1
            self._consecutive_failures += 1
            logger.error(
                "Tipsport %s fetch failed (consecutive: %d): %s",
                profile.key, self._consecutive_failures, e,
            )
            if self._consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES:
                await self._reset_session()
            return []
        self._consecutive_failures = 0
        return snapshots

    async def _fetch_moneyline(self, game_date: date, profile) -> list[OddsSnapshot]:
        body = self._build_request_body(profile)
        api_url = f"{config.TIPSPORT_API_URL}?limit=75"
        headers = {**HEADERS, "Referer": _warmup_url(profile)}
        client = await self._get_client(profile)

        captured_at = datetime.now(timezone.utc)
        resp = await client.post(api_url, json=body, headers=headers)
        if resp.status_code >= 400:
            logger.info("Tipsport: curl_cffi %d on %s, syncing browser API session",
                        resp.status_code, profile.key)
            force_new = resp.status_code == 401 and self._client_has_api_session(client)
            await self._copy_browser_api_session_to_client(client, profile, force_new_browser=force_new)
            captured_at = datetime.now(timezone.utc)
            resp = await client.post(api_url, json=body, headers=headers)

        if resp.status_code >= 400:
            logger.info("Tipsport: curl_cffi %d on %s after session sync, using Playwright",
                        resp.status_code, profile.key)
            cf = await self._get_cf_session(profile)
            captured_at = datetime.now(timezone.utc)
            try:
                data = await asyncio.wait_for(cf.post(api_url, json=body), timeout=60)
            except asyncio.TimeoutError:
                raise ConnectionError(f"Tipsport: Playwright timed out after 60s for {profile.key}")
        else:
            data = resp.json()

        raw_path = await asyncio.to_thread(self._save_raw_response, data, game_date)
        snapshots = self.parse_response(data, profile, raw_path, captured_at)
        logger.info("Tipsport %s: fetched %d odds from %d matches",
                    profile.key, len(snapshots), self._count_matches(data, profile))
        return snapshots

    def _build_request_body(self, profile) -> dict:
        return {
            "results": False,
            "highlightAnyTime": False,
            "limit": 75,
            "order": "DATESTART",
            "type": "COMPETITION",
            "id": profile.tipsport_competition_id,
            "matchViewFilters": _moneyline_filters(profile),
            "withLive": True,
        }

    def parse_response(
        self,
        data: dict,
        profile,
        raw_response_path: str | None = None,
        captured_at: datetime | None = None,
    ) -> list[OddsSnapshot]:
        snapshots: list[OddsSnapshot] = []
        now = captured_at if captured_at is not None else datetime.now(timezone.utc)
        try:
            tabs = data["offerSuperSports"][0]["tabs"]
        except (KeyError, IndexError, TypeError) as e:
            logger.warning("Tipsport: unexpected response structure: %s", e)
            raise

        target_view = profile.tipsport_moneyline_matchview
        tab = next((t for t in tabs if t.get("matchView") == target_view), None)
        if tab is None:
            # Fail closed: never fall back to another tab. For basketball and
            # hockey the default WINNER_WHOLE_MATCH tab carries the regulation
            # 3-way legs (1/1x/x/x2/2) — reading its 1/2 prices as moneyline
            # would fabricate edges (only MLB's WHOLE_MATCH is a true 2-way).
            # The envelope is well-formed, so this is "market not offered" (a
            # distinct, non-session-failure type) rather than a transport/parse
            # failure.
            raise TipsportMarketUnavailable(
                f"Tipsport response missing {target_view} tab for {profile.key}"
            )

        for comp in tab.get("offerCompetitionAnnuals", []):
            for match in comp.get("matches", []):
                match_id = match.get("id")
                match_url = match.get("url")
                match_start = self._parse_match_start(match)
                raw_home = match.get("participantHome", "")
                raw_away = match.get("participantVisiting", "")
                home_team = normalize_team(raw_home, "tipsport", profile.key)
                away_team = normalize_team(raw_away, "tipsport", profile.key)
                if home_team is None or away_team is None:
                    logger.debug("Tipsport: skipping match '%s' — could not normalize teams",
                                 match.get("nameFull", ""))
                    continue
                opp_rows = match.get("oppRows", [])
                if not opp_rows:
                    continue
                for opp in opp_rows[0].get("oppsTab", []):
                    if not opp or not opp.get("bettingEnabled", False):
                        continue
                    selection = self._map_opp(opp.get("type", ""))
                    if selection is None:
                        continue
                    odds = self._parse_odds(opp.get("odd"))
                    if odds is None:
                        logger.debug("Tipsport: skipping invalid odds %r for %s in '%s'",
                                     opp.get("odd"), selection, match.get("nameFull", ""))
                        continue
                    snapshots.append(OddsSnapshot(
                        game_id=0,
                        bookmaker="tipsport",
                        market_type="moneyline",
                        selection=selection,
                        odds=odds,
                        line=None,
                        raw_response_path=raw_response_path,
                        captured_at=now,
                        sport=profile.key,
                        _home_team=home_team,
                        _away_team=away_team,
                        # The listing's OWN start time — game attribution
                        # requires it (see _parse_match_start).
                        _commence_time=match_start,
                        # Execution identity (transient — persisted only on
                        # value_bets at detection, migration 009).
                        _tipsport_match_id=match_id,
                        _tipsport_opp_id=opp.get("id"),
                        _tipsport_url=match_url,
                    ))
        return snapshots

    @staticmethod
    def _parse_match_start(match: dict) -> datetime | None:
        """The listing's OWN scheduled start (``dateClosed``/``dateStartTv``,
        ISO-8601 with offset; per-opp ``dateClosed`` as a last fallback).

        Load-bearing for game attribution: Tipsport posts the NEXT game of an
        MLB series under /kurzy/ while today's game is still pregame — same
        team pair, different day. A names-only match pinned tomorrow's untuned
        opener prices onto today's game row, manufacturing fake pregame edges
        against today's sharp fair (2026-08-30 wrong-game forensics: every
        clean MLB detection since at least Aug 22 was a next-day listing).
        Carried as ``_commence_time`` so ``PollCoordinator._matches_game``
        applies its ±2h window to Tipsport exactly as it does to Pinnacle.
        None when absent or malformed. PollCoordinator fails those snapshots
        closed for attribution and exposes a health counter: losing all
        Tipsport matching is visible and safer than manufacturing next-game
        edges from ambiguous pair-only matching.
        """
        raw = match.get("dateClosed") or match.get("dateStartTv")
        if not raw:
            opp_rows = match.get("oppRows") or [{}]
            opps = opp_rows[0].get("oppsTab") or []
            raw = next(
                (o.get("dateClosed") for o in opps if o and o.get("dateClosed")),
                None,
            )
        if not isinstance(raw, str) or not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo is not None else None
        except ValueError:
            return None

    @staticmethod
    def _parse_odds(value: object) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            odds = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(odds) or odds <= 0:
            return None
        return odds

    @staticmethod
    def _map_opp(opp_type: str) -> str | None:
        if opp_type == "1":
            return "home"
        if opp_type == "2":
            return "away"
        return None

    @staticmethod
    def _count_matches(data: dict, profile) -> int:
        try:
            tabs = data["offerSuperSports"][0]["tabs"]
        except (KeyError, IndexError):
            return 0
        tab = next((t for t in tabs if t.get("matchView") == profile.tipsport_moneyline_matchview), None)
        if tab is None:
            return 0
        return sum(len(c.get("matches", [])) for c in tab.get("offerCompetitionAnnuals", []))

    async def close(self) -> None:
        # Guard each independently: a failing curl close must not skip the
        # Playwright/CF shutdown (otherwise a Chromium process leaks on exit).
        await self._safe_close(self._client)
        await self._safe_close(self._cf_session)
        self._client = None
        self._cf_session = None
