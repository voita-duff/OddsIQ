from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from oddsiq import backup, config
from oddsiq.db.models import Game
from oddsiq.db.store import Store
from oddsiq.notifications import telegram
from oddsiq.scrapers.base import BaseScraper
from oddsiq.scrapers.pinnacle import PinnacleScraper
from oddsiq.scrapers.preflight import chromium_available
from oddsiq.scrapers.registry import build_czech_scrapers
from oddsiq.scrapers.odds_api_schedule import OddsApiScheduleSource
from oddsiq.pipeline.stats import SessionStats
from oddsiq.pipeline.betting_bridge import BettingBridge
from oddsiq.pipeline.betting_service import BettingService
from oddsiq.pipeline.budget_governor import BudgetGovernor
from oddsiq.pipeline.coverage_monitor import CoverageMonitor
from oddsiq.pipeline.edge_service import EdgeService
from oddsiq.pipeline.settlement_service import SettlementService
from oddsiq.pipeline.poll_coordinator import PollCoordinator
from oddsiq.pipeline.schedule_service import ScheduleService
from oddsiq.pipeline import tiers
from oddsiq.sports.profiles import enabled_profiles

logger = logging.getLogger(__name__)


async def _send_heartbeat_ping(url: str) -> None:
    """GET the dead-man ping URL. Isolated for testability; callers own
    error handling (the ping must never disturb the poll loop)."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.get(url)


class Scheduler:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._scrapers: list[BaseScraper] = [
            PinnacleScraper(),
            *build_czech_scrapers(),
        ]
        self._running = False
        self._stop_event = asyncio.Event()
        self._stats = SessionStats()
        # Per-sport last-poll timestamps. Gating is per-sport so a fast cadence
        # in one sport (a hot game) can't drag warm-only slates in the others
        # into over-polling Pinnacle.
        self._last_poll_time: dict[str, datetime] = {}
        self._last_heartbeat: datetime | None = None
        # Budget-degradation alert dedup (each fires once per trip, resets on recovery).
        self._budget_alert_exhausted_sent = False
        self._budget_alert_throttle3_sent = False
        self._last_heartbeat_ping: datetime | None = None
        self._last_heartbeat_ping_warning: datetime | None = None
        self._last_periodic_backup: datetime | None = None
        self._budget_governor = BudgetGovernor(
            pinnacle=next((s for s in self._scrapers if s.name == "pinnacle"), None),
            store=store,
        )
        # Auto-betting engine (P1): constructed only when BETTING_ENABLED.
        # Fail-fast on a missing bridge token — a silently unauthenticated
        # betting bridge is not a state this system may ever run in.
        self._betting_bridge = None
        self._betting = None
        self._betting_state = "off"
        if config.BETTING_ENABLED:
            if not config.BETTING_BRIDGE_TOKEN:
                raise ValueError(
                    "BETTING_ENABLED=1 requires BETTING_BRIDGE_TOKEN to be set"
                )
            self._betting_bridge = BettingBridge(
                host=config.BETTING_BRIDGE_HOST,
                port=config.BETTING_BRIDGE_PORT,
                token=config.BETTING_BRIDGE_TOKEN,
                placement_timeout=config.BETTING_PLACEMENT_TIMEOUT_SECONDS,
                on_login_change=self._on_login_change,
            )
            self._betting = BettingService(store=store, executor=self._betting_bridge)
        self._edge_service = EdgeService(
            store=store, stats=self._stats, betting=self._betting
        )
        self._settlement = SettlementService(store=store, stats=self._stats)
        # Coverage-gap tracking:
        # - _coverage owns the match timestamps, alert-dedup, and gap checks.
        #   The governor lets it scale the staleness threshold with the throttled
        #   cadence (no false alerts when polling is intentionally slowed).
        self._coverage = CoverageMonitor(governor=self._budget_governor)
        self._poll_coordinator = PollCoordinator(
            scrapers=self._scrapers,
            store=store,
            stats=self._stats,
            edge_service=self._edge_service,
            coverage=self._coverage,
        )
        self._schedule = ScheduleService(
            schedule_source=OddsApiScheduleSource(),
            store=store,
            settlement=self._settlement,
        )

    async def _on_login_change(self, event: str) -> None:
        """Bettor-facing login alerts (2026-08-30): an armed engine with a dead
        Tipsport session is a silent outage — the Aug 28 expiry sat unnoticed
        for 32h and skipped the week's only clean bet. Fired by the bridge on
        confirmed status transitions; deliberately not gated on ``betting_armed``
        (the operator wants to know BEFORE arming, and the bridge only exists
        when BETTING_ENABLED)."""
        if event == "lost":
            await telegram.send_telegram(telegram.format_login_lost())
        elif event == "restored":
            await telegram.send_telegram(telegram.format_login_restored())

    async def run(self) -> None:
        self._running = True
        logger.info("Scheduler starting...")
        await self._recover_at_startup()
        await self._check_schedule_health()
        await self._check_browser_preflight()
        # probe() and refresh() do DB writes (budget, upsert_game, CLV recovery);
        # they run OUTSIDE the main loop's try/except, so a transient SQLITE_BUSY
        # / disk blip here would crash before polling starts — under Docker
        # `restart: unless-stopped` that's a crash-loop. Isolate them; both
        # self-heal in the loop (maybe_refresh / maybe_reprobe).
        try:
            await self._budget_governor.probe()
        except Exception:
            logger.exception(
                "Startup budget probe failed; continuing with last-known budget"
            )
        try:
            await self._schedule.refresh()
        except Exception:
            logger.exception("Startup schedule refresh failed; will retry in main loop")
        if self._betting_bridge is not None:
            try:
                await self._betting_bridge.start()
            except Exception:
                logger.exception(
                    "Betting bridge failed to start; betting stays inactive "
                    "(state 'disconnected') — polling is unaffected"
                )
        # After refresh has settled any in-window finals, flag genuinely stranded
        # games (older than the scores window, still non-final).
        await self._warn_stranded_games()
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                # Clear the budget latch if the monthly quota period rolled over
                # (free re-probe; no-op within a period). Must run before the
                # budget-dependent alert/throttle reads below.
                await self._budget_governor.maybe_reprobe(now)
                self._betting_state = await self._betting_heartbeat_state()
                self._maybe_emit_heartbeat(now)
                await self._maybe_ping_heartbeat_url(now)
                await self._maybe_periodic_backup(now)
                await self._maybe_alert_budget(now)
                await self._schedule.maybe_refresh(now)

                # Handle games that went live (once per game)
                await self._schedule.process_live_transitions(now)

                # Determine which games need polling and each sport's tier interval
                pollable, sport_min_interval = self._compute_pollable(now)

                if pollable and self._running:
                    now_poll = datetime.now(timezone.utc)
                    if await self._poll_due_sports(
                        now, now_poll, pollable, sport_min_interval
                    ):
                        await self._budget_governor.persist(now)
                    # A long sequential multi-sport poll can outlast the heartbeat
                    # cadence; re-emit so the dashboard doesn't read "stale".
                    self._maybe_emit_heartbeat(datetime.now(timezone.utc))

                if self._running:
                    await self._schedule.check_completed_games()
                    await self._coverage.check_gaps(
                        self._schedule.games, self._scrapers, datetime.now(timezone.utc)
                    )
                    # Sleep 30s but wake immediately on stop()
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in scheduler main loop")
                if self._running:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        pass
        await self._shutdown()

    async def _recover_at_startup(self) -> None:
        """Run startup state recovery, isolating each step. A transient DB
        error here must not crash the process before the main loop's
        try/except — under Docker `restart: unless-stopped` that would become a
        crash-loop. Recovery naturally retries on the next refresh."""
        try:
            await self._schedule.load_handled_live()
        except Exception:
            logger.exception("Startup load_handled_live failed; continuing without it")
        try:
            await self._settlement.recover_historical_bets()
        except Exception:
            logger.exception("Startup bet recovery failed; will retry on refresh")

    # Games older than the Odds API /scores window (daysFrom=3 = 72h) can no
    # longer be auto-settled; flag only those, and only AFTER refresh() has had
    # its chance to settle in-window finals (otherwise a normal overnight stop
    # would falsely warn for games the refresh settles seconds later).
    _STRANDED_MIN_AGE_HOURS = 96.0

    async def _warn_stranded_games(self) -> None:
        try:
            stranded = await self._store.get_stranded_games(
                datetime.now(timezone.utc), min_age_hours=self._STRANDED_MIN_AGE_HOURS
            )
            if stranded:
                logger.warning(
                    "%d game(s) stuck non-final >%.0fh past tipoff (cannot auto-settle "
                    "— past the Odds API scores window): %s",
                    len(stranded),
                    self._STRANDED_MIN_AGE_HOURS,
                    ", ".join(f"{g.external_id}({g.status})" for g in stranded[:20]),
                )
        except Exception:
            logger.exception("Startup stranded-game check failed; continuing")

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def _check_schedule_health(self) -> None:
        """Startup Odds API probe. A failure (typically a bad/expired
        ODDS_API_KEY) means zero Pinnacle snapshots and zero CLV data while the
        pipeline otherwise looks alive. Logged only — the phone is reserved for
        real-money placed/won/lost (2026-08-10 rebrand); liveness lives on the
        dashboard + the dead-man ping."""
        healthy = await self._schedule.health_check()
        if not healthy:
            logger.warning(
                "Odds API schedule health check failed — schedule may be unavailable"
            )

    async def _check_browser_preflight(self) -> None:
        """One-shot startup probe: confirm Playwright Chromium can launch when
        the Tipsport browser-based scraper is enabled.

        A missing or version-drifted browser binary otherwise fails silently
        per-poll for hours (the 2026-05-30 outage). The REST books (Pinnacle)
        are unaffected, so this warns loudly rather than aborting the run.
        """
        if not any(s.name == "tipsport" for s in self._scrapers):
            return
        ok, err = await chromium_available()
        if ok:
            logger.info("Browser preflight OK — Playwright Chromium launches")
        else:
            logger.warning(
                "Browser preflight FAILED — Tipsport cannot run this session. "
                "Fix: `playwright install chromium`. (%s)",
                err,
            )

    def _compute_pollable(self, now: datetime) -> tuple[list[Game], dict[str, int]]:
        """Games due for polling + the shortest effective interval PER SPORT.

        The per-game interval is the governor-throttled cadence for that tier;
        the per-sport minimum lets each sport gate independently, so a hot game
        in one sport doesn't drag warm-only slates in the others into fast
        Pinnacle polling. Post-tipoff games are never pollable (the live tier
        was removed 2026-08-27).
        """
        pollable: list[Game] = []
        sport_min_interval: dict[str, int] = {}
        for g in self._schedule.games:
            if g.id is None or g.status in ("final", "postponed"):
                continue
            ttip = tiers.time_to_tipoff(g, now)
            if tiers.get_poll_interval(ttip) is None:
                continue
            tier = tiers.tier_for(ttip)
            interval = self._budget_governor.interval_for_tier(tier, now)
            pollable.append(g)
            current = sport_min_interval.get(g.sport)
            if current is None or interval < current:
                sport_min_interval[g.sport] = interval
        return pollable, sport_min_interval

    async def _poll_due_sports(
        self,
        now: datetime,
        now_poll: datetime,
        pollable: list[Game],
        sport_min_interval: dict[str, int],
    ) -> bool:
        """Poll each enabled sport whose own throttled interval has elapsed since
        its own last poll. Returns True if any sport was polled (so the caller
        can persist the budget). Gating is per-sport to avoid one sport's fast
        cadence over-polling another's."""
        polled_any = False
        for profile in enabled_profiles():
            sport_pollable = [g for g in pollable if g.sport == profile.key]
            if not sport_pollable:
                continue
            last = self._last_poll_time.get(profile.key)
            mi = sport_min_interval.get(profile.key)
            if (
                last is not None
                and mi is not None
                and (now - last).total_seconds() < mi
            ):
                continue
            sport_slate = [g for g in self._schedule.games if g.sport == profile.key]
            # Per-sport isolation: the fetch fan-out inside poll() is already
            # exception-guarded, but a deterministic error in the ordered
            # post-fetch phase (matching/persist/detect) would otherwise skip
            # every sport after this one in profile order — every cycle.
            try:
                await self._poll_coordinator.poll(
                    profile, sport_pollable, sport_slate, now_poll
                )
            except Exception:
                logger.exception(
                    "Poll failed for sport=%s; continuing with remaining sports",
                    profile.key,
                )
            # Advance the cadence clock even on failure so a persistently
            # failing sport is retried at its tier interval, not hot-looped.
            self._last_poll_time[profile.key] = now
            polled_any = True
        return polled_any

    def _latest_poll_time(self) -> datetime | None:
        """Most-recent poll across all sports (for liveness/heartbeat fields)."""
        return max(self._last_poll_time.values(), default=None)

    def _derive_pipeline_state(self, now: datetime) -> str:
        """Return one of: ``polling``, ``between-game-windows``, ``idle-no-games``.

        ``polling`` — a poll completed within ``SCHEDULER_HEARTBEAT_SECONDS``
        of ``now`` (``_last_poll_time`` is recent), OR at least one tracked
        game is currently in a polling tier.

        ``between-game-windows`` — games exist on the schedule but none are
        currently in a polling tier (e.g. all tipoffs > 6h away).

        ``idle-no-games`` — no scheduled/live games at all.

        The dashboard combines this with log-file freshness to render its
        own four-state display (``alive`` / ``idle-no-games`` / ``stale``
        / ``unknown``) — see the P3 design spec.
        """
        latest_poll = self._latest_poll_time()
        if latest_poll is not None:
            if (now - latest_poll).total_seconds() < config.SCHEDULER_HEARTBEAT_SECONDS:
                return "polling"
        active = [
            g
            for g in self._schedule.games
            if g.status == "scheduled"
            and tiers.get_poll_interval(tiers.time_to_tipoff(g, now)) is not None
        ]
        if active:
            return "polling"
        # In-progress (status live) games are deliberately NOT "polling": the
        # live tier was removed 2026-08-27, so an all-tipped-off slate honestly
        # sits between game windows while settlement runs off the schedule clock.
        waiting = [g for g in self._schedule.games if g.status in ("scheduled", "live")]
        if waiting:
            return "between-game-windows"
        return "idle-no-games"

    def _maybe_emit_heartbeat(self, now: datetime) -> None:
        """Emit a ``heartbeat:`` log line if cadence elapsed since the last one.

        Called from ``Scheduler.run`` each main-loop iteration. The dashboard
        reads the most recent line matching this pattern to derive pipeline
        liveness; without periodic emission, an idle scheduler (off-season,
        quiet hours) would falsely appear ``stale`` after 2 minutes.
        """
        cadence = config.SCHEDULER_HEARTBEAT_SECONDS
        if (
            self._last_heartbeat is None
            or (now - self._last_heartbeat).total_seconds() >= cadence
        ):
            state = self._derive_pipeline_state(now)
            latest_poll = self._latest_poll_time()
            seconds_since_last_poll = (
                int((now - latest_poll).total_seconds())
                if latest_poll is not None
                else -1
            )
            level = self._budget_governor.throttle_level(now)
            logger.info(
                "heartbeat: state=%s games_tracked=%d seconds_since_last_poll=%d "
                "budget_throttle=%d betting=%s",
                state,
                len(self._schedule.games),
                seconds_since_last_poll,
                level,
                self._betting_state,
            )
            self._last_heartbeat = now

    async def _betting_heartbeat_state(self) -> str:
        """One word for the heartbeat line + dashboard: off (not enabled) |
        disconnected (enabled, no extension) | disarmed | armed."""
        if self._betting is None or self._betting_bridge is None:
            return "off"
        if not self._betting_bridge.connected:
            return "disconnected"
        return "armed" if await self._betting.armed() else "disarmed"

    async def _maybe_ping_heartbeat_url(self, now: datetime) -> None:
        """Dead-man ping: GET ``HEARTBEAT_PING_URL`` on the heartbeat cadence.

        The monitoring service (healthchecks.io-style) alerts the operator
        when pings STOP — true dead-process detection, complementing the
        log-line heartbeat the dashboard reads. Best-effort: failures are
        swallowed (WARNING at most once per 10 min) so a monitoring outage
        can never disturb the poll loop.
        """
        url = config.HEARTBEAT_PING_URL
        if not url:
            return
        cadence = config.SCHEDULER_HEARTBEAT_SECONDS
        if (
            self._last_heartbeat_ping is not None
            and (now - self._last_heartbeat_ping).total_seconds() < cadence
        ):
            return
        self._last_heartbeat_ping = now
        try:
            await _send_heartbeat_ping(url)
        except Exception as e:
            if (
                self._last_heartbeat_ping_warning is None
                or (now - self._last_heartbeat_ping_warning).total_seconds() >= 600
            ):
                self._last_heartbeat_ping_warning = now
                logger.warning(
                    "Heartbeat ping failed (suppressing repeats 10 min): %s", e
                )

    async def _maybe_periodic_backup(self, now: datetime) -> None:
        """Periodic in-process DB backup every ``BACKUP_INTERVAL_HOURS``.

        Startup/shutdown backups alone left a 17-day hole when the container
        ran Jul 22→Aug 8 without a restart. The first call only arms the
        clock (the CLI's startup backup just ran); the same WAL-safe,
        integrity-checked, retention-pruned routine is reused.

        Best-effort, like its ``_maybe_ping_heartbeat_url`` sibling: an
        exception escaping here reaches the main loop's blanket handler, which
        sleeps 10 s and skips the REST of that iteration — budget alert,
        schedule refresh, polling, finals. On an armed NFL Sunday a lost cycle
        can cost a bet. Arming stays BEFORE the call, so a persistent failure
        (full disk) stays once-per-interval instead of retrying every pass.
        """
        hours = config.BACKUP_INTERVAL_HOURS
        if not config.BACKUP_ENABLED or hours <= 0:
            return
        if self._last_periodic_backup is None:
            self._last_periodic_backup = now
            return
        if (now - self._last_periodic_backup).total_seconds() < hours * 3600:
            return
        self._last_periodic_backup = now
        try:
            path = await asyncio.to_thread(
                backup.backup_database,
                config.DATABASE_PATH,
                config.BACKUP_DIR,
                "periodic",
                keep_daily=config.BACKUP_KEEP_DAILY,
                keep_monthly=config.BACKUP_KEEP_MONTHLY,
            )
            if path is not None:
                logger.info("periodic_backup: path=%s", path)
            else:
                logger.warning("periodic_backup failed; see backup warnings above")
        except Exception:
            logger.exception("periodic_backup raised; polling continues")

    async def _maybe_alert_budget(self, now: datetime) -> None:
        """Log once when the Odds API budget is exhausted (Pinnacle stops → zero
        value bets / live edges) or throttling hits max (level 3). Logged only —
        the phone is reserved for real-money placed/won/lost (2026-08-10 rebrand);
        the dashboard + dead-man ping cover operational health."""
        _used, remaining, _total = self._budget_governor.snapshot()
        if remaining is None:
            return
        if remaining <= 0:
            if not self._budget_alert_exhausted_sent:
                self._budget_alert_exhausted_sent = True
                logger.warning(
                    "Odds API budget EXHAUSTED (0 remaining) — Pinnacle polling "
                    "stopped; no value bets or live edges until the quota resets."
                )
        else:
            self._budget_alert_exhausted_sent = False
        level = self._budget_governor.throttle_level(now)
        if level >= 3:
            if not self._budget_alert_throttle3_sent:
                self._budget_alert_throttle3_sent = True
                logger.warning(
                    "Odds API budget throttle at max (level 3): %s remaining. "
                    "Pinnacle cadence is now conservative.",
                    remaining,
                )
        else:
            self._budget_alert_throttle3_sent = False

    def print_summary(self) -> None:
        used, remaining, total = self._budget_governor.snapshot()
        print("\n=== Session Summary ===")
        print(f"Games tracked: {len(self._schedule.games)}")

        active_books = sorted(
            book for book, count in self._stats.value_bets_per_book.items() if count > 0
        )
        if len(active_books) > 1:
            print(f"Value bets flagged: {self._stats.value_bets_flagged} total")
            for book in active_books:
                book_count = self._stats.value_bets_per_book.get(book, 0)
                book_avg_edge = (
                    self._stats.total_edge_per_book.get(book, 0.0) / book_count * 100
                    if book_count
                    else 0.0
                )
                book_clv_count = self._stats.clv_count_per_book.get(book, 0)
                book_avg_clv = (
                    self._stats.total_clv_per_book.get(book, 0.0) / book_clv_count * 100
                    if book_clv_count
                    else 0.0
                )
                clv_part = (
                    f", CLV {book_avg_clv:+.1f}% ({book_clv_count})"
                    if book_clv_count
                    else ", no CLV"
                )
                print(f"  {book}: {book_count} (edge {book_avg_edge:+.1f}%{clv_part})")
        else:
            avg_edge = (
                self._stats.total_edge / self._stats.value_bets_flagged * 100
                if self._stats.value_bets_flagged
                else 0
            )
            avg_clv = (
                self._stats.total_clv / self._stats.clv_count * 100
                if self._stats.clv_count
                else 0
            )
            print(f"Value bets flagged: {self._stats.value_bets_flagged}")
            print(f"Average edge: {avg_edge:.1f}%")
            print(f"CLV: {avg_clv:+.1f}% ({self._stats.clv_count} bets)")

        if used is not None and remaining is not None:
            print(
                f"Odds API budget: {used} used / {remaining} remaining ({total} total)"
            )
        else:
            print("Odds API budget: unknown")

        if self._stats.scraper_polls:
            print("Scrapers:")
            for scraper_name in sorted(self._stats.scraper_polls):
                polls = self._stats.scraper_polls[scraper_name]
                total_ms = self._stats.scraper_duration_ms_total.get(scraper_name, 0)
                avg_ms = round(total_ms / polls) if polls else 0
                max_ms = self._stats.scraper_duration_ms_max.get(scraper_name, 0)
                zero_odds = self._stats.scraper_zero_odds.get(scraper_name, 0)
                errors = self._stats.scraper_errors.get(scraper_name, 0)
                last_coverage = self._coverage.last_coverage(scraper_name)
                print(
                    f"  {scraper_name}: polls={polls} zero_odds={zero_odds} "
                    f"errors={errors} avg_ms={avg_ms} max_ms={max_ms} "
                    f"last_coverage={last_coverage}"
                )

    async def _shutdown(self) -> None:
        """Best-effort teardown. Each step is isolated so an early failure (e.g.
        a Telegram timeout) can't skip the later scraper/browser cleanup and
        leak a Chromium process past Docker's stop grace period."""
        logger.info("Shutting down...")
        if self._betting_bridge is not None:
            try:
                await self._betting_bridge.stop()
            except Exception:
                logger.exception("Shutdown: betting bridge stop failed")
        try:
            await self._budget_governor.persist(datetime.now(timezone.utc), force=True)
        except Exception:
            logger.exception("Shutdown: budget persist failed")
        try:
            # Console/log summary only — no phone message (2026-08-10 rebrand).
            self.print_summary()
        except Exception:
            logger.exception("Shutdown: print_summary failed")
        try:
            await telegram.close()
        except Exception:
            logger.exception("Shutdown: telegram close failed")
        try:
            await self._schedule.close()
        except Exception:
            logger.exception("Shutdown: schedule close failed")
        for scraper in self._scrapers:
            try:
                await scraper.close()
            except Exception:
                logger.exception(
                    "Shutdown: %s close failed", getattr(scraper, "name", scraper)
                )
