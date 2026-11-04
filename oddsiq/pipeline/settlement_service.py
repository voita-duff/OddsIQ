"""Settlement: pregame CLV, bet result resolution, and one-shot historical
recovery. Extracted verbatim from the old Scheduler.

Preserved invariants: remove_vig() arg order (bet selection passed FIRST so
fair_sel is the wanted side); mark_closing_snapshots freshness; CLV uses the
opposite-signed Pinnacle line for spreads. (Live-candidate settlement was
removed with the live-edge subsystem, 2026-08-27.)
"""

from __future__ import annotations

import logging

from oddsiq import config
from oddsiq.db.models import Game, ValueBet
from oddsiq.engine.clv import calculate_clv, resolve_bet_result
from oddsiq.engine.vig import remove_vig
from oddsiq.notifications import telegram
from oddsiq.pipeline._console import console_timestamp, game_id_label
from oddsiq.pipeline._helpers import (
    log_value as _log_value,
    opposite_selection as _opposite_selection,
)
from oddsiq.pipeline.tiers import SHADOW_TIER

logger = logging.getLogger(__name__)


class SettlementService:
    def __init__(self, store, stats) -> None:
        self._store = store
        self._stats = stats

    async def handle_game_live(self, game: Game) -> None:
        game.status = "live"
        logger.info(
            "game_live: game_id=%d away=%s home=%s",
            game.id,
            _log_value(game.away_team),
            _log_value(game.home_team),
        )
        await self._store.update_game_status(game.id, "live")
        await self._store.mark_closing_snapshots(
            game.id, config.CLOSING_FRESHNESS_MINUTES
        )
        await self.calculate_clv_for_pending_bets(game)

    async def recover_historical_bets(self) -> None:
        """One-shot startup pass: replay recovery for final games that have
        value_bets missing CLV or result.

        The normal recovery path (`recover_finalized_game` triggered by
        `ScheduleService.refresh`) only sees games inside the Odds API 3-day scores
        window. Once a game ages past that, any stuck bet — CLV-less because
        the P1 closing bug shadowed the pre-tipoff snapshot, or result-less
        because the pipeline was down during the final transition — never
        gets revisited. This pass handles those.
        """
        games = await self._store.get_final_games_with_incomplete_bets()
        if games:
            logger.info(
                "Backfilling %d historical game(s) with incomplete bets", len(games)
            )
            for game in games:
                await self.recover_finalized_game(game)

    async def recover_finalized_game(self, game: Game) -> None:
        """Recover CLV/result work for a game found final during schedule refresh.

        This is safe to run on every refresh: CLV is skipped for bets that
        already have it, and result resolution reads only result IS NULL bets.
        """
        await self._store.mark_closing_snapshots(
            game.id, config.CLOSING_FRESHNESS_MINUTES
        )
        await self.calculate_clv_for_pending_bets(game)
        await self.resolve_bets(game)

    async def calculate_clv_for_pending_bets(self, game: Game) -> None:
        # Use the CLV-specific query — `get_pending_value_bets` filters
        # `result IS NULL` and therefore cannot reach a bet whose result was
        # set before its CLV could be computed (e.g. when the closing snapshot
        # was missing at the time of resolution and later became available
        # via the freshness-aware mark_closing_snapshots filter).
        pending = await self._store.get_value_bets_without_clv(game.id)
        for vb in pending:
            closing_snap = await self._store.get_closing_odds(
                game.id,
                vb.bookmaker,
                vb.market_type,
                vb.selection,
                vb.line,
            )
            # Get both sides of the Pinnacle closing market for vig removal.
            # The bet selection uses the exact bet line; spread opposites use
            # the opposite signed line (home -4.5 pairs with away +4.5). If
            # Pinnacle doesn't have the exact pair, CLV is skipped.
            opposite = _opposite_selection(vb.selection)
            opposite_line = self._opposite_line(vb.market_type, vb.line)
            pin_sel = await self._store.get_closing_odds(
                game.id,
                "pinnacle",
                vb.market_type,
                vb.selection,
                vb.line,
            )
            pin_opp = await self._store.get_closing_odds(
                game.id,
                "pinnacle",
                vb.market_type,
                opposite,
                opposite_line,
            )
            if closing_snap and pin_sel and pin_opp:
                # Explicit mapping: remove_vig returns (fair_sel, fair_opp) in argument order.
                # By always passing the bet's selection first, fair_sel is the one we need.
                fair_sel, _fair_opp = remove_vig(pin_sel.odds, pin_opp.odds)
                closing_fair = fair_sel
                clv = calculate_clv(vb.entry_odds, closing_fair)
                await self._store.update_value_bet_closing(
                    vb.id, closing_snap.odds, closing_fair, clv * 100
                )
                # Update in-memory object so Telegram formatter sees real values
                vb.clv_pct = clv * 100
                vb.pinnacle_closing = closing_fair
                if vb.excluded_reason is not None:
                    # Stamped suspect at detection (never alerted as a VALUE
                    # BET) — settlement mirrors that: CLV is written above so
                    # the guard stays validatable, but no session stats and
                    # no CLV UPDATE Telegram.
                    # `tier` rides along so a row that is BOTH shadow and
                    # guard-stamped stays grep-visible as shadow (the suspect
                    # branch wins the `continue`, so no clv_shadow: line is
                    # emitted for it).
                    logger.info(
                        "clv_suspect: bookmaker=%s game_id=%d market=%s "
                        "selection=%s line=%s clv_pct=%.2f reason=%s tier=%s",
                        vb.bookmaker,
                        vb.game_id,
                        vb.market_type,
                        vb.selection,
                        "" if vb.line is None else f"{vb.line}",
                        clv * 100,
                        vb.excluded_reason,
                        vb.tier,
                    )
                    continue
                if vb.tier == SHADOW_TIER:
                    # Far-tier SHADOW row (SportProfile.shadow_far_detection;
                    # NFL). CLV is written above — measuring it IS the
                    # experiment — but it is not a bettable value bet, so it
                    # stays out of the session summary exactly as it does on
                    # the detection side (EdgeService's SHADOW_TIER carve-out).
                    # Without this the shutdown CLV line silently blends the
                    # week-out shadow experiment into the warm/hot number.
                    logger.info(
                        "clv_shadow: bookmaker=%s game_id=%d market=%s "
                        "selection=%s line=%s clv_pct=%.2f",
                        vb.bookmaker,
                        vb.game_id,
                        vb.market_type,
                        vb.selection,
                        "" if vb.line is None else f"{vb.line}",
                        clv * 100,
                    )
                    continue
                self._stats.total_clv += clv
                self._stats.clv_count += 1
                book = vb.bookmaker
                self._stats.total_clv_per_book[book] = (
                    self._stats.total_clv_per_book.get(book, 0.0) + clv
                )
                self._stats.clv_count_per_book[book] = (
                    self._stats.clv_count_per_book.get(book, 0) + 1
                )
                logger.info(
                    "clv: bookmaker=%s game_id=%d market=%s selection=%s line=%s clv_pct=%.2f",
                    vb.bookmaker,
                    vb.game_id,
                    vb.market_type,
                    vb.selection,
                    "" if vb.line is None else f"{vb.line}",
                    clv * 100,
                )
                # CLV is recorded for evaluation, never messaged — the phone only
                # sees real-money placed/won/lost (2026-08-10 notification rebrand).

    async def resolve_bets(self, game: Game) -> None:
        # No SHADOW_TIER carve-out here (unlike CLV above): result resolution
        # has no SessionStats side effects at all, and the far-shadow review
        # reads exactly these settled rows.
        if game.home_score is None or game.away_score is None:
            return
        pending = await self._store.get_pending_value_bets(game.id)
        for vb in pending:
            result = resolve_bet_result(
                vb.market_type,
                vb.selection,
                vb.line,
                game.home_score,
                game.away_score,
            )
            await self._store.update_value_bet_result(vb.id, result)
            # Settle the linked auto-betting decision (real-money P&L) — a
            # no-op unless a 'placed' row exists for this value bet.
            await self._store.settle_placed_bet(vb.id, result)
            vb.result = result
            if vb.excluded_reason is not None:
                # Suspect bet — result written above (guard validation), but
                # no console print and no WIN/LOSS Telegram, mirroring the
                # suppressed detection alert.
                logger.info(
                    "result_suspect: bookmaker=%s game_id=%d market=%s "
                    "selection=%s line=%s result=%s reason=%s tier=%s",
                    vb.bookmaker,
                    game.id,
                    vb.market_type,
                    vb.selection,
                    "" if vb.line is None else f"{vb.line}",
                    result,
                    vb.excluded_reason,
                    vb.tier,
                )
                continue
            self._print_result(game, vb, result)
            logger.info(
                "result: bookmaker=%s game_id=%d market=%s selection=%s line=%s "
                "result=%s clv_pct=%s",
                vb.bookmaker,
                game.id,
                vb.market_type,
                vb.selection,
                "" if vb.line is None else f"{vb.line}",
                result,
                "" if vb.clv_pct is None else f"{vb.clv_pct:.2f}",
            )
            # WIN/LOSS Telegram fires ONLY for a real placed bet (a
            # `placed`->`settled` decision), with real-Kč P&L + running total.
            # Paper value-bet settlements notify nothing (2026-08-10 rebrand).
            placed = await self._store.get_placed_bet_for_value_bet(vb.id)
            if placed is not None and placed.status == "settled":
                total = await self._store.cumulative_realized_pl_czk()
                await telegram.send_telegram(
                    telegram.format_bet_result(game, vb, placed=placed, total_czk=total)
                )

    @staticmethod
    def _opposite_line(market_type: str, line: float | None) -> float | None:
        if market_type == "spread" and line is not None:
            return -line
        return line

    @staticmethod
    def _print_result(game: Game, vb: ValueBet, result: str) -> None:
        now = console_timestamp()
        clv_str = f" (CLV: {vb.clv_pct:+.1f}%)" if vb.clv_pct is not None else ""
        print(
            f"[{now}] RESULT: game_id={game_id_label(game)} | "
            f"{game.away_team} @ {game.home_team} | "
            f"{game.away_score}-{game.home_score} | "
            f"{vb.bookmaker} {vb.market_type} {vb.selection} "
            f"({vb.tier} tier): {result.upper()}{clv_str}"
        )
