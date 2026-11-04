"""BettingBridge — the WebSocket server the Chrome extension connects to.

The extension (Phase 2) dials OUT to this server from the Chrome container,
authenticates with the shared ``BETTING_BRIDGE_TOKEN``, and then executes
``place_bet`` commands, replying with correlation-id'd ``result`` messages.
The bridge implements the executor contract ``BettingService`` consumes:
``connected`` / ``logged_in`` properties and ``async place(cmd)``.

Protocol v1 (JSON text frames):
- ``hello {token, extension_version, logged_in}`` -> ``hello_ack {protocol: 1}``
  (bad token -> close 4401; a second client supersedes the first, close 4409)
- ``place_bet {id, bet_id, match_id, opp_id, url, stake_czk, min_odds}``
  -> ``result {id, status: placed|rejected|error, accepted_odds?, receipt_id?,
     balance_czk?, message?}``
- ``status {logged_in}`` — extension-initiated session-state updates. A
  status-driven logged-in -> logged-out transition fires the ``on_login_change``
  notifier ("lost"; the paired "restored" on recovery) — the extension
  navigate-confirms on its pinned probe page before reporting, so "lost"
  means the session really died.
- ``list_bets`` — reserved for Phase-2 reconciliation

Liveness: the websockets library's protocol-level ping every 20s (a client
that misses the 20s pong window is closed → ``connected`` flips false), plus
an application-level ``{"type": "ping"}`` frame every 20s — protocol pings
are invisible to the extension's MV3 service worker, and only a real
onmessage event resets Chrome's 30s worker-idle kill (see __init__).

Failure semantics: a client that disconnects AFTER receiving a command may
have placed the bet — pending placements resolve to ``PlacementTimeout``
(-> status 'unknown' -> auto-disarm), never a clean failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import uuid4

import websockets

from oddsiq.pipeline.betting_service import (
    PlacementError,
    PlacementResult,
    PlacementTimeout,
)

logger = logging.getLogger(__name__)


class BettingBridge:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        placement_timeout: float,
        on_login_change=None,
        login_alert_debounce: float = 600.0,
        login_startup_grace: float = 1800.0,
        app_ping_interval: float = 20.0,
    ) -> None:
        if not token:
            raise ValueError("BettingBridge requires a non-empty token")
        self._host = host
        self._port = port
        self._token = token
        self._timeout = placement_timeout
        self._server: websockets.Server | None = None
        self._client = None
        self._client_logged_in = False
        self._pending: dict[str, asyncio.Future] = {}
        # Login-transition notifier (2026-08-30): ``async on_login_change(event)``
        # with event "lost" | "restored". "lost" fires only on an extension
        # STATUS report (its probe actively confirmed the session died — a
        # hello logged_in=false is just "probe not settled yet", which every
        # fresh chrome start goes through) AND only after the loss survives
        # ``login_alert_debounce`` seconds (2026-08-31: page churn on drifted
        # live pages flapped lost/restored 4x in a day; the ENGINE always sees
        # the instant state — only the phone alert is debounced). "restored"
        # fires only when a loss was actually alerted, so the pair can't spam.
        # State survives client reconnects — only a pipeline restart resets it.
        self._on_login_change = on_login_change
        self._login_alert_debounce = login_alert_debounce
        # Startup watchdog (2026-09-01): a bridge restarted onto an ALREADY
        # dead session never sees True, so the transition alert above can
        # never fire — a probe-confirmed logged-out state that persists this
        # long from a fresh start alerts anyway.
        self._login_startup_grace = login_startup_grace
        # MV3 worker keepalive (2026-09-01): the websockets library's
        # protocol-level pings are answered by Chrome's network stack and are
        # INVISIBLE to the extension's service worker, so the worker's life
        # hung on its own 30s alarm racing Chrome's 30s idle kill — when the
        # race flipped, the socket churned reconnect every ~30s for hours
        # (Sep 1, 02:39-08:59). An application-level {"type": "ping"} frame
        # is a real onmessage event, which resets the worker's idle timer.
        self._app_ping_interval = app_ping_interval
        self._login_seen_true = False
        self._login_alerted = False
        self._pending_loss: asyncio.Task | None = None
        self._notify_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handler,
            self._host,
            self._port,
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info("betting_bridge: listening on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._pending_loss is not None and not self._pending_loss.done():
            self._pending_loss.cancel()
        if self._notify_tasks:
            await asyncio.gather(*self._notify_tasks, return_exceptions=True)

    @property
    def port(self) -> int:
        """Actual bound port (differs from the requested one when 0)."""
        if self._server is None:
            return self._port
        return self._server.sockets[0].getsockname()[1]

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def logged_in(self) -> bool:
        return self.connected and self._client_logged_in

    async def place(self, cmd: dict) -> PlacementResult:
        if not self.connected:
            raise PlacementError("extension not connected")
        msg_id = uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._client.send(
                json.dumps({"type": "place_bet", "id": msg_id, **cmd})
            )
            msg = await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            raise PlacementTimeout(
                f"no placement result within {self._timeout}s"
            ) from None
        finally:
            self._pending.pop(msg_id, None)
        return PlacementResult(
            status=msg.get("status", "error"),
            accepted_odds=msg.get("accepted_odds"),
            receipt_id=msg.get("receipt_id"),
            balance_czk=msg.get("balance_czk"),
            message=msg.get("message"),
        )

    # ------------------------------------------------------------------

    def _apply_login_state(self, logged_in: bool, *, source: str) -> None:
        """Update login state and fire lost/restored notifications on real
        transitions. ``source`` is "hello" or "status" — only a status frame
        may trigger "lost" (the extension navigate-confirms before sending it),
        and only after the loss survives the debounce window (a recovery
        inside it cancels the alert entirely)."""
        self._client_logged_in = logged_in
        if logged_in:
            self._login_seen_true = True
            if self._pending_loss is not None and not self._pending_loss.done():
                self._pending_loss.cancel()
            self._pending_loss = None
            if self._login_alerted:
                self._login_alerted = False
                self._fire_login_event("restored")
        elif source == "status" and not self._login_alerted:
            if self._login_seen_true:
                # Real transition: was logged in this bridge lifetime.
                self._login_seen_true = False
                delay = self._login_alert_debounce
            else:
                # Startup watchdog: probe-confirmed logged-out with no True
                # ever seen — a restart onto a dead session. Alert after the
                # (longer) grace so a normal boot's False->True never pages.
                delay = self._login_startup_grace
            if self._pending_loss is None or self._pending_loss.done():
                self._pending_loss = asyncio.get_running_loop().create_task(
                    self._debounced_loss(delay)
                )

    async def _debounced_loss(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._client_logged_in and not self._login_alerted:
            self._login_alerted = True
            self._fire_login_event("lost")

    async def _app_pinger(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self._app_ping_interval)
                await ws.send('{"type": "ping"}')
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            return

    def _fire_login_event(self, event: str) -> None:
        """Run the notifier as a detached task — the WS receive loop must never
        block on Telegram (a stalled send would delay placement results into
        PlacementTimeout territory)."""
        if self._on_login_change is None:
            return
        logger.warning("betting_bridge: login %s", event)

        async def _notify() -> None:
            try:
                await self._on_login_change(event)
            except Exception:
                logger.exception("betting_bridge: login-%s notify failed", event)

        task = asyncio.get_running_loop().create_task(_notify())
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    async def _handler(self, ws) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            hello = json.loads(raw)
        except (asyncio.TimeoutError, ValueError, websockets.ConnectionClosed):
            await ws.close(code=4400, reason="bad hello")
            return
        if hello.get("type") != "hello" or hello.get("token") != self._token:
            logger.warning("betting_bridge: rejected client (bad hello/token)")
            await ws.close(code=4401, reason="unauthorized")
            return
        if self._client is not None:
            logger.warning("betting_bridge: new client supersedes existing one")
            await self._client.close(code=4409, reason="superseded")
        self._client = ws
        self._apply_login_state(bool(hello.get("logged_in", False)), source="hello")
        await ws.send(json.dumps({"type": "hello_ack", "protocol": 1}))
        logger.info(
            "betting_bridge: extension connected (version=%s logged_in=%s)",
            hello.get("extension_version"),
            self._client_logged_in,
        )
        # Application-level keepalive for THIS client (see __init__) — a JSON
        # frame the worker actually sees, unlike protocol pings. The extension
        # ignores unknown message types, so no client change is needed.
        pinger = asyncio.get_running_loop().create_task(self._app_pinger(ws))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    logger.warning("betting_bridge: non-JSON frame ignored")
                    continue
                mtype = msg.get("type")
                if mtype == "result":
                    if msg.get("timings"):
                        # Per-step placement timings from the extension
                        # (nav/attach/inject/odd/slip/stake/outcome, ms from
                        # command receipt). Log-only telemetry: the latency
                        # budget is measured on real attempts, not guessed.
                        logger.info(
                            "betting_bridge: result_timings id=%s status=%s %s",
                            msg.get("id"),
                            msg.get("status"),
                            json.dumps(msg["timings"], sort_keys=True),
                        )
                    if msg.get("debug"):
                        # Extension diagnostics on a failed attempt: the page-
                        # state snapshot on an offer_gone timeout (0.6.0, where
                        # the tab landed and whether any odds rendered) and the
                        # {legs, legs_after} slip counts behind
                        # slip_not_singleton / slip_not_cleared, and the
                        # {expected_stake, actual_stake, actual_raw} triple behind
                        # stake_mismatch — the next failed placement explains
                        # itself from logs.
                        logger.info(
                            "betting_bridge: result_debug id=%s %s",
                            msg.get("id"),
                            json.dumps(msg["debug"], sort_keys=True),
                        )
                    fut = self._pending.pop(msg.get("id", ""), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif mtype == "status":
                    self._apply_login_state(
                        bool(msg.get("logged_in", False)), source="status"
                    )
                    if msg.get("page"):
                        # 0.7.0: logged-out reports carry the page the probe
                        # saw — a drifted live page / interstitial diagnoses
                        # itself from the log (the 2026-08-31 tennis-tab hunt).
                        logger.info(
                            "betting_bridge: status logged_in=%s page=%s",
                            self._client_logged_in,
                            json.dumps(msg["page"], sort_keys=True),
                        )
                    else:
                        logger.info(
                            "betting_bridge: status logged_in=%s",
                            self._client_logged_in,
                        )
        except websockets.ConnectionClosed:
            pass
        finally:
            pinger.cancel()
            if self._client is ws:
                self._client = None
                self._client_logged_in = False
                logger.warning("betting_bridge: extension disconnected")
                # A command sent but unanswered is UNKNOWN (the bet may exist
                # at the book) — PlacementTimeout semantics, never a clean error.
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(
                            PlacementTimeout(
                                "extension disconnected before placement result"
                            )
                        )
                self._pending.clear()
