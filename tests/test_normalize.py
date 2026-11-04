import logging
from oddsiq.scrapers.normalize import normalize_team


def test_canonical_name_passthrough():
    assert normalize_team("Los Angeles Lakers", "pinnacle", "nba") == "Los Angeles Lakers"


def test_tipsport_alias_mapping_nba():
    assert normalize_team("LA Lakers", "tipsport", "nba") == "Los Angeles Lakers"
    assert normalize_team("GS Warriors", "tipsport", "nba") == "Golden State Warriors"


def test_fuzzy_match_fallback():
    assert normalize_team("Los Angelees Lakers", "tipsport", "nba") == "Los Angeles Lakers"


def test_unknown_team_returns_none():
    assert normalize_team("Nonexistent Team FC", "tipsport", "nba") is None


def test_empty_name_returns_none_silently(caplog):
    with caplog.at_level(logging.DEBUG, logger="oddsiq.scrapers.normalize"):
        assert normalize_team("", "tipsport", "nba") is None
    assert caplog.records == []


def test_whitespace_name_returns_none_silently(caplog):
    with caplog.at_level(logging.DEBUG, logger="oddsiq.scrapers.normalize"):
        assert normalize_team("   ", "tipsport", "nba") is None
    assert caplog.records == []


def test_unknown_name_still_logs_error(caplog):
    with caplog.at_level(logging.ERROR, logger="oddsiq.scrapers.normalize"):
        assert normalize_team("Nonexistent Team FC", "tipsport", "nba") is None
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_tipsport_z_suffix_alias_mapping_wnba():
    # Tipsport suffixes women's teams with " Ž" (recon 2026-06-11).
    assert normalize_team("Indiana Fever Ž", "tipsport", "wnba") == "Indiana Fever"
    assert normalize_team("Golden State Valkyries Ž", "tipsport", "wnba") == "Golden State Valkyries"


def test_canonical_name_passthrough_wnba():
    assert normalize_team("Toronto Tempo", "tipsport", "wnba") == "Toronto Tempo"


def test_canonical_name_passthrough_nfl():
    # Tipsport NFL labels match the Odds API canonicals (futures recon
    # 2026-08-25) — no aliases; exact passthrough must hold.
    assert normalize_team("Kansas City Chiefs", "tipsport", "nfl") == "Kansas City Chiefs"
    assert normalize_team("San Francisco 49ers", "tipsport", "nfl") == "San Francisco 49ers"
    assert normalize_team("Washington Commanders", "pinnacle", "nfl") == "Washington Commanders"


def test_ambiguous_same_city_match_returns_none(caplog):
    # "Los Angeles" ties between the Lakers and the Clippers (both ~90 via
    # WRatio). Refuse to guess — silently picking one would mis-route the
    # snapshot to the wrong game. Fail closed and warn instead.
    with caplog.at_level(logging.WARNING, logger="oddsiq.scrapers.normalize"):
        assert normalize_team("Los Angeles", "tipsport", "nba") is None
    assert any("mbiguous" in r.getMessage() for r in caplog.records)
