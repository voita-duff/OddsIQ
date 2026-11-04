"""Tier classification + poll-interval policy, shared by the orchestrator loop
and the poll coordinator. Pure functions of time-to-tipoff (and config).

Data-driven cadence: far/warm/hot, all PREGAME. Post-tipoff games are
unpollable — the live tier (and the live scout it fed) was removed
2026-08-27; game lifecycle after tipoff is schedule/clock-driven
(finals via /scores, closing snapshots marked at tipoff).
"""

from __future__ import annotations

from datetime import datetime

from oddsiq import config
from oddsiq.db.models import Game

# The shadow tier: detected + CLV'd but never bettable (SportProfile.shadow_far_detection).
SHADOW_TIER = "far"


def effective_tipoff(game: Game) -> datetime:
    return game.commence_time or game.scheduled_at


def time_to_tipoff(game: Game, now: datetime) -> float:
    return (effective_tipoff(game) - now).total_seconds()


def tier_for(seconds_to_tipoff: float) -> str | None:
    """Pregame tier, or None once the game has tipped off (unpollable)."""
    if seconds_to_tipoff <= 0:
        return None
    if seconds_to_tipoff > 7200:
        return "far"
    if seconds_to_tipoff > 3600:
        return "warm"
    return "hot"


def pinnacle_needed(active_tiers: set[str], *, shadow_far: bool = False) -> bool:
    # Pinnacle's h2h fetch fires when any game is in a tier that uses it:
    # warm/hot pregame edge detection — plus the far tier for sports that
    # run far-tier SHADOW detection (SportProfile.shadow_far_detection; NFL).
    if active_tiers & {"warm", "hot"}:
        return True
    return bool(shadow_far and "far" in active_tiers)


def get_poll_interval(seconds_to_tipoff: float) -> int | None:
    if seconds_to_tipoff > 21600 or seconds_to_tipoff <= 0:
        return None
    if seconds_to_tipoff > 7200:
        return config.POLL_TIER_FAR
    if seconds_to_tipoff > 3600:
        return config.POLL_TIER_WARM
    return config.POLL_TIER_HOT
