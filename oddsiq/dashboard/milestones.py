"""System milestones for the /analysis activity calendar.

Curated deployment facts (dates are UTC deploy days, sourced from the commit
history) so the calendar can answer "when did the app start
working / when was auto-betting introduced / what changed before this quiet
stretch" without the reader cross-referencing the vault. Data-derived markers
(first odds collected, first detection, first placement attempt) are computed
in ``queries.activity_calendar`` and merged with this list.

``kind`` drives the marker style: ``engine`` (auto-betting), ``fix``
(integrity/correctness), ``scope`` (sports/features), ``ops``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Milestone:
    day: date
    label: str
    kind: str  # engine | fix | scope | ops


MILESTONES: tuple[Milestone, ...] = (
    Milestone(
        date(2026, 6, 2),
        "Multi-sport refactor (NBA/NHL/MLB), far/warm/hot tiers",
        "scope",
    ),
    Milestone(date(2026, 6, 11), "WNBA enabled", "scope"),
    Milestone(
        date(2026, 6, 12),
        "Basketball matchView fix (NBA rows before this are excluded)",
        "fix",
    ),
    Milestone(
        date(2026, 7, 10), "Pregame stale-price guards (8% cap, coherence floor)", "fix"
    ),
    Milestone(date(2026, 8, 8), "MLB-only for the off-season; quota overhaul", "scope"),
    Milestone(
        date(2026, 8, 9),
        "Auto-betting engine + Chrome actuator shipped (inert)",
        "engine",
    ),
    Milestone(date(2026, 8, 10), "Telegram rebrand: real-money events only", "ops"),
    Milestone(date(2026, 8, 25), "NFL enabled for detection", "scope"),
    Milestone(
        date(2026, 8, 27),
        "Live-edge subsystem removed; hot tier 60s; tipoff-boundary guards",
        "fix",
    ),
    Milestone(
        date(2026, 8, 30),
        "Wrong-game listing fix (time-scoped matching); warm-tier betting on",
        "fix",
    ),
    Milestone(
        date(2026, 8, 31), "Login probe pinned to /kurzy; loss alerts debounced", "ops"
    ),
    Milestone(
        date(2026, 9, 2), "Bridge app-level keepalive; startup login watchdog", "ops"
    ),
)
