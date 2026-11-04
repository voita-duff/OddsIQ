"""Store layer for the auto-betting decision ledger (`placed_bets`).

Covers CRUD, the schema-level double-bet guard surfacing as ``None`` from
``insert_placed_bet``, the day/open aggregates the caps consume, settlement
P&L, and the zombie-delete cascade including the new table.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from oddsiq.db import migrations
from oddsiq.db.models import Game, PlacedBet, ValueBet
from oddsiq.db.store import Store


@pytest_asyncio.fixture
async def store(tmp_path):
    db = tmp_path / "t.db"
    migrations.run(db)
    s = Store(db)
    await s.initialize()
    yield s
    await s.close()


_seed_n = 0


async def _seed_vb(store, *, sport="mlb") -> tuple[int, int]:
    """Seed a game + clean hot value bet; returns (game_id, value_bet_id)."""
    global _seed_n
    _seed_n += 1
    gid = await store.upsert_game(
        Game(
            external_id=f"bet-{_seed_n}",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            status="scheduled",
            sport=sport,
        )
    )
    vb_id = await store.insert_value_bet(
        ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=2.1,
            fair_odds=2.0,
            edge_pct=5.0,
            tier="hot",
            sport=sport,
        )
    )
    return gid, vb_id


@pytest.mark.asyncio
async def test_placed_bet_roundtrip_and_double_decision_guard(store):
    gid, vb_id = await _seed_vb(store)
    pb_id = await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb_id,
            game_id=gid,
            sport="mlb",
            status="pending",
            stake_czk=20.0,
            requested_odds=2.1,
            min_odds=2.06,
            commanded_at=datetime.now(timezone.utc),
            latency_detect_ms=1500,
        )
    )
    assert pb_id is not None
    # Second decision for the same value bet -> None (schema UNIQUE), never a raise.
    dup = await store.insert_placed_bet(
        PlacedBet(value_bet_id=vb_id, game_id=gid, sport="mlb", status="pending")
    )
    assert dup is None

    pb = await store.get_placed_bet_for_value_bet(vb_id)
    assert pb is not None and pb.status == "pending"
    assert pb.stake_czk == 20.0 and pb.min_odds == 2.06

    await store.update_placed_bet_result(
        pb_id,
        status="placed",
        accepted_odds=2.08,
        receipt_id="R123",
        balance_after_czk=980.0,
        latency_place_ms=900,
    )
    pb = await store.get_placed_bet_for_value_bet(vb_id)
    assert pb.status == "placed" and pb.accepted_odds == 2.08
    assert pb.receipt_id == "R123" and pb.balance_after_czk == 980.0
    assert pb.latency_place_ms == 900
    assert pb.resolved_at is not None


@pytest.mark.asyncio
async def test_betting_day_stats_counts_and_open_stakes(store):
    day = datetime.now(timezone.utc).date().isoformat()
    # skipped rows never count as commanded bets
    g1, v1 = await _seed_vb(store)
    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=v1,
            game_id=g1,
            sport="mlb",
            status="skipped",
            skip_reason="disarmed",
        )
    )
    # a placed (open) bet counts + carries open stake
    g2, v2 = await _seed_vb(store)
    pb2 = await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=v2, game_id=g2, sport="mlb", status="placed", stake_czk=50.0
        )
    )
    # a settled loss counts toward today's realized P&L, releases open stake
    g3, v3 = await _seed_vb(store)
    pb3 = await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=v3,
            game_id=g3,
            sport="mlb",
            status="placed",
            stake_czk=100.0,
            accepted_odds=2.0,
        )
    )
    await store.settle_placed_bet(v3, "loss")

    stats = await store.betting_day_stats(day)
    assert stats["bets_commanded"] == 2  # skipped excluded
    assert stats["open_stakes_czk"] == 50.0  # settled bet released
    assert stats["realized_pl_czk"] == -100.0  # the lost stake
    assert pb2 is not None and pb3 is not None


@pytest.mark.asyncio
async def test_settle_placed_bet_win_and_push(store):
    g1, v1 = await _seed_vb(store)
    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=v1,
            game_id=g1,
            sport="mlb",
            status="placed",
            stake_czk=100.0,
            accepted_odds=2.08,
        )
    )
    await store.settle_placed_bet(v1, "win")
    pb = await store.get_placed_bet_for_value_bet(v1)
    assert pb.status == "settled"
    assert pb.settled_pl_czk == pytest.approx(108.0)  # 100 * (2.08 - 1)

    # push/draw returns the stake -> P&L 0
    g2, v2 = await _seed_vb(store)
    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=v2,
            game_id=g2,
            sport="mlb",
            status="placed",
            stake_czk=100.0,
            accepted_odds=2.0,
        )
    )
    await store.settle_placed_bet(v2, "draw")
    pb2 = await store.get_placed_bet_for_value_bet(v2)
    assert pb2.status == "settled" and pb2.settled_pl_czk == 0.0

    # settlement only touches 'placed' rows: a skipped decision stays skipped
    g3, v3 = await _seed_vb(store)
    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=v3,
            game_id=g3,
            sport="mlb",
            status="skipped",
            skip_reason="disarmed",
        )
    )
    await store.settle_placed_bet(v3, "win")
    pb3 = await store.get_placed_bet_for_value_bet(v3)
    assert pb3.status == "skipped" and pb3.settled_pl_czk is None


@pytest.mark.asyncio
async def test_settle_win_falls_back_to_requested_odds_when_receipt_omits_odds(store):
    gid, vb_id = await _seed_vb(store)
    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb_id,
            game_id=gid,
            sport="mlb",
            status="placed",
            stake_czk=100.0,
            requested_odds=2.1,
            accepted_odds=None,
        )
    )
    await store.settle_placed_bet(vb_id, "win")
    pb = await store.get_placed_bet_for_value_bet(vb_id)
    assert pb.status == "settled"
    assert pb.settled_pl_czk == pytest.approx(110.0)
    assert "settled_using_requested_odds" in pb.error


@pytest.mark.asyncio
async def test_reconcile_only_resolves_ambiguous_rows_with_evidence(store):
    gid, vb_id = await _seed_vb(store)
    pb_id = await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb_id,
            game_id=gid,
            sport="mlb",
            status="unknown",
            stake_czk=20.0,
            requested_odds=2.1,
        )
    )
    with pytest.raises(ValueError, match="receipt"):
        await store.reconcile_placed_bet(
            pb_id,
            status="placed",
            accepted_odds=2.08,
        )
    await store.update_value_bet_result(vb_id, "win")
    assert (
        await store.reconcile_placed_bet(
            pb_id,
            status="placed",
            accepted_odds=2.08,
            receipt_id="T123",
            reason="verified_in_tipsport_history",
        )
        is True
    )
    pb = await store.get_placed_bet(pb_id)
    assert pb.status == "settled" and pb.receipt_id == "T123"
    assert pb.settled_pl_czk == pytest.approx(21.6)
    assert (
        await store.reconcile_placed_bet(
            pb_id,
            status="rejected",
            reason="not found",
        )
        is False
    )


@pytest.mark.asyncio
async def test_reconcile_appends_to_the_error_audit_trail(store):
    """The pending/unknown row's `error` is the original placement evidence —
    half the money audit trail. Reconciliation APPENDS the operator's finding
    to it rather than overwriting it."""
    gid, vb_id = await _seed_vb(store)
    pb_id = await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb_id,
            game_id=gid,
            sport="mlb",
            status="unknown",
            stake_czk=20.0,
            requested_odds=2.1,
            error="placement_timeout",
        )
    )
    assert (
        await store.reconcile_placed_bet(
            pb_id,
            status="rejected",
            reason="ticket 123 in history",
        )
        is True
    )
    pb = await store.get_placed_bet(pb_id)
    assert pb.status == "rejected"
    assert "placement_timeout" in pb.error  # original evidence kept
    assert "ticket 123 in history" in pb.error  # operator finding appended
    assert pb.error == "placement_timeout; ticket 123 in history"


@pytest.mark.asyncio
async def test_reconcile_seeds_the_error_trail_when_the_row_had_none(store):
    gid, vb_id = await _seed_vb(store)
    pb_id = await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb_id,
            game_id=gid,
            sport="mlb",
            status="pending",
            stake_czk=20.0,
            requested_odds=2.1,
        )
    )
    assert (
        await store.reconcile_placed_bet(
            pb_id,
            status="rejected",
            reason="no such ticket",
        )
        is True
    )
    pb = await store.get_placed_bet(pb_id)
    assert pb.error == "no such ticket"

    # A blank/whitespace reason (allowed on the placed path) must not blank
    # the audit note — the default marker is written instead.
    gid2, vb2_id = await _seed_vb(store)  # value_bet_id is UNIQUE per decision
    pb2_id = await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb2_id,
            game_id=gid2,
            sport="mlb",
            status="unknown",
            stake_czk=20.0,
            requested_odds=2.1,
        )
    )
    assert (
        await store.reconcile_placed_bet(
            pb2_id,
            status="placed",
            accepted_odds=2.05,
            receipt_id="T-1",
            reason="   ",
        )
        is True
    )
    assert (await store.get_placed_bet(pb2_id)).error == "operator_reconciled_placed"


@pytest.mark.asyncio
async def test_delete_games_with_children_cascades_placed_bets(store):
    gid, vb_id = await _seed_vb(store)
    await store.insert_placed_bet(
        PlacedBet(
            value_bet_id=vb_id,
            game_id=gid,
            sport="mlb",
            status="placed",
            stake_czk=20.0,
        )
    )
    counts = await store.delete_games_with_children([gid])
    assert counts["placed_bets"] == 1
    assert counts["value_bets"] == 1
    assert counts["games"] == 1
    assert await store.get_placed_bet_for_value_bet(vb_id) is None
