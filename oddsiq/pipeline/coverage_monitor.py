"""Coverage-gap monitor: alerts when a scraper's matched coverage for a watched
game goes stale (>10 min) while any game sits in warm/hot tier. Owns the
in-memory match timestamps + the alert-dedup set. Extracted verbatim from the
old Scheduler._check_coverage_gaps / _format_last_coverage.
"""

from __future__ import annotations

import logging
from datetime import datetime

from oddsiq.db.models import Game
from oddsiq.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


# Staleness threshold = max(_STALE_FLOOR_SECONDS, _STALE_INTERVAL_MULT × the
# tier's effective (possibly throttled) poll interval) — so a healthy pipeline
# that's simply not due to poll yet (under budget throttle) doesn't false-alert.
_STALE_FLOOR_SECONDS = 600
_STALE_INTERVAL_MULT = 2.5


class CoverageMonitor:
    def __init__(self, governor=None) -> None:
        # _last_game_coverage_success: a scraper only "covers" a watched game
        # after its returned odds actually MATCH that game (set by record_match
        # from the poll distribution loop).
        self._last_game_coverage_success: dict[tuple[int, str], datetime] = {}
        # Per-scraper most-recent match, kept forever (one entry per scraper):
        # backs the dashboard scoreboard's "last seen" so the per-game dict
        # above can be pruned to the slate without losing that signal.
        self._last_scraper_match: dict[str, datetime] = {}
        # Alerts already fired for a given (game_id, scraper_name) gap.
        self._coverage_gap_alerted: set[tuple[int, str]] = set()
        self._last_coverage_check: datetime | None = None
        # Optional BudgetGovernor — used to scale the staleness threshold with
        # the current throttled cadence. None → fixed _STALE_FLOOR_SECONDS.
        self._governor = governor

    def _stale_threshold(self, tier: str, now: datetime) -> float:
        """Staleness bound for ``tier``: at least the floor, but ~2.5 expected
        poll intervals so a throttle-widened cadence doesn't trip false alerts."""
        if self._governor is None:
            return _STALE_FLOOR_SECONDS
        interval = self._governor.interval_for_tier(tier, now)
        return max(_STALE_FLOOR_SECONDS, _STALE_INTERVAL_MULT * interval)

    def record_match(self, game_id: int, scraper_name: str, now: datetime) -> None:
        """Record that `scraper_name` produced odds matching game `game_id`, and
        clear any outstanding gap alert for that (game, scraper)."""
        key = (game_id, scraper_name)
        self._last_game_coverage_success[key] = now
        prev = self._last_scraper_match.get(scraper_name)
        if prev is None or now > prev:
            self._last_scraper_match[scraper_name] = now
        self._coverage_gap_alerted.discard(key)

    async def check_gaps(
        self, games: list[Game], scrapers: list[BaseScraper], now: datetime
    ) -> None:
        """Alert when a scraper's matched coverage is >10 min stale while any
        game sits in warm or hot tier. Reads in-memory match timestamps (not
        DB rows — snapshot dedupe means rows can stay quiet on a healthy pipeline).
        Dedup: one alert per (game_id, scraper_name) until the scraper's next
        successful match clears the key. Throttled to one run per 2 min.
        """
        if self._last_coverage_check is not None:
            if (now - self._last_coverage_check).total_seconds() < 120:
                return
        self._last_coverage_check = now

        # Prune per-game state for games that have dropped out of the slate so
        # neither structure grows unbounded over a long-lived (24/7) process.
        # The dashboard scoreboard's per-scraper "last seen" survives in
        # _last_scraper_match (bounded: one entry per scraper).
        slate_ids = {g.id for g in games}
        self._coverage_gap_alerted = {
            key for key in self._coverage_gap_alerted if key[0] in slate_ids
        }
        self._last_game_coverage_success = {
            key: seen
            for key, seen in self._last_game_coverage_success.items()
            if key[0] in slate_ids
        }

        watched_games = [
            g
            for g in games
            if g.id is not None
            and g.status not in ("final", "postponed", "live")
            and 0 < ((g.commence_time or g.scheduled_at) - now).total_seconds() <= 7200
        ]
        if not watched_games:
            return

        for game in watched_games:
            ttip = ((game.commence_time or game.scheduled_at) - now).total_seconds()
            tier = "hot" if ttip <= 3600 else "warm"
            threshold = self._stale_threshold(tier, now)
            for scraper in scrapers:
                last = self._last_game_coverage_success.get((game.id, scraper.name))
                if last is None:
                    stale_seconds = None
                else:
                    stale_seconds = (now - last).total_seconds()
                    if stale_seconds <= threshold:
                        continue
                key = (game.id, scraper.name)
                if key in self._coverage_gap_alerted:
                    continue
                self._coverage_gap_alerted.add(key)
                minutes = (
                    int(stale_seconds // 60) if stale_seconds is not None else None
                )
                # Logged for the dashboard activity feed; no phone alert
                # (2026-08-10 rebrand — only real placed/won/lost notify).
                logger.warning(
                    "Coverage gap: game_id=%d scraper=%s tier=%s minutes_stale=%s",
                    game.id,
                    scraper.name,
                    tier,
                    minutes,
                )

    def last_coverage(self, scraper_name: str) -> str:
        last = self._last_scraper_match.get(scraper_name)
        if last is None:
            return "never"
        return last.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
