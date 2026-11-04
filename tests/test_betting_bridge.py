"""BettingBridge — the WebSocket server the Chrome extension connects to.

Driven with a REAL websockets client (loopback, ephemeral port) so the auth
handshake, correlation-id round-trip, timeout, and disconnect semantics are
tested at the protocol level, not against mocks.
"""

import asyncio
import json

import pytest
import pytest_asyncio
import websockets

from oddsiq.pipeline.betting_bridge import BettingBridge
from oddsiq.pipeline.betting_service import PlacementError, PlacementTimeout

TOKEN = "test-secret"


@pytest_asyncio.fixture
async def bridge():
    b = BettingBridge(host="127.0.0.1", port=0, token=TOKEN, placement_timeout=0.3)
    await b.start()
    yield b
    await b.stop()


async def _connect(bridge, *, token=TOKEN, logged_in=True):
    ws = await websockets.connect(f"ws://127.0.0.1:{bridge.port}")
    await ws.send(json.dumps({
        "type": "hello", "token": token,
        "extension_version": "0.1.0", "logged_in": logged_in,
    }))
    return ws


@pytest.mark.asyncio
async def test_hello_with_bad_token_is_closed(bridge):
    ws = await _connect(bridge, token="wrong")
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(ws.recv(), timeout=2)
    assert bridge.connected is False


@pytest.mark.asyncio
async def test_hello_ack_and_connected_state(bridge):
    assert bridge.connected is False
    ws = await _connect(bridge)
    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
    assert ack["type"] == "hello_ack" and ack["protocol"] == 1
    await asyncio.sleep(0.05)
    assert bridge.connected is True
    assert bridge.logged_in is True
    await ws.close()
    await asyncio.sleep(0.1)
    assert bridge.connected is False


@pytest.mark.asyncio
async def test_logged_out_hello_and_status_update(bridge):
    ws = await _connect(bridge, logged_in=False)
    await asyncio.wait_for(ws.recv(), timeout=2)  # ack
    await asyncio.sleep(0.05)
    assert bridge.connected is True and bridge.logged_in is False
    await ws.send(json.dumps({"type": "status", "logged_in": True}))
    await asyncio.sleep(0.1)
    assert bridge.logged_in is True
    await ws.close()


# ---------------------------------------------------------------------------
# Login lost/restored notifications (2026-08-30) — the Aug 28 session expiry
# sat silent for 32h and skipped the week's only clean bet.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def alert_bridge():
    events: list[str] = []

    async def _record(event: str) -> None:
        events.append(event)

    b = BettingBridge(host="127.0.0.1", port=0, token=TOKEN,
                      placement_timeout=0.3, on_login_change=_record,
                      login_alert_debounce=0)
    b.events = events  # test-only handle on the recorded notifications
    await b.start()
    yield b
    await b.stop()


async def _status(ws, logged_in):
    await ws.send(json.dumps({"type": "status", "logged_in": logged_in}))
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_login_lost_fires_once_on_status_transition(alert_bridge):
    ws = await _connect(alert_bridge, logged_in=True)
    await asyncio.wait_for(ws.recv(), timeout=2)
    await _status(ws, False)
    assert alert_bridge.events == ["lost"]
    await _status(ws, False)                     # still down: no re-alert
    assert alert_bridge.events == ["lost"]
    await ws.close()


@pytest.mark.asyncio
async def test_login_restored_pairs_with_loss_and_cycle_repeats(alert_bridge):
    ws = await _connect(alert_bridge, logged_in=True)
    await asyncio.wait_for(ws.recv(), timeout=2)
    await _status(ws, True)                      # steady logged-in: silent
    assert alert_bridge.events == []
    await _status(ws, False)
    await _status(ws, True)
    assert alert_bridge.events == ["lost", "restored"]
    await _status(ws, False)                     # a second outage alerts again
    assert alert_bridge.events == ["lost", "restored", "lost"]
    await ws.close()


@pytest.mark.asyncio
async def test_hello_false_never_fires_lost(alert_bridge):
    # A fresh chrome start hellos logged_in=False before its probe settles —
    # that must not page anyone. A status False without a prior True only
    # arms the (long) startup watchdog, which the fixture's default 1800s
    # grace keeps far outside this test.
    ws = await _connect(alert_bridge, logged_in=False)
    await asyncio.wait_for(ws.recv(), timeout=2)
    await _status(ws, False)
    assert alert_bridge.events == []
    await ws.close()


@pytest.mark.asyncio
async def test_startup_watchdog_alerts_dead_session_without_prior_true():
    """2026-09-01: a bridge restarted onto an ALREADY dead session never sees
    True, so the transition alert can never fire — the startup grace pages
    anyway once a probe-confirmed logged-out state persists. A True inside
    the grace (normal boot: hello False -> probe -> True) cancels it."""
    events: list[str] = []

    async def _record(event: str) -> None:
        events.append(event)

    b = BettingBridge(host="127.0.0.1", port=0, token=TOKEN,
                      placement_timeout=0.3, on_login_change=_record,
                      login_alert_debounce=0, login_startup_grace=0.3)
    await b.start()
    try:
        ws = await _connect(b, logged_in=False)
        await asyncio.wait_for(ws.recv(), timeout=2)
        await _status(ws, False)                 # probe-confirmed logged out
        await asyncio.sleep(0.1)
        assert events == []                      # inside the grace: silent
        await asyncio.sleep(0.35)
        assert events == ["lost"]                # grace expired: paged
        await _status(ws, True)
        assert events == ["lost", "restored"]
        await ws.close()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_startup_watchdog_cancelled_by_normal_boot_login():
    events: list[str] = []

    async def _record(event: str) -> None:
        events.append(event)

    b = BettingBridge(host="127.0.0.1", port=0, token=TOKEN,
                      placement_timeout=0.3, on_login_change=_record,
                      login_alert_debounce=0, login_startup_grace=0.3)
    await b.start()
    try:
        ws = await _connect(b, logged_in=False)
        await asyncio.wait_for(ws.recv(), timeout=2)
        await _status(ws, False)                 # watchdog armed
        await _status(ws, True)                  # normal boot: probe settles
        await asyncio.sleep(0.4)                 # past the grace deadline
        assert events == []                      # never alerted, no restore
        await ws.close()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_bridge_sends_app_level_pings():
    """Protocol pings are invisible to the MV3 worker; the bridge must send
    real {"type": "ping"} frames so the worker's idle timer keeps resetting
    (the Sep 1 02:39-08:59 thirty-second reconnect churn)."""
    b = BettingBridge(host="127.0.0.1", port=0, token=TOKEN,
                      placement_timeout=0.3, app_ping_interval=0.05)
    await b.start()
    try:
        ws = await _connect(b)
        await asyncio.wait_for(ws.recv(), timeout=2)  # hello_ack
        pings = 0
        for _ in range(3):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            if msg.get("type") == "ping":
                pings += 1
        assert pings == 3
        await ws.close()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_login_state_survives_client_reconnect(alert_bridge):
    # Seen-logged-in is bridge-lifetime state: chrome restarting (hello False)
    # then confirming the session died must still alert exactly once, and a
    # hello that comes back logged_in=True is a real restore.
    ws1 = await _connect(alert_bridge, logged_in=True)
    await asyncio.wait_for(ws1.recv(), timeout=2)
    await ws1.close()
    await asyncio.sleep(0.1)
    ws2 = await _connect(alert_bridge, logged_in=False)
    await asyncio.wait_for(ws2.recv(), timeout=2)
    await asyncio.sleep(0.05)
    assert alert_bridge.events == []             # disconnect + hello: silent
    await _status(ws2, False)
    assert alert_bridge.events == ["lost"]
    await ws2.close()
    await asyncio.sleep(0.1)
    ws3 = await _connect(alert_bridge, logged_in=True)
    await asyncio.wait_for(ws3.recv(), timeout=2)
    await asyncio.sleep(0.1)
    assert alert_bridge.events == ["lost", "restored"]
    await ws3.close()


@pytest.mark.asyncio
async def test_login_notifier_exception_does_not_break_the_handler():
    async def _boom(event: str) -> None:
        raise RuntimeError("notifier down")

    b = BettingBridge(host="127.0.0.1", port=0, token=TOKEN,
                      placement_timeout=0.3, on_login_change=_boom,
                      login_alert_debounce=0)
    await b.start()
    try:
        ws = await _connect(b, logged_in=True)
        await asyncio.wait_for(ws.recv(), timeout=2)
        await _status(ws, False)                 # notifier raises in its task
        await _status(ws, True)                  # handler must still be alive
        assert b.logged_in is True
        await ws.close()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_place_roundtrip(bridge):
    ws = await _connect(bridge)
    await asyncio.wait_for(ws.recv(), timeout=2)  # ack

    async def _extension():
        raw = await asyncio.wait_for(ws.recv(), timeout=2)
        cmd = json.loads(raw)
        assert cmd["type"] == "place_bet"
        assert cmd["match_id"] == 8109316 and cmd["stake_czk"] == 20.0
        await ws.send(json.dumps({
            "type": "result", "id": cmd["id"], "status": "placed",
            "accepted_odds": 2.08, "receipt_id": "T-1",
            "balance_czk": 980.0,
        }))

    ext = asyncio.create_task(_extension())
    result = await bridge.place({
        "bet_id": 1, "match_id": 8109316, "opp_id": 2624446456,
        "url": "/kurzy/zapas/x/8109316", "stake_czk": 20.0, "min_odds": 2.06,
    })
    await ext
    assert result.status == "placed"
    assert result.accepted_odds == 2.08 and result.receipt_id == "T-1"
    assert result.balance_czk == 980.0
    await ws.close()


@pytest.mark.asyncio
async def test_result_step_timings_are_logged(bridge, caplog):
    """The extension attaches per-step timings (nav/odd/slip/...) to its
    result; the bridge logs them (log-only — no schema/model change) so the
    placement latency budget is measurable on real attempts."""
    import logging
    ws = await _connect(bridge)
    await asyncio.wait_for(ws.recv(), timeout=2)  # ack

    async def _extension():
        raw = await asyncio.wait_for(ws.recv(), timeout=2)
        cmd = json.loads(raw)
        await ws.send(json.dumps({
            "type": "result", "id": cmd["id"], "status": "rejected",
            "message": "offer_gone",
            "timings": {"nav_ms": 2100, "attach_ms": 2200, "odd_ms": 14200},
        }))

    ext = asyncio.create_task(_extension())
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="oddsiq.pipeline.betting_bridge"):
        result = await bridge.place({
            "bet_id": 2, "match_id": 1, "opp_id": 2,
            "url": "/kurzy/zapas/x/1", "stake_czk": 10.0, "min_odds": 1.9,
        })
    await ext
    assert result.status == "rejected"
    logged = [r.getMessage() for r in caplog.records if "result_timings" in r.getMessage()]
    assert logged and "nav_ms" in logged[0] and "14200" in logged[0]
    await ws.close()


@pytest.mark.asyncio
async def test_place_timeout_raises_placement_timeout(bridge):
    ws = await _connect(bridge)
    await asyncio.wait_for(ws.recv(), timeout=2)  # ack; then never reply
    with pytest.raises(PlacementTimeout):
        await bridge.place({"bet_id": 1, "match_id": 1, "opp_id": 1,
                            "url": "/x", "stake_czk": 20.0, "min_odds": 2.0})
    await ws.close()


@pytest.mark.asyncio
async def test_disconnect_mid_placement_is_unknown_semantics(bridge):
    """A client that dies after receiving the command MAY have placed the bet
    — the bridge must surface PlacementTimeout (-> 'unknown' -> disarm), never
    a clean failure."""
    ws = await _connect(bridge)
    await asyncio.wait_for(ws.recv(), timeout=2)  # ack

    async def _die_after_command():
        await asyncio.wait_for(ws.recv(), timeout=2)   # got place_bet
        await ws.close()

    task = asyncio.create_task(_die_after_command())
    with pytest.raises(PlacementTimeout):
        await bridge.place({"bet_id": 1, "match_id": 1, "opp_id": 1,
                            "url": "/x", "stake_czk": 20.0, "min_odds": 2.0})
    await task


@pytest.mark.asyncio
async def test_place_without_client_raises_placement_error(bridge):
    with pytest.raises(PlacementError):
        await bridge.place({"bet_id": 1, "match_id": 1, "opp_id": 1,
                            "url": "/x", "stake_czk": 20.0, "min_odds": 2.0})


@pytest.mark.asyncio
async def test_supersede_closes_old_with_4409_and_keeps_new(bridge):
    """The contract the extension's reconnect fix relies on (2026-08-10 storm:
    881 supersedes in ~30 min): a second authenticated client kicks the first
    with close code 4409, the bridge stays bound to the NEW client, and the
    old client's departure must NOT clear the new one."""
    ws1 = await _connect(bridge)
    ack1 = json.loads(await asyncio.wait_for(ws1.recv(), timeout=2))
    assert ack1["type"] == "hello_ack"

    ws2 = await _connect(bridge)
    ack2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2))
    assert ack2["type"] == "hello_ack"

    # The first client is closed by the bridge with the supersede code.
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(ws1.recv(), timeout=2)
    assert ws1.close_code == 4409

    # The bridge is still connected — to the SECOND client (old one's close
    # handler ran without clearing it: the `self._client is ws` guard).
    await asyncio.sleep(0.1)
    assert bridge.connected is True
    assert bridge.logged_in is True

    # And the second client still works end-to-end.
    async def _respond():
        msg = json.loads(await ws2.recv())
        await ws2.send(json.dumps({
            "type": "result", "id": msg["id"], "status": "rejected",
            "message": "confirm_disabled"}))
    responder = asyncio.ensure_future(_respond())
    res = await bridge.place({"bet_id": 1})
    await responder
    assert res.status == "rejected"
    await ws2.close()


@pytest.mark.asyncio
async def test_login_loss_alert_is_debounced():
    """2026-08-31: page churn on drifted live pages flapped the probe 4x in a
    day — a loss that recovers inside the debounce window must never page the
    operator (and, never alerted, produces no restore either). A loss that
    OUTLIVES the window alerts and pairs with its restore."""
    events: list[str] = []

    async def _record(event: str) -> None:
        events.append(event)

    b = BettingBridge(host="127.0.0.1", port=0, token=TOKEN,
                      placement_timeout=0.3, on_login_change=_record,
                      login_alert_debounce=0.3)
    await b.start()
    try:
        ws = await _connect(b, logged_in=True)
        await asyncio.wait_for(ws.recv(), timeout=2)
        # Flap: loss recovers inside the window -> total silence.
        await _status(ws, False)
        await asyncio.sleep(0.1)
        assert events == []
        await _status(ws, True)
        await asyncio.sleep(0.4)                  # past where the alert would have fired
        assert events == []
        # Real outage: loss outlives the window -> lost, then restored.
        await _status(ws, False)
        await asyncio.sleep(0.45)
        assert events == ["lost"]
        await _status(ws, True)
        assert events == ["lost", "restored"]
        await ws.close()
    finally:
        await b.stop()
