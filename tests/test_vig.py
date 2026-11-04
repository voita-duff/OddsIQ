import pytest
from oddsiq.engine.vig import remove_vig, implied_probability


def test_implied_probability():
    assert implied_probability(2.0) == pytest.approx(0.5)
    assert implied_probability(1.5) == pytest.approx(0.6667, abs=0.001)


def test_remove_vig_balanced_market():
    fair_home, fair_away = remove_vig(1.91, 1.91)
    assert fair_home == pytest.approx(2.0, abs=0.01)
    assert fair_away == pytest.approx(2.0, abs=0.01)


def test_remove_vig_typical_nba():
    fair_home, fair_away = remove_vig(1.90, 2.00)
    assert fair_home > 1.90
    assert fair_away > 2.00
    assert (1 / fair_home + 1 / fair_away) == pytest.approx(1.0, abs=0.001)


def test_remove_vig_heavy_favorite():
    fair_home, fair_away = remove_vig(1.20, 5.00)
    assert (1 / fair_home + 1 / fair_away) == pytest.approx(1.0, abs=0.001)
    assert fair_home > 1.20
    assert fair_away > 5.00


def test_remove_vig_is_proportional_not_additive():
    """Pin the devig METHOD. The sum-to-1 + direction assertions above also hold
    for an additive devig, so they don't distinguish the two; multiplicative
    (proportional) devig preserves the RATIO of implied probabilities, additive
    does not. For (1.20, 5.00) the favorite fair differs by ~1.3% between methods.
    """
    fair_fav, fair_dog = remove_vig(1.20, 5.00)
    # Exact multiplicative result (additive would give ~1.2245 / ~5.4545).
    assert fair_fav == pytest.approx(1.2400, abs=0.001)
    assert fair_dog == pytest.approx(5.1667, abs=0.001)
    # Ratio-of-fair-probabilities is preserved iff the devig is multiplicative.
    assert (1 / fair_fav) / (1 / fair_dog) == pytest.approx(
        (1 / 1.20) / (1 / 5.00), abs=1e-6
    )


def test_remove_vig_invalid_odds():
    with pytest.raises(ValueError):
        remove_vig(0.0, 2.0)
    with pytest.raises(ValueError):
        remove_vig(1.5, -1.0)
