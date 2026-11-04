import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from oddsiq.db.models import Game, PlacedBet, ValueBet
from oddsiq.notifications.telegram import (
    format_bet_attempt_failed,
    format_bet_placed,
    format_bet_result,
    format_bet_skipped_not_logged_in,
    format_betting_disarmed,
    format_login_lost,
    format_login_restored,
    is_configured,
    send_telegram,
)


@pytest.fixture(autouse=True)
def _pin_telegram_recipients(monkeypatch):
    """Hermetic against the local ``.env``.

    ``oddsiq.config`` calls ``load_dotenv()`` at import, so a developer's real
    Telegram tokens (incl. the optional account 2) leak into ``config.*``. Pin
    all four to empty by default; each send/recipient test opts into exactly the
    accounts it needs."""
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN_2", "")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID_2", "")


@pytest.fixture
def game():
    """MLB final, home team won 5–3."""
    return Game(
        external_id="1",
        home_team="Toronto Blue Jays",
        away_team="Boston Red Sox",
        scheduled_at=datetime(2026, 8, 10, 23, 7, tzinfo=timezone.utc),
        status="final",
        home_score=5,
        away_score=3,
        sport="mlb",
        id=1,
    )


@pytest.fixture
def value_bet():
    """A moneyline bet on the home team (Blue Jays)."""
    return ValueBet(
        game_id=1,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="home",
        entry_odds=2.34,
        fair_odds=2.10,
        edge_pct=5.0,
        sport="mlb",
    )


@pytest.fixture
def placed():
    """A real placed bet: 10 Kč at accepted 2.30."""
    return PlacedBet(
        value_bet_id=1,
        game_id=1,
        sport="mlb",
        status="settled",
        stake_czk=10.0,
        requested_odds=2.34,
        accepted_odds=2.30,
        balance_after_czk=490.0,
        receipt_id="T-4711",
    )


# ---------------------------------------------------------------------------
# Bet placed
# ---------------------------------------------------------------------------

def test_format_bet_placed_is_money_first(game, value_bet):
    from oddsiq.pipeline.betting_service import PlacementResult
    result = PlacementResult(status="placed", accepted_odds=2.30,
                             receipt_id="T-4711", balance_czk=490.0)
    text = format_bet_placed(game, value_bet, 10.0, result)
    assert "Bet placed · 10 Kč" in text
    assert "⚾" in text                        # sport emoji cue
    assert "<b>Toronto Blue Jays</b>" in text  # bet team, bold
    assert "to win" in text
    assert "vs Boston Red Sox" in text
    assert "@ 2.34 → 2.30" in text
    assert "returns <b>23 Kč</b>" in text      # 10 * 2.30
    assert "Balance 490 Kč" in text
    # No analytics leak from the old design.
    assert "Edge" not in text and "CLV" not in text and "Tier" not in text


def test_format_bet_placed_omits_balance_when_unknown(game, value_bet):
    from oddsiq.pipeline.betting_service import PlacementResult
    result = PlacementResult(status="placed", accepted_odds=2.30,
                             receipt_id=None, balance_czk=None)
    text = format_bet_placed(game, value_bet, 10.0, result)
    assert "Balance" not in text


def test_format_bet_attempt_failed_rejected(game, value_bet):
    # A rejected attempt is a real-money event (the engine tried to stake and
    # couldn't) — added 2026-08-25 after 6/6 silent offer_gone rejections.
    text = format_bet_attempt_failed(
        game, value_bet, stake_czk=10.0, status="rejected", reason="offer_gone")
    assert "REJECTED" in text
    assert "10 Kč" in text
    assert "offer_gone" in text
    assert "<b>Toronto Blue Jays</b>" in text
    assert "@ 2.34" in text                    # requested entry odds


def test_format_bet_attempt_failed_failed(game, value_bet):
    text = format_bet_attempt_failed(
        game, value_bet, stake_czk=10.0, status="failed", reason="ws send failed")
    assert "FAILED" in text and "ws send failed" in text


def test_format_bet_attempt_failed_handles_missing_reason(game, value_bet):
    text = format_bet_attempt_failed(
        game, value_bet, stake_czk=10.0, status="rejected", reason=None)
    assert "unknown" in text


def test_format_bet_placed_nfl_emoji(game, value_bet):
    # NFL joined 2026-08-25 — the pick line carries the 🏈 cue.
    from oddsiq.pipeline.betting_service import PlacementResult
    game.sport = "nfl"
    game.home_team = "Kansas City Chiefs"
    game.away_team = "Buffalo Bills"
    result = PlacementResult(status="placed", accepted_odds=1.55,
                             receipt_id=None, balance_czk=None)
    text = format_bet_placed(game, value_bet, 10.0, result)
    assert "\U0001f3c8" in text
    assert "<b>Kansas City Chiefs</b>" in text


# ---------------------------------------------------------------------------
# Won / lost
# ---------------------------------------------------------------------------

def test_format_bet_result_win_shows_profit_payout_and_total(game, value_bet, placed):
    value_bet.result = "win"  # home won 5–3, bet was home
    text = format_bet_result(game, value_bet, placed=placed, total_czk=45.0)
    assert "WON +13 Kč" in text            # 10 * (2.30 - 1)
    assert "<b>Toronto Blue Jays</b> 5 – 3 Boston Red Sox" in text
    assert "10 Kč @ 2.30 → 23 Kč back" in text
    assert "Total: +45 Kč" in text
    assert "✅" in text


def test_format_bet_result_loss_shows_negative_and_no_payout(game, value_bet, placed):
    game.home_score, game.away_score = 3, 5   # home (bet) lost
    value_bet.result = "loss"
    text = format_bet_result(game, value_bet, placed=placed, total_czk=35.0)
    assert "LOST -10 Kč" in text
    assert "<b>Toronto Blue Jays</b> 3 – 5 Boston Red Sox" in text
    assert "back" not in text               # no payout line on a loss
    assert "Total: +35 Kč" in text
    assert "❌" in text


def test_format_bet_result_push_is_zero(game, value_bet, placed):
    value_bet.result = "push"
    text = format_bet_result(game, value_bet, placed=placed, total_czk=0.0)
    assert "PUSH 0 Kč" in text
    assert "Total: 0 Kč" in text


def test_format_bet_result_uses_accepted_odds_not_entry(game, value_bet, placed):
    """Profit must use the real accepted price (2.30), not the detected 2.34."""
    value_bet.result = "win"
    placed.accepted_odds = 2.30
    value_bet.entry_odds = 2.34
    text = format_bet_result(game, value_bet, placed=placed, total_czk=13.0)
    assert "+13 Kč" in text          # 10*(2.30-1), NOT 10*(2.34-1)=13.4→+13? guard exact
    assert "@ 2.30" in text and "2.34" not in text


def test_format_bet_result_without_scores_falls_back_to_vs(value_bet, placed):
    game = Game(external_id="2", home_team="A Team", away_team="B Team",
                scheduled_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                status="final", sport="mlb", id=1)
    value_bet.result = "win"
    text = format_bet_result(game, value_bet, placed=placed, total_czk=13.0)
    assert "<b>A Team</b> vs B Team" in text   # no score line available


# ---------------------------------------------------------------------------
# Auto-disarm (slim safety alert)
# ---------------------------------------------------------------------------

def test_format_betting_disarmed_is_slim_and_humanizes_reason():
    text = format_betting_disarmed("consecutive_failures")
    assert "Auto-betting stopped" in text
    assert "2 consecutive failed placements" in text   # humanized, not the slug
    assert "re-arm" in text


def test_format_betting_disarmed_falls_back_to_slug_text():
    text = format_betting_disarmed("some_new_reason")
    assert "some new reason" in text


# ---------------------------------------------------------------------------
# Login lost / restored / not-logged-in skip (2026-08-30)
# ---------------------------------------------------------------------------

def test_format_login_lost_names_the_fix():
    text = format_login_lost()
    assert "login LOST" in text
    assert "paralyzed" in text
    assert "VNC" in text


def test_format_login_restored_is_the_all_clear():
    text = format_login_restored()
    assert "login restored" in text
    assert "operational" in text


def test_format_bet_skipped_not_logged_in_shows_the_missed_bet(game, value_bet):
    text = format_bet_skipped_not_logged_in(game, value_bet)
    assert "SKIPPED" in text and "not logged in" in text
    assert "Toronto Blue Jays" in text and "Boston Red Sox" in text
    assert "2.34" in text
    assert "VNC" in text


# ---------------------------------------------------------------------------
# HTML safety
# ---------------------------------------------------------------------------

def test_formatters_escape_html(value_bet, placed):
    from oddsiq.pipeline.betting_service import PlacementResult
    g = Game(external_id="3", home_team="A & B", away_team="C <script>",
             scheduled_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
             status="final", home_score=1, away_score=0, sport="mlb", id=1)
    value_bet.result = "win"
    placed_text = format_bet_placed(
        g, value_bet, 10.0,
        PlacementResult(status="placed", accepted_odds=2.0, receipt_id=None, balance_czk=None))
    result_text = format_bet_result(g, value_bet, placed=placed, total_czk=10.0)
    for text in (placed_text, result_text):
        assert "&amp;" in text
        assert "<script>" not in text


# ---------------------------------------------------------------------------
# Send infrastructure (unchanged behavior)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_telegram_disabled_when_no_token(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "12345")
    mock_client = AsyncMock()
    monkeypatch.setattr("oddsiq.notifications.telegram._client", mock_client)
    await send_telegram("test message")
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_telegram_sends_message(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "chat456")
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    monkeypatch.setattr("oddsiq.notifications.telegram._client", mock_client)
    await send_telegram("hello")
    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.call_args
    assert "tok123" in call_args.args[0]
    payload = call_args.kwargs["json"]
    assert payload["chat_id"] == "chat456"
    assert payload["text"] == "hello"
    assert payload["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_send_telegram_logs_success_for_audit(monkeypatch, caplog):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "chat456")
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    monkeypatch.setattr("oddsiq.notifications.telegram._client", mock_client)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="oddsiq.notifications.telegram"):
        await send_telegram("hello")
    sent = [r for r in caplog.records if "telegram_sent" in r.getMessage()]
    assert len(sent) == 1
    assert "chat_id=chat456" in sent[0].getMessage()


@pytest.mark.asyncio
async def test_send_telegram_does_not_log_success_on_non_200(monkeypatch, caplog):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "chat456")
    mock_resp = AsyncMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    monkeypatch.setattr("oddsiq.notifications.telegram._client", mock_client)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="oddsiq.notifications.telegram"):
        await send_telegram("hello")
    assert not [r for r in caplog.records if "telegram_sent" in r.getMessage()]


@pytest.mark.asyncio
async def test_send_telegram_swallows_errors(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "id")
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("network down"))
    monkeypatch.setattr("oddsiq.notifications.telegram._client", mock_client)
    await send_telegram("test")  # must not raise


@pytest.mark.asyncio
async def test_send_telegram_truncates_long_messages(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "id")
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    monkeypatch.setattr("oddsiq.notifications.telegram._client", mock_client)
    await send_telegram("x" * 5000)
    payload = mock_client.post.call_args.kwargs["json"]
    assert len(payload["text"]) <= 4096
    assert payload["text"].endswith("...")


@pytest.mark.asyncio
async def test_send_telegram_mirrors_to_both_accounts(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "tok1")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "chat1")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN_2", "tok2")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID_2", "chat2")
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    monkeypatch.setattr("oddsiq.notifications.telegram._client", mock_client)
    await send_telegram("hello both")
    assert mock_client.post.await_count == 2
    sent = [(c.args[0], c.kwargs["json"]["chat_id"]) for c in mock_client.post.call_args_list]
    assert any("tok1" in url and chat == "chat1" for url, chat in sent)
    assert any("tok2" in url and chat == "chat2" for url, chat in sent)


@pytest.mark.asyncio
async def test_send_telegram_second_account_independent(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN_2", "tok2")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID_2", "chat2")
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    monkeypatch.setattr("oddsiq.notifications.telegram._client", mock_client)
    await send_telegram("hi #2")
    mock_client.post.assert_awaited_once()
    assert "tok2" in mock_client.post.call_args.args[0]


@pytest.mark.asyncio
async def test_send_telegram_first_account_failure_does_not_block_second(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "tok1")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "chat1")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN_2", "tok2")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID_2", "chat2")
    ok_resp = AsyncMock()
    ok_resp.status_code = 200

    async def post(url, json):  # noqa: A002
        if "tok1" in url:
            raise Exception("account 1 down")
        return ok_resp

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=post)
    monkeypatch.setattr("oddsiq.notifications.telegram._client", mock_client)
    await send_telegram("resilient")
    assert mock_client.post.await_count == 2
    assert any("tok2" in c.args[0] for c in mock_client.post.call_args_list)


def test_is_configured_true_when_only_first_account_set(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "tok1")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID", "chat1")
    assert is_configured() is True


def test_is_configured_true_when_only_second_account_set(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN_2", "tok2")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_CHAT_ID_2", "chat2")
    assert is_configured() is True


def test_is_configured_false_when_no_account_fully_set(monkeypatch):
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN", "tok1")
    monkeypatch.setattr("oddsiq.config.TELEGRAM_BOT_TOKEN_2", "tok2")
    assert is_configured() is False


def test_truncate_does_not_split_html_tag():
    from oddsiq.notifications.telegram import _truncate_for_telegram, _TELEGRAM_MAX_LENGTH
    text = "a" * (_TELEGRAM_MAX_LENGTH - 4) + "<b>xx</b>"
    assert len(text) > _TELEGRAM_MAX_LENGTH
    out = _truncate_for_telegram(text)
    assert len(out) <= _TELEGRAM_MAX_LENGTH
    assert out.rfind("<") <= out.rfind(">")


def test_truncate_closes_dangling_bold_tag():
    from oddsiq.notifications.telegram import _truncate_for_telegram, _TELEGRAM_MAX_LENGTH
    text = "<b>" + "a" * _TELEGRAM_MAX_LENGTH
    out = _truncate_for_telegram(text)
    assert len(out) <= _TELEGRAM_MAX_LENGTH
    assert out.count("<b>") == out.count("</b>")
    assert out.endswith("...</b>")
