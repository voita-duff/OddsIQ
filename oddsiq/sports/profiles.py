"""SportProfile registry. Each profile bundles every sport-specific constant
so scrapers/engine/scheduler can become sport-agnostic."""

from __future__ import annotations

from dataclasses import dataclass

from oddsiq.sports.teams import (
    NBA_TEAMS,
    NBA_TIPSPORT_ALIASES,
    NHL_TEAMS,
    NHL_TIPSPORT_ALIASES,
    MLB_TEAMS,
    MLB_TIPSPORT_ALIASES,
    WNBA_TEAMS,
    WNBA_TIPSPORT_ALIASES,
    NFL_TEAMS,
    NFL_TIPSPORT_ALIASES,
)


@dataclass(frozen=True)
class SportProfile:
    key: str  # internal id + DB value (e.g. "nba")
    display_name: str  # e.g. "NBA"
    odds_api_key: str  # The Odds API sport key (Pinnacle /odds + /scores)
    tipsport_competition_id: int  # Tipsport offer-API competition id
    tipsport_sport_slug: str  # Tipsport URL sport slug (e.g. "basketbal")
    tipsport_super_sport_id: int  # Tipsport superSportId; required when the moneyline
    # matchView needs an explicit filter (e.g. NHL)
    canonical_teams: frozenset[str]  # canonical team names for this sport
    team_mappings: dict[str, str]  # Tipsport label -> canonical name
    live_backstop_hours: float  # game-LIFECYCLE window after tipoff (zombie-game
    # detection, /scores refresh scoping) — post-tipoff
    # games are never polled (live tier removed 2026-08-27)
    market_rule: str  # reserved descriptor (e.g. "winner_incl_ot_2way"); the
    # active 2-way selection is tipsport_moneyline_matchview
    tipsport_league_slug: str  # Tipsport URL league slug (e.g. "nba")
    tipsport_moneyline_matchview: str  # Tipsport tab for the 2-way moneyline
    tipsport_gender_slug: str = (
        "muzi"  # Tipsport URL category segment between sport and
    )
    # league ({sport_slug}-muzi / {sport_slug}-zeny);
    # only the warmup/Referer URL uses it
    use_canonical_set: bool = True  # RESERVED (not yet read by normalize_team):
    # False -> trust source names + fuzzy (college, later)
    shadow_far_detection: bool = False  # Fetch Pinnacle + detect edges in the FAR tier
    # (2-6h) as SHADOW data: rows carry tier='far',
    # get CLV at kickoff, and the betting engine skips
    # them (tier_not_bettable). NFL only (2026-09-06):
    # its lines move all week, so the MLB-tuned 2h
    # window may miss where NFL edges actually live.


NBA = SportProfile(
    key="nba",
    display_name="NBA",
    odds_api_key="basketball_nba",
    tipsport_competition_id=23,
    tipsport_sport_slug="basketbal",
    tipsport_super_sport_id=7,  # REQUIRED for the WINNER_FINAL_DECISION filter
    canonical_teams=NBA_TEAMS,
    team_mappings=NBA_TIPSPORT_ALIASES,
    live_backstop_hours=3.0,
    market_rule="winner_incl_ot_2way",
    tipsport_league_slug="nba",
    # Basketball's WHOLE_MATCH tab carries the regulation 3-way legs (1/1x/x/x2/2);
    # its 1/2 prices are NOT the moneyline (verified 2026-06-12: they sum under
    # 100% two-way implied). The true incl-OT winner is WINNER_FINAL_DECISION —
    # same fail-closed pattern as NHL.
    tipsport_moneyline_matchview="WINNER_FINAL_DECISION",
    tipsport_gender_slug="muzi",
    use_canonical_set=True,
)

NHL = SportProfile(
    key="nhl",
    display_name="NHL",
    odds_api_key="icehockey_nhl",
    tipsport_competition_id=22,
    tipsport_sport_slug="ledni-hokej",
    tipsport_super_sport_id=23,  # confirmed; REQUIRED for the WINNER_FINAL_DECISION request
    canonical_teams=NHL_TEAMS,
    team_mappings=NHL_TIPSPORT_ALIASES,
    live_backstop_hours=2.5,
    market_rule="winner_incl_ot_2way",
    tipsport_league_slug="nhl",
    tipsport_moneyline_matchview="WINNER_FINAL_DECISION",  # 2-way incl-OT; NOT the 3-way WINNER_WHOLE_MATCH
    tipsport_gender_slug="muzi",
    use_canonical_set=True,
)

MLB = SportProfile(
    key="mlb",
    display_name="MLB",
    odds_api_key="baseball_mlb",
    tipsport_competition_id=5795,
    tipsport_sport_slug="baseball",
    tipsport_super_sport_id=6,  # unused for moneyline (default [] request); kept for correctness
    canonical_teams=MLB_TEAMS,
    team_mappings=MLB_TIPSPORT_ALIASES,
    live_backstop_hours=4.0,
    market_rule="winner_incl_ot_2way",
    tipsport_league_slug="mlb",
    tipsport_moneyline_matchview="WINNER_WHOLE_MATCH",  # clean 2-way (baseball cannot tie)
    tipsport_gender_slug="muzi",
    use_canonical_set=True,
)

WNBA = SportProfile(
    key="wnba",
    display_name="WNBA",
    odds_api_key="basketball_wnba",
    tipsport_competition_id=10,  # recon 2026-06-11: /kurzy/basketbal/basketbal-zeny/wnba-10
    tipsport_sport_slug="basketbal",
    tipsport_super_sport_id=7,  # basketball superSport (same as NBA); REQUIRED for the WINNER_FINAL_DECISION filter
    canonical_teams=WNBA_TEAMS,
    team_mappings=WNBA_TIPSPORT_ALIASES,
    live_backstop_hours=2.5,  # 4x10min format runs ~2h real time; NHL-style buffer
    market_rule="winner_incl_ot_2way",
    tipsport_league_slug="wnba",
    # Same basketball engine as NBA: WHOLE_MATCH = regulation 3-way legs, so the
    # 2-way incl-OT moneyline is WINNER_FINAL_DECISION (A/B price-verified at
    # enablement, 2026-06-12).
    tipsport_moneyline_matchview="WINNER_FINAL_DECISION",
    tipsport_gender_slug="zeny",  # women's category: /kurzy/basketbal/basketbal-zeny/wnba-10
    use_canonical_set=True,
)

NFL = SportProfile(
    key="nfl",
    display_name="NFL",
    odds_api_key="americanfootball_nfl",
    tipsport_competition_id=5748,  # recon 2026-08-25: /kurzy/americky-fotbal/americky-fotbal-muzi/nfl-5748
    tipsport_sport_slug="americky-fotbal",
    tipsport_super_sport_id=2,  # captured from Tipsport's own offer API 2026-08-25;
    # REQUIRED for the WINNER_FINAL_DECISION filter
    canonical_teams=NFL_TEAMS,
    team_mappings=NFL_TIPSPORT_ALIASES,
    live_backstop_hours=3.5,  # NFL games run ~3h10m; OT buffer
    market_rule="winner_incl_ot_2way",
    tipsport_league_slug="nfl",
    # American football's WHOLE_MATCH tab carries the regulation 3-way legs
    # (1/1x/x/x2/2 — a pre-OT draw is real), the same trap as basketball. The
    # 2-way incl-OT moneyline is WINNER_FINAL_DECISION ("Vítěz do rozhodnutí"),
    # price-verified on live NCAAF games 2026-08-25 (FD two-way implied
    # 105.8-109.4%, coherent A/B vs the regulation legs). NFL ties (rare,
    # post-OT) settle as draw -> push via resolve_bet_result.
    tipsport_moneyline_matchview="WINNER_FINAL_DECISION",
    tipsport_gender_slug="muzi",
    use_canonical_set=True,
    shadow_far_detection=True,  # far-tier shadow rows (see field comment); review after 4 weeks
)

SPORTS: dict[str, SportProfile] = {
    NBA.key: NBA,
    NHL.key: NHL,
    MLB.key: MLB,
    WNBA.key: WNBA,
    NFL.key: NFL,
}

# 2026-08-08: MLB-only during the northern off-season (quota strategy).
#   - nba/nhl: dark until October — their hourly /scores sweeps burned ~96
#     credits/day for zero pollable games (and NHL's feed already leaks
#     October preseason fixtures). Re-enable at preseason, after a fresh
#     `oddsiq tipsport-smoke` for each.
#   - wnba: a full two-month in-season sample produced ZERO value bets —
#     structural, not a bug (Tipsport's WNBA hold exceeds the max co-temporal
#     edge; verified 2026-07). Not worth its ~80-150 credits/day.
# 2026-08-25: NFL added (week-1 kickoff Sep 10). Detection runs from deploy —
#   Tipsport posts game moneylines ~early Sep; until then the FD tab is absent
#   and the scraper's TipsportMarketUnavailable path idles gracefully.
#   Real-money placement is additionally gated by the meta `betting_sports`
#   key (default mlb-only; the operator ticks NFL before week 1).
# All profiles stay defined; enablement is this list only.
ENABLED_SPORTS: list[str] = ["mlb", "nfl"]


def get_profile(key: str) -> SportProfile:
    return SPORTS[key]


def enabled_profiles() -> list[SportProfile]:
    return [SPORTS[k] for k in ENABLED_SPORTS]
