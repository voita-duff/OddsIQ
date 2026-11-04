"""Budget governor — tracks the Odds API monthly quota and throttles Pinnacle
polling cadence to fit it (spec §3.5).

This module owns budget probe/persist/snapshot, lifted verbatim from the old
Scheduler. It reads the authoritative used/remaining from the PinnacleScraper
(which updates them from real quota response headers) and persists them to the
``meta`` table under ``odds_api_budget_used`` / ``odds_api_budget_remaining`` —
the exact keys the dashboard reads, so that contract is preserved.

Throttle model (discrete levels; hot protected — widened only gently at 2/3):
  Level 0 NONE       projected ≤ total×slack (or unprobed / <1 day into period)
  Level 1 WARM       slack < ratio ≤ 1.25
  Level 2 WARM+HOT   1.25 < ratio ≤ 1.6
  Level 3 CONSERVATIVE  ratio > 1.6 OR remaining ≤ reserve floor
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime

from oddsiq import config

logger = logging.getLogger(__name__)

# Minimum spacing between exhaustion-recovery re-probes (see maybe_reprobe).
# The probe endpoint is quota-free, so hourly is purely a courtesy rate.
_EXHAUSTED_REPROBE_SECONDS = 3600

# Throttle interval table (seconds) — per level, per tier.
# Level 0 references config constants so it stays in sync with cadence changes.
_THROTTLE_INTERVALS: dict[int, dict[str, int]] = {
    0: {
        "far": config.POLL_TIER_FAR,
        "warm": config.POLL_TIER_WARM,
        "hot": config.POLL_TIER_HOT,
    },
    1: {
        "far": config.POLL_TIER_FAR,
        "warm": 600,
        "hot": config.POLL_TIER_HOT,
    },
    # The hot (betting) tier is PROTECTED — budget pressure sacrifices warm
    # (a data tier) first, and only gently widens hot, so the window where
    # real money is placed stays tight. (2026-08-11 rebalance; the live tier
    # was removed entirely 2026-08-27.)
    2: {
        "far": config.POLL_TIER_FAR,
        "warm": 900,
        "hot": 120,
    },
    3: {
        "far": config.POLL_TIER_FAR,
        "warm": 1800,
        "hot": 180,
    },
}


def throttle_intervals(level: int) -> dict[str, int]:
    """Effective per-tier poll intervals at a throttle level (public accessor
    for the dashboard, which must show the cadence actually running rather
    than the base config). Unknown levels fall back to level 0."""
    return dict(_THROTTLE_INTERVALS.get(level, _THROTTLE_INTERVALS[0]))


class BudgetGovernor:
    def __init__(self, pinnacle, store) -> None:
        # pinnacle: the PinnacleScraper instance (or None if not enabled); store: Store.
        self._pinnacle = pinnacle
        self._store = store
        self._last_budget_persist: datetime | None = None
        # Period start seen at the last reprobe check; advances only when a
        # boundary-triggered re-probe succeeds (see maybe_reprobe).
        self._period_start: datetime | None = None
        # Last exhaustion-recovery re-probe attempt (see maybe_reprobe).
        self._last_exhausted_reprobe: datetime | None = None

    async def probe(self) -> None:
        """Initialize Pinnacle budget from real quota headers, or DB fallback.

        The probe hits the free /v4/sports endpoint. If it fails (network blip,
        bad key, missing headers), fall back to the last budget snapshot stored
        in the meta table — better than displaying the misleading configured
        default (config.ODDS_API_MONTHLY_BUDGET, 20000).
        """
        pin = self._pinnacle
        if pin is None:
            return
        result = await pin.probe_budget()
        if result is not None:
            total = pin.budget_total
            logger.info(
                "Odds API budget: %d used / %d remaining (%d total)",
                result["used"],
                result["remaining"],
                total,
            )
            return
        # Probe failed — try to restore last-known values from DB.
        used = await self._store.get_meta("odds_api_budget_used")
        remaining = await self._store.get_meta("odds_api_budget_remaining")
        if used is not None and remaining is not None:
            pin._budget_used = int(used)
            pin._budget_remaining = int(remaining)
            logger.info(
                "Odds API budget (from DB fallback): %d used / %d remaining",
                pin._budget_used,
                pin._budget_remaining,
            )
        else:
            logger.warning("Odds API budget unknown — probe failed and no DB snapshot")

    async def persist(self, now: datetime, *, force: bool = False) -> None:
        """Throttled write of current budget values to the meta table.

        Only persists once per 5 min (meta writes are cheap but frequent
        polls don't need per-cycle disk churn). ``force`` is used on shutdown
        so the persisted fallback snapshot matches the final in-memory budget.
        Skips if the probe never populated real values (still at configured
        default with used=0).
        """
        if not force and self._last_budget_persist is not None:
            if (now - self._last_budget_persist).total_seconds() < 300:
                return
        pin = self._pinnacle
        if pin is None:
            return
        if (
            pin.budget_used == 0
            and pin.budget_remaining == config.ODDS_API_MONTHLY_BUDGET
        ):
            return  # never probed successfully — don't persist the default
        await self._store.set_meta("odds_api_budget_used", str(pin.budget_used))
        await self._store.set_meta(
            "odds_api_budget_remaining", str(pin.budget_remaining)
        )
        await self._persist_daily_history(now, pin.budget_used)
        self._last_budget_persist = now

    async def _persist_daily_history(self, now: datetime, used: int) -> None:
        """Small per-UTC-day history of cumulative `used` (last 10 days) in
        meta ``odds_api_budget_history``. Each entry converges to that day's
        LAST persisted value, so the dashboard can compute a TRAILING burn
        rate — the period average is permanently poisoned by regime changes
        (e.g. the 2026-08-27 live-tier removal cut ~200/day but the average
        kept projecting the old rate). Malformed/absent history starts fresh."""
        import json

        today = now.date().isoformat()
        try:
            history = json.loads(
                await self._store.get_meta("odds_api_budget_history") or "[]"
            )
            assert isinstance(history, list)
        except (ValueError, AssertionError):
            history = []
        if history and history[-1].get("d") == today:
            history[-1]["u"] = used
        else:
            history.append({"d": today, "u": used})
        await self._store.set_meta("odds_api_budget_history", json.dumps(history[-10:]))

    @staticmethod
    def _reset_boundary(year: int, month: int, now: datetime) -> datetime:
        """Reset-day boundary for (year, month), clamped into the month's length
        and stamped with ``now``'s tzinfo."""
        dim = calendar.monthrange(year, month)[1]
        return datetime(
            year, month, min(config.ODDS_API_QUOTA_RESET_DAY, dim), tzinfo=now.tzinfo
        )

    def _current_period_start(self, now: datetime) -> datetime:
        """The most-recent quota reset boundary at or before ``now``.

        Honors a possibly non-calendar reset day
        (``config.ODDS_API_QUOTA_RESET_DAY``) — exact for calendar-1st AND
        billing-date resets, handling month-length variation.
        """
        start = self._reset_boundary(now.year, now.month, now)
        if now < start:  # period began in the previous month
            py, pm = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
            start = self._reset_boundary(py, pm, now)
        return start

    def _period_progress(self, now: datetime) -> tuple[float, float]:
        """Return ``(frac_elapsed, days_elapsed)`` of the CURRENT quota period."""
        start = self._current_period_start(now)
        ny, nm = (
            (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
        )
        nxt = self._reset_boundary(ny, nm, now)
        period_len = (nxt - start).total_seconds()
        elapsed = (now - start).total_seconds()
        frac = elapsed / period_len if period_len > 0 else 0.0
        return frac, elapsed / 86400.0

    async def maybe_reprobe(self, now: datetime) -> None:
        """Re-probe the budget at the period rollover AND while exhausted.

        Pinnacle latches dead once ``budget_remaining`` hits 0
        (``should_skip_request`` blocks every request, so live quota headers
        never refresh it), and the startup probe runs only once — so without
        this a 24/7 process stays Pinnacle-dead across the reset until a manual
        restart. On a detected period boundary we re-probe the free /v4/sports
        endpoint (no quota cost), refreshing ``budget_remaining`` and clearing
        the latch. The tracked period only advances on a SUCCESSFUL probe, so a
        failed boundary probe is retried on the next loop iteration rather than
        skipped for the whole next period.

        The boundary probe alone is not enough (2026-07 incident): if the
        configured reset day precedes the provider's real reset, the boundary
        probe reads the pre-reset zeros, advances the period, and the actual
        refill lands with no boundary left to trigger on — Pinnacle stays dead
        for the whole month. So while the budget reads exhausted we ALSO
        re-probe every ``_EXHAUSTED_REPROBE_SECONDS`` regardless of boundaries,
        which additionally picks up mid-month top-ups the moment they arrive.
        """
        pin = self._pinnacle
        if pin is None:
            return
        start = self._current_period_start(now)
        if self._period_start is None:
            self._period_start = start  # seed on first call (after startup probe)
            return
        if start > self._period_start:
            result = await pin.probe_budget()
            if result is not None:
                self._period_start = start
                logger.info(
                    "Odds API quota period rolled over — re-probed budget: "
                    "%d used / %d remaining",
                    pin.budget_used,
                    pin.budget_remaining,
                )
            else:
                logger.warning(
                    "Odds API quota period rolled over but budget re-probe failed; "
                    "will retry next cycle",
                )
            return
        # Exhaustion recovery: keep re-probing (rate-limited) until tokens
        # reappear. Does NOT touch _period_start — the next real boundary
        # still runs its own re-probe.
        if pin.budget_remaining > 0:
            return
        if (
            self._last_exhausted_reprobe is not None
            and (now - self._last_exhausted_reprobe).total_seconds()
            < _EXHAUSTED_REPROBE_SECONDS
        ):
            return
        self._last_exhausted_reprobe = now
        result = await pin.probe_budget()
        if result is not None and result["remaining"] > 0:
            logger.info(
                "Odds API budget refilled — re-probed: %d used / %d remaining",
                pin.budget_used,
                pin.budget_remaining,
            )

    def throttle_level(self, now: datetime) -> int:
        """Return the current throttle level (0-3) based on budget projection.

        The reserve floor (``config.BUDGET_RESERVE_FLOOR``) is checked first
        against the authoritative ``remaining`` value — it is the primary guard.
        The projection (``used / frac_elapsed``) is a soft early-warning that
        only activates after at least 1 day into the period.
        """
        used, remaining, total = self.snapshot()
        if used is None or remaining is None or total is None:
            return 0
        # Reserve floor always wins — authoritative `remaining`.
        if remaining <= config.BUDGET_RESERVE_FLOOR:
            return 3
        # Unprobed default → no signal.
        if used == 0 and remaining == config.ODDS_API_MONTHLY_BUDGET:
            return 0
        frac, days_elapsed = self._period_progress(now)
        if days_elapsed < 1.0 or frac <= 0:
            return 0  # too early into the period to project reliably
        projected = used / frac
        ratio = projected / total
        if ratio <= config.BUDGET_THROTTLE_SLACK:
            return 0
        if ratio <= 1.25:
            return 1
        if ratio <= 1.6:
            return 2
        return 3

    def interval_for_tier(self, tier: str, now: datetime) -> int:
        """Return the effective poll interval (seconds) for ``tier`` at this moment.

        Consults ``throttle_level`` and looks up the corresponding interval from
        the ``_THROTTLE_INTERVALS`` table.  Falls back to ``POLL_TIER_FAR`` for
        unknown tier names.
        """
        level = self.throttle_level(now)
        return _THROTTLE_INTERVALS[level].get(tier, config.POLL_TIER_FAR)

    def snapshot(self) -> tuple[int | None, int | None, int | None]:
        pin = self._pinnacle
        if pin is None or not hasattr(pin, "budget_remaining"):
            return None, None, None
        return pin.budget_used, pin.budget_remaining, pin.budget_total
