from datetime import datetime, timezone

from oddsiq.db.models import Game, OddsSnapshot, ValueBet


def test_game_creation():
    game = Game(
        external_id="nba_20260410_LAL_BOS",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
    )
    assert game.home_team == "Boston Celtics"
    assert game.status == "scheduled"
    assert game.home_score is None


def test_odds_snapshot_creation():
    snap = OddsSnapshot(
        game_id=1,
        bookmaker="pinnacle",
        market_type="moneyline",
        selection="home",
        odds=1.95,
    )
    assert snap.odds == 1.95
    assert snap.line is None
    assert snap.is_closing is False


def test_odds_snapshot_with_line():
    snap = OddsSnapshot(
        game_id=1,
        bookmaker="tipsport",
        market_type="spread",
        selection="home",
        line=-4.5,
        odds=1.91,
    )
    assert snap.line == -4.5
    assert snap.market_type == "spread"


def test_value_bet_creation():
    vb = ValueBet(
        game_id=1,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.05,
        fair_odds=1.95,
        edge_pct=5.13,
    )
    assert vb.edge_pct == 5.13
    assert vb.closing_odds is None
    assert vb.clv_pct is None
    assert vb.result is None


