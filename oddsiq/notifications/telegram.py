"""One-way Telegram notifications — bettor-facing only.

Deliberately narrow: a bet was placed, a placed bet won/lost, a real-money
attempt that didn't stick, the safety alert when the engine stops itself, and
(2026-08-30) Tipsport login lost/restored plus the not-logged-in skip — an
armed engine with a dead login is a silent outage, not an ops detail (the
Aug 28 session expiry silently burned the only clean bet in a week 32h
later). All the evaluation-mode analytics (value-bet detections, CLV, live
edges, session summaries) and operational alerts (coverage gaps, budget,
backups, health pings) were removed 2026-08-10 — liveness/ops live on the
dashboard + the dead-man ping, not the phone.
"""

from __future__ import annotations

import logging
from html import escape
from typing import TYPE_CHECKING

import httpx

from oddsiq import config

if TYPE_CHECKING:
    from oddsiq.db.models import Game, PlacedBet, ValueBet

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LENGTH = 4096
_client: httpx.AsyncClient | None = None


def _truncate_for_telegram(text: str) -> str:
    """Truncate to Telegram's 4096-char limit without producing invalid HTML.

    Messages use parse_mode=HTML, so the kept prefix must (1) not end inside a
    ``<...>`` tag and (2) not leave an unclosed ``<b>`` — either makes Telegram
    reject the whole message. We back the cut up before a dangling ``<``, then
    append ``</b>`` if a bold span was left open. Room for the ``...`` and a
    possible ``</b>`` is reserved up front so the result still fits the limit.
    (Dynamic content is already html.escape()'d, so the only raw ``<`` chars are
    real tags, and messages only ever use a single ``<b>...</b>``.)
    """
    if len(text) <= _TELEGRAM_MAX_LENGTH:
        return text
    ellipsis, close = "...", "</b>"
    cut = _TELEGRAM_MAX_LENGTH - len(ellipsis) - len(close)
    last_open = text.rfind("<", 0, cut)
    last_close = text.rfind(">", 0, cut)
    if last_open > last_close:  # kept prefix would end inside an open tag
        cut = last_open
    kept = text[:cut]
    suffix = ellipsis
    if kept.count("<b>") > kept.count("</b>"):  # bold left open by the cut
        suffix += close
    return kept + suffix


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


async def close() -> None:
    """Shut down the httpx client. Call on pipeline shutdown."""
    global _client
    if _client:
        await _client.aclose()
        _client = None


def _recipients() -> list[tuple[str, str]]:
    """Every fully-configured ``(bot_token, chat_id)`` recipient.

    Account 2 (``*_2``) is fully independent of account 1 — either, both, or
    neither may be set. An account counts only when BOTH its token and chat id
    are present, so a half-filled pair is silently ignored.
    """
    recipients: list[tuple[str, str]] = []
    for token, chat_id in (
        (config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID),
        (config.TELEGRAM_BOT_TOKEN_2, config.TELEGRAM_CHAT_ID_2),
    ):
        if token and chat_id:
            recipients.append((token, chat_id))
    return recipients


def is_configured() -> bool:
    """True when at least one Telegram recipient is fully configured."""
    return bool(_recipients())


async def send_telegram(text: str) -> None:
    """Best-effort inline send to every configured Telegram recipient.

    Mirrors the same message to account 1 and (if set) account 2. Silently
    returns if no recipient is configured. Uses the shared httpx client with a
    5s timeout and never raises — each recipient is sent independently so a
    failure to one (network error or non-200) is logged and does not block the
    other.
    """
    recipients = _recipients()
    if not recipients:
        return

    # Telegram enforces a 4096-char limit per message
    text = _truncate_for_telegram(text)
    client = _get_client()

    for token, chat_id in recipients:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "Telegram API returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
            else:
                # Delivery audit trail — every real-money notification must be
                # verifiable from logs after the fact.
                logger.info("telegram_sent: chat_id=%s chars=%d", chat_id, len(text))
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Message formatters — pure functions, no side effects. Money-first and
# minimal; exact figures live on the dashboard.
# ---------------------------------------------------------------------------

# Sport emoji cue for the pick line: ⚾ MLB / 🏀 NBA·WNBA / 🏒 NHL / 🏈 NFL.
_SPORT_EMOJI: dict[str, str] = {
    "nba": "\U0001f3c0",
    "nhl": "\U0001f3d2",
    "mlb": "⚾",
    "wnba": "\U0001f3c0",
    "nfl": "\U0001f3c8",
}

# Result -> (emoji, past-tense label) for the win/loss header.
_RESULT_LABELS: dict[str, tuple[str, str]] = {
    "win": ("✅", "WON"),
    "loss": ("❌", "LOST"),
    "push": ("➖", "PUSH"),
    "draw": ("\U0001f91d", "DRAW"),
}

# Auto-disarm reason slug -> readable phone text.
_DISARM_REASONS: dict[str, str] = {
    "consecutive_failures": "2 consecutive failed placements",
    "unknown_placement": "a placement came back unknown",
    "placement_timeout": "a placement timed out",
    "daily_loss_cap": "hit the daily loss cap",
}


def _sport_emoji(sport: str) -> str:
    return _SPORT_EMOJI.get((sport or "").lower(), "")


def _odds(value: float) -> str:
    return f"{value:.2f}"


def _czk(value: float) -> str:
    """Whole-koruna display for a phone glance, e.g. ``10 Kč``."""
    return f"{round(value)} Kč"


def _signed_czk(value: float) -> str:
    """Signed whole-koruna, e.g. ``+13 Kč`` / ``-10 Kč``; zero renders ``0 Kč``."""
    r = round(value)
    return "0 Kč" if r == 0 else f"{r:+d} Kč"


def _bet_team(game: Game, selection: str) -> str:
    """The team backed for a home/away pick, else the title-cased selection."""
    s = (selection or "").lower()
    if s == "home":
        return game.home_team
    if s == "away":
        return game.away_team
    return (selection or "").title()


def _placed_odds(placed: PlacedBet, vb: ValueBet) -> float:
    """Accepted price if known, else requested, else the detected entry."""
    return placed.accepted_odds or placed.requested_odds or vb.entry_odds


def format_bet_placed(game: Game, vb: ValueBet, stake_czk: float, result) -> str:
    """Real-money placement confirmation. ``result`` is a betting_service
    PlacementResult (duck-typed to avoid an import cycle)."""
    emoji = _sport_emoji(game.sport)
    team = escape(_bet_team(game, vb.selection))
    is_home = (vb.selection or "").lower() == "home"
    opp = escape(game.away_team if is_home else game.home_team)
    pick = (
        "to win"
        if vb.market_type == "moneyline"
        else escape((vb.selection or "").title())
    )
    odds = result.accepted_odds if result.accepted_odds is not None else vb.entry_odds
    lines = [
        f"\U0001f3ab <b>Bet placed · {_czk(stake_czk)}</b>",
        f"{emoji} <b>{team}</b> {pick} — vs {opp}",
    ]
    if odds:
        if (
            result.accepted_odds is not None
            and abs(result.accepted_odds - vb.entry_odds) >= 0.005
        ):
            price = f"@ {_odds(vb.entry_odds)} → {_odds(result.accepted_odds)}"
        else:
            price = f"@ {_odds(odds)}"
        lines.append(f"{price} · returns <b>{_czk(stake_czk * odds)}</b>")
    if result.balance_czk is not None:
        lines.append(f"\U0001f4b0 Balance {_czk(result.balance_czk)}")
    return "\n".join(lines)


def format_bet_attempt_failed(
    game: Game, vb: ValueBet, *, stake_czk: float, status: str, reason: str | None
) -> str:
    """A real-money attempt that didn't stick (rejected/failed). Slim, so a
    streak of these is visible from the phone without opening the dashboard —
    the August 2026 lesson (6/6 silent offer_gone rejections). ``skipped``
    qualification decisions deliberately do NOT alert (they're not attempts)
    — except ``not_logged_in`` (see ``format_bet_skipped_not_logged_in``)."""
    emoji = _sport_emoji(game.sport)
    team = escape(_bet_team(game, vb.selection))
    is_home = (vb.selection or "").lower() == "home"
    opp = escape(game.away_team if is_home else game.home_team)
    head = "⚠️ <b>Bet REJECTED</b>" if status == "rejected" else "❗ <b>Bet FAILED</b>"
    return "\n".join(
        [
            f"{head} · {_czk(stake_czk)} not placed",
            f"{emoji} <b>{team}</b> vs {opp} @ {_odds(vb.entry_odds)}",
            f"Reason: {escape(reason or 'unknown')}",
        ]
    )


def format_bet_result(
    game: Game, vb: ValueBet, *, placed: PlacedBet, total_czk: float
) -> str:
    """Real-money win/loss for a PLACED bet. Kč P&L from the actual stake +
    accepted odds; ``total_czk`` is cumulative realized P&L across placed bets."""
    r = (vb.result or "").lower()
    emoji, label = _RESULT_LABELS.get(r, ("ℹ️", (vb.result or "?").upper()))
    stake = placed.stake_czk or 0.0
    odds = _placed_odds(placed, vb)
    if r == "win":
        profit = stake * (odds - 1.0)
    elif r == "loss":
        profit = -stake
    else:  # push / draw -> stake returned
        profit = 0.0
    sport = _sport_emoji(game.sport)
    team = escape(_bet_team(game, vb.selection))
    is_home = (vb.selection or "").lower() == "home"
    opp = escape(game.away_team if is_home else game.home_team)
    bet_score = game.home_score if is_home else game.away_score
    opp_score = game.away_score if is_home else game.home_score
    lines = [f"{emoji} <b>{escape(label)} {_signed_czk(profit)}</b>"]
    if bet_score is not None and opp_score is not None:
        lines.append(f"{sport} <b>{team}</b> {bet_score} – {opp_score} {opp}")
    else:
        lines.append(f"{sport} <b>{team}</b> vs {opp}")
    if r == "win":
        lines.append(f"{_czk(stake)} @ {_odds(odds)} → {_czk(stake * odds)} back")
    else:
        lines.append(f"{_czk(stake)} @ {_odds(odds)}")
    lines.append(f"Total: {_signed_czk(total_czk)}")
    return "\n".join(lines)


def format_betting_disarmed(reason: str) -> str:
    """Slim safety alert: the engine stopped itself (circuit breaker / loss cap)."""
    human = _DISARM_REASONS.get(reason, (reason or "").replace("_", " "))
    return (
        f"\U0001f6d1 <b>Auto-betting stopped</b>\n"
        f"Reason: {escape(human)}\n"
        f"No more bets until you re-arm."
    )


def format_login_lost() -> str:
    """The extension confirmed the Tipsport session died (it reloads the tab
    and re-probes before reporting, so this is a REAL loss, not a stale tab).
    Fired by the bridge on the logged-in -> logged-out transition."""
    return (
        "\U0001f510 <b>Tipsport login LOST</b>\n"
        "Auto-betting is paralyzed — bets will be skipped, not placed.\n"
        "Re-login via VNC to resume."
    )


def format_login_restored() -> str:
    """Paired all-clear for ``format_login_lost`` — sent only after a loss
    alert actually went out, so the phone never sees an unpaired restore."""
    return (
        "\U0001f513 <b>Tipsport login restored</b>\nAuto-betting is operational again."
    )


def format_bet_skipped_not_logged_in(game: Game, vb: ValueBet) -> str:
    """A qualified bet was skipped ONLY because the session is logged out —
    money was on the table. The one skip reason that alerts (rate-limited by
    the caller): unlike every other skip, this one is an outage the operator
    can fix in two minutes, and the bridge-transition alert alone misses a
    session that was already dead at pipeline startup."""
    emoji = _sport_emoji(game.sport)
    team = escape(_bet_team(game, vb.selection))
    is_home = (vb.selection or "").lower() == "home"
    opp = escape(game.away_team if is_home else game.home_team)
    return "\n".join(
        [
            "\U0001f510 <b>Bet SKIPPED — not logged in</b>",
            f"{emoji} <b>{team}</b> vs {opp} @ {_odds(vb.entry_odds)}",
            "Re-login via VNC; opportunities are being missed.",
        ]
    )
