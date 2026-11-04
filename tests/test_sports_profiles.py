from oddsiq.sports.profiles import (
    ENABLED_SPORTS,
    SPORTS,
    enabled_profiles,
    get_profile,
    NBA,
    NHL,
    MLB,
    WNBA,
    NFL,
)


def test_mlb_and_nfl_enabled_all_profiles_defined():
    # 2026-08-25: NFL joins MLB (detection from deploy; real-money placement is
    # additionally gated by the meta `betting_sports` key, ticked by the
    # operator before week 1). NBA/NHL stay dark until October; WNBA stays off
    # (structural: Tipsport's WNBA hold exceeds the max observed edge).
    # Profiles stay defined — enablement is the ENABLED_SPORTS list only.
    assert ENABLED_SPORTS == ["mlb", "nfl"]
    assert {p.key for p in enabled_profiles()} == {"mlb", "nfl"}
    assert set(SPORTS) == {"nba", "nhl", "mlb", "wnba", "nfl"}


def test_team_set_sizes():
    assert len(NBA.canonical_teams) == 30
    assert len(NHL.canonical_teams) == 32
    assert len(MLB.canonical_teams) == 30
    assert len(WNBA.canonical_teams) == 15
    assert len(NFL.canonical_teams) == 32


def test_nhl_uses_final_decision_matchview_and_super_sport_id():
    assert NHL.tipsport_moneyline_matchview == "WINNER_FINAL_DECISION"
    assert NHL.tipsport_super_sport_id == 23
    assert NHL.tipsport_competition_id == 22


def test_mlb_uses_whole_match_default():
    # Baseball cannot tie, so WHOLE_MATCH is the true 2-way for MLB only.
    assert MLB.tipsport_moneyline_matchview == "WINNER_WHOLE_MATCH"
    assert MLB.tipsport_competition_id == 5795


def test_basketball_uses_final_decision_matchview():
    # Basketball's WHOLE_MATCH tab carries the regulation 3-way legs (1/1x/x/x2/2)
    # whose 1/2 prices are NOT the moneyline — verified by same-book A/B price
    # comparison 2026-06-12 (WHOLE legs summed under 100% two-way implied). Both
    # basketball profiles must use the incl-OT WINNER_FINAL_DECISION tab.
    assert NBA.tipsport_moneyline_matchview == "WINNER_FINAL_DECISION"
    assert WNBA.tipsport_moneyline_matchview == "WINNER_FINAL_DECISION"
    assert NBA.tipsport_super_sport_id == 7
    assert WNBA.tipsport_super_sport_id == 7


def test_profile_odds_api_slugs():
    assert get_profile("nhl").odds_api_key == "icehockey_nhl"
    assert get_profile("mlb").odds_api_key == "baseball_mlb"
    assert get_profile("wnba").odds_api_key == "basketball_wnba"


def test_wnba_profile_fields():
    assert WNBA.key == "wnba"
    assert WNBA.display_name == "WNBA"
    assert WNBA.tipsport_competition_id == 10
    assert WNBA.tipsport_sport_slug == "basketbal"
    assert WNBA.tipsport_league_slug == "wnba"
    assert WNBA.tipsport_super_sport_id == 7
    assert WNBA.tipsport_gender_slug == "zeny"
    assert WNBA.tipsport_moneyline_matchview == "WINNER_FINAL_DECISION"
    assert WNBA.live_backstop_hours == 2.5
    assert "Golden State Valkyries" in WNBA.canonical_teams
    assert "Toronto Tempo" in WNBA.canonical_teams
    assert WNBA.team_mappings["Indiana Fever Ž"] == "Indiana Fever"


def test_nfl_profile_fields():
    # Recon 2026-08-25: competition + superSportId captured from Tipsport's own
    # offer API; the FD matchView price-verified on live NCAAF games (american
    # football's WHOLE_MATCH tab carries regulation 3-way legs — the basketball
    # trap). See docs/Design/2026-08-25-nfl-fifth-sport-autobetting-design.md.
    assert NFL.key == "nfl"
    assert NFL.display_name == "NFL"
    assert NFL.odds_api_key == "americanfootball_nfl"
    assert NFL.tipsport_competition_id == 5748
    assert NFL.tipsport_sport_slug == "americky-fotbal"
    assert NFL.tipsport_league_slug == "nfl"
    assert NFL.tipsport_super_sport_id == 2
    assert NFL.tipsport_gender_slug == "muzi"
    assert NFL.tipsport_moneyline_matchview == "WINNER_FINAL_DECISION"
    assert NFL.live_backstop_hours == 3.5
    assert "Kansas City Chiefs" in NFL.canonical_teams
    assert "Washington Commanders" in NFL.canonical_teams
    assert NFL.team_mappings == {}


def test_gender_slug_defaults_to_muzi_for_existing_sports():
    # The category segment of the Tipsport warmup URL: men's leagues live under
    # {sport}-muzi; only WNBA uses -zeny. The default keeps NBA/NHL/MLB
    # behavior bit-identical to the pre-field era.
    assert NBA.tipsport_gender_slug == "muzi"
    assert NHL.tipsport_gender_slug == "muzi"
    assert MLB.tipsport_gender_slug == "muzi"
    assert NFL.tipsport_gender_slug == "muzi"


def test_market_rule_is_two_way_for_all_enabled_sports():
    # market_rule is a reserved descriptor (active 2-way selection is
    # tipsport_moneyline_matchview). Pin it so a future divergence is visible.
    for p in enabled_profiles():
        assert p.market_rule == "winner_incl_ot_2way"


def test_only_nfl_runs_far_tier_shadow_detection():
    # 2026-09-06: NFL lines move all week; far-tier (2-6h) detection is
    # recorded as shadow data (tier 'far', CLV at kickoff, never bet) to learn
    # where NFL edges live. Every other sport keeps the MLB-tuned 2h window.
    assert NFL.shadow_far_detection is True
    assert all(p.shadow_far_detection is False for p in (NBA, NHL, MLB, WNBA))
