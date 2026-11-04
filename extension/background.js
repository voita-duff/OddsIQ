// OddsIQ Betting Actuator — MV3 background service worker.
//
// Connects OUT to the pipeline's WebSocket bridge, then turns `place_bet`
// commands into a real Tipsport bet slip using CHROME DEBUGGER (CDP) trusted
// input events — the anti-detection actuator described in
// docs/Design/2026-08-09-p1-auto-betting-design.md and the Phase-2
// plan. Protocol v1 matches oddsiq/pipeline/betting_bridge.py.
//
// Money gate: the final submit click only fires when OTS_CONFIG.CONFIRM_ENABLED
// is true (Phase 4). Until then the routine builds + validates the slip and
// stops at `ready_not_confirmed`, so on-VM verification never risks a bet.

// Only config.js runs in the worker. selectors.js is page-context (uses
// window/document) and is injected into the tab via chrome.scripting, never
// imported here.
importScripts("config.js");

const CFG = globalThis.OTS_CONFIG;

let ws = null;
let backoffMs = 1000;
let loggedIn = false;
let reconnectTimer = null;
// Placement mutex: while a bet is being placed, the login probe, session
// refresh, and tab pruning must not touch the tab (the 0.5.0 probe no longer
// uses the debugger, but a reload or tab close mid-placement would still kill
// the money path).
let inFlight = false;
// Probe mutex: one reportStatus can run ~55s worst case (probe nav ≤15s +
// confirm nav ≤15s + ≤11s header poll (deadline-bounded; the probe
// round-trips on top are uncapped) + logged-out sendStatus nav ≤15s), well
// past the 30s keepalive cadence — never stack probes.
let probing = false;

// ---------------------------------------------------------------------------
// WebSocket bridge client
//
// Reconnect discipline (hardened after the 2026-08-10 supersede storm: 881
// "new client supersedes existing one" events in ~30 min, one every ~2s,
// right after a pipeline redeploy):
//   1. Every handler is bound to ITS socket and no-ops when that socket is
//      no longer the current `ws` — a superseded/stale socket's close must
//      never re-dial. (The storm: two live sockets each re-dialing on the
//      close the other one caused, forever.)
//   2. connect() is idempotent — it bails if the current socket is already
//      OPEN or CONNECTING, so an alarm firing mid-handshake can't stack a
//      second socket over the first.
//   3. One pending reconnect timer at most, and the delayed re-dial goes
//      through ensureConnected() so it no-ops if something else already
//      connected in the meantime.
//   4. Backoff resets on hello_ack (the bridge ACCEPTED us), not on bare
//      socket open — a pathological open→superseded cycle now backs off
//      exponentially to 30s instead of hammering every ~2s.
// ---------------------------------------------------------------------------

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  let sock;
  try {
    sock = new WebSocket(CFG.BRIDGE_URL);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  ws = sock;
  sock.onopen = () => {
    if (ws !== sock) { try { sock.close(); } catch {} return; }
    send({
      type: "hello",
      token: CFG.BRIDGE_TOKEN,
      extension_version: CFG.EXTENSION_VERSION,
      logged_in: loggedIn,
    });
  };
  sock.onmessage = (ev) => {
    if (ws !== sock) return;
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "hello_ack") { backoffMs = 1000; return; }
    if (msg.type === "place_bet") handlePlaceBet(msg);
    // Protocol pings are handled by the WS layer.
  };
  sock.onclose = () => {
    if (ws !== sock) return;   // stale socket (superseded/replaced): stay quiet
    ws = null;
    scheduleReconnect();
  };
  // Close SELF on error — never the global ws, which may already point at a
  // healthy newer socket.
  sock.onerror = () => { try { sock.close(); } catch {} };
}

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  backoffMs = Math.min(backoffMs * 2, 30000);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    ensureConnected();
  }, backoffMs);
}

// MV3 service workers are killed after ~30s idle, so a setTimeout-based
// reconnect dies with the worker and never fires. The keepalive alarm below
// wakes the worker on a fixed cadence and calls this to (re)establish the
// socket if it isn't OPEN or CONNECTING. Once connected, the bridge's 20s
// protocol pings keep the worker alive. This is what actually gets — and
// keeps — the extension connected on the headless VM.
function ensureConnected() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  connect();
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// Periodic + on-change login-state reporting so the pipeline's qualification
// chain can skip when the session drops. A logged-in -> logged-out flip is
// NAVIGATE-CONFIRMED before it is reported: the persistent tab DRIFTS —
// placements leave it on match pages, and Tipsport auto-forwards ended
// matches to other live events (observed 2026-08-31: a live tennis stream
// page, whose layout lacks the login header markers — the probe flapped
// lost/restored all day while the session was fine). Confirmation always
// re-pins the tab to PROBE_PAGE, whose standard header is reliable.
// While logged out, the confirm re-runs every RELOGIN_RETRY_MS: a fresh
// navigation gives Tipsport's remembered-session auto-login a chance, so a
// real transient logout self-heals in minutes instead of hours.
async function reportStatus() {
  if (inFlight || probing) return;
  probing = true;
  try {
    let now = await probeLoggedIn();
    // A positive reading on ANY page proves the session is alive — stronger
    // evidence than the confirms the counter tracks, so it clears the run.
    // Without this the counter is a lifetime tally: the hourly session refresh
    // fires a confirm unconditionally, so three inconclusive ones hours apart
    // report LOST straight through a healthy session.
    if (now) inconclusiveConfirms = 0;
    if (loggedIn && !now) {
      now = await confirmLoginOnProbePage();
    } else if (!loggedIn && !now && Date.now() - lastConfirmAt > RELOGIN_RETRY_MS) {
      now = await confirmLoginOnProbePage();
    }
    if (inFlight) return; // a placement started mid-probe: report next cycle
    if (now !== loggedIn) {
      loggedIn = now;
      await sendStatus();
    }
  } finally {
    probing = false;
  }
}

// Canonical probe/keepalive page. Never probe login on whatever page the tab
// drifted to — live/stream layouts don't carry the standard header.
const PROBE_PAGE = "/kurzy";
// Cap on waiting for tab status "complete" on every probe-side navigation —
// the placement path's mirror of NAV_COMPLETE_CAP_MS. Used by every
// ensureTipsportTab() call outside handlePlaceBet, so a stuck navigation on
// the probe path can never wedge `probing` open (see waitTabComplete).
const PROBE_COMPLETE_CAP_MS = 15000;
const RELOGIN_RETRY_MS = 10 * 60 * 1000;
let lastConfirmAt = 0;
// Consecutive confirms that never reached PROBE_PAGE before we stop treating
// "couldn't observe" as benign and report the session lost (the status frame
// carries the page the tab is on when the frame is sent — usually /kurzy by
// then — so treat the trip itself as the diagnostic and read the bridge's
// status/result logs).
const INCONCLUSIVE_CONFIRMS_BEFORE_LOST = 3;
let inconclusiveConfirms = 0;

// Navigate the tab to PROBE_PAGE and poll the login probe, trusting ONLY the
// readings taken while the tab is actually ON that page — chrome.tabs.update
// resolves before the navigation commits, so the outgoing (drifted) page can
// report "complete" and answer the probe for the whole window. A confirm that
// never reached PROBE_PAGE made no observation at all: it keeps the current
// state and only reports the session lost once
// INCONCLUSIVE_CONFIRMS_BEFORE_LOST of them land in a row. The path test
// deliberately accepts the whole /kurzy/* family (the match pages a placement
// leaves the tab on carry the standard header) — the documented flap source is
// /live/*, so do NOT tighten this to an exact match. The path read and the
// login read are two round-trips, so a spontaneous navigation between them
// (Tipsport's ended-match auto-forward) can still yield one false reading —
// the original flap window narrowed from ~8s to tens to low hundreds of ms,
// accepted. Serves three callers: the logged-out confirmation, the
// while-logged-out retry, and the session-refresh keepalive (authenticated
// requests on a navigation are real activity against an idle-expiry policy —
// the Aug 28 session died ~24h after login).
async function confirmLoginOnProbePage() {
  // Never navigate the shared tab while a placement owns it — the callers'
  // entry checks race the alarm handlers against a ~50s worst-case placement, so the
  // check must sit HERE, after their awaits. lastConfirmAt stays untouched
  // so the retry re-fires next cycle instead of waiting RELOGIN_RETRY_MS,
  // and the yield returns the CURRENT `loggedIn` state (not a hardcoded
  // false) so neither caller sees a spurious change mid-placement.
  if (inFlight) return loggedIn;
  lastConfirmAt = Date.now();
  try {
    const tabId = await ensureTipsportTab(PROBE_PAGE, PROBE_COMPLETE_CAP_MS);
    let deadline = Date.now() + 8000; // SPA renders the header post-complete
    let observed = false;
    while (Date.now() < deadline) {
      const path = await probePath(tabId);
      if (path == null || !path.startsWith(PROBE_PAGE)) { await sleep(500); continue; }
      // The path commits before the SPA paints the header — once the page is
      // actually here, always allow a few polls for the markers to render.
      if (!observed) { observed = true; deadline = Math.max(deadline, Date.now() + 3000); }
      if (await probeLoggedIn(tabId)) { inconclusiveConfirms = 0; return true; }
      await sleep(500);
    }
    if (observed) { inconclusiveConfirms = 0; return false; }
    // Never saw the probe page: no observation was made. Tolerate a few in a
    // row (slow VM Chrome, a navigation that lost a race) before reporting.
    inconclusiveConfirms += 1;
    if (inconclusiveConfirms >= INCONCLUSIVE_CONFIRMS_BEFORE_LOST) { inconclusiveConfirms = 0; return false; }
    return loggedIn;
  } catch { return false; }
}

// Status frames carry the page the probe saw when reporting logged OUT — the
// bridge logs it, so a drifted/interstitial page diagnoses itself from logs.
async function sendStatus() {
  const msg = { type: "status", logged_in: loggedIn };
  if (!loggedIn) {
    try {
      const tabId = await ensureTipsportTab(null, PROBE_COMPLETE_CAP_MS);
      const [r] = await chrome.scripting.executeScript({
        target: { tabId }, world: "MAIN",
        func: () => ({
          href: location.href.slice(0, 120), title: (document.title || "").slice(0, 60),
          markers: (window.__ots && window.__ots.loginMarkers) ? window.__ots.loginMarkers() : null,
        }),
      });
      if (r && r.result) msg.page = r.result;
    } catch {}
  }
  send(msg);
}

// Session keepalive (2026-08-30): reload the tab every hour so the
// session sees authenticated activity, then report the (navigate-confirmed)
// login state. Also prunes stray Tipsport tabs — every flow assumes ONE
// persistent tab (tabs[0]), so extras from manual VNC sessions would make
// probes and placements look at the wrong page.
// Was 240 min. Multi-day login losses motivated an hourly canary; the logs
// do not establish which cookie expired or whether activity extends its life.
// Keepalive efficacy still needs observation over the canary window.
const SESSION_REFRESH_MINUTES = 60;
// create() with an existing name RESETS the countdown, and this top-level code
// re-runs on every worker restart — re-create when the period changed, else only when absent, or a restart-happy
// stretch (e.g. bridge down, worker bouncing on the 30s alarm) would push the
// refresh out forever. The 0.5-min keepalive below doesn't care (≤30s slip).
chrome.alarms.get("session-refresh", (a) => {
  if (!a || a.periodInMinutes !== SESSION_REFRESH_MINUTES) {
    chrome.alarms.create("session-refresh", { periodInMinutes: SESSION_REFRESH_MINUTES });
  }
});
// Retain a due refresh when a probe or placement owns the shared tab.
let sessionRefreshDue = false;
async function sessionRefresh() {
  if (inFlight || probing) return;
  sessionRefreshDue = false;
  probing = true;
  try {
    await pruneTipsportTabs();
    const now = await confirmLoginOnProbePage();
    if (inFlight) return;
    if (now !== loggedIn) {
      loggedIn = now;
      await sendStatus();
    }
  } finally {
    probing = false;
  }
}

// Keepalive/reconnect cadence. 0.5 min is the MV3 minimum alarm period — the
// fastest self-heal we can get after a socket drop or a worker restart.
chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "keepalive") {
    ensureConnected();
    if (sessionRefreshDue) sessionRefresh();
    else reportStatus();
  } else if (a.name === "session-refresh") {
    sessionRefreshDue = true;
    sessionRefresh();
  }
});

// MV3 worker lifecycle: connect on browser start (persisted profile) and on
// first load (fresh profile). Both fire an event that instantiates the worker.
chrome.runtime.onStartup.addListener(ensureConnected);
chrome.runtime.onInstalled.addListener(ensureConnected);

// ---------------------------------------------------------------------------
// Placement
// ---------------------------------------------------------------------------

// Cap on waiting for tab status "complete" during a placement navigation.
// "complete" waits for every subresource (trackers included); the tolerant
// waitForOdd poll below absorbs any not-ready-yet state. 5s (was 3s) and a
// 20s odd budget (was 12s): the 2026-08-30 forensics couldn't rule out the
// headless VM Chrome (software rendering, no GPU) simply not having painted
// the offer inside the old 15s total — on the money path a few extra seconds
// of waiting beats a false offer_gone (entry prices survive: 72% at T+60s).
// Keep nav + odd + the inline slip (5s) and ticket (12s) waits under the
// pipeline's BETTING_PLACEMENT_TIMEOUT_SECONDS (60s; oddsiq/config.py) —
// tests/test_config.py pins all five wait caps against it. The post-
// submit tail (re-attach + receipt read) is UNCAPPED — evalPage's 3rd arg is
// only an awaitPromise flag — which is why that test also demands >=10s
// headroom over the 44s enforced sum. The slip-singleton checks around the odd click
// are uncapped too, but they are two CDP evals on the normal path (sub-second)
// and on the abnormal one a bounded clearSlip (~3.5s: ≤5 iterations × ~0.7s)
// plus ≤2s of odd polling to re-measure the rect — the two singleton counts, a
// bounded clearSlip, and the ≤2s odd poll — so the worst-case success path is
// ≈ 44s enforced + ~4s cleanup + ≤2s ≈ 50s, leaving ~10s (60 − 50) for that
// uncapped post-submit tail: still above the >=10s the same test demands.
// That ≤2s post-cleanup re-probe is now TERMINAL (offer_gone_after_cleanup —
// a rejection, so it does not trip the circuit breaker) rather than a fall
// back to the stale rect, which is the accepted trade for spending it out of
// the same ≥10s post-submit headroom: two extra seconds of waiting can only
// cost a refusal, while a stale viewport click costs a wrong bet.
const NAV_COMPLETE_CAP_MS = 5000;
const ODD_WAIT_BUDGET_MS = 20000;
const STAKE_VERIFY_BUDGET_MS = 2000;

async function handlePlaceBet(cmd) {
  const reply = (o) => send({ type: "result", id: cmd.id, ...o });
  // The command URL comes from the pipeline DB and is joined onto
  // TIPSPORT_ORIGIN — accept only a same-origin absolute PATH. Anything else
  // ("//evil.tld/…", "https://…", relative junk) would navigate the
  // logged-in profile off-site; refuse instead.
  if (cmd.url != null && (typeof cmd.url !== "string"
      || !cmd.url.startsWith("/") || cmd.url.startsWith("//"))) {
    reply({ status: "error", message: "bad_url" });
    return;
  }
  // One placement at a time — the bet slip is a singleton on ONE tab. The
  // pipeline already serializes dispatch; a second command mid-placement means
  // something upstream is wrong, so refuse it loudly (the bridge records a
  // failed attempt) rather than navigate the tab out from under a live slip.
  if (inFlight) {
    reply({ status: "error", message: "busy" });
    return;
  }
  // Per-step timings (ms since command receipt), attached to every result and
  // logged by the bridge — the latency budget is measured, not guessed.
  // mark(k) records elapsed ms; mark(k, v) records a plain value instead (the
  // slip-cleanup leg count), riding the same log-only telemetry frame.
  const t0 = Date.now();
  const timings = {};
  const mark = (k, v) => { timings[k] = v === undefined ? Date.now() - t0 : v; };
  let tabId;
  inFlight = true;
  try {
    // 0) Login gate BEFORE touching the slip or navigating: probe the pinned
    //    /kurzy tab (where the session-timer marker is authoritative) instead
    //    of trusting a state that may be hours old. A dead session is reported
    //    (status frame → bridge login LOST → Telegram) and the command is
    //    refused, so the engine skips `not_logged_in` instead of driving a
    //    logged-out slip into an `unknown` placement and an auto-disarm.
    //    Sub-second on the normal path (one scripting eval; no navigation).
    const pinned = await chrome.tabs.query({ url: "*://*.tipsport.cz/*" });
    if (pinned.length && !(await probeLoggedIn(pinned[0].id))) {
      if (loggedIn) { loggedIn = false; await sendStatus(); }
      reply({ status: "error", message: "not_logged_in", timings });
      return;
    }
    tabId = await ensureTipsportTab(cmd.url, NAV_COMPLETE_CAP_MS);
    mark("nav_ms");
    await attach(tabId);
    mark("attach_ms");
    const r = await runPlacement(tabId, cmd, mark);
    reply({ ...r, timings });
  } catch (e) {
    reply({ status: "error", message: String(e && e.message ? e.message : e), timings });
  } finally {
    // Release the mutex only once the debugger session is gone — the next
    // placement's attach must never race this detach.
    if (tabId != null) { try { await detach(tabId); } catch {} }
    inFlight = false;
  }
}

async function runPlacement(tabId, cmd, mark) {
  // selectors.js is injected into the page (MAIN world) so window.__ots exists.
  // With the capped nav wait this may land on a page still being replaced —
  // waitForOdd re-injects whenever the probe finds no __ots context.
  try { await injectSelectors(tabId); } catch {}
  mark("inject_ms");

  // 1) Locate the odd button by opp_id — POLLING, not a single probe. The tab
  //    reports "complete" before Tipsport's React app renders the offer
  //    buttons, so one immediate probe reported offer_gone on EVERY live
  //    placement (6/6 on 2026-08-22/23, uniform ~5.4s latency = page load +
  //    one too-early look). Only a full timeout is a genuinely gone offer —
  //    and when that happens, attach a page-state snapshot to the result so
  //    the NEXT failure explains itself (wrong page? empty DOM? no odds
  //    rendered? interstitial?) instead of a bare "offer_gone".
  const meta = await waitForOdd(tabId, cmd.opp_id, ODD_WAIT_BUDGET_MS);
  mark("odd_ms");
  if (!meta || !meta.found) {
    let dbg = null;
    try { dbg = await evalPage(tabId, `(${pageDebug})(${JSON.stringify(cmd.opp_id)})`); } catch {}
    return { status: "rejected", message: "offer_gone", debug: dbg };
  }
  if (String(meta.matchId) !== String(cmd.match_id)) {
    return { status: "error", message: "match_id_mismatch" };
  }

  // 2) Trusted-click the odd -> adds to slip. The slip is SHARED MUTABLE
  //    STATE across placements, and nothing guarantees it starts empty: any
  //    earlier attempt that died between its own odd click and a terminal
  //    remove (the slip wait timing out on a slow page, setStake throwing, a
  //    CDP failure) left its leg behind. Clicking on top of that builds an
  //    ACCUMULATOR. So clear first. An unreadable count is a hard stop: the
  //    actuator must prove the slip is empty before adding anything.
  const legsBefore = await slipLegCount(tabId);
  if (legsBefore < 0) {
    return { status: "error", message: "slip_unreadable" };
  }
  if (legsBefore >= 1) {
    mark("slip_cleanup_legs", legsBefore);
    const legsLeft = await clearSlip(tabId);
    if (legsLeft !== 0) {
      // Could not empty the slip (remove selector drifted, or more legs than
      // maxLegs): refuse NOW rather than click, wait 5s for our leg, and fail
      // later with less information.
      return { status: "error", message: "slip_not_cleared", debug: { legs: legsBefore, legs_after: legsLeft } };
    }
    // Removing legs mutates the page; meta.rect is a VIEWPORT point measured
    // BEFORE that, so re-measure or a reflow/scroll restore would land the
    // trusted click on whatever moved under it. Failure to re-acquire the
    // exact offer is terminal; never click a stale viewport coordinate.
    // ≤2s of polling, not one probe: emptying the slip re-renders the odds
    // list, so a single look can miss the button that is about to repaint.
    const fresh = await waitForOdd(tabId, cmd.opp_id, 2000);
    if (!fresh || !fresh.found) {
      let dbg = null;
      try { dbg = await evalPage(tabId, `(${pageDebug})(${JSON.stringify(cmd.opp_id)})`); } catch {}
      return { status: "rejected", message: "offer_gone_after_cleanup", debug: dbg };
    }
    // Same check the first probe made: this opp under a DIFFERENT match
    // means the page is no longer the one we measured on.
    if (String(fresh.matchId) !== String(cmd.match_id)) {
      return { status: "error", message: "match_id_mismatch" };
    }
    meta.rect = fresh.rect;
  }
  await trustedClick(tabId, meta.rect);
  await waitFor(tabId, `(${slipHasOpp})(${JSON.stringify(cmd.opp_id)})`, 5000);
  mark("slip_ms");

  // 2b) …and exactly OUR leg must be on it now. slipHasOpp() only proves ours
  //     is present, and the min_odds guard below reads the slip TOTAL, which
  //     for two legs is their PRODUCT — it would clear any threshold
  //     trivially, stake a bet nobody asked for, and report the product as
  //     accepted_odds against a single selection's result. Refuse loudly
  //     (and leave the slip clean) instead. Exactly one is required; an
  //     unreadable or empty slip cannot reach the money gate.
  //     The two failures are distinct diagnoses and must not share a message:
  //     -1 is a BROKEN READER (selectors drifted, __ots gone) while 0 or ≥2 is
  //     a genuinely wrong slip, and the fix for each is a different one.
  const legs = await slipLegCount(tabId);
  if (legs !== 1) {
    const legsAfter = await clearSlip(tabId);
    return {
      status: "error",
      message: legs < 0 ? "slip_unreadable_after_click" : "slip_not_singleton",
      debug: { legs, legs_after: legsAfter },
    };
  }

  // 3) min_odds guard: read the live total odds; abort if the price moved down.
  const total = await evalPage(tabId, `(${readTotalOdds})()`);
  if (total == null || total < cmd.min_odds) {
    await clickRemove(tabId);
    return { status: "rejected", message: "price_below_min_odds", accepted_odds: total };
  }

  // 4) Set the stake via trusted input (React controlled input rejects the
  //    naive .value setter — verified live 2026-08-09).
  await setStake(tabId, cmd.stake_czk);
  mark("stake_ms");
  const expectedStake = Math.round(cmd.stake_czk);
  const actualStake = await waitForStake(tabId, expectedStake, STAKE_VERIFY_BUDGET_MS);
  mark("stake_verify_ms");
  if (actualStake !== expectedStake) {
    // stakeAmount() maps every unreadable field to null, so the parsed value
    // alone can't distinguish "wrong number" from "no digits at all" ("10 Kč",
    // empty, garbage). Carry the raw field text so the refusal diagnoses
    // itself. Explicit ?? null on both: JSON.stringify DROPS undefined keys,
    // and a missing key in the bridge log reads as a missing code path.
    let rawStake = null;
    try { rawStake = await evalPage(tabId, "window.__ots.stakeRaw()"); } catch {}
    await clickRemove(tabId);
    return {
      status: "error", message: "stake_mismatch",
      debug: {
        expected_stake: expectedStake,
        actual_stake: actualStake ?? null,
        actual_raw: rawStake ?? null,
      },
    };
  }

  // 5) Re-check login; never submit while logged out. (Probes THIS tab and,
  //    since 0.5.0, without the debugger — the placement's CDP session stays
  //    attached through steps 6-7.)
  if (!(await probeLoggedIn(tabId))) {
    await clickRemove(tabId);
    return { status: "error", message: "not_logged_in" };
  }

  // 6) Money gate.
  if (!CFG.CONFIRM_ENABLED) {
    const label = await evalPage(tabId, `(${readSubmitLabel})()`);
    await clickRemove(tabId); // leave the slip clean in verification mode
    return { status: "rejected", message: "confirm_disabled", accepted_odds: total, submit_label: label };
  }

  // 7) Confirm. The submit click navigates the tab to /tiket?idu=<id> (the
  //    ticket receipt), which DETACHES chrome.debugger — so we wait for that
  //    navigation, RE-ATTACH, then read the receipt. Contract characterized
  //    live during the first supervised placement 2026-08-10. Reconciliation
  //    (reading bet history) remains the safety net.
  const submitRect = await evalPage(tabId, `(${readSubmitRect})()`);
  if (!submitRect) {
    // Off-screen or missing submit: refuse and leave the slip clean rather
    // than click a viewport coordinate that is not the button.
    await clickRemove(tabId);
    return { status: "error", message: "submit_unreachable" };
  }
  await trustedClick(tabId, submitRect);
  await waitForTicketPage(tabId, 12000);
  await attach(tabId); // re-attach after the post-submit navigation detached us
  const outcome = await evalPage(tabId, `(${readOutcome})()`, 8000);
  mark("outcome_ms");
  return outcome || { status: "unknown", message: "no_confirmation_parsed" };
}

// After submit, Tipsport navigates to https://www.tipsport.cz/tiket?idu=...
// Poll the tab URL (no debugger needed) until it lands on /tiket or we time
// out; either way we return and let readOutcome classify what we actually see.
function waitForTicketPage(tabId, timeoutMs) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const check = () => chrome.tabs.get(tabId, (t) => {
      if (chrome.runtime.lastError) { resolve(); return; }
      if (t && /\/tiket\b/.test(t.url || "") && t.status === "complete") { resolve(); return; }
      if (Date.now() > deadline) { resolve(); return; }
      setTimeout(check, 250);
    });
    check();
  });
}

// ---------------------------------------------------------------------------
// Page-probe functions (stringified, run in MAIN world via CDP Runtime.evaluate)
// These call window.__ots from selectors.js.
// ---------------------------------------------------------------------------

// Page-state snapshot attached to an offer_gone timeout: where the tab
// actually landed and whether ANY odd buttons rendered — distinguishes a
// truly pulled offer (page fine, other odds present, ours absent) from a
// wrong/blank/unrendered page.
const pageDebug = (oppId) => {
  const d = {
    href: location.href,
    title: (document.title || "").slice(0, 60),
    atids: document.querySelectorAll("[data-atid]").length,
    odd_atids: document.querySelectorAll('[data-atid^="content||ODD||"]').length,
    ots: !!window.__ots,
    logged_in_timer: /Doba přihlášení/.test(document.body ? document.body.innerText : ""),
  };
  // Per-opp geometry (0.8.3): absent vs hidden vs never-on-screen — the three
  // different bugs a bare offer_gone used to hide behind.
  try { if (window.__ots && window.__ots.oddDiagnostics) d.opp = window.__ots.oddDiagnostics(oppId); } catch (e) {}
  return d;
};

const probeOdd = (oppId) => {
  const el = window.__ots.findOddButton(oppId);
  if (!el) return { found: false };
  const m = window.__ots.parseAtid(el.getAttribute("data-atid"));
  // Bring the button ON SCREEN before measuring: the rect below becomes a
  // trusted click at VIEWPORT coordinates, and on the /co-se-sazi match tab the
  // main market sits well below the fold of the actuator window — a click at
  // an off-screen y lands on nothing (2026-09-20/23 forensics).
  try { el.scrollIntoView({ block: "center", inline: "nearest" }); } catch (e) {}
  const r = el.getBoundingClientRect();
  // A found button is not yet a clickable one: a node probed mid-render or
  // still off-screen after the scroll measures 0x0, at a negative origin, or
  // beyond the viewport, and the returned centre would send trustedClick to
  // the viewport origin or off the page. (Zero-size DUPLICATES of the same
  // offer are already skipped by findOddButton itself.) Report not-found
  // instead: this runs inside waitForOdd's poll, so a button that simply has
  // not been laid out yet is retried, and only a full budget is a real
  // offer_gone.
  if (r.width === 0 || r.height === 0 || r.x < 0 || r.y < 0 || r.bottom > window.innerHeight || r.right > window.innerWidth) return { found: false };
  return { found: true, matchId: m.matchId, type: m.type, odd: m.odd,
           rect: { x: r.x + r.width / 2, y: r.y + r.height / 2 } };
};
const slipHasOpp = (oppId) => window.__ots.selectionInSlip(oppId);
const readTotalOdds = () => window.__ots.totalOdds();
const readSubmitLabel = () => { const b = window.__ots.submitButton(); return b ? b.textContent.trim() : null; };
const readSubmitRect = () => window.__ots.clickPoint(window.__ots.submitButton());
const readOutcome = () => {
  // Runs on the post-submit ticket page (/tiket?idu=<id>). Success signals,
  // characterized live 2026-08-10: the body carries "Tiket byl vsazen." and
  // the URL is /tiket?idu=<ticketId>. Anything else stays "unknown" so the
  // pipeline auto-disarms rather than assuming a bet landed.
  const body = document.body ? document.body.innerText : "";
  const idu = (location.href.match(/idu=(-?\d+)/) || [])[1] || null;
  const placed = /Tiket byl vsazen/i.test(body) || (/\/tiket\?/.test(location.href) && idu);
  if (!placed) {
    return { status: "unknown", message: "no_ticket_confirmation" };
  }
  // Accepted odds + stake from the ticket detail ("Celkový kurz 2.30",
  // "Vklad 10,00 Kč"). Comma decimals -> dots.
  const detail = document.querySelector('[data-atid="ticketDetailContent"]');
  const text = detail ? detail.innerText : body;
  const num = (m) => (m ? parseFloat(m[1].replace(/\s/g, "").replace(",", ".")) : null);
  const accepted = num(text.match(/Celkový kurz\s*([0-9]+[.,][0-9]+)/i));
  // Balance: best-effort from the account-state chip (styled-component class,
  // no stable data-atid) — reconciliation is the real safety net.
  let balance = null;
  const bal = document.querySelector('[class*="AccountState"]');
  if (bal) balance = num((bal.textContent || "").match(/([0-9][0-9\s]*[.,][0-9]{2})/));
  return { status: "placed", receipt_id: idu, accepted_odds: accepted, balance_czk: balance };
};

// ---------------------------------------------------------------------------
// CDP helpers
// ---------------------------------------------------------------------------

function cdp(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params || {}, (res) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message));
      else resolve(res);
    });
  });
}

async function attach(tabId) {
  await new Promise((resolve, reject) => {
    chrome.debugger.attach({ tabId }, "1.3", () => {
      const err = chrome.runtime.lastError;
      if (err && !/already attached/i.test(err.message)) reject(new Error(err.message));
      else resolve();
    });
  });
  await cdp(tabId, "Runtime.enable");
}
async function detach(tabId) {
  await new Promise((resolve) => chrome.debugger.detach({ tabId }, () => resolve()));
}

async function evalPage(tabId, expr, awaitMs) {
  const res = await cdp(tabId, "Runtime.evaluate", {
    expression: awaitMs ? `Promise.resolve(${expr})` : expr,
    returnByValue: true,
    awaitPromise: !!awaitMs,
  });
  return res && res.result ? res.result.value : null;
}

// Injects selectors.js source into the page's MAIN world so window.__ots exists.
async function injectSelectors(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["selectors.js"],
    world: "MAIN",
  });
}

async function trustedClick(tabId, rect) {
  const base = { x: Math.round(rect.x), y: Math.round(rect.y), button: "left", clickCount: 1 };
  await humanDelay();
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x: base.x, y: base.y });
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", ...base });
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", ...base });
}

async function setStake(tabId, stake) {
  // Scrolled on screen + viewport-checked by clickPoint: the slip may sit
  // below the fold after the odd click scrolled the page.
  const rect = await evalPage(tabId, "window.__ots.clickPoint(window.__ots.stakeInput())");
  if (!rect) throw new Error("stake_input_unreachable");
  await trustedClick(tabId, rect);
  // Select-all then type the stake as trusted input.
  await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyDown", modifiers: 2, key: "a", code: "KeyA", windowsVirtualKeyCode: 65 });
  await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyUp", modifiers: 2, key: "a", code: "KeyA", windowsVirtualKeyCode: 65 });
  await cdp(tabId, "Input.insertText", { text: String(Math.round(stake)) });
}

async function waitForStake(tabId, expected, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let actual = null;
  while (Date.now() < deadline) {
    try { actual = await evalPage(tabId, "window.__ots.stakeAmount()"); } catch { actual = null; }
    if (actual === expected) return actual;
    await sleep(100);
  }
  return actual;
}

async function clickRemove(tabId) {
  const rect = await evalPage(tabId, "window.__ots.clickPoint(window.__ots.removeButton())");
  if (rect) await trustedClick(tabId, rect);
}

// Legs currently on the slip via selectors.js; -1 = could not inspect. Never
// throws: the caller treats the sentinel as a hard stop.
async function slipLegCount(tabId) {
  let n = null;
  try { n = await evalPage(tabId, "window.__ots.slipLegCount()"); } catch { n = null; }
  return typeof n === "number" ? n : -1;
}

// Remove every leg (bounded) — a leftover from an earlier aborted placement
// would turn the next single into an accumulator. Returns the count after.
// Worst case ~5 × (a click + 400ms + two evals); a remove that can't fire just
// burns the iterations, so the caller still gets a truthful final count.
async function clearSlip(tabId, maxLegs = 5) {
  let legs = await slipLegCount(tabId);
  for (let i = 0; i < maxLegs && legs >= 1; i++) {
    try { await clickRemove(tabId); } catch {}
    await sleep(400);
    legs = await slipLegCount(tabId);
  }
  return legs;
}

async function waitForOdd(tabId, oppId, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let meta = null;
  while (Date.now() < deadline) {
    try {
      meta = await evalPage(tabId, `(${probeOdd})(${JSON.stringify(oppId)})`);
      if (meta && meta.found) return meta;
      if (meta == null) {
        // Page-side exception: window.__ots is gone (the capped nav wait
        // means we can start while the navigation is still replacing the
        // document) — re-inject and keep polling.
        try { await injectSelectors(tabId); } catch {}
      }
    } catch {
      // CDP-level failure (execution context destroyed mid-navigation) —
      // same recovery: re-inject on the fresh document and keep polling.
      meta = null;
      try { await injectSelectors(tabId); } catch {}
    }
    await sleep(200);
  }
  return meta;
}

async function waitFor(tabId, expr, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evalPage(tabId, expr)) return true;
    await sleep(150);
  }
  throw new Error("timeout waiting for slip state");
}

// Login probe — debugger-FREE since 0.5.0. The old CDP probe attach/detach'd
// the tab's single debugger session; running every 30s (and at placement
// step 5) it could detach the session out from under a LIVE placement, whose
// next CDP call then dies mid-money-path (never observed only because every
// real attempt so far ended earlier at offer_gone). chrome.scripting
// evaluates in the MAIN world without touching the debugger at all.
async function probeLoggedIn(tabId) {
  try {
    const id = tabId != null ? tabId : await ensureTipsportTab(null, PROBE_COMPLETE_CAP_MS);
    await injectSelectors(id); // idempotent IIFE; keeps selectors.js the single DOM source
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: id },
      world: "MAIN",
      func: () => !!(window.__ots && window.__ots.loggedIn()),
    });
    return !!(res && res.result);
  } catch { return false; }
}

// Where the tab actually is — the confirm above only trusts a login reading
// taken ON the probe page (a stale "complete" from the outgoing page, or a
// slow /kurzy load, must read as "no observation", never as logged out).
async function probePath(tabId) {
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => location.pathname,
    });
    return res && typeof res.result === "string" ? res.result : null;
  } catch { return null; }
}

// ---------------------------------------------------------------------------
// Tab management
// ---------------------------------------------------------------------------

async function ensureTipsportTab(url, completeCapMs) {
  const target = url ? CFG.TIPSPORT_ORIGIN + url : CFG.TIPSPORT_ORIGIN + "/kurzy";
  const tabs = await chrome.tabs.query({ url: "*://*.tipsport.cz/*" });
  if (tabs.length) {
    if (url) await chrome.tabs.update(tabs[0].id, { url: target });
    await waitTabComplete(tabs[0].id, completeCapMs);
    return tabs[0].id;
  }
  const tab = await chrome.tabs.create({ url: target, active: true });
  await waitTabComplete(tab.id, completeCapMs);
  return tab.id;
}

// Close every Tipsport tab beyond the first — probes and placements all target
// tabs[0], so strays (e.g. opened during a manual VNC login) would leave them
// operating on a page nobody is looking at. Yields to a live placement
// (inFlight) — enforced, not assumed.
async function pruneTipsportTabs() {
  if (inFlight) return;
  try {
    const tabs = await chrome.tabs.query({ url: "*://*.tipsport.cz/*" });
    for (const t of tabs.slice(1)) {
      try { await chrome.tabs.remove(t.id); } catch {}
    }
  } catch {}
}

// Resolves on tab status "complete", or after maxMs. Reached only through
// ensureTipsportTab, and every path into it supplies a cap —
// NAV_COMPLETE_CAP_MS on the placement path, PROBE_COMPLETE_CAP_MS on every
// probe-side path — but the deadline logic below still tolerates a falsy
// maxMs defensively rather than assuming that discipline holds.
function waitTabComplete(tabId, maxMs) {
  return new Promise((resolve) => {
    const deadline = maxMs ? Date.now() + maxMs : null;
    const check = () => chrome.tabs.get(tabId, (t) => {
      // A closed tab must not poll forever — the probe path would pin `probing` for good.
      if (chrome.runtime.lastError) { resolve(); return; }
      if (t && t.status === "complete") { resolve(); return; }
      if (deadline && Date.now() > deadline) { resolve(); return; }
      setTimeout(check, 200);
    });
    check();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const humanDelay = () => sleep(120 + Math.floor(Math.random() * 180));

// Connect as soon as the worker is instantiated (covers the case where the
// worker is woken by something other than onStartup/onInstalled).
ensureConnected();
