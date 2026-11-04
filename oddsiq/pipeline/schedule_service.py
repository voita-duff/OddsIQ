"""Per-sport game discovery + status transitions over The Odds API schedule
source. Owns the in-memory game slate, refresh throttle, completion check, and
live transitions. Delegates CLV / result / recovery / closing-mark to
SettlementService.

Per-sport: fetch_fresh_games iterates enabled_profiles() (NBA/NHL/MLB/WNBA),
fetching each sport's slate from the Odds API and stamping games with their
sport.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from oddsiq import config
from oddsiq.db.models import Game
from oddsiq.pipeline import tiers
from oddsiq.pipeline._helpers import log_value as _log_value
from oddsiq.scrapers.odds_api_schedule import ScheduleFetchError
from oddsiq.sports.profiles import enabled_profiles, get_profile

logger = logging.getLogger(__name__)


class ScheduleService:
    def __init__(self, *, schedule_source, store, settlement) -> None:
        self._schedule_source = schedule_source
        self._store = store
        self._settlement = settlement
        self._games: list[Game] = []
        self._last_schedule_refresh: datetime | None = None
        # Tracks the last FULL (all-sports) sweep separately from the last refresh:
        # active-window refreshes fetch only sports with a pollable game, but a full
        # sweep must still run every SCHEDULE_REFRESH_INTERVAL so new games in
        # currently-idle sports are discovered.
        self._last_full_sweep: datetime | None = None
        self._last_completion_check: datetime | None = None
        self._handled_live: set[int] = set()

    @property
    def games(self) -> list[Game]:
        return self._games

    async def load_handled_live(self) -> None:
        self._handled_live = await self._store.get_handled_live_game_ids()
        if self._handled_live:
            logger.info("Restored %d handled-live game(s) from database", len(self._handled_live))

    async def health_check(self) -> bool:
        return await self._schedule_source.health_check()

    async def close(self) -> None:
        await self._schedule_source.close()

    async def fetch_fresh_games(self, sports: set[str] | None = None) -> list[Game]:
        """Fetch recent-final + live + upcoming games — one Odds API /scores call
        per fetched sport. With ``sports=None`` (default) it sweeps every enabled
        profile (NBA/NHL/MLB/WNBA); with a ``sports`` filter it fetches only those,
        so idle sports cost no Odds API quota."""
        all_games: list[Game] = []
        for profile in enabled_profiles():
            if sports is not None and profile.key not in sports:
                continue
            all_games.extend(await self._schedule_source.fetch_games(profile.odds_api_key))
        return all_games

    async def refresh(self, sports: set[str] | None = None) -> None:
        # Fetch recent-final + live + upcoming games from The Odds API. ``sports``
        # None = full sweep (all enabled); a subset = partial active-window refresh.
        try:
            fetched = await self.fetch_fresh_games(sports)
        except ScheduleFetchError as e:
            logger.warning(
                "Schedule refresh failed; keeping %d existing game(s): %s",
                len(self._games), e,
            )
            # _last_schedule_refresh is deliberately NOT updated on failure, so
            # maybe_refresh retries on every scheduler loop (~30s) — NOT at
            # SCHEDULE_GAME_WINDOW_REFRESH cadence — until a fetch succeeds.
            # Rejected calls (4xx/5xx, e.g. an exhausted quota's 401s) consume
            # no credits, so fast recovery wins over cadence discipline here.
            return

        for game in fetched:
            game_id = await self._store.upsert_game(game)
            game.id = game_id

            # Final-game recovery is intentionally state-independent: after a
            # restart, a game can already be final with unresolved value_bets.
            # Per-game isolation (like process_live_transitions): one game's
            # recovery failure must not abort the refresh — an unguarded raise
            # here leaves self._games stale for every sport until it clears.
            if (
                game.status == "final"
                and game.home_score is not None
                and game.away_score is not None
            ):
                try:
                    await self._settlement.recover_finalized_game(game)
                except Exception:
                    logger.exception(
                        "Final-game recovery failed for game_id=%s (%s @ %s); continuing refresh",
                        game.id, game.away_team, game.home_team,
                    )

        if sports is None:
            self._games = fetched
        else:
            # Partial refresh: replace ONLY the fetched sports' entries and retain
            # the rest of the slate. A just-final game in an unfetched (idle) sport
            # must not vanish before settlement — the daysFrom feed for the fetched
            # sports still carries their own finals, and the hourly full sweep
            # reconciles everything else.
            self._games = [g for g in self._games if g.sport not in sports] + fetched
        # Prune the handled-live set to the current slate so it can't grow
        # unbounded over a 24/7 process — a game that aged out of the slate can
        # never re-transition. Mirrors CoverageMonitor's per-refresh prune.
        slate_ids = {g.id for g in self._games if g.id is not None}
        self._handled_live &= slate_ids
        refreshed_at = datetime.now(timezone.utc)
        self._last_schedule_refresh = refreshed_at
        self._last_completion_check = refreshed_at
        if sports is None:
            self._last_full_sweep = refreshed_at
        logger.info(
            "Schedule refreshed: %d games (%s)",
            len(self._games),
            "full sweep" if sports is None else f"sports={sorted(sports)}",
        )

    def _sports_to_fetch(self, now: datetime) -> set[str] | None:
        """Which sports the next active-window refresh should fetch.

        Returns ``None`` (= full sweep over every enabled sport) when a full sweep
        is due — never swept, or ``SCHEDULE_REFRESH_INTERVAL`` elapsed since the
        last one — so new games in currently-idle sports are still discovered at
        least hourly. Otherwise returns the set of sports with a pollable game
        (same predicate the scheduler uses for polling), so idle sports cost no
        Odds API quota. Falls back to a full sweep when nothing is active."""
        if (
            self._last_full_sweep is None
            or (now - self._last_full_sweep).total_seconds() >= config.SCHEDULE_REFRESH_INTERVAL
        ):
            return None
        active: set[str] = set()
        for g in self._games:
            if g.status in ("final", "postponed"):
                continue
            ttip = tiers.time_to_tipoff(g, now)
            # A sport is active while it has a pollable (pregame) game OR an
            # in-progress game inside its lifecycle window. The second clause
            # is NOT about polling (the live tier was removed 2026-08-27) —
            # it keeps /scores refreshes flowing while games run, so status
            # flips and finals are picked up promptly.
            backstop_s = get_profile(g.sport).live_backstop_hours * 3600
            if tiers.get_poll_interval(ttip) is not None or 0 >= ttip > -backstop_s:
                active.add(g.sport)
        return active or None

    async def maybe_refresh(self, now: datetime) -> None:
        if self._last_schedule_refresh is None:
            await self.refresh()
            return
        elapsed = (now - self._last_schedule_refresh).total_seconds()
        any_active = any(
            0 < tiers.time_to_tipoff(g, now) < 21600
            for g in self._games
            if g.status == "scheduled"
        )
        interval = config.SCHEDULE_GAME_WINDOW_REFRESH if any_active else config.SCHEDULE_REFRESH_INTERVAL
        if elapsed >= interval:
            await self.refresh(sports=self._sports_to_fetch(now))

    async def check_completed_games(self) -> None:
        """Detect games that went final and resolve bets.

        Throttled to one Odds API fetch per SCHEDULE_COMPLETION_CHECK_INTERVAL
        window via _last_completion_check. Successful schedule refreshes also
        satisfy this window because they already process final transitions.

        Only sports that currently have a live game can produce a final this
        cycle, so the /scores fetch is scoped to those sports — idle sports cost
        no Odds API quota here.
        """
        live_sports = {g.sport for g in self._games if g.status == "live"}
        if not live_sports:
            return
        now = datetime.now(timezone.utc)
        if self._last_completion_check is not None:
            since_check = (now - self._last_completion_check).total_seconds()
            if since_check < config.SCHEDULE_COMPLETION_CHECK_INTERVAL:
                return
        self._last_completion_check = now
        try:
            fresh = await self.fetch_fresh_games(live_sports)
        except ScheduleFetchError as e:
            logger.warning("Completion check skipped; schedule fetch failed: %s", e)
            return
        for fresh_game in fresh:
            if fresh_game.status != "final":
                continue
            for game in self._games:
                if (
                    game.external_id == fresh_game.external_id
                    and game.sport == fresh_game.sport
                    and game.status != "final"
                ):
                    game.status = "final"
                    game.home_score = fresh_game.home_score
                    game.away_score = fresh_game.away_score
                    logger.info(
                        "game_final: game_id=%d away=%s home=%s away_score=%d home_score=%d",
                        game.id, _log_value(game.away_team), _log_value(game.home_team),
                        fresh_game.away_score, fresh_game.home_score,
                    )
                    await self._store.update_game_status(
                        game.id, "final", fresh_game.home_score, fresh_game.away_score
                    )
                    await self._settlement.resolve_bets(game)
                    break

    async def process_live_transitions(self, now: datetime) -> None:
        """Run the live-transition handler for each game whose tipoff just
        passed, exactly once per game per process lifetime.

        Adding to the in-memory ``_handled_live`` set happens ONLY after the
        live handler returns successfully. If ``_handle_game_live`` raises a
        transient error (DB blip, CLV calc edge case), the game's id is left
        out of the set so the next scheduler iteration retries the handler.
        Without that ordering, a one-off failure would silently skip the
        closing-mark/CLV path until process restart — and even then, only if
        no ``is_closing=1`` rows already existed for the game.
        """
        for game in self._games:
            if game.id is None or game.status in ("final", "postponed"):
                continue
            ttip = tiers.time_to_tipoff(game, now)
            if ttip <= 0 and game.id not in self._handled_live:
                try:
                    await self._settlement.handle_game_live(game)
                except Exception:
                    # Isolate per game: a transient failure (DB blip, CLV edge
                    # case) must not skip the remaining live games this pass.
                    # The game is left out of _handled_live so it retries next
                    # cycle.
                    logger.exception(
                        "Live-transition handler failed for game_id=%s; "
                        "will retry next cycle", game.id,
                    )
                    continue
                self._handled_live.add(game.id)
