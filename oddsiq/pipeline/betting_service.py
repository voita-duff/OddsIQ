"""BettingService — the auto-betting decision engine (P1 Phase 1).

Invoked synchronously by EdgeService for every CLEAN value-bet insert
(suspect/guard-stamped bets never reach it). Runs an ordered qualification
chain; the first failing rule records a ``skipped`` decision row with its
reason (the dashboard's "why not bet" feed). A qualifying bet gets a
``pending`` row, is dispatched to the executor (the Chrome-extension
WebSocket bridge; mockable), and resolves to ``placed`` / ``rejected`` /
``failed`` / ``unknown``. Both detection tiers (warm AND hot) bet since
2026-08-30; the ``selection_already_open`` rule closes the cross-tier
double-bet (warm + hot detections of one selection are separate rows).

Safety model:
- One decision per value bet, enforced by schema (placed_bets.value_bet_id
  UNIQUE). No retries in Phase 1 — a failed/skipped decision is terminal.
- ``min_odds = fair_odds * (1 + EDGE_THRESHOLD)``: the extension re-checks the
  live price at confirm time and aborts below it (last-line freshness guard).
- Circuit breaker: 2 consecutive placement failures, ANY unknown (timeout)
  placement, or the daily loss cap auto-DISARM the engine (meta
  ``betting_armed=0`` + ``betting_disarm_reason`` + Telegram). Re-arming is
  always a human action (CLI / dashboard).
- Runtime money knobs live in the meta table (dashboard-adjustable):
  ``betting_flat_stake_czk`` (default 20), ``betting_max_bets_per_day`` (10),
  ``betting_max_open_stakes_czk`` (500), ``betting_daily_loss_limit_czk``
  (300), and ``betting_sports`` (CSV sport gate, default ``mlb`` — see
  ``parse_betting_sports``). The env ``BETTING_MAX_STAKE_CZK`` is a hard
  ceiling the meta stake is clamped to.

See docs/Design/2026-08-09-p1-auto-betting-design.md.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from oddsiq import config
from oddsiq.db.models import Game, PlacedBet, ValueBet
from oddsiq.notifications import telegram

logger = logging.getLogger(__name__)


@dataclass
class PlacementResult:
    """Executor outcome for a place_bet command. ``status``: ``placed`` |
    ``rejected`` (a normal outcome — price moved below min_odds / offer gone;
    does NOT trip the breaker) | ``error``."""
    status: str
    accepted_odds: float | None = None
    receipt_id: str | None = None
    balance_czk: float | None = None
    message: str | None = None


def parse_betting_sports(raw: str | None) -> set[str]:
    """Meta ``betting_sports`` (CSV of sport keys) -> the exact set of sports
    the engine may bet. Absent/blank/whitespace-only falls back to ``{"mlb"}``;
    unrecognized tokens are kept as-is and simply match no sport, so a
    corrupted value CLOSES the gate rather than widening it. (Added 2026-08-25
    with NFL; the operator ticks new sports in via the dashboard config form,
    which validates against ENABLED_SPORTS.)"""
    sports = {t.strip().lower() for t in (raw or "").split(",") if t.strip()}
    return sports or {"mlb"}


class PlacementTimeout(Exception):
    """No result from the executor within BETTING_PLACEMENT_TIMEOUT_SECONDS.
    The bet may or may not exist at the book -> status 'unknown' -> disarm."""


class PlacementError(Exception):
    """Executor-side failure with a definitive 'no bet was placed' signal."""


class BettingService:
    # Min spacing between not-logged-in skip alerts — a busy slate can skip
    # several bets in one evening; one phone ping per outage window is enough.
    _NOT_LOGGED_IN_ALERT_INTERVAL_S = 6 * 3600
    # accepted/requested odds above this ratio is not slippage — it's the
    # shape of an ACCUMULATOR (the product of two legs). Slippage in our
    # favour is a few percent at most, so 1.05 clears every honest fill.
    _ACCEPTED_ODDS_ANOMALY_RATIO = 1.05

    def __init__(self, store, executor) -> None:
        # executor contract: `.connected` (bool), `.logged_in` (bool),
        # `async place(cmd: dict) -> PlacementResult` raising
        # PlacementTimeout / PlacementError. The WS bridge implements it;
        # tests use fakes.
        self._store = store
        self._executor = executor
        self._consecutive_failures = 0
        self._last_not_logged_in_alert: datetime | None = None

    async def on_value_bet(
        self,
        game: Game,
        vb: ValueBet,
        pinnacle_fresh_at: datetime,
        pinnacle_source_updated_at: datetime | None = None,
    ) -> None:
        if vb.id is None:
            return
        now = datetime.now(timezone.utc)
        reason = await self._qualify(
            game, vb, pinnacle_fresh_at, now, pinnacle_source_updated_at)
        if reason is not None:
            await self._store.insert_placed_bet(PlacedBet(
                value_bet_id=vb.id, game_id=game.id, sport=vb.sport,
                status="skipped", skip_reason=reason,
            ))
            logger.info("bet_skipped: value_bet_id=%d reason=%s", vb.id, reason)
            if reason == "not_logged_in":
                # The one skip that alerts (2026-08-30): a qualified bet lost
                # purely to a dead session is a fixable outage with money on
                # the table — and it also covers a session already dead at
                # pipeline startup, which the bridge transition alert can't see.
                await self._maybe_alert_not_logged_in(game, vb, now)
            return

        stake = await self._resolve_stake()
        min_odds = round(vb.fair_odds * (1.0 + config.EDGE_THRESHOLD), 3)
        latency_detect_ms = max(0, int((now - vb.flagged_at).total_seconds() * 1000))
        pb_id = await self._store.insert_placed_bet(PlacedBet(
            value_bet_id=vb.id, game_id=game.id, sport=vb.sport,
            status="pending", stake_czk=stake, requested_odds=vb.entry_odds,
            min_odds=min_odds, commanded_at=now,
            latency_detect_ms=latency_detect_ms,
        ))
        if pb_id is None:
            return  # decision already exists (schema double-bet guard)

        cmd = {
            "bet_id": vb.id,
            "match_id": vb.tipsport_match_id,
            "opp_id": vb.tipsport_opp_id,
            "url": vb.tipsport_url,
            "stake_czk": stake,
            "min_odds": min_odds,
        }
        logger.info(
            "bet_command: value_bet_id=%d match_id=%s opp_id=%s stake_czk=%.0f min_odds=%.3f",
            vb.id, vb.tipsport_match_id, vb.tipsport_opp_id, stake, min_odds,
        )
        dispatched_at = datetime.now(timezone.utc)
        try:
            result = await self._executor.place(cmd)
        except PlacementTimeout as e:
            # The bet MAY exist at the book. Freeze everything until a human
            # (or Phase-2 reconciliation) resolves it — a blind retry here is
            # the double-bet failure mode.
            await self._store.update_placed_bet_result(
                pb_id, status="unknown", error=str(e))
            logger.error("bet_unknown: value_bet_id=%d error=%s", vb.id, e)
            await self._auto_disarm("unknown_placement")
            return
        except Exception as e:
            await self._store.update_placed_bet_result(
                pb_id, status="failed", error=str(e))
            logger.error("bet_failed: value_bet_id=%d error=%s", vb.id, e)
            # Breaker BEFORE the alert: the circuit count on the money path
            # must never depend on the notification formatter succeeding.
            await self._register_failure()
            await telegram.send_telegram(telegram.format_bet_attempt_failed(
                game, vb, stake_czk=stake, status="failed", reason=str(e)))
            return

        latency_place_ms = int(
            (datetime.now(timezone.utc) - dispatched_at).total_seconds() * 1000)
        if result.status == "placed":
            self._consecutive_failures = 0
            await self._store.update_placed_bet_result(
                pb_id, status="placed", accepted_odds=result.accepted_odds,
                receipt_id=result.receipt_id,
                balance_after_czk=result.balance_czk,
                latency_place_ms=latency_place_ms,
            )
            logger.info(
                "bet_placed: value_bet_id=%d stake_czk=%.0f accepted_odds=%s "
                "receipt=%s latency_place_ms=%d",
                vb.id, stake, result.accepted_odds, result.receipt_id,
                latency_place_ms,
            )
            self._warn_on_accepted_odds_anomaly(vb, result)
            await telegram.send_telegram(
                telegram.format_bet_placed(game, vb, stake, result))
        elif result.status == "rejected":
            # Normal outcome (price moved / offer gone) — a real-money attempt,
            # so it alerts (2026-08-25: 6/6 silent offer_gone rejections hid a
            # placement bug for a day). Breaker rule: a rejection is NEUTRAL —
            # it neither increments the consecutive-failure count nor resets
            # it. A rejection is a MARKET outcome, not evidence the actuator is
            # healthy, so failed -> rejected -> failed is still two actuator
            # failures and must auto-disarm. Only a `placed` result resets.
            await self._store.update_placed_bet_result(
                pb_id, status="rejected", error=result.message,
                latency_place_ms=latency_place_ms,
            )
            logger.info(
                "bet_rejected: value_bet_id=%d reason=%s", vb.id, result.message)
            await telegram.send_telegram(telegram.format_bet_attempt_failed(
                game, vb, stake_czk=stake, status="rejected", reason=result.message))
        else:
            await self._store.update_placed_bet_result(
                pb_id, status="failed", error=result.message,
                latency_place_ms=latency_place_ms,
            )
            logger.error(
                "bet_failed: value_bet_id=%d error=%s", vb.id, result.message)
            # Breaker before the alert (same rationale as the exception path).
            await self._register_failure()
            await telegram.send_telegram(telegram.format_bet_attempt_failed(
                game, vb, stake_czk=stake, status="failed", reason=result.message))

    def _warn_on_accepted_odds_anomaly(
        self, vb: ValueBet, result: PlacementResult,
    ) -> None:
        """Server-side backstop against a NON-SINGLETON bet slip.

        The extension's in-page min-odds guard reads the slip TOTAL (the
        ``Celkový kurz`` row), so a leftover leg from an earlier attempt would
        submit an ACCUMULATOR whose accepted odds is the product of the legs —
        comfortably ABOVE min_odds, so nothing in the page notices. Only the
        accepted-vs-requested ratio gives it away.

        This never blocks anything: by the time we see the result the money is
        already down, so the row stays ``placed`` and the alert still goes out.
        It exists so the ticket gets eyeballed the same evening rather than at
        settlement, when the P&L would look inexplicable."""
        requested = vb.entry_odds
        accepted = result.accepted_odds
        if accepted is None or not requested or requested <= 0:
            return
        if accepted <= requested * self._ACCEPTED_ODDS_ANOMALY_RATIO:
            return
        logger.warning(
            "accepted_odds_anomaly: value_bet_id=%s requested=%.3f accepted=%.3f "
            "ratio=%.3f — slip may not have been a singleton; verify the ticket "
            "at Tipsport",
            vb.id, requested, accepted, accepted / requested,
        )

    async def armed(self) -> bool:
        return (await self._store.get_meta("betting_armed")) == "1"

    # ------------------------------------------------------------------

    async def _qualify(
        self,
        game: Game,
        vb: ValueBet,
        pinnacle_fresh_at: datetime,
        now: datetime,
        pinnacle_source_updated_at: datetime | None = None,
    ) -> str | None:
        """First failing rule wins; ordering is cheapest-first (the sport gate
        is one meta point-read — kept first so skip-reason semantics stay
        comparable across the 2026-08-25 gate change), DB-aggregate caps last."""
        if vb.sport not in parse_betting_sports(await self._store.get_meta("betting_sports")):
            return "sport_not_enabled"
        if vb.tier not in ("warm", "hot"):
            # Warm-tier betting enabled 2026-08-30 (operator decision): both
            # detection tiers bet — a warm edge often dies before the hot
            # re-detection, and pregame offers 1-2h out are stable (the
            # offer_gone failures all clustered at the tipoff boundary).
            # Detection-time fair is same-cycle fresh in both tiers, and the
            # cross-tier double-bet is closed by selection_already_open below.
            return "tier_not_bettable"
        if not vb.tipsport_match_id or not vb.tipsport_opp_id:
            return "missing_tipsport_ids"
        if not await self.armed():
            return "disarmed"
        if not self._executor.connected:
            return "executor_disconnected"
        if not getattr(self._executor, "logged_in", True):
            return "not_logged_in"
        if (now - pinnacle_fresh_at).total_seconds() > config.BETTING_FAIR_MAX_AGE_SECONDS:
            return "stale_fair"
        # Not a duplicate of the detection-time guard in _suspect_reason: that
        # one measures observed_at - source_at, this one now - source_at, so it
        # adds the dispatch-queue latency on top. Defense in depth — a feed
        # that was fresh enough at detection can age past the cap while the
        # kickoff-ordered dispatch works through the cycle's pending bets.
        source_at = (
            pinnacle_source_updated_at
            or vb.sharp_source_updated_at
            or pinnacle_fresh_at
        )
        max_source_age = config.PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS
        if max_source_age > 0 and (now - source_at).total_seconds() > max_source_age:
            return "stale_sharp_source"
        tipoff = game.commence_time or game.scheduled_at
        if tipoff <= now:
            return "game_started"
        if (tipoff - now).total_seconds() <= config.BETTING_TIPOFF_BUFFER_SECONDS:
            # Tipsport moves the match to its live section around first pitch
            # (observed flipping up to ~30s BEFORE the scheduled time), so a
            # placement started inside this window navigates to a page whose
            # prematch odd button no longer exists -> guaranteed offer_gone.
            return "too_close_to_tipoff"
        if await self._store.has_open_decision_for_selection(game.id, vb.selection):
            # Cross-tier double-bet guard: a warm placement followed by the
            # hot re-detection of the same selection is a NEW value_bets row
            # (tier is in the dedup key) — without this it would bet twice.
            return "selection_already_open"
        stats = await self._store.betting_day_stats(now.date().isoformat())
        max_bets = await self._bounded_meta_int(
            "betting_max_bets_per_day", 10, config.BETTING_MAX_BETS_PER_DAY,
        )
        if stats["bets_commanded"] >= max_bets:
            return "daily_bet_cap"
        stake = await self._resolve_stake()
        if stake <= 0:
            return "invalid_stake"
        max_open = await self._bounded_meta_float(
            "betting_max_open_stakes_czk", 500.0,
            config.BETTING_MAX_OPEN_STAKES_CZK,
        )
        if stats["open_stakes_czk"] + stake > max_open:
            return "open_stakes_cap"
        loss_limit = await self._bounded_meta_float(
            "betting_daily_loss_limit_czk", 300.0,
            config.BETTING_MAX_DAILY_LOSS_CZK,
        )
        if stats["realized_pl_czk"] <= -loss_limit:
            # Hitting the loss cap doesn't just skip this bet — it stands the
            # engine down for the day (failure matrix in the design spec).
            await self._auto_disarm("daily_loss_cap")
            return "daily_loss_cap"
        return None

    async def _maybe_alert_not_logged_in(
        self, game: Game, vb: ValueBet, now: datetime,
    ) -> None:
        last = self._last_not_logged_in_alert
        if last is not None and (
            (now - last).total_seconds() < self._NOT_LOGGED_IN_ALERT_INTERVAL_S
        ):
            return
        self._last_not_logged_in_alert = now
        await telegram.send_telegram(
            telegram.format_bet_skipped_not_logged_in(game, vb))

    async def _resolve_stake(self) -> float:
        return await self._bounded_meta_float(
            "betting_flat_stake_czk", 20.0,
            config.BETTING_MAX_STAKE_CZK,
        )

    async def _register_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= 2:
            await self._auto_disarm("consecutive_failures")

    async def _auto_disarm(self, reason: str) -> None:
        already_disarmed = not await self.armed()
        await self._store.set_meta("betting_armed", "0")
        await self._store.set_meta(
            "betting_disarm_reason",
            f"{reason} @ {datetime.now(timezone.utc).isoformat()}",
        )
        self._consecutive_failures = 0
        logger.warning("betting_disarmed: reason=%s", reason)
        if not already_disarmed:
            await telegram.send_telegram(telegram.format_betting_disarmed(reason))

    async def _meta_float(self, key: str, default: float) -> float:
        raw = await self._store.get_meta(key)
        try:
            value = float(raw) if raw is not None else default
            return value if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default

    async def _meta_int(self, key: str, default: int) -> int:
        raw = await self._store.get_meta(key)
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    async def _bounded_meta_float(
        self, key: str, default: float, maximum: float, *, minimum: float = 0.0,
    ) -> float:
        ceiling = maximum if math.isfinite(maximum) else default
        ceiling = max(minimum, ceiling)
        value = await self._meta_float(key, default)
        return max(minimum, min(value, ceiling))

    async def _bounded_meta_int(
        self, key: str, default: int, maximum: int, *, minimum: int = 0,
    ) -> int:
        ceiling = max(minimum, maximum)
        value = await self._meta_int(key, default)
        return max(minimum, min(value, ceiling))
