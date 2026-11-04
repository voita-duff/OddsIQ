from __future__ import annotations

import logging
from oddsiq.db.models import OddsSnapshot
from oddsiq.engine.vig import remove_vig
from oddsiq import config

logger = logging.getLogger(__name__)


def calculate_edge(czech_odds: float, fair_odds: float) -> float:
    return (czech_odds / fair_odds) - 1.0


def find_edges(
    pinnacle_odds: list[OddsSnapshot],
    czech_odds: list[OddsSnapshot],
    threshold: float | None = None,
) -> list[dict]:
    if threshold is None:
        threshold = config.EDGE_THRESHOLD

    # Group Pinnacle odds by (market_type, abs(line)) so spread home=-4.5
    # and away=+4.5 end up in the same group for vig removal.
    pin_by_market: dict[tuple[str, float | None], dict[str, OddsSnapshot]] = {}
    for snap in pinnacle_odds:
        norm_line = abs(snap.line) if snap.line is not None else None
        key = (snap.market_type, norm_line)
        if key not in pin_by_market:
            pin_by_market[key] = {}
        pin_by_market[key][snap.selection] = snap

    edges = []
    for czech_snap in czech_odds:
        norm_line = abs(czech_snap.line) if czech_snap.line is not None else None
        market_key = (czech_snap.market_type, norm_line)
        pin_pair = pin_by_market.get(market_key)
        if not pin_pair:
            if (
                czech_snap.market_type in ("spread", "total")
                and czech_snap.line is not None
            ):
                logger.debug(
                    "Line mismatch: %s %s line=%s (no Pinnacle match)",
                    czech_snap.market_type,
                    czech_snap.selection,
                    czech_snap.line,
                )
            continue

        if czech_snap.market_type == "total":
            side_a, side_b = "over", "under"
        else:
            side_a, side_b = "home", "away"

        if side_a not in pin_pair or side_b not in pin_pair:
            continue

        if czech_snap.market_type == "spread":
            pin_same_side = pin_pair.get(czech_snap.selection)
            if pin_same_side is None or pin_same_side.line != czech_snap.line:
                logger.debug(
                    "Spread side/line mismatch: %s line=%s (Pinnacle line=%s)",
                    czech_snap.selection,
                    czech_snap.line,
                    pin_same_side.line if pin_same_side else None,
                )
                continue

        pin_sides = (pin_pair[side_a], pin_pair[side_b])
        fair_a, fair_b = remove_vig(pin_sides[0].odds, pin_sides[1].odds)
        fair_map = {side_a: fair_a, side_b: fair_b}
        fair = fair_map.get(czech_snap.selection)
        if fair is None:
            continue

        edge = calculate_edge(czech_snap.odds, fair)
        if edge >= threshold:
            edges.append(
                {
                    "game_id": czech_snap.game_id,
                    "bookmaker": czech_snap.bookmaker,
                    "market_type": czech_snap.market_type,
                    "selection": czech_snap.selection,
                    "line": czech_snap.line,
                    "czech_odds": czech_snap.odds,
                    "fair_odds": fair,
                    "edge": edge,
                    # Exact evidence behind this fair price. captured_at answers
                    # when OddsIQ observed the pair; source_updated_at answers
                    # when Pinnacle/The Odds API last refreshed it. The minimum
                    # is conservative when the two outcomes carry different
                    # timestamps. Parser-marked missing freshness fails closed;
                    # hand-built legacy/test snapshots use captured_at.
                    "sharp_observed_at": min(s.captured_at for s in pin_sides),
                    "sharp_source_updated_at": (
                        None
                        if any(s._source_freshness_missing for s in pin_sides)
                        else min(
                            (s.source_updated_at or s.captured_at) for s in pin_sides
                        )
                    ),
                    "sharp_source_unknown": any(
                        s._source_freshness_missing for s in pin_sides
                    ),
                    # Execution identity: transient parser fields (None for
                    # sources that don't carry them) — persisted on the value
                    # bet by EdgeService (migration 009).
                    "tipsport_match_id": czech_snap._tipsport_match_id,
                    "tipsport_opp_id": czech_snap._tipsport_opp_id,
                    "tipsport_url": czech_snap._tipsport_url,
                }
            )

    return edges
