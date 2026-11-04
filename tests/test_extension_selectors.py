"""DOM-contract lock for the betting extension (Phase 2).

The extension's actuation is verified live on the VM (Playwright can't run in
CI — the browser binary is deliberately absent). What CI *can* guarantee is
that the DOM contract the extension depends on still matches the real markup
captured from tipsport.cz, and that the extension source references exactly
those selectors. Both halves read the same captured fixture + the same JS
source, so a selector regression in either the extension or the fixture is
caught here.

Evidence: tests/fixtures/tipsport_offer_slip.html (captured 2026-08-09,
match 8355990) + the offer-API cross-check recorded in the Phase-2 plan.
"""

import re
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "tipsport_offer_slip.html"
LOGIN_FIXTURE = Path(__file__).parent / "fixtures" / "tipsport_logged_in_header.html"
RECEIPT_FIXTURE = Path(__file__).parent / "fixtures" / "tipsport_ticket_receipt.html"
SELECTORS_JS = Path(__file__).parents[1] / "extension" / "selectors.js"
BACKGROUND_JS = Path(__file__).parents[1] / "extension" / "background.js"
MANIFEST_JSON = Path(__file__).parents[1] / "extension" / "manifest.json"
CONFIG_DEFAULT_JS = Path(__file__).parents[1] / "extension" / "config.default.js"
CHROME_ENTRYPOINT = Path(__file__).parents[1] / "docker" / "chrome" / "entrypoint.sh"


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def login_html() -> str:
    return LOGIN_FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def receipt_html() -> str:
    return RECEIPT_FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def background_src() -> str:
    return BACKGROUND_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def selectors_src() -> str:
    return SELECTORS_JS.read_text(encoding="utf-8")


def _atids(html: str) -> list[str]:
    return re.findall(r'data-atid="([^"]+)"', html)


def test_odd_button_atid_grammar_maps_stored_ids(html):
    """The stored tipsport_opp_id must locate the exact odd button, and the
    button's data-atid must expose match_id + type at the fields the extension
    reads. This is the mapping the whole placement path relies on."""
    odd_atids = [a for a in _atids(html) if a.startswith("content||ODD||")]
    assert len(odd_atids) == 2  # home + away for the fixture match

    # Grammar: content || ODD || <oppId> || <eventId> || <odd> || <matchId> || <type>
    home = next(a for a in odd_atids if a.endswith("||1"))
    parts = home.split("||")
    assert parts[0] == "content" and parts[1] == "ODD"
    opp_id, _event_id, odd, match_id, sel_type = parts[2], parts[3], parts[4], parts[5], parts[6]
    assert opp_id == "2681257493"      # == value_bets.tipsport_opp_id
    assert match_id == "8355990"       # == value_bets.tipsport_match_id (field 6)
    assert sel_type == "1"             # 1 = home
    assert odd == "1.91"
    # opp_id is unique -> an exact oppId match resolves exactly one button.
    assert sum(1 for a in odd_atids if a.split("||")[2] == opp_id) == 1


def test_bet_slip_controls_present_in_fixture(html):
    atids = set(_atids(html))
    assert "txtAmountPaid" in atids                     # stake input
    assert "btn-betTicket" in atids                     # submit
    assert "ticketBuilderRemoveOppButton" in atids      # abort-on-price-moved
    assert "ticketBuilderChangeBet-2681257493" in atids  # slip echoes our opp_id
    assert "ticketBuilderOportunity-8355990" in atids    # slip echoes our match_id
    # Live total-odds readout for the min_odds guard.
    assert "Celkový kurz" in html
    assert re.search(r"Celkový kurz</div><div[^>]*>1\.91</div>", html)


def test_extension_source_references_the_contract(selectors_src):
    """The extension must key on these stable data-atids — never the
    styled-component class hashes (which rotate per build)."""
    for token in (
        "content||ODD||",
        "txtAmountPaid",
        "btn-betTicket",
        "ticketBuilderRemoveOppButton",
        "ticketBuilderChangeBet-",
        "Celkový kurz",
    ):
        assert token in selectors_src, f"extension/selectors.js must reference {token!r}"
    # Guard against a fragile styled-components-hash selector sneaking in.
    assert "sc-6e89a8b6" not in selectors_src
    assert "hKYbrs" not in selectors_src


def test_logged_in_header_fixture_carries_the_markers(login_html):
    """Login markers verified on the real logged-in session (2026-08-10):
    the avatar atid and the session timer. The timer label is followed by
    &nbsp;, so any matcher must key on the bare label, never 'label: '."""
    assert 'data-atid="profile-avatar"' in login_html
    assert "Doba přihlášení:&nbsp;" in login_html


def test_logged_out_fixture_has_no_login_markers(html):
    """The logged-out capture must NOT contain the logged-in markers —
    otherwise the probe could false-positive on a logged-out page."""
    assert "profile-avatar" not in html
    assert "Doba přihlášení" not in html


def test_login_probe_keys_on_the_session_timer_only(selectors_src):
    """loggedIn() must key ONLY on the session timer ("Doba přihlášení"),
    which Tipsport renders solely for an authenticated session. The avatar
    atid is NOT a login marker: the pinned /kurzy probe page carries a feed
    of OTHER users' avatars (20+ `profile-avatar` nodes observed 2026-09-09),
    so an avatar-based probe reported logged-in for days while the session
    was dead. The avatar may appear in comments/diagnostics, never as a
    positive signal inside loggedIn()."""
    # selectors.js functions are indented inside an IIFE, so slice to the next
    # indented `function` rather than relying on the top-level helper.
    start = _locate("function loggedIn(", selectors_src, "loggedIn")
    end = _locate("\n  function ", selectors_src, "next selectors.js function", start + 1)
    body = _strip_js_comments(selectors_src[start:end])
    assert "Doba přihlášení" in body
    assert "profile-avatar" not in body
    assert "loginMarkers" in selectors_src          # diagnostics for status frames
    assert "accountBalance" not in selectors_src


def test_place_bet_login_gate_precedes_navigation(background_src):
    """A placement must re-verify login on the pinned /kurzy tab (where the
    session-timer marker is authoritative) BEFORE navigating to the match
    page or touching the slip, and a dead session must be REPORTED (status
    frame) and refused `not_logged_in` — never driven into an `unknown`
    placement that auto-disarms the engine mid-slate."""
    body = _strip_js_comments(_function_body(background_src, "handlePlaceBet"))
    set_at = _locate("inFlight = true;", body, "mutex set")
    gate_at = _locate("probeLoggedIn(pinned[0].id)", body, "pre-nav login gate", set_at)
    report_at = _locate("await sendStatus();", body, "status frame on a dead session", gate_at)
    refuse_at = _locate('"not_logged_in"', body, "not_logged_in refusal", gate_at)
    nav_at = _locate("ensureTipsportTab(cmd.url", body, "match-page navigation", gate_at)
    assert set_at < gate_at < report_at < nav_at and gate_at < refuse_at < nav_at, body


def test_receipt_fixture_carries_the_placement_markers(receipt_html):
    """The placed-bet receipt (captured live 2026-08-10) must carry the
    success text, the ticket-detail container, and the accepted-odds/stake
    labels the outcome parser reads."""
    assert "Tiket byl vsazen." in receipt_html
    assert 'data-atid="ticketDetailContent"' in receipt_html
    assert "Celkový kurz" in receipt_html
    assert "Vklad" in receipt_html


def test_readoutcome_keys_on_the_verified_receipt_contract(background_src):
    """background.js readOutcome() must classify success from the real
    receipt signals (the /tiket idu URL + 'Tiket byl vsazen' + 'Celkový
    kurz'), not the old placeholder that always returned unknown."""
    assert "Tiket byl vsazen" in background_src
    assert "Celkový kurz" in background_src
    assert "idu=" in background_src
    assert "ticketDetailContent" in background_src
    # the old placeholder marker must be gone
    assert "receipt_parser_phase4" not in background_src
    # post-submit navigation must be handled (re-attach after /tiket nav)
    assert "waitForTicketPage" in background_src


def _strip_js_comments(src: str) -> str:
    """Blank out full-line `//` comments. Deliberately does NOT touch trailing
    comments: splitting on `//` would also truncate `startsWith("//")`."""
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in src.splitlines())


def _locate(needle: str, text: str, what: str, start: int = 0) -> int:
    """Like str.index, but fails with an assertion message naming `what`
    instead of a bare ValueError."""
    i = text.find(needle, start)
    assert i != -1, f"{what}: {needle!r} not found"
    return i


def _must_find(pattern: str, text: str, what: str) -> re.Match:
    """Like re.search, but asserts the match exists so a failure names `what`
    instead of raising AttributeError on None."""
    m = re.search(pattern, text)
    assert m, f"{what}: pattern {pattern!r} not found"
    return m


def _function_body(src: str, name: str) -> str:
    """Source of one top-level `async function <name>`, sliced at the next
    top-level function declaration, with full-line comments stripped — so
    ordering assertions read statements, not prose about them."""
    start = _locate(f"async function {name}(", src, name)
    ends = [i for i in (src.find("\nasync function ", start + 1),
                        src.find("\nfunction ", start + 1)) if i != -1]
    return _strip_js_comments(src[start:min(ends)] if ends else src[start:])


def test_place_bet_busy_guard_is_atomic(background_src):
    """The `busy` refusal only works because nothing yields between the
    `if (inFlight)` check and `inFlight = true` — one `await` slipped in for a
    log or metric would let two place_bet commands both pass the check and
    drive the single bet-slip tab at once."""
    start = _locate("async function handlePlaceBet(", background_src, "handlePlaceBet")
    end = _locate("inFlight = true;", background_src, "inFlight = true;", start)
    prologue = _strip_js_comments(background_src[start:end])
    assert "if (inFlight)" in prologue
    assert '"busy"' in prologue
    assert not re.search(r"\bawait\b", prologue), prologue


def test_tab_navigating_helpers_yield_to_a_live_placement(background_src):
    """confirmLoginOnProbePage navigates the shared tab and pruneTipsportTabs
    closes tabs; both are reached from alarm handlers (the 30s keepalive and
    the 60-min session-refresh) whose entry checks race a ~50s placement.
    The yield must be the FIRST statement of each body (after any comment), or
    the reload lands on top of a live bet slip. confirmLoginOnProbePage yields
    the CURRENT `loggedIn` state rather than a hardcoded false: a microtask
    interleave could clear `inFlight` between the yield's return and the
    caller's re-check, and a hardcoded false would then read as a real
    logout instead of no observation."""
    for fn, ret in (("confirmLoginOnProbePage", "return loggedIn;"), ("pruneTipsportTabs", "return;")):
        start = _locate(f"async function {fn}(", background_src, fn)
        body = _strip_js_comments(background_src[start:])
        first_stmt = body.split("{", 1)[1].strip().splitlines()[0].strip()
        assert first_stmt == f"if (inFlight) {ret}", (fn, first_stmt)


def test_login_confirm_only_trusts_probe_page_observations(background_src):
    """confirmLoginOnProbePage must (1) check the tab's path against PROBE_PAGE
    before trusting a login reading — tabs.update resolves before the navigation
    commits, so the outgoing (drifted) page can report 'complete' — and (2) turn
    a run of never-reached-the-page confirms into a real 'lost' only after
    INCONCLUSIVE_CONFIRMS_BEFORE_LOST, returning the current state meanwhile.
    probeLoggedIn itself stays a plain boolean: the money path calls it."""
    start = _locate("async function confirmLoginOnProbePage(", background_src, "confirmLoginOnProbePage")
    end = _locate("async function sendStatus(", background_src, "sendStatus", start)
    body = _strip_js_comments(background_src[start:end])
    assert "probePath(tabId)" in body
    assert "path.startsWith(PROBE_PAGE)" in body
    assert "inconclusiveConfirms >= INCONCLUSIVE_CONFIRMS_BEFORE_LOST" in body
    assert "return loggedIn;" in body
    assert re.search(r"^const INCONCLUSIVE_CONFIRMS_BEFORE_LOST = \d+;", background_src, re.M)
    probe = background_src[_locate("async function probeLoggedIn(", background_src, "probeLoggedIn"):]
    assert "return !!(res && res.result);" in probe.split("\n}", 1)[0]
    # every conclusive exit resets the run counter; only the inconclusive
    # `return loggedIn;` leaves it standing
    assert body.count("inconclusiveConfirms = 0") == 3, body
    # a reading taken off the probe page must skip the iteration WITHOUT
    # recording an observation — inverting this is the bug being fixed
    gate = _must_find(r"if \(path == null \|\| !path\.startsWith\(PROBE_PAGE\)\)\s*\{([^}]*)\}",
                      body, "probe-page gate").group(1)
    assert "continue;" in gate and "observed" not in gate, gate
    # once the page is here, the header gets render grace before a verdict
    assert "deadline = Math.max(deadline, Date.now() + 3000)" in body
    # ...which REASSIGNS the deadline: a `const` here throws on the very first
    # observation, inside the try, so every confirm would fall to the bare
    # `catch { return false; }` and report a false LOST through a live session.
    assert "let deadline = Date.now() + 8000" in body


def test_positive_plain_probe_clears_the_inconclusive_run(background_src):
    """reportStatus must reset inconclusiveConfirms on a positive probeLoggedIn()
    reading: without it the counter is a lifetime tally, and the unconditional
    hourly session-refresh confirm turns three inconclusive navigations hours apart
    into a false 'login lost' through a healthy session (every bet dispatched
    in that window is skipped not_logged_in and permanently burned)."""
    start = _locate("async function reportStatus(", background_src, "reportStatus")
    end = _locate("const PROBE_PAGE", background_src, "PROBE_PAGE", start)
    body = _strip_js_comments(background_src[start:end])
    probe_at = _locate("let now = await probeLoggedIn();", body, "plain probe")
    reset_at = _locate("if (now) inconclusiveConfirms = 0;", body, "positive-probe reset")
    assert probe_at < reset_at
    assert reset_at < _locate("confirmLoginOnProbePage()", body, "confirm call")


def test_slip_singleton_guard_present_and_fail_safe(background_src, selectors_src, html):
    """The bet slip is shared mutable state across placements, and nothing
    guarantees it starts empty: any attempt that died between its odd click and
    a terminal remove (the slip wait timing out, setStake throwing, a CDP
    failure) left its leg behind. The next placement's click then builds an
    ACCUMULATOR — slipHasOpp() is still true, and the min_odds guard reads the
    slip TOTAL, which for two legs is their PRODUCT and clears any threshold
    trivially — so real money goes on a two-leg ticket that the pipeline
    records as one selection at the product price. runPlacement must clear the
    slip before its click and refuse a non-singleton slip after it, while
    failing CLOSED so an uninspectable count (-1) blocks the money path.

    Two follow-ons are pinned here too. (1) The cleanup runs up to ~3.5s of
    DOM-mutating remove clicks between the rect measurement inside waitForOdd
    and the trusted click that uses it, so the rect must be RE-MEASURED after
    a cleanup — by POLLING (the re-render that motivates the re-measure is
    also what makes a single probe miss the button) and with the re-probed
    button's match id re-checked before its rect is trusted, a DIFFERENT match
    being terminal (match_id_mismatch) rather than a fall-back to the stale
    rect — otherwise the click lands on whatever reflowed under a stale
    viewport point, and a slip that could NOT be emptied must be refused
    (slip_not_cleared) instead of clicked onto. (2) The page-context counter
    counts DISTINCT leg ids: the fixture is a trimmed fragment and cannot rule
    out duplicated slip markup, which under `.length` would read 0 on a clean
    slip and 2 once our own single leg exists — slip_not_singleton on every
    placement, two consecutive failures, engine auto-disarmed."""
    # Premise: one leg == exactly one ticketBuilderChangeBet-<oppId> control
    # (the captured live slip holds a single selection).
    assert html.count('data-atid="ticketBuilderChangeBet-') == 1

    # Page-context counter keys on that same control and is exported.
    c_start = _locate("function slipLegCount()", selectors_src, "selectors.slipLegCount")
    counter = selectors_src[c_start:_locate("\n  }", selectors_src, "slipLegCount body", c_start)]
    assert 'ticketBuilderChangeBet-' in counter
    assert "return -1" in counter, counter          # fail-safe sentinel
    assert "slipLegCount," in selectors_src         # on window.__ots
    # ...counting distinct ids, not matched nodes (duplicated markup is fatal).
    assert "new Set(" in counter, counter
    assert "ids.size" in counter, counter
    assert not re.search(r"\)\.length", counter), counter   # not a node count

    body = _function_body(background_src, "runPlacement")
    click_at = _locate("trustedClick(tabId, meta.rect)", body, "odd click")
    slip_wait_at = _locate("slipHasOpp", body, "slip wait")
    total_at = _locate("readTotalOdds", body, "min_odds total-odds read")

    # Pre-click: a leftover leg is cleared BEFORE ours is added. Positional,
    # not a bare `in` — the post-click cleanup satisfies that on its own.
    cleanup_at = _locate("legsBefore >= 1", body, "pre-click slip cleanup")
    clear_at = _locate("clearSlip(tabId)", body, "pre-click clearSlip call")
    assert cleanup_at < clear_at < click_at, body
    # ...and its return value is acted on: legs still there => refuse, never
    # click onto a slip we failed to empty. The threshold is pinned too — a
    # `legsLeft >= 99` typo leaves the refusal unreachable while every
    # positional assertion above still passes.
    assert _locate('"slip_not_cleared"', body, "slip_not_cleared refusal") < click_at, body
    assert "legsLeft !== 0" in body, body
    assert "legsBefore < 0" in body, body
    # ...then the rect is re-measured, after the mutating cleanup and before
    # the click that consumes it — by POLLING, not one probe: emptying the slip
    # collapses the ticket aside and React re-renders the odds list, which is
    # exactly the moment a single look comes back {found:false} and silently
    # falls back to the stale point the re-measure exists to replace.
    remeasure_at = _locate("waitForOdd(tabId, cmd.opp_id, ", body, "rect re-measure poll", clear_at)
    # ...and the re-probed button's match id is re-checked BEFORE its rect is
    # trusted, exactly as the first probe checked it. findOddButton compares
    # the parsed oppId exactly, so this is not about a substring collision:
    # it guards against the PAGE swapping match identity under the re-render
    # (a navigation or feed update landing a different match while we cleaned
    # the slip). That mismatch is TERMINAL, not a fall back to the old rect —
    # a page carrying this opp under another match is no longer the page the
    # first rect was measured on either, so clicking it is blind, and
    # slipHasOpp (oppId only) would not catch the wrong leg.
    recheck_at = _locate("String(fresh.matchId) !== String(cmd.match_id)", body,
                         "re-measured match id re-check", remeasure_at)
    refuse_at = _locate('"match_id_mismatch"', body, "re-measure mismatch refusal", recheck_at)
    assign_at = _locate("meta.rect = fresh.rect", body, "rect re-measure", clear_at)
    assert clear_at < remeasure_at < recheck_at < refuse_at < assign_at < click_at, body

    # Post-click: the singleton check sits between the slip wait and the
    # min_odds read — after it there is a leg to count, before it the total
    # would already have been trusted.
    post_at = _locate("legs !== 1", body, "post-click singleton check")
    assert slip_wait_at < post_at < total_at, body
    assert '"slip_not_singleton"' in body
    # ...and the two failures it now covers keep DISTINCT messages: -1 is a
    # broken reader (selectors drifted, __ots gone), 0-or-≥2 is a genuinely
    # wrong slip, and they call for different fixes. One shared message would
    # send the operator hunting the wrong one.
    assert '"slip_unreadable_after_click"' in body, body
    assert 'legs < 0 ? "slip_unreadable_after_click"' in body, body

    # Bounded cleanup + a counter that maps anything non-numeric to -1. The
    # bound is what makes the worst case ~3.5s and keeps slip_not_cleared
    # reachable at all rather than looping on an undismissable slip.
    max_legs = int(_must_find(r"async function clearSlip\(tabId, maxLegs = (\d+)\)",
                              background_src, "clearSlip bound").group(1))
    assert 1 <= max_legs <= 5, max_legs
    assert 'typeof n === "number" ? n : -1' in background_src


def test_stake_is_read_back_before_the_money_gate(background_src, selectors_src):
    body = _function_body(background_src, "runPlacement")
    set_at = _locate("setStake(tabId, cmd.stake_czk)", body, "stake input")
    verify_at = _locate("waitForStake(tabId, expectedStake", body, "stake read-back")
    money_at = _locate("if (!CFG.CONFIRM_ENABLED)", body, "money gate")
    assert set_at < verify_at < money_at
    assert 'message: "stake_mismatch"' in body
    assert "function stakeAmount()" in selectors_src
    assert "stakeAmount," in selectors_src

    # The refusal must diagnose itself: stakeAmount() collapses "10 Kč", "" and
    # outright garbage to the same null, so the RAW field text is the only
    # thing that says which one happened — and it must be read BEFORE the
    # clickRemove that empties the slip (and with it the field).
    raw_at = _locate("window.__ots.stakeRaw()", body, "raw stake read", verify_at)
    remove_at = _locate("clickRemove(tabId)", body, "stake-mismatch cleanup", raw_at)
    assert verify_at < raw_at < remove_at < money_at, body
    assert "actual_raw" in body, body
    # ...and both reported values are explicitly null-coalesced: JSON.stringify
    # DROPS undefined keys, so an undefined would vanish from the bridge log
    # and read as a code path that never ran.
    assert "actual_stake: actualStake ?? null" in body, body
    assert "actual_raw: rawStake ?? null" in body, body
    assert "function stakeRaw()" in selectors_src
    assert "stakeRaw," in selectors_src
    # Empty is UNREADABLE, not zero: Number("") is 0, which would sail through
    # a `== expected` check the moment the trusted input silently failed and
    # the expected stake happened to be 0 — and, worse, reads as a real stake.
    s_start = _locate("function stakeAmount()", selectors_src, "selectors.stakeAmount")
    reader = selectors_src[s_start:_locate("\n  }", selectors_src, "stakeAmount body", s_start)]
    assert "if (!raw) return null;" in reader, reader
    assert _locate("if (!raw) return null;", reader, "empty guard") < \
        _locate("Number(raw)", reader, "numeric parse"), reader


def test_probe_odd_refuses_a_degenerate_rect(background_src):
    """probeOdd's rect becomes a TRUSTED CLICK POINT. A hidden breakpoint
    duplicate of the odd button (the same duplicated-markup risk slipLegCount
    counts around) or one probed mid-render measures 0x0 / at a negative
    origin, and its centre would send the click to the viewport origin —
    whatever sits there. The guard must sit between the rect read and the
    found:true return, and report found:false (NOT a throw or a rejection) so
    it stays inside waitForOdd's poll: a not-yet-laid-out button is simply
    retried, and only a full budget is a real offer_gone."""
    start = _locate("const probeOdd = ", background_src, "probeOdd")
    body = _strip_js_comments(background_src[start:_locate("const slipHasOpp", background_src,
                                                          "probeOdd end", start)])
    rect_at = _locate("getBoundingClientRect()", body, "rect measurement")
    guard_at = _locate("r.width === 0", body, "degenerate-rect guard")
    found_at = _locate("found: true", body, "found:true return")
    assert rect_at < guard_at < found_at, body
    for term in ("r.height === 0", "r.x < 0", "r.y < 0"):
        assert term in body, body
    # The guard's own return keeps the caller polling.
    guard_line = body[guard_at:body.index("\n", guard_at)]
    assert "return { found: false }" in guard_line, guard_line


def test_extension_version_in_lockstep_across_manifest_config_and_entrypoint():
    """manifest.json busts Chrome's unpacked-extension cache; config.default.js
    and entrypoint.sh feed the hello frame the bridge logs — a bump that misses
    one makes the deploy-verification log lie about which worker is live."""
    manifest_src = MANIFEST_JSON.read_text(encoding="utf-8")
    config_src = CONFIG_DEFAULT_JS.read_text(encoding="utf-8")
    entry_src = CHROME_ENTRYPOINT.read_text(encoding="utf-8")
    manifest = _must_find(r'"version":\s*"([^"]+)"', manifest_src, "manifest").group(1)
    config = _must_find(r'EXTENSION_VERSION:\s*"([^"]+)"', config_src, "config").group(1)
    entry = _must_find(r'EXTENSION_VERSION:=([0-9][^}"]*)\}', entry_src, "entrypoint").group(1)
    assert manifest == config == entry, f"manifest={manifest} config={config} entrypoint={entry}"


def test_session_refresh_keepalive_is_hourly_and_migrates_the_period(background_src):
    """Keep the hourly canary cadence and migrate any older persisted alarm.

    Login logs motivate the experiment but do not prove cookie expiry causes.
    Ordinary worker restarts must preserve the existing alarm countdown.
    """
    period = int(_must_find(r"const SESSION_REFRESH_MINUTES = (\d+);",
                            background_src, "session-refresh period").group(1))
    assert 0 < period <= 60, f"keepalive cadence too coarse: {period} min"
    src = _strip_js_comments(background_src)
    reg = _locate('chrome.alarms.get("session-refresh"', src, "session-refresh registration")
    window = src[reg:reg + 400]
    # migrate on a period change (not just absence)…
    assert "a.periodInMinutes !== SESSION_REFRESH_MINUTES" in window, window
    assert 'chrome.alarms.create("session-refresh"' in window
    # …but still preserve the countdown on an ordinary worker restart
    assert "!a" in window


def test_click_targets_are_scrolled_on_screen_and_offer_gone_explains_itself(background_src, selectors_src):
    """Every trusted click target — the odd button, stake input, remove and
    submit — must be brought into the viewport BEFORE its rect is measured,
    and an off-screen or unsized target must refuse rather than click a
    viewport coordinate that lands on nothing (or on something else). Both
    offer_gone exits must attach the per-opp diagnostics so the next failure
    says whether the node was absent, hidden, or off-screen."""
    start = _locate("const probeOdd = ", background_src, "probeOdd")
    body = _strip_js_comments(background_src[start:_locate("const slipHasOpp", background_src, "probeOdd end", start)])
    assert _locate("scrollIntoView", body, "scroll before measure") < _locate("getBoundingClientRect()", body, "rect")
    assert "innerHeight" in body and "innerWidth" in body, body
    for fn in ("setStake", "clickRemove"):
        fb = _function_body(background_src, fn)
        assert "window.__ots.clickPoint(" in fb, (fn, fb)
    submit = background_src[_locate("const readSubmitRect", background_src, "readSubmitRect"):]
    assert "window.__ots.clickPoint(window.__ots.submitButton())" in submit.split("\n", 1)[0]
    run = _function_body(background_src, "runPlacement")
    assert run.count("(${pageDebug})(") == 2, "both offer_gone exits carry diagnostics"
    assert "submit_unreachable" in run
    sel = _strip_js_comments(selectors_src)
    for name in ("function clickPoint(", "function oddDiagnostics("):
        assert name in sel, name
    assert "clickPoint," in sel and "oddDiagnostics," in sel  # exported on window.__ots


def test_actuator_chrome_starts_with_a_full_size_window():
    """The profile's saved placement was 1050x1004 (not maximized) on a
    1280x1024 Xvfb screen — a ~920px viewport that put the /co-se-sazi main
    market below the fold. Pin the launch flags so the window fills the screen."""
    entry = CHROME_ENTRYPOINT.read_text(encoding="utf-8")
    assert "--window-size=1280,1024" in entry and "--window-position=0,0" in entry
