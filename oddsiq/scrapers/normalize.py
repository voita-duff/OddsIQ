from __future__ import annotations

import logging
from rapidfuzz import process, fuzz

from oddsiq.sports.profiles import get_profile

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 85
# When the top two canonical candidates score within this many points of each
# other, the fuzzy match is ambiguous (e.g. a bare city "Los Angeles" tying
# between the Lakers and the Clippers). Fail closed rather than silently picking
# one and mis-routing the snapshot to the wrong game. Legitimate typos clear the
# real team by a wide margin (e.g. "Los Angelees Lakers" -> Lakers 97 vs 77).
FUZZY_AMBIGUITY_MARGIN = 8


def normalize_team(name: str, bookmaker: str, sport: str) -> str | None:
    # Tipsport returns placeholder entries with blank participant fields on
    # every poll; treat those as "nothing to normalize" without logging noise.
    if not name or not name.strip():
        return None
    profile = get_profile(sport)
    canonical = profile.canonical_teams
    if name in canonical:
        return name
    mapped = profile.team_mappings.get(name)
    if mapped is not None:
        return mapped
    matches = process.extract(name, canonical, scorer=fuzz.WRatio, limit=2)
    if not matches or matches[0][1] < FUZZY_THRESHOLD:
        logger.error(
            "Could not normalize team name '%s' from %s (%s)", name, bookmaker, sport
        )
        return None
    best_name, best_score = matches[0][0], matches[0][1]
    if len(matches) > 1 and matches[1][1] >= best_score - FUZZY_AMBIGUITY_MARGIN:
        # Two canonical teams score within the ambiguity margin — refuse to guess.
        logger.warning(
            "Ambiguous fuzzy match for '%s' (%s/%s): '%s' (%.0f) vs '%s' (%.0f); "
            "refusing to guess — add it to the sport's team_mappings.",
            name,
            bookmaker,
            sport,
            best_name,
            best_score,
            matches[1][0],
            matches[1][1],
        )
        return None
    logger.warning(
        "Fuzzy matched '%s' (%s/%s) -> '%s' (score: %d). Add to the sport's team_mappings.",
        name,
        bookmaker,
        sport,
        best_name,
        int(best_score),
    )
    return best_name
