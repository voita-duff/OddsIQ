import logging
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from oddsiq.db import migrations
from oddsiq.db.models import Game, OddsSnapshot, PlacedBet, ValueBet
from oddsiq.db.store import Store
from oddsiq.pipeline.settlement_service import SettlementService
from oddsiq.pipeline.stats import SessionStats


@pytest.fixture(autouse=True)
def _silence_real_telegram(monkeypatch):
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", AsyncMock())


@pytest_asyncio.fixture
async def store(tmp_path):
    db = tmp_path / "t.db"
    migrations.run(db)
    s = Store(db)
    await s.initialize()
    yield s
    await s.close()


def test_print_result_uses_away_home_score_order(capsys):
    game = Game(
        external_id="nba_result",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=datetime.now(timezone.utc),
        status="final",
        home_score=112,
        away_score=105,
    )
    bet = ValueBet(
        game_id=1,
        bookmaker="tipsport",
        market_type="moneyline",
        selection="away",
        entry_odds=2.10,
        fair_odds=1.90,
        edge_pct=4.2,
    )

    SettlementService._print_result(game, bet, "loss")

    out = capsys.readouterr().out
    assert "Los Angeles Lakers @ Boston Celtics | 105-112" in out
    assert "Los Angeles Lakers @ Boston Celtics | 112-105" not in out
    assert "tipsport moneyline away (hot tier): LOSS" in out


@pytest.mark.asyncio
async def test_calculate_clv_backfills_already_resolved_bet(store: Store):
    """A bet with result already set but clv_pct=NULL must be backfilled.

    Regression for the audit's P2 finding: the previous query filtered
    `result IS NULL`, so a bet whose closing snapshot was missing at
    resolution time (then later marked closing by the freshness-aware fix)
    would be stuck without CLV forever.
    """
    svc = SettlementService(store=store, stats=SessionStats())
    tipoff = datetime.now(timezone.utc) - timedelta(hours=2)
    game = Game(
        external_id="nba_resolved_no_clv",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
        status="final",
        home_score=112,
        away_score=105,
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(ValueBet(
        game_id=game.id, bookmaker="tipsport", market_type="moneyline",
        selection="home", entry_odds=2.10, fair_odds=2.00, edge_pct=5.0,
    ))
    # Mark the bet as already resolved without CLV — simulates the stuck state.
    await store._db.execute(
        "UPDATE value_bets SET result='win' WHERE game_id=?", (game.id,),
    )
    captured_at = tipoff - timedelta(minutes=2)
    await store.insert_odds_snapshots_batch([
        OddsSnapshot(
            game_id=game.id, bookmaker="tipsport", market_type="moneyline",
            selection="home", odds=2.05, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.91, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="away", odds=1.91, captured_at=captured_at,
        ),
    ])
    await store.mark_closing_snapshots(game.id, freshness_minutes=30)

    await svc.calculate_clv_for_pending_bets(game)

    cursor = await store._db.execute(
        "SELECT clv_pct, result FROM value_bets WHERE game_id=?",
        (game.id,),
    )
    row = await cursor.fetchone()
    assert row["clv_pct"] is not None, "CLV must be backfilled on the resolved bet"
    assert row["result"] == "win", "existing result must be preserved"


@pytest.mark.asyncio
async def test_clv_arg_order_pinned_by_asymmetric_favorite_pair(store: Store):
    """The remove_vig arg order at the settlement call site is load-bearing — the
    BET side must be passed FIRST. With a symmetric 1.91/1.91 pair a swap is
    invisible, so use an asymmetric pair on the FAVORITE: a swap flips CLV from
    small-positive to strongly negative."""
    from oddsiq.engine.vig import remove_vig
    from oddsiq.engine.clv import calculate_clv

    svc = SettlementService(store=store, stats=SessionStats())
    tipoff = datetime.now(timezone.utc) - timedelta(hours=2)
    game = Game(
        external_id="nba_clv_asym",
        home_team="Boston Celtics", away_team="Los Angeles Lakers",
        scheduled_at=tipoff, status="final", home_score=112, away_score=105,
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(ValueBet(
        game_id=game.id, bookmaker="tipsport", market_type="moneyline",
        selection="home", entry_odds=1.30, fair_odds=1.27, edge_pct=2.0,
    ))
    await store._db.execute(
        "UPDATE value_bets SET result='win' WHERE game_id=?", (game.id,))
    captured_at = tipoff - timedelta(minutes=2)
    await store.insert_odds_snapshots_batch([
        OddsSnapshot(game_id=game.id, bookmaker="tipsport", market_type="moneyline",
                     selection="home", odds=1.30, captured_at=captured_at),
        OddsSnapshot(game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
                     selection="home", odds=1.25, captured_at=captured_at),
        OddsSnapshot(game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
                     selection="away", odds=4.50, captured_at=captured_at),
    ])
    await store.mark_closing_snapshots(game.id, freshness_minutes=30)

    await svc.calculate_clv_for_pending_bets(game)

    cursor = await store._db.execute(
        "SELECT clv_pct FROM value_bets WHERE game_id=?", (game.id,))
    clv_pct = (await cursor.fetchone())["clv_pct"]

    # Correct: fair from the bet side (home) first.
    fair_home, _ = remove_vig(1.25, 4.50)
    expected = calculate_clv(1.30, fair_home) * 100
    assert clv_pct == pytest.approx(expected, abs=0.01)
    assert clv_pct > 0  # a swapped remove_vig would yield a strongly negative CLV


@pytest.mark.asyncio
async def test_calculate_clv_for_spread_uses_opposite_signed_pinnacle_line(
    store: Store,
):
    """Spread CLV needs the opposite side at the opposite signed line.

    A home -4.5 bet pairs with Pinnacle home -4.5 and away +4.5. Passing the
    bet line unchanged to the away lookup would miss the pair and leave CLV
    NULL even though the exact market is present.
    """
    svc = SettlementService(store=store, stats=SessionStats())
    tipoff = datetime.now(timezone.utc)
    game = Game(
        external_id="nba_spread_clv",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
        status="live",
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(ValueBet(
        game_id=game.id,
        bookmaker="tipsport",
        market_type="spread",
        selection="home",
        line=-4.5,
        entry_odds=2.10,
        fair_odds=1.95,
        edge_pct=7.7,
    ))
    captured_at = tipoff - timedelta(minutes=2)
    await store.insert_odds_snapshots_batch([
        OddsSnapshot(
            game_id=game.id,
            bookmaker="tipsport",
            market_type="spread",
            selection="home",
            line=-4.5,
            odds=2.05,
            captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id,
            bookmaker="pinnacle",
            market_type="spread",
            selection="home",
            line=-4.5,
            odds=1.91,
            captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id,
            bookmaker="pinnacle",
            market_type="spread",
            selection="away",
            line=4.5,
            odds=1.91,
            captured_at=captured_at,
        ),
    ])
    await store.mark_closing_snapshots(game.id, freshness_minutes=30)

    await svc.calculate_clv_for_pending_bets(game)

    cursor = await store._db.execute(
        "SELECT closing_odds, pinnacle_closing, clv_pct FROM value_bets WHERE game_id=?",
        (game.id,),
    )
    row = await cursor.fetchone()
    assert row["closing_odds"] == 2.05
    assert row["pinnacle_closing"] == pytest.approx(2.0)
    assert row["clv_pct"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_recover_historical_bets_backfills_aged_out_game(
    store: Store, monkeypatch
):
    """Final games older than the 3-day refresh window get a one-shot startup
    backfill — without it, bets stuck before the freshness fix never re-resolve.

    Regression for the audit's other P2 finding (historical repair window).
    """
    svc = SettlementService(store=store, stats=SessionStats())
    # 25 days old — well outside the yesterday/today/tomorrow window.
    tipoff = datetime.now(timezone.utc) - timedelta(days=25)
    game = Game(
        external_id="nba_aged_out",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
        status="final",
        home_score=112,
        away_score=105,
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(ValueBet(
        game_id=game.id, bookmaker="tipsport", market_type="moneyline",
        selection="home", entry_odds=2.10, fair_odds=2.00, edge_pct=5.0,
    ))
    captured_at = tipoff - timedelta(minutes=2)
    await store.insert_odds_snapshots_batch([
        OddsSnapshot(
            game_id=game.id, bookmaker="tipsport", market_type="moneyline",
            selection="home", odds=2.05, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.91, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="away", odds=1.91, captured_at=captured_at,
        ),
    ])

    await svc.recover_historical_bets()

    cursor = await store._db.execute(
        "SELECT clv_pct, result FROM value_bets WHERE game_id=?",
        (game.id,),
    )
    row = await cursor.fetchone()
    assert row["clv_pct"] is not None, "CLV must be backfilled on aged-out game"
    assert row["result"] == "win", "result must be backfilled on aged-out game"


@pytest.mark.asyncio
async def test_clv_emits_structured_log(store: Store, caplog, monkeypatch):
    """CLV log line uses key=value form (clv: bookmaker= game_id= ...)."""
    svc = SettlementService(store=store, stats=SessionStats())
    tipoff = datetime.now(timezone.utc) - timedelta(hours=2)
    game = Game(
        external_id="nba_clv_structured_log",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
        status="final",
        home_score=112,
        away_score=105,
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(ValueBet(
        game_id=game.id, bookmaker="tipsport", market_type="moneyline",
        selection="home", entry_odds=2.10, fair_odds=2.00, edge_pct=5.0,
    ))
    captured_at = tipoff - timedelta(minutes=2)
    await store.insert_odds_snapshots_batch([
        OddsSnapshot(
            game_id=game.id, bookmaker="tipsport", market_type="moneyline",
            selection="home", odds=2.05, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.91, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="away", odds=1.91, captured_at=captured_at,
        ),
    ])
    await store.mark_closing_snapshots(game.id, freshness_minutes=30)

    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.settlement_service")
    await svc.calculate_clv_for_pending_bets(game)

    msgs = [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("clv:")]
    assert len(msgs) >= 1, f"expected at least 1 clv: log line, got: {msgs}"
    line = msgs[0]
    for key in ("bookmaker=", "game_id=", "market=", "selection=", "line=", "clv_pct="):
        assert key in line, f"missing key {key!r} in: {line}"


@pytest.mark.asyncio
async def test_result_emits_structured_log(store, caplog):
    """resolve_bets must emit a structured result: log line for each resolved bet.

    Modeled after test_clv_emits_structured_log: set up a final game with both
    scores populated, insert one pending value_bet (result=None), then call
    resolve_bets directly and assert the log event fires with all expected keys.
    """
    svc = SettlementService(store=store, stats=SessionStats())
    tipoff = datetime.now(timezone.utc) - timedelta(hours=2)
    game = Game(
        external_id="nba_result_log",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
        status="final",
        home_score=112,
        away_score=105,
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(ValueBet(
        game_id=game.id, bookmaker="tipsport", market_type="moneyline",
        selection="home", entry_odds=2.10, fair_odds=2.00, edge_pct=5.0,
    ))

    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.settlement_service")
    await svc.resolve_bets(game)

    msgs = [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("result:")]
    assert len(msgs) >= 1
    line = msgs[0]
    for key in ("bookmaker=", "game_id=", "market=", "selection=", "line=",
                "result=", "clv_pct="):
        assert key in line, f"missing key {key!r} in: {line}"


@pytest.mark.asyncio
async def test_game_live_emits_log(store: Store, caplog):
    """handle_game_live must emit exactly one game_live: log line with game fields."""
    svc = SettlementService(store=store, stats=SessionStats())
    now = datetime.now(timezone.utc)
    game = Game(
        external_id="nba_live_log",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=now - timedelta(seconds=30),
        status="scheduled",
    )
    game.id = await store.upsert_game(game)

    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.settlement_service")
    await svc.handle_game_live(game)

    msgs = [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("game_live:")]
    assert len(msgs) == 1
    line = msgs[0]
    assert f"game_id={game.id}" in line
    assert f'away="{game.away_team}"' in line
    assert f'home="{game.home_team}"' in line


async def _seed_final_game_with_closing(store, *, excluded_reason=None,
                                        external_id="excl_settle", tier="hot"):
    """Final game + one moneyline value bet (optionally excluded, optionally in
    a given tier) with a complete closing pair so CLV is computable."""
    tipoff = datetime.now(timezone.utc) - timedelta(hours=2)
    game = Game(
        external_id=external_id,
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        scheduled_at=tipoff,
        status="final",
        home_score=112,
        away_score=105,
    )
    game.id = await store.upsert_game(game)
    await store.insert_value_bet(ValueBet(
        game_id=game.id, bookmaker="tipsport", market_type="moneyline",
        selection="home", entry_odds=2.10, fair_odds=2.00, edge_pct=5.0,
        excluded_reason=excluded_reason, tier=tier,
    ))
    captured_at = tipoff - timedelta(minutes=2)
    await store.insert_odds_snapshots_batch([
        OddsSnapshot(
            game_id=game.id, bookmaker="tipsport", market_type="moneyline",
            selection="home", odds=2.05, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="home", odds=1.91, captured_at=captured_at,
        ),
        OddsSnapshot(
            game_id=game.id, bookmaker="pinnacle", market_type="moneyline",
            selection="away", odds=1.91, captured_at=captured_at,
        ),
    ])
    await store.mark_closing_snapshots(game.id, freshness_minutes=30)
    return game


@pytest.mark.asyncio
async def test_excluded_bet_clv_written_but_no_telegram_or_stats(
    store: Store, caplog, monkeypatch,
):
    """A bet stamped excluded_reason at detection was never alerted as a
    VALUE BET — settlement must mirror that: CLV is still computed and written
    (the guards stay validatable) but no CLV UPDATE Telegram goes out and the
    session CLV stats stay untouched."""
    sent = AsyncMock()
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", sent)
    stats = SessionStats()
    svc = SettlementService(store=store, stats=stats)
    game = await _seed_final_game_with_closing(
        store, excluded_reason="incoherent_book_pregame", tier="far",
    )

    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.settlement_service")
    await svc.calculate_clv_for_pending_bets(game)

    cursor = await store._db.execute(
        "SELECT clv_pct FROM value_bets WHERE game_id=?", (game.id,),
    )
    row = await cursor.fetchone()
    assert row["clv_pct"] is not None, "CLV must still be written to the DB"
    sent.assert_not_awaited()
    assert stats.clv_count == 0
    assert stats.total_clv == 0.0
    assert stats.total_clv_per_book == {}
    msgs = [r.getMessage() for r in caplog.records]
    susp = [m for m in msgs if m.startswith("clv_suspect:")]
    assert len(susp) == 1 and "tier=far" in susp[0], susp
    assert not any(m.startswith("clv_shadow:") for m in msgs)   # suspect wins the continue
    assert not any(m.startswith("clv:") for m in msgs)


@pytest.mark.asyncio
async def test_excluded_bet_result_written_but_no_telegram(
    store: Store, caplog, monkeypatch,
):
    """Result resolution for an excluded bet writes the result (and settles any
    linked placed bet) but sends no WIN/LOSS Telegram."""
    sent = AsyncMock()
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", sent)
    svc = SettlementService(store=store, stats=SessionStats())
    game = await _seed_final_game_with_closing(
        store, excluded_reason="implausible_edge_pregame",
        external_id="excl_result",
    )

    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.settlement_service")
    await svc.resolve_bets(game)

    cursor = await store._db.execute(
        "SELECT result FROM value_bets WHERE game_id=?", (game.id,),
    )
    row = await cursor.fetchone()
    assert row["result"] == "win", "result must still be written to the DB"
    sent.assert_not_awaited()
    msgs = [r.getMessage() for r in caplog.records]
    susp = [m for m in msgs if m.startswith("result_suspect:")]
    assert len(susp) == 1 and "tier=" in susp[0], susp
    assert not any(m.startswith("result:") for m in msgs)


@pytest.mark.asyncio
async def test_shadow_far_bet_clv_written_but_kept_out_of_session_stats(
    store: Store, caplog, monkeypatch,
):
    """NFL far-tier SHADOW rows (tier='far') exist to be CLV'd — that IS the
    experiment — but they must not blend into the shutdown summary's warm/hot
    CLV number. EdgeService already keeps them out of the detection-side
    counters; settlement is the other half of the same rule."""
    sent = AsyncMock()
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", sent)
    stats = SessionStats()
    svc = SettlementService(store=store, stats=stats)
    game = await _seed_final_game_with_closing(
        store, tier="far", external_id="shadow_clv",
    )

    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.settlement_service")
    caplog.clear()
    await svc.calculate_clv_for_pending_bets(game)

    cursor = await store._db.execute(
        "SELECT clv_pct FROM value_bets WHERE game_id=?", (game.id,),
    )
    row = await cursor.fetchone()
    assert row["clv_pct"] is not None, "shadow CLV must still be written to the DB"
    sent.assert_not_awaited()
    assert stats.clv_count == 0
    assert stats.total_clv == 0.0
    assert stats.total_clv_per_book == {}
    assert stats.clv_count_per_book == {}
    msgs = [r.getMessage() for r in caplog.records]
    shadow = [m for m in msgs if m.startswith("clv_shadow:")]
    assert len(shadow) == 1, msgs
    # The 4-week shadow review greps exactly these fields out of the log.
    assert "clv_pct=" in shadow[0]
    assert "game_id=" in shadow[0]
    assert "selection=" in shadow[0]
    assert not any(m.startswith("clv:") for m in msgs)


@pytest.mark.asyncio
async def test_shadow_far_bet_result_resolves_and_logs_normally(
    store: Store, caplog,
):
    """The shadow carve-out is CLV-stats-only. `resolve_bets` has no session-
    stats side effects at all, so a far-tier row must keep resolving and
    logging `result:` — the 4-week shadow review reads exactly those rows."""
    svc = SettlementService(store=store, stats=SessionStats())
    game = await _seed_final_game_with_closing(
        store, tier="far", external_id="shadow_result",
    )

    caplog.set_level(logging.INFO, logger="oddsiq.pipeline.settlement_service")
    caplog.clear()
    await svc.resolve_bets(game)

    cursor = await store._db.execute(
        "SELECT result FROM value_bets WHERE game_id=?", (game.id,),
    )
    row = await cursor.fetchone()
    assert row["result"] == "win"
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("result:") for m in msgs), msgs


@pytest.mark.asyncio
async def test_clean_paper_bet_settles_silently(store: Store, monkeypatch):
    """A clean (non-excluded) value bet that was NEVER placed (paper) computes
    CLV and resolves in the DB, but sends NO Telegram — CLV alerts were cut and
    win/loss only fires for real placed bets (2026-08-10 rebrand)."""
    sent = AsyncMock()
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", sent)
    svc = SettlementService(store=store, stats=SessionStats())
    game = await _seed_final_game_with_closing(store, external_id="clean_paper")

    await svc.calculate_clv_for_pending_bets(game)
    await svc.resolve_bets(game)

    cursor = await store._db.execute(
        "SELECT clv_pct, result FROM value_bets WHERE game_id=?", (game.id,)
    )
    row = await cursor.fetchone()
    assert row["clv_pct"] is not None   # computed + stored...
    assert row["result"] == "win"       # ...and resolved...
    sent.assert_not_awaited()           # ...but never phoned.


@pytest.mark.asyncio
async def test_placed_bet_sends_one_result_telegram(store: Store, monkeypatch):
    """A REAL placed bet gets exactly one win/loss Telegram at settlement, with
    real-Kč P&L + the cumulative total. This is the only settlement notification
    that survives the rebrand."""
    sent = AsyncMock()
    monkeypatch.setattr("oddsiq.notifications.telegram.send_telegram", sent)
    svc = SettlementService(store=store, stats=SessionStats())
    game = await _seed_final_game_with_closing(store, external_id="placed_settle")
    vb = (await store.get_pending_value_bets(game.id))[0]
    await store.insert_placed_bet(PlacedBet(
        value_bet_id=vb.id, game_id=game.id, sport="nba", status="placed",
        stake_czk=10.0, requested_odds=vb.entry_odds, accepted_odds=2.05,
    ))

    await svc.calculate_clv_for_pending_bets(game)
    await svc.resolve_bets(game)

    assert sent.await_count == 1, "one result Telegram for the placed bet, no CLV"
    text = sent.await_args_list[0].args[0]
    assert "WON" in text and "Total:" in text


