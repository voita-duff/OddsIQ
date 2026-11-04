from datetime import datetime, timedelta, timezone

from oddsiq import config
from oddsiq.db.models import Game
from oddsiq.pipeline import tiers


def _make_game(*, seconds_to_tipoff: float, commence_time: datetime | None = None) -> Game:
    now = datetime.now(timezone.utc)
    return Game(
        external_id="test_game",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=now + timedelta(seconds=seconds_to_tipoff),
        commence_time=commence_time,
        status="scheduled",
    )


def test_time_to_tipoff_legacy_null_commence_time_uses_scheduled_at():
    """No backfill contract: games without commence_time keep scheduled_at timing."""
    game = _make_game(seconds_to_tipoff=600)
    now = datetime.now(timezone.utc)

    seconds = tiers.time_to_tipoff(game, now)

    assert 0 < seconds <= 600
    assert tiers.get_poll_interval(seconds) == 60  # hot tier (<60m)


def test_tier_boundaries_far_warm_hot():
    assert tiers.tier_for(7201) == "far"
    assert tiers.tier_for(7200) == "warm"     # >1h..2h is warm
    assert tiers.tier_for(3601) == "warm"
    assert tiers.tier_for(3600) == "hot"      # <=60m is hot
    assert tiers.tier_for(1) == "hot"
    # Post-tipoff is unpollable — the live tier was removed 2026-08-27.
    assert tiers.tier_for(0) is None
    assert tiers.tier_for(-1) is None


def test_get_poll_interval_cadence():
    assert tiers.get_poll_interval(8000) == config.POLL_TIER_FAR
    assert tiers.get_poll_interval(5400) == config.POLL_TIER_WARM == 300
    assert tiers.get_poll_interval(1800) == config.POLL_TIER_HOT == 60   # 30m -> hot now
    assert tiers.get_poll_interval(-30) is None   # post-tipoff -> unpollable


def test_pinnacle_needed_warm_hot_only():
    assert tiers.pinnacle_needed({"warm"}) is True
    assert tiers.pinnacle_needed({"hot"}) is True
    assert tiers.pinnacle_needed({"far"}) is False
    assert tiers.pinnacle_needed(set()) is False


def test_pinnacle_needed_far_only_when_shadow_flag():
    assert tiers.pinnacle_needed({"far"}) is False
    assert tiers.pinnacle_needed({"far"}, shadow_far=True) is True
    assert tiers.pinnacle_needed(set(), shadow_far=True) is False
    assert tiers.pinnacle_needed({"warm"}, shadow_far=False) is True


def test_get_poll_interval_post_tipoff_always_none():
    # Any post-tipoff age -> None (there is no live polling window at all).
    assert tiers.get_poll_interval(0) is None
    assert tiers.get_poll_interval(-60) is None
    assert tiers.get_poll_interval(-(3 * 3600)) is None
    # Pregame boundaries unchanged.
    assert tiers.get_poll_interval(60) == config.POLL_TIER_HOT       # <60m -> hot
    assert tiers.get_poll_interval(3601) == config.POLL_TIER_WARM    # >60m..2h -> warm
    # Beyond the 6h window -> not polled yet.
    assert tiers.get_poll_interval(21601) is None
