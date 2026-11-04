import pytest
from oddsiq.sports import NBA, SPORTS, get_profile, enabled_profiles


@pytest.fixture(autouse=True)
def _hermetic_enabled_sports(monkeypatch):
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", ["nba"])


def test_nba_profile_fields():
    assert NBA.key == "nba"
    assert NBA.odds_api_key == "basketball_nba"
    assert NBA.tipsport_competition_id == 23
    assert NBA.tipsport_sport_slug == "basketbal"
    assert NBA.tipsport_super_sport_id == 7
    assert NBA.market_rule == "winner_incl_ot_2way"
    assert NBA.use_canonical_set is True
    assert "Los Angeles Lakers" in NBA.canonical_teams
    assert len(NBA.canonical_teams) == 30
    assert NBA.team_mappings["LA Lakers"] == "Los Angeles Lakers"


def test_registry_lookup():
    assert get_profile("nba") is NBA
    assert SPORTS["nba"] is NBA
    assert enabled_profiles() == [NBA]


def test_profile_is_frozen():
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        NBA.key = "x"  # type: ignore[misc]
