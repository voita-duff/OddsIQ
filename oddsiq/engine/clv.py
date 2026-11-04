from __future__ import annotations


def calculate_clv(entry_odds: float, closing_fair_odds: float) -> float:
    return (entry_odds / closing_fair_odds) - 1.0


def resolve_bet_result(
    market_type: str,
    selection: str,
    line: float | None,
    home_score: int,
    away_score: int,
) -> str:
    if market_type == "moneyline":
        # A tie resolves to "draw" for both sides (defensive: NHL/MLB don't tie
        # in the final, but a misfetched 3-way market must never double-loss).
        if home_score == away_score:
            return "draw"
        if selection == "home":
            return "win" if home_score > away_score else "loss"
        if selection == "away":
            return "win" if away_score > home_score else "loss"
        raise ValueError(f"Unknown moneyline selection: {selection!r}")
    elif market_type == "spread":
        if selection == "home":
            margin = home_score - away_score + line
        elif selection == "away":
            margin = away_score - home_score + line
        else:
            raise ValueError(f"Unknown spread selection: {selection!r}")
        if margin > 0:
            return "win"
        elif margin < 0:
            return "loss"
        else:
            return "push"
    elif market_type == "total":
        total = home_score + away_score
        if selection == "over":
            if total > line:
                return "win"
            elif total < line:
                return "loss"
            else:
                return "push"
        elif selection == "under":
            if total < line:
                return "win"
            elif total > line:
                return "loss"
            else:
                return "push"
        else:
            raise ValueError(f"Unknown total selection: {selection!r}")
    raise ValueError(f"Unknown market type: {market_type}")
