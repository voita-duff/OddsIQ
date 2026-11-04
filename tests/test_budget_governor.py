import asyncio
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from oddsiq import config
from oddsiq.db import migrations
from oddsiq.db.store import Store
from oddsiq.pipeline.budget_governor import BudgetGovernor


@pytest.fixture(autouse=True)
def _hermetic_budget_config(monkeypatch):
    # These tests hand-compute period fractions and throttle ratios from the
    # defaults, but config reads .env at import — a local override (e.g.
    # ODDS_API_QUOTA_RESET_DAY=2 after the 2026-07 reset-day-drift incident)
    # shifts the period start and silently changes every projection. Pin the
    # documented defaults so local .env never changes governor behavior here
    # (same pattern as test_scheduler.py's _hermetic_bookmaker_flags).
    monkeypatch.setattr(config, "ODDS_API_QUOTA_RESET_DAY", 1)
    monkeypatch.setattr(config, "BUDGET_RESERVE_FLOOR", 1500)
    monkeypatch.setattr(config, "BUDGET_THROTTLE_SLACK", 1.10)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = tmp_path / "t.db"
    await asyncio.to_thread(migrations.run, db)
    s = Store(db)
    await s.initialize()
    yield s
    await s.close()


def _fake_pinnacle(used=1000, remaining=19000):
    # Mimics the PinnacleScraper budget surface the governor reads:
    # the budget_used / budget_remaining / budget_total properties + name.
    return SimpleNamespace(
        name="pinnacle",
        budget_used=used,
        budget_remaining=remaining,
        budget_total=used + remaining,
    )


@pytest.mark.asyncio
async def test_persist_writes_meta_keys(store):
    pin = _fake_pinnacle(used=1234, remaining=18766)
    gov = BudgetGovernor(pinnacle=pin, store=store)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    await gov.persist(now, force=True)
    assert await store.get_meta("odds_api_budget_used") == "1234"
    assert await store.get_meta("odds_api_budget_remaining") == "18766"


@pytest.mark.asyncio
async def test_persist_maintains_daily_used_history(store):
    """persist() keeps a small per-UTC-day history of cumulative `used` in
    meta (`odds_api_budget_history`) so the dashboard can project from a
    TRAILING burn rate instead of the regime-change-poisoned period average.
    Same-day persists update the day's entry; a new day appends; the list is
    pruned to the last 10 entries."""
    import json
    pin = _fake_pinnacle(used=100, remaining=19900)
    gov = BudgetGovernor(pinnacle=pin, store=store)
    day1 = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    await gov.persist(day1, force=True)
    pin.budget_used = 250
    await gov.persist(day1.replace(hour=23), force=True)
    hist = json.loads(await store.get_meta("odds_api_budget_history"))
    assert hist == [{"d": "2026-06-03", "u": 250}]   # same day updated, not appended

    pin.budget_used = 400
    await gov.persist(day1 + timedelta(days=1), force=True)
    hist = json.loads(await store.get_meta("odds_api_budget_history"))
    assert hist == [{"d": "2026-06-03", "u": 250}, {"d": "2026-06-04", "u": 400}]

    # Prune: 12 more days -> only the last 10 entries survive.
    for i in range(2, 14):
        pin.budget_used = 400 + i
        await gov.persist(day1 + timedelta(days=i), force=True)
    hist = json.loads(await store.get_meta("odds_api_budget_history"))
    assert len(hist) == 10
    assert hist[-1]["d"] == "2026-06-16"


@pytest.mark.asyncio
async def test_persist_throttled_within_window(store):
    pin = _fake_pinnacle(used=10, remaining=19990)
    gov = BudgetGovernor(pinnacle=pin, store=store)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    await gov.persist(now, force=True)          # first write
    pin.budget_used = 20                        # change values
    later = now.replace(minute=2)               # within 5-min window
    await gov.persist(later)                     # throttled -> no write
    assert await store.get_meta("odds_api_budget_used") == "10"


@pytest.mark.asyncio
async def test_persist_skips_unprobed_default(store):
    # used==0 and remaining==configured default -> never probed; don't persist.
    pin = _fake_pinnacle(used=0, remaining=config.ODDS_API_MONTHLY_BUDGET)
    gov = BudgetGovernor(pinnacle=pin, store=store)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    await gov.persist(now, force=True)
    assert await store.get_meta("odds_api_budget_used") is None


def test_snapshot_returns_used_remaining_total():
    pin = _fake_pinnacle(used=500, remaining=19500)
    gov = BudgetGovernor(pinnacle=pin, store=None)
    assert gov.snapshot() == (500, 19500, 20000)


def test_snapshot_none_when_no_pinnacle():
    gov = BudgetGovernor(pinnacle=None, store=None)
    assert gov.snapshot() == (None, None, None)


def _gov(used, remaining):
    return BudgetGovernor(pinnacle=_fake_pinnacle(used, remaining), store=None)


def test_throttle_level_none_on_track():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)  # ~half the month
    gov = _gov(used=7000, remaining=13000)   # projected ~14k < 20k*1.1
    assert gov.throttle_level(now) == 0
    assert gov.interval_for_tier("warm", now) == config.POLL_TIER_WARM


def test_throttle_widens_warm_then_hot():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    gov = _gov(used=13000, remaining=7000)   # projected ~26k => ratio ~1.3 => level 2
    assert gov.throttle_level(now) == 2
    assert gov.interval_for_tier("warm", now) == 900
    assert gov.interval_for_tier("hot", now) == 120   # betting tier protected — only gently widened


def test_reserve_floor_forces_conservative():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    gov = _gov(used=18600, remaining=1400)   # below 1500 reserve
    assert gov.throttle_level(now) == 3
    assert gov.interval_for_tier("hot", now) == 180   # betting tier still tighter than the old 300


def test_no_throttle_when_unprobed():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    gov = _gov(used=0, remaining=config.ODDS_API_MONTHLY_BUDGET)
    assert gov.throttle_level(now) == 0


def test_no_throttle_first_day_insufficient_signal():
    now = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)   # <1 day into period
    gov = _gov(used=900, remaining=19100)
    assert gov.throttle_level(now) == 0


def test_throttle_level_one_widens_only_warm():
    # ratio in (slack, 1.25] -> level 1: warm widened, hot + live still protected.
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)  # ~half the month
    gov = _gov(used=11600, remaining=8400)   # projected ~24k => ratio ~1.20 => level 1
    assert gov.throttle_level(now) == 1
    assert gov.interval_for_tier("warm", now) == 600
    assert gov.interval_for_tier("hot", now) == config.POLL_TIER_HOT   # protected at level 1


def test_interval_for_unknown_tier_falls_back_to_far():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    gov = _gov(used=7000, remaining=13000)   # level 0
    assert gov.interval_for_tier("bogus", now) == config.POLL_TIER_FAR


def test_reset_day_boundary_no_projection_throttle(monkeypatch):
    # On the reset day itself (days_elapsed < 1) the projection is not yet
    # reliable, so even high cumulative `used` doesn't trigger a soft throttle —
    # only the reserve floor would (remaining here is comfortably above it).
    monkeypatch.setattr(config, "ODDS_API_QUOTA_RESET_DAY", 1)
    gov = _gov(used=18000, remaining=2000)   # above the 1500 reserve floor
    on_reset = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    assert gov.throttle_level(on_reset) == 0


def test_non_calendar_reset_day_changes_projection(monkeypatch):
    # Quota resets on the 7th. On the 8th (~1 day into the period), frac_elapsed
    # must be ~1/30 (NOT ~8/30 as a calendar-1st assumption would give).
    monkeypatch.setattr(config, "ODDS_API_QUOTA_RESET_DAY", 7)
    gov = _gov(used=600, remaining=19400)
    on_8th = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    frac, days = gov._period_progress(on_8th)
    assert 0.9 < days < 1.6
    assert 0.02 < frac < 0.06


class _ResettablePin:
    """Pinnacle stand-in latched at remaining=0; probe_budget() simulates the
    free /v4/sports probe seeing the post-reset quota."""

    name = "pinnacle"

    def __init__(self, *, probe_result):
        self.budget_used = 20000
        self.budget_remaining = 0
        self.budget_total = 20000
        self._probe_result = probe_result
        self.probe_calls = 0

    async def probe_budget(self):
        self.probe_calls += 1
        if self._probe_result is not None:
            self.budget_used = 0
            self.budget_remaining = 20000
        return self._probe_result


@pytest.mark.asyncio
async def test_maybe_reprobe_clears_exhausted_budget_at_period_rollover(monkeypatch):
    """After the monthly reset, Pinnacle is latched dead (remaining<=0 blocks
    every request, so live quota headers never refresh it). The governor must
    re-probe the free endpoint at the detected period boundary to clear it.
    (While exhausted it also re-probes mid-period — see the exhaustion tests —
    so the mid-period probe here reads still-0 until the provider resets.)"""
    monkeypatch.setattr(config, "ODDS_API_QUOTA_RESET_DAY", 1)
    pin = _SequencePin(results=[
        {"used": 20000, "remaining": 0},   # Jun 28 exhaustion re-probe: no reset yet
        {"used": 0, "remaining": 20000},   # Jul 2 boundary: reset visible
    ])
    gov = BudgetGovernor(pinnacle=pin, store=None)

    # First call seeds the period start; no reprobe yet.
    await gov.maybe_reprobe(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))
    assert pin.probe_calls == 0
    assert pin.budget_remaining == 0  # still latched

    # Same period, later — exhaustion re-probe fires but provider still reads 0.
    await gov.maybe_reprobe(datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc))
    assert pin.probe_calls == 1
    assert pin.budget_remaining == 0  # still latched

    # Period rolls over (July 1 reset) — boundary reprobe clears the latch.
    await gov.maybe_reprobe(datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc))
    assert pin.probe_calls == 2
    assert pin.budget_remaining == 20000

    # Healthy in the new period — no further reprobe.
    await gov.maybe_reprobe(datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc))
    assert pin.probe_calls == 2


@pytest.mark.asyncio
async def test_maybe_reprobe_retries_when_probe_fails(monkeypatch):
    """A failed boundary probe must NOT advance the tracked period, so the next
    loop iteration retries instead of waiting a whole month."""
    monkeypatch.setattr(config, "ODDS_API_QUOTA_RESET_DAY", 1)
    pin = _ResettablePin(probe_result=None)
    gov = BudgetGovernor(pinnacle=pin, store=None)

    await gov.maybe_reprobe(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))  # seed
    await gov.maybe_reprobe(datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc))   # rollover, fails
    assert pin.probe_calls == 1
    await gov.maybe_reprobe(datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc))   # retry
    assert pin.probe_calls == 2


class _SequencePin:
    """Pinnacle stand-in whose probe_budget() returns queued results in order
    (None = probe failure). A dict result also updates the budget fields,
    mimicking the header refresh a real probe performs."""

    name = "pinnacle"

    def __init__(self, *, used=20000, remaining=0, results=()):
        self.budget_used = used
        self.budget_remaining = remaining
        self.budget_total = 20000
        self._results = list(results)
        self.probe_calls = 0

    async def probe_budget(self):
        self.probe_calls += 1
        result = self._results.pop(0) if self._results else None
        if result is not None:
            self.budget_used = result["used"]
            self.budget_remaining = result["remaining"]
        return result


@pytest.mark.asyncio
async def test_exhausted_budget_reprobes_within_period(monkeypatch):
    """Regression for the 2026-07 incident: with the configured reset day
    EARLIER than the provider's real reset, the boundary re-probe reads the
    pre-reset zeros, advances the period, and the actual refill lands with no
    boundary left to trigger on — latching Pinnacle dead for a month. An
    exhausted budget must keep re-probing (the endpoint is free) until tokens
    reappear, boundary or not."""
    monkeypatch.setattr(config, "ODDS_API_QUOTA_RESET_DAY", 1)
    pin = _SequencePin(results=[
        {"used": 20000, "remaining": 0},      # Jul 1 boundary: pre-reset read
        {"used": 100, "remaining": 19900},    # Jul 2+: provider has refilled
    ])
    gov = BudgetGovernor(pinnacle=pin, store=None)

    await gov.maybe_reprobe(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))  # seed
    await gov.maybe_reprobe(datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc))    # boundary, still 0
    assert pin.probe_calls == 1
    assert pin.budget_remaining == 0  # latched despite "successful" probe

    # Later, same period: exhaustion re-probe fires and clears the latch.
    await gov.maybe_reprobe(datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc))
    assert pin.probe_calls == 2
    assert pin.budget_remaining == 19900

    # Healthy again — no more off-boundary probes.
    await gov.maybe_reprobe(datetime(2026, 7, 2, 14, 0, tzinfo=timezone.utc))
    assert pin.probe_calls == 2


@pytest.mark.asyncio
async def test_exhausted_reprobe_throttled_to_interval(monkeypatch):
    """Exhaustion re-probes are rate-limited (default hourly), not per-cycle."""
    monkeypatch.setattr(config, "ODDS_API_QUOTA_RESET_DAY", 1)
    pin = _SequencePin(results=[
        {"used": 20000, "remaining": 0},      # still exhausted on first re-probe
        {"used": 20000, "remaining": 0},      # and on the second
    ])
    gov = BudgetGovernor(pinnacle=pin, store=None)

    await gov.maybe_reprobe(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))  # seed
    await gov.maybe_reprobe(datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc))  # first re-probe
    assert pin.probe_calls == 1
    await gov.maybe_reprobe(datetime(2026, 6, 15, 13, 10, tzinfo=timezone.utc))  # within interval
    assert pin.probe_calls == 1
    await gov.maybe_reprobe(datetime(2026, 6, 15, 14, 1, tzinfo=timezone.utc))   # past interval
    assert pin.probe_calls == 2


@pytest.mark.asyncio
async def test_healthy_budget_no_offboundary_reprobe(monkeypatch):
    """A healthy (non-zero) budget must not probe off-boundary at all."""
    monkeypatch.setattr(config, "ODDS_API_QUOTA_RESET_DAY", 1)
    pin = _SequencePin(used=4000, remaining=16000)
    gov = BudgetGovernor(pinnacle=pin, store=None)

    await gov.maybe_reprobe(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))  # seed
    await gov.maybe_reprobe(datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc))
    await gov.maybe_reprobe(datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc))
    assert pin.probe_calls == 0


@pytest.mark.asyncio
async def test_exhausted_reprobe_does_not_advance_period(monkeypatch):
    """A mid-month top-up recovered via exhaustion re-probe must not consume
    the period boundary — the next real rollover still triggers its re-probe."""
    monkeypatch.setattr(config, "ODDS_API_QUOTA_RESET_DAY", 1)
    pin = _SequencePin(results=[
        {"used": 18000, "remaining": 2000},   # Jun 25: top-up arrives mid-month
        {"used": 0, "remaining": 20000},      # Jul 1: real boundary re-probe
    ])
    gov = BudgetGovernor(pinnacle=pin, store=None)

    await gov.maybe_reprobe(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))  # seed
    await gov.maybe_reprobe(datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc))  # exhaustion re-probe
    assert pin.probe_calls == 1
    assert pin.budget_remaining == 2000

    # Real boundary still fires its own re-probe.
    await gov.maybe_reprobe(datetime(2026, 7, 1, 0, 5, tzinfo=timezone.utc))
    assert pin.probe_calls == 2
    assert pin.budget_remaining == 20000


def test_throttle_intervals_public_accessor():
    """The dashboard renders effective cadences per level via this accessor."""
    from oddsiq.pipeline.budget_governor import throttle_intervals
    l0 = throttle_intervals(0)
    assert l0["hot"] == 60 and "live" not in l0  # hot 60s since 2026-08-27; live tier removed
    l2 = throttle_intervals(2)
    assert l2 == {"far": 1800, "warm": 900, "hot": 120}
    assert throttle_intervals(99) == l0  # unknown level falls back to base
