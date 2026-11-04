from __future__ import annotations


def implied_probability(decimal_odds: float) -> float:
    if decimal_odds <= 0:
        raise ValueError(f"Odds must be positive, got {decimal_odds}")
    return 1.0 / decimal_odds


def remove_vig(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Strip the bookmaker overround from a two-way market.

    Returns ``(fair_a, fair_b)`` in the SAME order as the inputs. Callers MUST
    pass the bet/target side FIRST, so the first element is the fair odds for
    the side being evaluated — swapping the arguments silently inverts CLV and
    edge. Both inputs are decimal odds and must be > 0.
    """
    if odds_a <= 0 or odds_b <= 0:
        raise ValueError(f"Odds must be positive, got ({odds_a}, {odds_b})")
    imp_a = implied_probability(odds_a)
    imp_b = implied_probability(odds_b)
    overround = imp_a + imp_b
    fair_prob_a = imp_a / overround
    fair_prob_b = imp_b / overround
    return (1.0 / fair_prob_a, 1.0 / fair_prob_b)
