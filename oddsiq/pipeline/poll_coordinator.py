"""Poll coordinator: for one sport, categorize the pollable games by tier, fetch
each scraper concurrently (asyncio.gather; Pinnacle gated by tier-need), match
snapshots to games, persist, and dispatch to EdgeService (pregame). Emits the
per-scraper health log and records coverage. DB writes + edge dispatch stay
ordered after the concurrent fetch. (Post-tipoff games are unpollable — the
live scout was removed 2026-08-27.)

`profile` is threaded through and consumed by both scrapers: Pinnacle keys its
Odds API URL + snapshot sport off it; Tipsport keys its warmup/competition/2-way
matchView + snapshot sport off it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timezone

from oddsiq.db.models import Game, OddsSnapshot
from oddsiq.pipeline import tiers
from oddsiq.pipeline.edge_service import PendingBet

logger = logging.getLogger(__name__)


class PollCoordinator:
    def __init__(self, *, scrapers, store, stats, edge_service, coverage) -> None:
        self._scrapers = scrapers
        self._store = store
        self._stats = stats
        self._edge_service = edge_service
        self._coverage = coverage

    async def poll(
        self, profile, pollable: list[Game], slate: list[Game], now: datetime
    ) -> None:
        """Fetch odds once per scraper, then distribute to matching games.

        Pinnacle calls are gated by tier to balance Odds API budget against
        information collected:
          - far tier (>2h): not called; lines are still too soft to compare —
            except for sports with SportProfile.shadow_far_detection, where
            it is called for shadow rows (see below)
          - warm tier (1-2h): called every cycle (detection runs here too)
          - hot tier (<60m): called every cycle (detection + closing line)

        Edge detection fires for any game in the hot OR warm tier. Earlier
        detection in the warm window gives better placement opportunities
        than waiting for the last 60 minutes; the extra Pinnacle calls fit
        under the monthly budget (the governor throttles cadence if a month
        tracks to overspend). Shadow sports also detect in the far tier: the
        rows are stamped tier='far' and never bet, they just measure whether
        the horizon is worth widening.

        Tipsport (the sole soft book) is polled every cycle regardless of
        tier.
        """
        # NOTE: used ONLY to label the raw-response archive dir — scrapers fetch
        # the full sport/competition slate and match by team pair, so this date
        # does not scope the fetch. (Taken from the first pollable game; a slate
        # spanning two calendar days just labels the archive with one of them.)
        game_date = pollable[0].scheduled_at.date()

        # Categorize each game by current tier. Pollable games are pregame by
        # construction, but a game can cross tipoff between the pollable
        # computation and this poll — tier_for returns None then and the game
        # is skipped below (its lifecycle continues via the schedule clock).
        tier_by_id: dict[int, str | None] = {}
        for g in pollable:
            tier_by_id[g.id] = tiers.tier_for(tiers.time_to_tipoff(g, now))

        # Pinnacle is needed for pregame edge detection (any warm/hot game) —
        # and for the far tier when the sport runs far-tier SHADOW detection
        # (SportProfile.shadow_far_detection; NFL since 2026-09-06).
        active_tiers = {t for t in tier_by_id.values() if t is not None}
        needs_pinnacle = tiers.pinnacle_needed(
            active_tiers, shadow_far=profile.shadow_far_detection
        )
        detect_tiers = (
            {"hot", "warm", tiers.SHADOW_TIER}
            if profile.shadow_far_detection
            else {"hot", "warm"}
        )

        # Fetch odds from each scraper concurrently (one call each; Pinnacle
        # conditionally). DB writes and edge detection remain ordered below.
        scraper_odds: dict[str, list[OddsSnapshot]] = {}
        scraper_health: dict[str, dict] = {}
        scrapers_to_fetch = [
            scraper
            for scraper in self._scrapers
            if scraper.name != "pinnacle" or needs_pinnacle
        ]
        fetch_results = await asyncio.gather(
            *(
                self._fetch_scraper_odds(scraper, game_date, profile)
                for scraper in scrapers_to_fetch
            ),
            return_exceptions=True,
        )
        for result in fetch_results:
            if isinstance(result, BaseException):
                # A fetch task escaped its own try/except guard. Don't let one
                # failed task cancel its siblings or sink the whole poll — log
                # and skip; that scraper just contributes no odds this cycle.
                logger.error("Scraper fetch task failed: %s", result)
                continue
            scraper_name, odds, health = result
            scraper_odds[scraper_name] = odds
            scraper_health[scraper_name] = health

        # Distribute odds to matching games. Far-tier games never receive
        # Pinnacle rows (they haven't earned a CLV baseline yet) UNLESS the
        # sport runs far-tier shadow detection, which needs them. Warm and
        # hot games take whatever Pinnacle returned; the call was already
        # paid for, and the store dedupes unchanged rows.
        commence_time_updated: set[int] = set()
        pending_bets: list[PendingBet] = []

        def _pinnacle_skipped(is_pin: bool, tier: str | None) -> bool:
            # Far-tier games get no Pinnacle rows (no CLV baseline yet) unless
            # the sport runs far-tier shadow detection, which needs them.
            return is_pin and tier == "far" and not profile.shadow_far_detection

        for game in pollable:
            tier = tier_by_id.get(game.id)
            if tier is None:
                # The game crossed tipoff between the pollable computation and
                # this poll (or slipped through uncategorized) — post-tipoff
                # games take no snapshots and no detection.
                logger.info("poll: game %s tipped off mid-cycle; skipping", game.id)
                continue
            game_snapshots: dict[str, list[OddsSnapshot]] = {}
            for scraper_name, all_odds in scraper_odds.items():
                if _pinnacle_skipped(scraper_name == "pinnacle", tier):
                    continue
                matched = [s for s in all_odds if self._matches_game(s, game)]
                if matched:
                    health = scraper_health.get(scraper_name)
                    if health is not None:
                        health["matched_snapshot_count"] += len(matched)
                        health["matched_game_ids"].add(game.id)
                        if scraper_name == "pinnacle":
                            health["sharp_source_ages_ms"].extend(
                                max(
                                    0,
                                    int(
                                        (
                                            snap.captured_at - snap.source_updated_at
                                        ).total_seconds()
                                        * 1000
                                    ),
                                )
                                for snap in matched
                                if snap.source_updated_at is not None
                            )
                    self._coverage.record_match(
                        game.id, scraper_name, datetime.now(timezone.utc)
                    )
                    for snap in matched:
                        snap.game_id = game.id
                    if (
                        scraper_name == "pinnacle"
                        and game.id not in commence_time_updated
                        and any(s._commence_time is not None for s in matched)
                    ):
                        await self._reconcile_commence_time(game, matched)
                        commence_time_updated.add(game.id)
                    await self._store.insert_odds_snapshots_batch(matched)
                    game_snapshots[scraper_name] = matched

            # Detection tiers: hot + warm, plus far for shadow sports (see the
            # docstring — far rows are recorded as shadow data, never bet).
            # No doubleheader/shared-pair guard runs here any more: fail-closed
            # start-time matching subsumes it. `_matches_game` drops a Tipsport
            # snapshot that carries no start of its own, so a shared team pair
            # can only ever be attributed BY start time and one leg can never
            # be handed the other leg's price. (The guard's sole remaining
            # trigger was a MATCHED start-less soft-book snapshot, which
            # fail-closed matching makes unreachable — and over-skipping cost a
            # real +3.2% hot-tier edge on the 2026-09-04 Guardians–Tigers
            # doubleheader.)
            if tier in detect_tiers:
                try:
                    pending_bets.extend(
                        await self._edge_service.detect_pregame_edges(
                            game, game_snapshots, tier
                        )
                    )
                except Exception:
                    # One game's detection failure (e.g. a store error) must not
                    # abort the sport's cycle and strand the bets already
                    # collected — those rows exist, so they can never be
                    # re-pended for this tier on the next cycle.
                    logger.exception(
                        "detect_failed: game_id=%s sport=%s", game.id, profile.key
                    )

        # Betting handoff for the whole cycle, earliest kickoff first (largest
        # edge as the tiebreak) — each bet is re-qualified on the engine's clock
        # at dispatch (Sunday-burst rule).
        await self._edge_service.dispatch_pending(pending_bets)

        pollable_ids = {g.id for g in pollable if g.id is not None}
        far_pollable = [g for g in pollable if tier_by_id.get(g.id) == "far"]
        other_games = [
            g
            for g in slate
            if g.id is not None
            and g.id not in pollable_ids
            and g.status not in ("final", "postponed", "live")
        ]
        for scraper_name, all_odds in scraper_odds.items():
            health = scraper_health.get(scraper_name)
            if health is None:
                continue
            is_pinnacle = scraper_name == "pinnacle"
            other_snapshot_count = 0
            for snap in all_odds:
                # "Genuinely matched + stored above" mirrors the distribution
                # loop's per-scraper gating (`_pinnacle_skipped`): Pinnacle
                # deliberately skips far-tier games (unless the sport runs
                # far-tier shadow detection, which stores them), Tipsport
                # stores them.
                stored = any(
                    self._matches_game(snap, g)
                    and not _pinnacle_skipped(is_pinnacle, tier_by_id.get(g.id))
                    for g in pollable
                )
                if stored:
                    continue
                matched_other = False
                # A far-tier pollable game whose Pinnacle snapshot we intentionally
                # skipped is an on-slate game, not an unmatched snapshot — count it
                # as "other" so it doesn't inflate the unmatched_snapshots metric.
                for game in (*other_games, *far_pollable):
                    if self._matches_game(snap, game):
                        health["other_game_ids"].add(game.id)
                        matched_other = True
                if matched_other:
                    other_snapshot_count += 1
            health["other_snapshot_count"] = other_snapshot_count
            if is_pinnacle:
                slate_external_ids = {g.external_id for g in slate}
                health["event_id_mismatch_snapshot_count"] = sum(
                    1
                    for snap in all_odds
                    if snap._source_event_id is not None
                    and snap._source_event_id not in slate_external_ids
                )

        for scraper_name, health in scraper_health.items():
            matched_ids = health["matched_game_ids"]
            snapshot_count = health["snapshot_count"]
            matched_snapshot_count = health["matched_snapshot_count"]
            other_snapshot_count = health["other_snapshot_count"]
            unmatched_snapshot_count = max(
                0,
                snapshot_count - matched_snapshot_count - other_snapshot_count,
            )
            logger.info(
                "Scraper health: name=%s duration_ms=%d snapshots=%d "
                "matched_snapshots=%d matched_games=%d other_games=%d "
                "unmatched_snapshots=%d errors=%d unsafe_start_snapshots=%d "
                "sharp_source_age_max_ms=%d missing_source_updates=%d "
                "missing_event_ids=%d event_id_mismatches=%d",
                scraper_name,
                health["duration_ms"],
                snapshot_count,
                matched_snapshot_count,
                len(matched_ids),
                len(health["other_game_ids"]),
                unmatched_snapshot_count,
                health["error_count"],
                health["unsafe_start_snapshot_count"],
                max(health["sharp_source_ages_ms"], default=0),
                health["missing_source_update_count"],
                health["missing_event_id_count"],
                health["event_id_mismatch_snapshot_count"],
            )

    async def _fetch_scraper_odds(
        self,
        scraper,
        game_date: date,
        profile,
    ) -> tuple[str, list[OddsSnapshot], dict]:
        started = time.perf_counter()
        error_count = 0
        try:
            odds = await scraper.fetch_odds(game_date, profile)
            if not odds:
                status_code = getattr(scraper, "last_fetch_status_code", None)
                if status_code is None:
                    logger.warning("Scraper %s returned no odds", scraper.name)
                else:
                    logger.warning(
                        "Scraper %s returned no odds (status=%s)",
                        scraper.name,
                        status_code,
                    )
        except Exception as e:
            error_count = 1
            logger.error("Scraper %s failed: %s", scraper.name, e)
            odds = []
        # Scrapers swallow internal exceptions and return [] on failure; the
        # scheduler's try/except only catches escapes from that contract. Read
        # the scraper's per-fetch counter so silently recovered failures still
        # show up in the health log.
        internal_errors = getattr(scraper, "last_fetch_error_count", 0)
        unsafe_start_snapshot_count = sum(
            1
            for snap in odds
            if scraper.name == "tipsport" and snap._commence_time is None
        )
        if unsafe_start_snapshot_count:
            logger.warning(
                "Scraper %s produced %d snapshots without listing start; "
                "discarding them from game attribution",
                scraper.name,
                unsafe_start_snapshot_count,
            )
        health = {
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "snapshot_count": len(odds),
            "matched_snapshot_count": 0,
            "matched_game_ids": set(),
            "other_snapshot_count": 0,
            "other_game_ids": set(),
            "error_count": error_count + internal_errors,
            "unsafe_start_snapshot_count": unsafe_start_snapshot_count,
            "sharp_source_ages_ms": [],
            "missing_source_update_count": getattr(
                scraper, "last_parse_missing_source_updates", 0
            ),
            "missing_event_id_count": getattr(
                scraper, "last_parse_missing_event_ids", 0
            ),
            "event_id_mismatch_snapshot_count": 0,
        }
        self._stats.record_scraper_poll(
            scraper.name,
            duration_ms=health["duration_ms"],
            zero_odds=len(odds) == 0,
            error_count=health["error_count"],
        )
        return scraper.name, odds, health

    # Doubleheader/series disambiguation tolerance. scheduled_at (Odds API
    # /scores) and commence_time (Odds API /odds) come from the same provider so
    # normally agree within minutes, but can diverge under a postponement/
    # reschedule; the window must be generous enough to never split one game, yet
    # tight enough to separate two legs of the same pairing. MLB doubleheader
    # legs start ≥3h apart (a game runs ~3h), so 2h cleanly separates them.
    _COMMENCE_MATCH_WINDOW_SECONDS = 7200

    @staticmethod
    def _matches_game(snap: OddsSnapshot, game: Game) -> bool:
        # Pinnacle schedule and odds come from the same Odds API event. Prefer
        # that exact immutable identity over fuzzy team/time attribution. The
        # fallback is retained only for legacy and hand-built test snapshots;
        # the production parser drops events without a usable id.
        if (
            snap.bookmaker == "pinnacle"
            and snap._source_event_id is not None
            and snap._source_event_id != game.external_id
        ):
            return False
        if not (snap._home_team and snap._away_team):
            return False
        if snap._home_team != game.home_team or snap._away_team != game.away_team:
            return False
        # When two listings share a team pair the pair alone is ambiguous —
        # and for Tipsport that is the NORM, not the edge case: it posts the
        # next game of an MLB series under /kurzy/ while today's is still
        # pregame, so a names-only match pinned tomorrow's untuned opener
        # prices onto today's game (2026-08-30 wrong-game forensics; every
        # clean MLB detection since at least Aug 22 was such a ghost). Both
        # scrapers now carry the event's own start time (Pinnacle from the
        # API event, Tipsport from the listing's dateClosed), so the ±2h
        # window applies to both. A Tipsport snapshot with a missing/malformed
        # start fails closed: pair-only matching can silently attribute a
        # next-day series listing to today's game.
        game_tipoff = game.commence_time or game.scheduled_at
        if snap.bookmaker == "tipsport" and snap._commence_time is None:
            return False
        if snap._commence_time is not None and game_tipoff is not None:
            return (
                abs((snap._commence_time - game_tipoff).total_seconds())
                <= PollCoordinator._COMMENCE_MATCH_WINDOW_SECONDS
            )
        return True

    async def _reconcile_commence_time(
        self, game: Game, snapshots: list[OddsSnapshot]
    ) -> None:
        times = sorted(
            {s._commence_time for s in snapshots if s._commence_time is not None}
        )
        if not times:
            return
        commence_time = times[-1]
        if len(times) > 1:
            logger.warning(
                "Multiple Pinnacle commence_time values for game_id=%d; using latest %s",
                game.id,
                commence_time.isoformat(),
            )
        if game.commence_time == commence_time:
            return
        game.commence_time = commence_time
        await self._store.update_game_commence_time(game.id, commence_time)
