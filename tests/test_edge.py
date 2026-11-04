import pytest
from oddsiq.engine.edge import calculate_edge
from oddsiq.db.models import OddsSnapshot


def test_calculate_edge_positive():
    edge = calculate_edge(czech_odds=2.05, fair_odds=1.95)
    assert edge == pytest.approx(0.0513, abs=0.001)


def test_calculate_edge_negative():
    edge = calculate_edge(czech_odds=1.85, fair_odds=1.95)
    assert edge < 0


def test_calculate_edge_zero():
    edge = calculate_edge(czech_odds=1.95, fair_odds=1.95)
    assert edge == pytest.approx(0.0, abs=0.001)


def test_find_edges_threads_tipsport_ids():
    """The soft-book snapshot's transient Tipsport ids flow into the edge dict
    so EdgeService can persist them on the value bet (execution identity for
    the P0 availability probe / P1 placement)."""
    from oddsiq.engine.edge import find_edges

    pinnacle_odds = [
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="home",
            odds=2.0,
        ),
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="away",
            odds=2.0,
        ),
    ]
    czech = OddsSnapshot(
        game_id=1,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        odds=2.3,
    )
    czech._tipsport_match_id = 8109316
    czech._tipsport_opp_id = 2624446456
    czech._tipsport_url = "/kurzy/zapas/baseball-x/8109316"
    edges = find_edges(pinnacle_odds, [czech])
    assert len(edges) == 1
    assert edges[0]["tipsport_match_id"] == 8109316
    assert edges[0]["tipsport_opp_id"] == 2624446456
    assert edges[0]["tipsport_url"] == "/kurzy/zapas/baseball-x/8109316"


def test_find_edges_tipsport_ids_default_none():
    """Snapshots without ids (Pinnacle-sourced, legacy fixtures) yield None —
    the fields are optional everywhere downstream."""
    from oddsiq.engine.edge import find_edges

    pinnacle_odds = [
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="home",
            odds=2.0,
        ),
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="moneyline",
            selection="away",
            odds=2.0,
        ),
    ]
    czech = OddsSnapshot(
        game_id=1,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        odds=2.3,
    )
    edges = find_edges(pinnacle_odds, [czech])
    assert edges[0]["tipsport_match_id"] is None
    assert edges[0]["tipsport_opp_id"] is None
    assert edges[0]["tipsport_url"] is None


def test_find_edges_matching_lines():
    from oddsiq.engine.edge import find_edges

    pinnacle_odds = [
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="spread",
            selection="home",
            line=-4.5,
            odds=1.90,
        ),
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="spread",
            selection="away",
            line=4.5,
            odds=1.95,
        ),
    ]
    czech_odds = [
        OddsSnapshot(
            game_id=1,
            bookmaker="tipsport",
            market_type="spread",
            selection="home",
            line=-4.5,
            odds=2.05,
        ),
        OddsSnapshot(
            game_id=1,
            bookmaker="tipsport",
            market_type="spread",
            selection="away",
            line=4.5,
            odds=1.85,
        ),
    ]
    edges = find_edges(pinnacle_odds, czech_odds)
    assert len(edges) >= 1
    assert edges[0]["selection"] == "home"
    assert edges[0]["edge"] > 0.03


def test_find_edges_different_lines_skipped():
    from oddsiq.engine.edge import find_edges

    pinnacle_odds = [
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="spread",
            selection="home",
            line=-4.5,
            odds=1.90,
        ),
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="spread",
            selection="away",
            line=4.5,
            odds=1.95,
        ),
    ]
    czech_odds = [
        OddsSnapshot(
            game_id=1,
            bookmaker="tipsport",
            market_type="spread",
            selection="home",
            line=-5.5,
            odds=2.05,
        ),
        OddsSnapshot(
            game_id=1,
            bookmaker="tipsport",
            market_type="spread",
            selection="away",
            line=5.5,
            odds=1.85,
        ),
    ]
    edges = find_edges(pinnacle_odds, czech_odds)
    assert len(edges) == 0


def test_find_edges_spread_requires_same_signed_side_line():
    from oddsiq.engine.edge import find_edges

    pinnacle_odds = [
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="spread",
            selection="home",
            line=-4.5,
            odds=1.91,
        ),
        OddsSnapshot(
            game_id=1,
            bookmaker="pinnacle",
            market_type="spread",
            selection="away",
            line=4.5,
            odds=1.91,
        ),
    ]
    czech_odds = [
        # Same abs(line), but opposite side of the handicap for the home team.
        OddsSnapshot(
            game_id=1,
            bookmaker="tipsport",
            market_type="spread",
            selection="home",
            line=4.5,
            odds=2.20,
        ),
    ]

    edges = find_edges(pinnacle_odds, czech_odds, threshold=0.0)

    assert edges == []
