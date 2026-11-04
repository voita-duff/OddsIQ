import pytest
from oddsiq.engine.clv import calculate_clv, resolve_bet_result


def test_clv_positive():
    clv = calculate_clv(entry_odds=2.05, closing_fair_odds=1.98)
    assert clv == pytest.approx(0.0354, abs=0.001)


def test_clv_negative():
    clv = calculate_clv(entry_odds=1.90, closing_fair_odds=1.98)
    assert clv < 0


def test_resolve_bet_result_raises_on_unknown_selection():
    with pytest.raises(ValueError):
        resolve_bet_result("moneyline", "over", None, 100, 90)
    with pytest.raises(ValueError):
        resolve_bet_result("spread", "bogus", -4.5, 100, 90)
    with pytest.raises(ValueError):
        resolve_bet_result("total", "home", 200.5, 100, 90)


def test_resolve_bet_result_raises_on_unknown_market():
    with pytest.raises(ValueError):
        resolve_bet_result("parlay", "home", None, 100, 90)


def test_clv_zero():
    clv = calculate_clv(entry_odds=2.00, closing_fair_odds=2.00)
    assert clv == pytest.approx(0.0, abs=0.001)


def test_resolve_moneyline_win():
    result = resolve_bet_result(market_type="moneyline", selection="home", line=None, home_score=110, away_score=105)
    assert result == "win"


def test_resolve_moneyline_loss():
    result = resolve_bet_result(market_type="moneyline", selection="home", line=None, home_score=100, away_score=105)
    assert result == "loss"


def test_resolve_spread_win():
    result = resolve_bet_result(market_type="spread", selection="home", line=-4.5, home_score=110, away_score=100)
    assert result == "win"


def test_resolve_spread_loss():
    result = resolve_bet_result(market_type="spread", selection="home", line=-4.5, home_score=103, away_score=100)
    assert result == "loss"


def test_resolve_spread_push():
    result = resolve_bet_result(market_type="spread", selection="home", line=-5.0, home_score=105, away_score=100)
    assert result == "push"


def test_resolve_total_over_win():
    result = resolve_bet_result(market_type="total", selection="over", line=215.5, home_score=110, away_score=108)
    assert result == "win"


def test_resolve_total_under_win():
    result = resolve_bet_result(market_type="total", selection="under", line=220.5, home_score=110, away_score=108)
    assert result == "win"


def test_resolve_total_push():
    result = resolve_bet_result(market_type="total", selection="over", line=218.0, home_score=110, away_score=108)
    assert result == "push"


def test_resolve_moneyline_tie_is_draw():
    # A tie must resolve to "draw" for BOTH sides, never double-loss.
    assert resolve_bet_result("moneyline", "home", None, 3, 3) == "draw"
    assert resolve_bet_result("moneyline", "away", None, 3, 3) == "draw"


def test_resolve_moneyline_win_loss_unchanged():
    assert resolve_bet_result("moneyline", "home", None, 5, 4) == "win"
    assert resolve_bet_result("moneyline", "away", None, 5, 4) == "loss"
    assert resolve_bet_result("moneyline", "away", None, 4, 5) == "win"
