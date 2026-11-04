"""BettingService — the auto-betting decision engine (Phase 1, executor mocked).

Every test drives ``on_value_bet`` with a clean hot-tier MLB bet carrying
Tipsport ids and asserts the decision row + executor traffic. The FakeExecutor
implements the same contract the WebSocket bridge exposes.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from oddsiq import config
from oddsiq.db import migrations
from oddsiq.db.models import Game, PlacedBet, ValueBet
from oddsiq.db.store import Store
from oddsiq.pipeline.betting_service import (
    BettingService,
    PlacementError,
    PlacementResult,
    PlacementTimeout,
)


@pytest.fixture(autouse=True)
def _silence_telegram(monkeypatch):
    sent: list[str] = []

    async def _capture(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(
        "oddsiq.notifications.telegram.send_telegram", _capture)
    return sent


@pytest.fixture(autouse=True)
def _pin_betting_env(monkeypatch):
    monkeypatch.setattr(config, "BETTING_MAX_STAKE_CZK", 500.0)
    monkeypatch.setattr(config, "BETTING_MAX_BETS_PER_DAY", 10)
    monkeypatch.setattr(config, "BETTING_MAX_OPEN_STAKES_CZK", 500.0)
    monkeypatch.setattr(config, "BETTING_MAX_DAILY_LOSS_CZK", 300.0)
    monkeypatch.setattr(config, "BETTING_FAIR_MAX_AGE_SECONDS", 120.0)
    monkeypatch.setattr(config, "BETTING_TIPOFF_BUFFER_SECONDS", 120.0)
    monkeypatch.setattr(config, "EDGE_THRESHOLD", 0.03)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = tmp_path / "t.db"
    migrations.run(db)
    s = Store(db)
    await s.initialize()
    yield s
    await s.close()


class FakeExecutor:
    def __init__(self):
        self.connected = True
        self.logged_in = True
        self.commands: list[dict] = []
        self.result = PlacementResult(
            status="placed", accepted_odds=2.08, receipt_id="R1",
            balance_czk=980.0)
        self.exc: Exception | None = None

    async def place(self, cmd: dict) -> PlacementResult:
        self.commands.append(cmd)
        if self.exc is not None:
            raise self.exc
        return self.result


_n = 0


async def _clean_bet(store, *, sport="mlb", tier="hot", with_ids=True,
                     minutes_to_tip=30, entry_odds=2.1):
    global _n
    _n += 1
    now = datetime.now(timezone.utc)
    game = Game(
        external_id=f"bs-{_n}", home_team="New York Yankees",
        away_team="Boston Red Sox",
        scheduled_at=now + timedelta(minutes=minutes_to_tip),
        commence_time=now + timedelta(minutes=minutes_to_tip),
        status="scheduled", sport=sport,
    )
    game.id = await store.upsert_game(game)
    vb = ValueBet(
        game_id=game.id, bookmaker="tipsport", market_type="moneyline",
        selection="home", entry_odds=entry_odds, fair_odds=2.0, edge_pct=5.0,
        tier=tier, sport=sport,
        tipsport_match_id=8109316 if with_ids else None,
        tipsport_opp_id=2624446456 if with_ids else None,
        tipsport_url="/kurzy/zapas/x/8109316" if with_ids else None,
    )
    vb.id = await store.insert_value_bet(vb)
    return game, vb


async def _arm(store):
    await store.set_meta("betting_armed", "1")


@pytest.mark.asyncio
async def test_places_qualifying_bet(store):
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))

    assert len(ex.commands) == 1
    cmd = ex.commands[0]
    assert cmd["match_id"] == 8109316 and cmd["opp_id"] == 2624446456
    assert cmd["stake_czk"] == 20.0                      # default flat stake
    assert cmd["min_odds"] == pytest.approx(2.06)        # fair * (1 + EDGE_THRESHOLD)
    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "placed"
    assert pb.accepted_odds == 2.08 and pb.receipt_id == "R1"
    assert pb.balance_after_czk == 980.0
    assert pb.latency_detect_ms is not None and pb.latency_place_ms is not None


@pytest.mark.asyncio
async def test_placed_bet_sends_telegram(store, _silence_telegram):
    await _arm(store)
    svc = BettingService(store=store, executor=FakeExecutor())
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert any("Bet placed" in t for t in _silence_telegram)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate,expected_reason", [
    ("disarmed", "disarmed"),
    ("sport", "sport_not_enabled"),
    ("tier", "tier_not_bettable"),
    ("no_ids", "missing_tipsport_ids"),
    ("disconnected", "executor_disconnected"),
    ("logged_out", "not_logged_in"),
    ("stale_fair", "stale_fair"),
    ("started", "game_started"),
    ("near_tipoff", "too_close_to_tipoff"),
])
async def test_skip_reasons(store, mutate, expected_reason):
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    kwargs = {}
    fresh_at = datetime.now(timezone.utc)
    if mutate == "disarmed":
        await store.set_meta("betting_armed", "0")
    elif mutate == "sport":
        kwargs["sport"] = "nba"
    elif mutate == "tier":
        # warm BETS since 2026-08-30; only a non-detection tier is blocked
        # (defensive — EdgeService only ever stamps warm/hot).
        kwargs["tier"] = "far"
    elif mutate == "no_ids":
        kwargs["with_ids"] = False
    elif mutate == "disconnected":
        ex.connected = False
    elif mutate == "logged_out":
        ex.logged_in = False
    elif mutate == "stale_fair":
        fresh_at = datetime.now(timezone.utc) - timedelta(seconds=300)
    elif mutate == "started":
        kwargs["minutes_to_tip"] = -5
    elif mutate == "near_tipoff":
        # Inside the tipoff buffer (120s): Tipsport flips the offer to
        # in-play around first pitch, so a placement started now dies
        # offer_gone (2026-08-26/27 forensics — 4 of 7 rejections).
        kwargs["minutes_to_tip"] = 1

    game, vb = await _clean_bet(store, **kwargs)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=fresh_at)
    assert ex.commands == []
    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "skipped" and pb.skip_reason == expected_reason


@pytest.mark.asyncio
async def test_stale_upstream_sharp_timestamp_is_defense_in_depth_skip(store):
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)
    now = datetime.now(timezone.utc)
    await svc.on_value_bet(
        game,
        vb,
        pinnacle_fresh_at=now,
        pinnacle_source_updated_at=now - timedelta(seconds=301),
    )

    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "skipped"
    assert pb.skip_reason == "stale_sharp_source"
    assert ex.commands == []


@pytest.mark.asyncio
async def test_not_logged_in_skip_alerts_rate_limited(store, _silence_telegram):
    # The one skip reason that pages the operator (2026-08-30): a qualified
    # bet lost purely to a dead session — but at most once per window, so a
    # busy slate doesn't turn one outage into five pings.
    await _arm(store)
    ex = FakeExecutor()
    ex.logged_in = False
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert [t for t in _silence_telegram if "not logged in" in t] != []
    game2, vb2 = await _clean_bet(store)
    await svc.on_value_bet(game2, vb2, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert len([t for t in _silence_telegram if "not logged in" in t]) == 1
    # Outside the window it alerts again.
    svc._last_not_logged_in_alert = (
        datetime.now(timezone.utc)
        - timedelta(seconds=BettingService._NOT_LOGGED_IN_ALERT_INTERVAL_S + 1)
    )
    game3, vb3 = await _clean_bet(store)
    await svc.on_value_bet(game3, vb3, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert len([t for t in _silence_telegram if "not logged in" in t]) == 2


@pytest.mark.asyncio
async def test_other_skip_reasons_stay_silent(store, _silence_telegram):
    await _arm(store)
    await store.set_meta("betting_armed", "0")   # -> "disarmed" skip
    svc = BettingService(store=store, executor=FakeExecutor())
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.skip_reason == "disarmed"
    assert _silence_telegram == []


@pytest.mark.asyncio
async def test_betting_sports_gate_defaults_to_mlb(store):
    # Absent meta key -> {"mlb"}: NFL detections are recorded as skipped.
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store, sport="nfl")
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "skipped" and pb.skip_reason == "sport_not_enabled"
    assert ex.commands == []


@pytest.mark.asyncio
async def test_betting_sports_gate_meta_enables_nfl(store):
    await _arm(store)
    await store.set_meta("betting_sports", "mlb,nfl")
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store, sport="nfl")
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "placed"


@pytest.mark.asyncio
async def test_betting_sports_gate_is_an_exact_set(store):
    # "nfl" alone gates MLB OFF — the key is the whole policy, not additive.
    await _arm(store)
    await store.set_meta("betting_sports", "nfl")
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store, sport="mlb")
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "skipped" and pb.skip_reason == "sport_not_enabled"


def test_parse_betting_sports_malformed_falls_back_conservative():
    # A broken meta value must never widen the gate.
    from oddsiq.pipeline.betting_service import parse_betting_sports
    assert parse_betting_sports(None) == {"mlb"}
    assert parse_betting_sports("") == {"mlb"}
    assert parse_betting_sports(" , ,") == {"mlb"}
    assert parse_betting_sports("MLB, Nfl ") == {"mlb", "nfl"}


@pytest.mark.asyncio
async def test_rejected_placement_sends_attempt_alert(store, _silence_telegram):
    # 2026-08-25: rejected/failed attempts alert (the August offer_gone streak
    # went unnoticed for a day). Rejection still doesn't trip the breaker.
    await _arm(store)
    ex = FakeExecutor()
    ex.result = PlacementResult(status="rejected", message="offer_gone")
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert any("REJECTED" in t for t in _silence_telegram)
    assert any("offer_gone" in t for t in _silence_telegram)


@pytest.mark.asyncio
async def test_failed_placement_sends_attempt_alert(store, _silence_telegram):
    await _arm(store)
    ex = FakeExecutor()
    ex.exc = PlacementError("bridge send failed")
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert any("FAILED" in t for t in _silence_telegram)


@pytest.mark.asyncio
async def test_skipped_decision_sends_no_attempt_alert(store, _silence_telegram):
    # Qualification skips are not attempts — they stay log/dashboard-only.
    # (tier="far" since warm BETS as of 2026-08-30; not_logged_in is the one
    # skip that alerts, covered by its own test.)
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store, tier="far")
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert _silence_telegram == []


@pytest.mark.asyncio
async def test_daily_bet_cap(store):
    await _arm(store)
    await store.set_meta("betting_max_bets_per_day", "1")
    svc = BettingService(store=store, executor=FakeExecutor())
    g1, v1 = await _clean_bet(store)
    await svc.on_value_bet(g1, v1, pinnacle_fresh_at=datetime.now(timezone.utc))
    g2, v2 = await _clean_bet(store)
    await svc.on_value_bet(g2, v2, pinnacle_fresh_at=datetime.now(timezone.utc))
    pb2 = await store.get_placed_bet_for_value_bet(v2.id)
    assert pb2.status == "skipped" and pb2.skip_reason == "daily_bet_cap"


@pytest.mark.asyncio
async def test_open_stakes_cap(store):
    await _arm(store)
    await store.set_meta("betting_max_open_stakes_czk", "30")
    svc = BettingService(store=store, executor=FakeExecutor())
    g1, v1 = await _clean_bet(store)
    await svc.on_value_bet(g1, v1, pinnacle_fresh_at=datetime.now(timezone.utc))
    g2, v2 = await _clean_bet(store)   # open 20 + 20 > 30
    await svc.on_value_bet(g2, v2, pinnacle_fresh_at=datetime.now(timezone.utc))
    pb2 = await store.get_placed_bet_for_value_bet(v2.id)
    assert pb2.status == "skipped" and pb2.skip_reason == "open_stakes_cap"


@pytest.mark.asyncio
async def test_daily_loss_cap_skips_and_disarms(store, _silence_telegram):
    await _arm(store)
    # Seed a settled loss at the default 300 CZK limit.
    g0, v0 = await _clean_bet(store)
    await store.insert_placed_bet(PlacedBet(
        value_bet_id=v0.id, game_id=g0.id, sport="mlb", status="placed",
        stake_czk=300.0, accepted_odds=2.0))
    await store.settle_placed_bet(v0.id, "loss")

    svc = BettingService(store=store, executor=FakeExecutor())
    g1, v1 = await _clean_bet(store)
    await svc.on_value_bet(g1, v1, pinnacle_fresh_at=datetime.now(timezone.utc))
    pb = await store.get_placed_bet_for_value_bet(v1.id)
    assert pb.status == "skipped" and pb.skip_reason == "daily_loss_cap"
    assert await store.get_meta("betting_armed") == "0"       # auto-disarmed
    assert "daily_loss_cap" in (await store.get_meta("betting_disarm_reason") or "")
    assert any("Auto-betting stopped" in t for t in _silence_telegram)


@pytest.mark.asyncio
async def test_stake_clamped_to_hard_ceiling(store):
    await _arm(store)
    await store.set_meta("betting_flat_stake_czk", "9999")
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert ex.commands[0]["stake_czk"] == 500.0   # BETTING_MAX_STAKE_CZK


@pytest.mark.asyncio
async def test_nonfinite_and_negative_stakes_fail_to_safe_bounds(store):
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    await store.set_meta("betting_flat_stake_czk", "nan")
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert ex.commands[-1]["stake_czk"] == 20.0

    await store.set_meta("betting_flat_stake_czk", "-500")
    game2, vb2 = await _clean_bet(store)
    await svc.on_value_bet(game2, vb2, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert len(ex.commands) == 1
    pb = await store.get_placed_bet_for_value_bet(vb2.id)
    assert pb.status == "skipped" and pb.skip_reason == "invalid_stake"


@pytest.mark.asyncio
async def test_meta_aggregate_caps_cannot_exceed_env_ceilings(store, monkeypatch):
    monkeypatch.setattr(config, "BETTING_MAX_BETS_PER_DAY", 1)
    await _arm(store)
    await store.set_meta("betting_max_bets_per_day", "999999")
    svc = BettingService(store=store, executor=FakeExecutor())
    g1, v1 = await _clean_bet(store)
    await svc.on_value_bet(g1, v1, pinnacle_fresh_at=datetime.now(timezone.utc))
    g2, v2 = await _clean_bet(store)
    await svc.on_value_bet(g2, v2, pinnacle_fresh_at=datetime.now(timezone.utc))
    pb2 = await store.get_placed_bet_for_value_bet(v2.id)
    assert pb2.status == "skipped" and pb2.skip_reason == "daily_bet_cap"


@pytest.mark.asyncio
async def test_two_consecutive_failures_disarm(store, _silence_telegram):
    await _arm(store)
    ex = FakeExecutor()
    ex.exc = PlacementError("boom")
    svc = BettingService(store=store, executor=ex)

    g1, v1 = await _clean_bet(store)
    await svc.on_value_bet(g1, v1, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert (await store.get_placed_bet_for_value_bet(v1.id)).status == "failed"
    assert await store.get_meta("betting_armed") == "1"       # one failure: still armed

    g2, v2 = await _clean_bet(store)
    await svc.on_value_bet(g2, v2, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert (await store.get_placed_bet_for_value_bet(v2.id)).status == "failed"
    assert await store.get_meta("betting_armed") == "0"       # second: disarmed
    assert any("Auto-betting stopped" in t for t in _silence_telegram)


@pytest.mark.asyncio
async def test_success_resets_failure_counter(store):
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)

    ex.exc = PlacementError("boom")
    g1, v1 = await _clean_bet(store)
    await svc.on_value_bet(g1, v1, pinnacle_fresh_at=datetime.now(timezone.utc))
    ex.exc = None
    g2, v2 = await _clean_bet(store)
    await svc.on_value_bet(g2, v2, pinnacle_fresh_at=datetime.now(timezone.utc))
    ex.exc = PlacementError("boom")
    g3, v3 = await _clean_bet(store)
    await svc.on_value_bet(g3, v3, pinnacle_fresh_at=datetime.now(timezone.utc))
    # fail, success, fail -> never two consecutive -> still armed
    assert await store.get_meta("betting_armed") == "1"


@pytest.mark.asyncio
async def test_timeout_marks_unknown_and_disarms_immediately(store, _silence_telegram):
    await _arm(store)
    ex = FakeExecutor()
    ex.exc = PlacementTimeout("no result in 10s")
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "unknown"
    assert await store.get_meta("betting_armed") == "0"
    assert any("Auto-betting stopped" in t for t in _silence_telegram)


@pytest.mark.asyncio
async def test_rejected_is_not_a_breaker_failure(store):
    await _arm(store)
    ex = FakeExecutor()
    ex.result = PlacementResult(status="rejected", message="price below min_odds")
    svc = BettingService(store=store, executor=ex)
    g1, v1 = await _clean_bet(store)
    await svc.on_value_bet(g1, v1, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert (await store.get_placed_bet_for_value_bet(v1.id)).status == "rejected"

    ex.exc = PlacementError("boom")
    g2, v2 = await _clean_bet(store)
    await svc.on_value_bet(g2, v2, pinnacle_fresh_at=datetime.now(timezone.utc))
    # rejection + one error is NOT two consecutive failures
    assert await store.get_meta("betting_armed") == "1"


@pytest.mark.asyncio
async def test_rejection_is_neutral_for_the_breaker(store, _silence_telegram):
    """A rejection is a MARKET outcome (price moved / offer gone), not evidence
    the actuator is healthy — so it neither counts as a failure nor CLEARS the
    consecutive-failure count. failed -> rejected -> failed is still two broken
    placements and must auto-disarm; only a `placed` result resets."""
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)

    ex.exc = PlacementError("first")
    g1, v1 = await _clean_bet(store)
    await svc.on_value_bet(g1, v1, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert svc._consecutive_failures == 1
    assert await store.get_meta("betting_armed") == "1"

    ex.exc = None
    ex.result = PlacementResult(status="rejected", message="offer_gone")
    g2, v2 = await _clean_bet(store)
    await svc.on_value_bet(g2, v2, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert (await store.get_placed_bet_for_value_bet(v2.id)).status == "rejected"
    # Neutral: not incremented by the rejection, and NOT reset by it either.
    assert svc._consecutive_failures == 1
    assert await store.get_meta("betting_armed") == "1"

    ex.exc = PlacementError("second")
    g3, v3 = await _clean_bet(store)
    await svc.on_value_bet(g3, v3, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert await store.get_meta("betting_armed") == "0"
    assert (await store.get_meta("betting_disarm_reason") or "").startswith(
        "consecutive_failures")
    assert any("Auto-betting stopped" in t for t in _silence_telegram)


@pytest.mark.asyncio
async def test_duplicate_detection_is_single_decision(store):
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert len(ex.commands) == 1   # second call: decision exists, no re-dispatch


@pytest.mark.asyncio
async def test_settlement_service_settles_placed_bet(store):
    """When the value bet resolves, the linked placed bet settles with real P&L."""
    from oddsiq.pipeline.settlement_service import SettlementService
    from oddsiq.pipeline.stats import SessionStats

    await _arm(store)
    svc = BettingService(store=store, executor=FakeExecutor())
    game, vb = await _clean_bet(store)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))

    game.status = "final"
    game.home_score, game.away_score = 5, 3   # home bet wins
    settlement = SettlementService(store=store, stats=SessionStats())
    await settlement.resolve_bets(game)

    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "settled"
    assert pb.settled_pl_czk == pytest.approx(20.0 * (2.08 - 1))


# ---------------------------------------------------------------------------
# Warm-tier betting + the cross-tier double-bet guard (2026-08-30)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warm_tier_bet_places(store):
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store, tier="warm", minutes_to_tip=90)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert len(ex.commands) == 1
    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "placed"


async def _second_vb_same_selection(store, game, vb, tier="hot"):
    """The hot re-detection of a warm bet: same game+selection, its own row
    (tier is in the dedup key)."""
    vb2 = ValueBet(
        game_id=game.id, bookmaker="tipsport", market_type="moneyline",
        selection=vb.selection, entry_odds=2.05, fair_odds=1.95, edge_pct=5.1,
        tier=tier, sport=vb.sport, tipsport_match_id=vb.tipsport_match_id,
        tipsport_opp_id=vb.tipsport_opp_id, tipsport_url=vb.tipsport_url,
    )
    vb2.id = await store.insert_value_bet(vb2)
    return vb2


@pytest.mark.asyncio
async def test_selection_already_open_blocks_cross_tier_double_bet(store):
    await _arm(store)
    ex = FakeExecutor()
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store, tier="warm", minutes_to_tip=90)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert len(ex.commands) == 1  # warm bet placed

    vb2 = await _second_vb_same_selection(store, game, vb, tier="hot")
    await svc.on_value_bet(game, vb2, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert len(ex.commands) == 1  # no second command
    pb2 = await store.get_placed_bet_for_value_bet(vb2.id)
    assert pb2.status == "skipped" and pb2.skip_reason == "selection_already_open"


@pytest.mark.asyncio
async def test_rejected_attempt_does_not_block_fresh_detection(store):
    """A rejected/failed attempt is terminal for ITS row, but a new detection
    of the same selection is a new opportunity — it must be tried, not
    blocked (only pending/placed/unknown/settled money blocks)."""
    await _arm(store)
    ex = FakeExecutor()
    ex.result = PlacementResult(status="rejected", message="offer_gone")
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store, tier="warm", minutes_to_tip=90)
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert len(ex.commands) == 1

    ex.result = PlacementResult(status="placed", accepted_odds=2.05, receipt_id="R2")
    vb2 = await _second_vb_same_selection(store, game, vb, tier="hot")
    await svc.on_value_bet(game, vb2, pinnacle_fresh_at=datetime.now(timezone.utc))
    assert len(ex.commands) == 2
    pb2 = await store.get_placed_bet_for_value_bet(vb2.id)
    assert pb2.status == "placed"


@pytest.mark.asyncio
async def test_placed_bet_with_accumulator_shaped_odds_warns(store, caplog):
    """Server-side backstop for a leftover slip leg: the extension's min-odds
    guard reads the slip TOTAL, so an accumulator of two ~1.95 legs submits at
    ~3.80 and passes the in-page check. Money is already down when we find
    out, so the row stays `placed` — but it must not pass silently."""
    await _arm(store)
    ex = FakeExecutor()
    # Two ~2.10 legs on one slip submit at ~4.20 (the fixture's entry_odds is
    # 2.10 against fair 2.00 — a +EV bet is always priced ABOVE fair).
    ex.result = PlacementResult(
        status="placed", accepted_odds=4.20, receipt_id="R9")
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)

    caplog.set_level(logging.WARNING, logger="oddsiq.pipeline.betting_service")
    caplog.clear()
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))

    msgs = [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("accepted_odds_anomaly:")]
    assert len(msgs) == 1, msgs
    line = msgs[0]
    assert f"value_bet_id={vb.id}" in line
    assert "requested=2.100" in line and "accepted=4.200" in line
    assert "ratio=2.000" in line
    # The operator-facing tail is the whole point of the warning — it must say
    # what to do, not just that a number looked odd.
    assert "verify the ticket at Tipsport" in line
    # The money is real: the decision row still records the placement.
    pb = await store.get_placed_bet_for_value_bet(vb.id)
    assert pb.status == "placed" and pb.accepted_odds == 4.20


@pytest.mark.asyncio
async def test_normal_placed_bet_does_not_warn(store, caplog):
    """A tick of positive slippage (2.12 vs 2.10) is an ordinary better fill,
    not an accumulator — the backstop must stay quiet."""
    await _arm(store)
    ex = FakeExecutor()
    ex.result = PlacementResult(
        status="placed", accepted_odds=2.12, receipt_id="R10")
    svc = BettingService(store=store, executor=ex)
    game, vb = await _clean_bet(store)

    caplog.set_level(logging.WARNING, logger="oddsiq.pipeline.betting_service")
    caplog.clear()
    await svc.on_value_bet(game, vb, pinnacle_fresh_at=datetime.now(timezone.utc))

    msgs = [r.getMessage() for r in caplog.records]
    assert not any(m.startswith("accepted_odds_anomaly:") for m in msgs), msgs
