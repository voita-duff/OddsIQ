"""Pregame edge detection: compare the soft book's moneyline vs Pinnacle fair,
write value_bets, update SessionStats (bettable tiers only), and RETURN the
clean detections as pending bets for the coordinator to dispatch to the
betting engine (the two-phase handoff, 2026-09-06). Detections themselves are
never alerted — since the 2026-08-10 rebrand only real-money events notify.
(The engine's spread/total comparison paths are latent — the system is
moneyline-only in practice.) Extracted from the old monolithic scheduler's
pregame poll block.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import NamedTuple

from oddsiq import config
from oddsiq.db.models import Game, OddsSnapshot, ValueBet
from oddsiq.engine.edge import find_edges
from oddsiq.pipeline._console import console_timestamp, game_id_label
from oddsiq.pipeline.tiers import SHADOW_TIER

logger = logging.getLogger(__name__)


class PendingBet(NamedTuple):
    """A clean detection awaiting the betting engine."""

    game: Game
    vb: ValueBet
    pinnacle_fresh_at: datetime
    pinnacle_source_updated_at: datetime | None


def _dispatch_order(pb: PendingBet) -> tuple[datetime, float]:
    """Earliest kickoff first, biggest edge as the tiebreak. Placements are
    serial and each bet has a hard deadline (the tipoff buffer), so a bet with
    an hour of slack must not spend the last window of one that is two minutes
    out; within one kickoff wave this is exactly edge order."""
    return (pb.game.commence_time or pb.game.scheduled_at, -pb.vb.edge_pct)


class EdgeService:
    def __init__(self, store, stats, betting=None) -> None:
        self._store = store
        self._stats = stats
        # Optional auto-betting engine (BettingService). None = detection-only
        # (the default; every pre-P1 behavior is unchanged).
        self._betting = betting

    async def detect_pregame_edges(
        self,
        game: Game,
        game_snapshots: dict[str, list[OddsSnapshot]],
        tier: str,
    ) -> list[PendingBet]:
        """For a pregame game with Pinnacle present, flag +EV soft-book lines,
        stamp them with the given polling tier (opaque to this service), and
        RETURN the clean inserts as pending bets. The betting handoff is a
        separate phase (``dispatch_pending``) so the coordinator can order a
        whole poll cycle's detections by edge and qualify each at dispatch
        time — the NFL Sunday-burst rule (2026-09-06)."""
        pending: list[PendingBet] = []
        pinnacle_odds = game_snapshots.get("pinnacle", [])
        if not pinnacle_odds:
            return pending
        for book_name, book_odds in game_snapshots.items():
            if book_name == "pinnacle":
                continue
            edges = find_edges(pinnacle_odds, book_odds)
            for edge_info in edges:
                # `tier` is part of the value_bets unique key — a warm
                # detection and a later hot detection of the same
                # selection get independent rows (and independent CLV).
                vb = ValueBet(
                    game_id=game.id,
                    bookmaker=edge_info["bookmaker"],
                    market_type=edge_info["market_type"],
                    selection=edge_info["selection"],
                    line=edge_info["line"],
                    entry_odds=edge_info["czech_odds"],
                    fair_odds=edge_info["fair_odds"],
                    edge_pct=edge_info["edge"] * 100,
                    tier=tier,
                    sport=game.sport,
                    excluded_reason=_suspect_reason(edge_info, book_odds),
                    tipsport_match_id=edge_info["tipsport_match_id"],
                    tipsport_opp_id=edge_info["tipsport_opp_id"],
                    tipsport_url=edge_info["tipsport_url"],
                    sharp_source_updated_at=edge_info["sharp_source_updated_at"],
                )
                bet_id = await self._store.insert_value_bet(vb)

                # insert_value_bet returns None if bet already exists (ON CONFLICT DO NOTHING).
                # This survives restarts — no in-memory set needed.
                if bet_id is None:
                    continue
                # The betting engine keys its decision row on this id — a
                # None id would silently no-op the whole betting path.
                vb.id = bet_id

                if vb.excluded_reason is not None:
                    # Stale-price artifact: recorded (settlement keeps
                    # validating the guard) but hidden from reporting, stats,
                    # and alerts. See PREGAME_MAX_PLAUSIBLE_EDGE /
                    # PREGAME_MIN_BOOK_OVERROUND in config.py.
                    self._print_value_bet(game, vb, suspect=True)
                    logger.info(
                        "value_bet_suspect: bookmaker=%s game_id=%d market=%s selection=%s "
                        "edge_pct=%.2f entry_odds=%.2f fair_odds=%.2f tier=%s reason=%s",
                        vb.bookmaker,
                        game.id,
                        vb.market_type,
                        vb.selection,
                        vb.edge_pct,
                        vb.entry_odds,
                        vb.fair_odds,
                        vb.tier,
                        vb.excluded_reason,
                    )
                    continue

                # Shadow (far-tier) rows are recorded and CLV'd but are not
                # bettable value bets, so they stay out of the session summary.
                if tier != SHADOW_TIER:
                    book = vb.bookmaker
                    self._stats.value_bets_flagged += 1
                    self._stats.total_edge += edge_info["edge"]
                    self._stats.value_bets_per_book[book] = (
                        self._stats.value_bets_per_book.get(book, 0) + 1
                    )
                    self._stats.total_edge_per_book[book] = (
                        self._stats.total_edge_per_book.get(book, 0.0)
                        + edge_info["edge"]
                    )
                self._print_value_bet(game, vb)
                logger.info(
                    "value_bet: bookmaker=%s game_id=%d market=%s selection=%s line=%s "
                    "edge_pct=%.2f entry_odds=%.2f fair_odds=%.2f tier=%s",
                    vb.bookmaker,
                    game.id,
                    vb.market_type,
                    vb.selection,
                    "" if vb.line is None else f"{vb.line}",
                    vb.edge_pct,
                    vb.entry_odds,
                    vb.fair_odds,
                    vb.tier,
                )
                # Detections are recorded + logged but no longer alerted to the
                # phone (2026-08-10 rebrand — only real placed/won/lost notify).
                # Auto-betting: every clean bet becomes a pending bet; the
                # coordinator dispatches the cycle's pending bets afterwards
                # (see dispatch_pending). Suspect bets exited above.
                pending.append(
                    PendingBet(
                        game,
                        vb,
                        edge_info["sharp_observed_at"],
                        edge_info["sharp_source_updated_at"],
                    )
                )
        return pending

    async def dispatch_pending(self, pending: list[PendingBet]) -> None:
        """Hand a poll cycle's clean detections to the betting engine, earliest
        kickoff first and largest edge as the tiebreak, one at a time.
        Placements are serial (the bet slip is a singleton), so on a
        nine-kickoff NFL Sunday the wave closest to kickoff gets the freshest
        shot; BettingService re-qualifies each bet on its own clock (tipoff
        buffer, fair age, caps) when its turn comes."""
        if self._betting is None or not pending:
            return
        for pb in sorted(pending, key=_dispatch_order):
            try:
                await self._betting.on_value_bet(
                    pb.game,
                    pb.vb,
                    pinnacle_fresh_at=pb.pinnacle_fresh_at,
                    pinnacle_source_updated_at=pb.pinnacle_source_updated_at,
                )
            except Exception:
                # A stranded bet is UNRECOVERABLE: its value_bets row exists, so
                # the next cycle's re-detection dedups away and it is never
                # re-pended for this tier. One bad dispatch must not take the
                # rest of a kickoff wave with it. (CancelledError is a
                # BaseException and still propagates — shutdown aborts the wave.)
                logger.exception(
                    "bet_dispatch_failed: value_bet_id=%s game_id=%s",
                    pb.vb.id,
                    pb.game.id,
                )

    def _print_value_bet(self, game: Game, vb: ValueBet, suspect: bool = False) -> None:
        now = console_timestamp()
        label = "SUSPECT VALUE BET" if suspect else "VALUE BET"
        reason = f" | {vb.excluded_reason}" if suspect else ""
        print(
            f"[{now}] {label}: {vb.bookmaker} | "
            f"game_id={game_id_label(game)} | "
            f"{game.away_team} @ {game.home_team} | "
            f"{vb.tier.title()} tier | "
            f"{vb.market_type.title()} {vb.selection.title()} | "
            f"{vb.entry_odds:.2f} (fair: {vb.fair_odds:.2f}) | "
            f"edge: {vb.edge_pct:.1f}%{reason}"
        )


def _suspect_reason(edge_info: dict, book_odds: list[OddsSnapshot]) -> str | None:
    """Classify a detected edge as a stale-price artifact, or None if clean.

    Five guards:
    - in-play: the Tipsport match entry sits under /live/ — the offer is
      already in-play (Tipsport flips at first pitch, sometimes before the
      schedule clock crosses commence_time), so a "pregame" edge against it
      is a live-boundary mirage and its prematch odd button no longer exists
      (2026-08-26/27 forensics: all three clean /live/-url detections were
      commanded and all died offer_gone). Checked first — it is the root
      cause the other guards were catching indirectly (76 of the 86
      /live/-url detections had been stamped incoherent_book_pregame).
    - sharp timestamp: missing/invalid upstream ``last_update`` fails closed;
      a known timestamp older than the configured cap is a feed stall even
      when the HTTP response arrived now.
    - plausibility cap (calibrated 2026-07-10): a pregame edge >=
      PREGAME_MAX_PLAUSIBLE_EDGE doesn't happen against a sharp fair on a
      fresh price — it's the soft book not having moved yet. CLV cannot
      catch these (it grades the same stale price), so the guard must live
      at detection time.
    - coherence floor: if the soft book's own two-way implied sum is below
      PREGAME_MIN_BOOK_OVERROUND, the book is arbing against itself — one
      side must be stale, so no edge from that market is trustworthy.
    - completeness: a Tipsport listing with an execution id must carry both
      sides; otherwise neither fair comparison nor self-coherence is enough
      evidence that the offer is actionable. Gated by
      PREGAME_REQUIRE_COMPLETE_BOOK_MARKET (default on) so it has its own
      off-switch — PREGAME_MIN_BOOK_OVERROUND<=0 disables only the floor.
    """
    if (edge_info.get("tipsport_url") or "").startswith("/live/"):
        return "in_play_pregame"
    if edge_info.get("sharp_source_unknown"):
        return "sharp_source_unknown"
    source_at = edge_info.get("sharp_source_updated_at")
    observed_at = edge_info.get("sharp_observed_at")
    max_source_age = config.PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS
    if (
        max_source_age > 0
        and source_at is not None
        and observed_at is not None
        and (observed_at - source_at).total_seconds() > max_source_age
    ):
        return "stale_sharp_source"
    cap = config.PREGAME_MAX_PLAUSIBLE_EDGE
    if cap > 0 and edge_info["edge"] >= cap:
        return "implausible_edge_pregame"
    floor = config.PREGAME_MIN_BOOK_OVERROUND
    require_complete = (
        config.PREGAME_REQUIRE_COMPLETE_BOOK_MARKET
        and edge_info.get("tipsport_match_id") is not None
    )
    if floor > 0 or require_complete:
        implied_sum = _book_two_way_implied_sum(
            book_odds,
            edge_info["market_type"],
            edge_info["line"],
            tipsport_match_id=edge_info.get("tipsport_match_id"),
        )
        if implied_sum is None and require_complete:
            return "incomplete_book_market"
        if implied_sum is not None and implied_sum < floor:
            return "incoherent_book_pregame"
    return None


def _book_two_way_implied_sum(
    book_odds: list[OddsSnapshot],
    market_type: str,
    line: float | None,
    *,
    tipsport_match_id: int | None = None,
) -> float | None:
    """Sum of 1/odds over the soft book's two sides of one market, or None
    when both sides aren't present. Lines are matched by abs() like
    find_edges groups them, so spread home -4.5 / away +4.5 pair up.

    When the edge carries a Tipsport match id, only snapshots from that SAME
    listing pair up — with two listings for one game in a batch (the wrong-game
    ghost scenario), mixing sides across listings manufactured a coherent-
    looking sum for an incoherent listing and silently defeated this guard
    (2026-08-30). Snapshot attribution now keeps ghost listings off the game
    entirely, so this is defense-in-depth."""
    sides = ("over", "under") if market_type == "total" else ("home", "away")
    norm_line = abs(line) if line is not None else None
    seen: dict[str, float] = {}
    for snap in book_odds:
        if (
            tipsport_match_id is not None
            and snap._tipsport_match_id != tipsport_match_id
        ):
            continue
        snap_line = abs(snap.line) if snap.line is not None else None
        if (
            snap.market_type == market_type
            and snap_line == norm_line
            and snap.selection in sides
        ):
            seen[snap.selection] = snap.odds
    if len(seen) != 2:
        return None
    return sum(1.0 / odds for odds in seen.values())
