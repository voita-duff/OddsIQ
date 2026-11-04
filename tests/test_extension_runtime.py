"""Behavior-level checks of the page-context actuator in real Chromium.

The sibling test_extension_selectors.py locks the DOM contract by reading the
sources as TEXT; this file runs selectors.js in a real engine against the same
captured fixture, so a helper that parses/normalizes wrongly is caught by
behavior rather than by grep. Hermetic: no network, `set_content` +
`add_script_tag(path=...)` only.

Chromium is installed in CI (`playwright install chromium`); a fresh clone
without it must SKIP, never error — hence the importorskip and the guarded
launch below.
"""

from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SELECTORS = ROOT / "extension" / "selectors.js"
SLIP_FIXTURE = ROOT / "tests" / "fixtures" / "tipsport_offer_slip.html"
LOGIN_FIXTURE = ROOT / "tests" / "fixtures" / "tipsport_logged_in_header.html"


@pytest.fixture(scope="module")
def browser():
    """Module-scoped: the launch is the expensive part, pages are cheap."""
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=True)
        except Exception as e:  # browser binary absent / unlaunchable
            pytest.skip(f"chromium unavailable: {e}")
        yield instance
        instance.close()


@pytest.fixture
def selector_page(browser):
    """Function-scoped: these tests MUTATE the DOM (extra slip legs, stake
    values), so each one gets a fresh page off the shared browser."""
    page = browser.new_page()
    page.set_content(SLIP_FIXTURE.read_text(encoding="utf-8"))
    page.add_script_tag(path=SELECTORS)
    yield page
    page.close()


def test_runtime_odd_lookup_compares_opp_id_exactly(selector_page):
    exact = selector_page.evaluate(
        "window.__ots.findOddButton('2681257493').getAttribute('data-atid')"
    )
    assert exact.startswith("content||ODD||2681257493||")
    # 8355990 occurs in the match-id field, not the opp-id field.
    assert selector_page.evaluate("window.__ots.findOddButton('8355990')") is None


def test_runtime_slip_counter_counts_distinct_legs(selector_page):
    assert selector_page.evaluate("window.__ots.slipLegCount()") == 1
    selector_page.evaluate("""
        () => {
          const first = document.querySelector('[data-atid^="ticketBuilderChangeBet-"]');
          first.parentElement.appendChild(first.cloneNode(true));
        }
    """)
    assert selector_page.evaluate("window.__ots.slipLegCount()") == 1
    selector_page.evaluate("""
        () => {
          const extra = document.createElement('div');
          extra.dataset.atid = 'ticketBuilderChangeBet-999';
          document.querySelector('#tiket').appendChild(extra);
        }
    """)
    assert selector_page.evaluate("window.__ots.slipLegCount()") == 2


def test_runtime_stake_readback_normalizes_czech_number(selector_page):
    selector_page.evaluate("document.querySelector('[data-atid=txtAmountPaid]').value = '1 234,5'")
    assert selector_page.evaluate("window.__ots.stakeAmount()") == 1234.5


@pytest.mark.parametrize(
    ("field", "amount"),
    [
        ("10 Kč", None),   # a currency suffix is NOT a number -> unreadable
        ("", None),        # empty is unreadable, NOT Number("") == 0
        ("   ", None),     # whitespace collapses to empty, same rule
        ("abc", None),
        ("10,50", 10.5),   # Czech decimal comma
        ("10", 10),
    ],
)
def test_runtime_stake_reader_fails_closed(selector_page, field, amount):
    """stakeAmount() gates the money path (background.js waitForStake ->
    stake_mismatch), so anything it cannot parse must read null and block the
    submit — never 0, which would look like a real (wrong) stake. stakeRaw()
    returns the field verbatim so the refusal says WHICH unreadable form it
    was."""
    selector_page.evaluate(
        "(v) => { document.querySelector('[data-atid=txtAmountPaid]').value = v; }", field
    )
    assert selector_page.evaluate("window.__ots.stakeAmount()") == amount
    assert selector_page.evaluate("window.__ots.stakeRaw()") == field


def test_runtime_stake_raw_is_null_without_the_input(selector_page):
    selector_page.evaluate("document.querySelector('[data-atid=txtAmountPaid]').remove()")
    assert selector_page.evaluate("window.__ots.stakeRaw()") is None
    assert selector_page.evaluate("window.__ots.stakeAmount()") is None


def test_runtime_login_probe_needs_the_session_timer_not_avatars(browser, selector_page):
    """The 2026-09-09 incident: /kurzy shows a feed of other users' avatars,
    so `[data-atid=profile-avatar]` is present while logged OUT. loggedIn()
    must ignore avatars entirely and key on the session timer alone."""
    # The captured (logged-out) offer/slip fixture: no timer -> logged out.
    assert selector_page.evaluate("window.__ots.loggedIn()") is False
    page = browser.new_page()
    try:
        feed = "".join(
            f'<div class="tip"><img data-atid="profile-avatar" alt="u{i}"><span>tip {i}</span></div>'
            for i in range(20)
        )
        page.set_content(f"<header id='hdr'><span>Čas:&nbsp;<span>12:55:36</span></span>"
                         f"<button>Přihlásit</button></header><section id='feed'>{feed}</section>")
        page.add_script_tag(path=SELECTORS)
        assert page.evaluate("window.__ots.loggedIn()") is False
        markers = page.evaluate("window.__ots.loginMarkers()")
        assert markers == {"timer": False, "avatar_nodes": 20, "prihlasit": True}
        # The session timer alone flips it — that is the authenticated-session marker.
        page.evaluate("""() => { const s = document.createElement('span');
            s.innerHTML = 'Doba přihlášení:&nbsp;<span>00:05:43</span>';
            document.querySelector('#hdr').appendChild(s); }""")
        assert page.evaluate("window.__ots.loggedIn()") is True
        # And the real captured logged-in header reads logged in.
        page.set_content(LOGIN_FIXTURE.read_text(encoding="utf-8"))
        page.add_script_tag(path=SELECTORS)
        assert page.evaluate("window.__ots.loggedIn()") is True
    finally:
        page.close()


def test_runtime_session_refresh_retries_after_busy_tab(browser):
    """An hourly alarm racing a probe/placement must survive until a free tick."""
    source = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
    # Execute the actual worker in an isolated, offline function scope. Browser
    # APIs are stubs; no socket, navigation, credentials, or placement is used.
    page = browser.new_page()
    try:
        result = page.evaluate(r"""source => {
                let alarm;
                const chrome = {
                    alarms: { get: (_, cb) => cb({periodInMinutes: 60}),
                              create: () => {}, onAlarm: {addListener: f => alarm = f} },
                    runtime: {onStartup: {addListener: () => {}},
                              onInstalled: {addListener: () => {}}}
                };
                const importScripts = () => {};
                const WebSocket = class { constructor() { this.readyState = 1; }
                    static OPEN = 1; static CONNECTING = 0; };
                globalThis.OTS_CONFIG = {BRIDGE_URL: 'unused'};
                return eval(source + '\n' + `
                    (async () => {
                        let refreshes = 0;
                        let probes = 0;
                        pruneTipsportTabs = async () => {};
                        confirmLoginOnProbePage = async () => { refreshes++; return loggedIn; };
                        reportStatus = async () => { probes++; };
                        const counts = [];
                        for (const owner of ['probe', 'placement']) {
                            probing = owner === 'probe';
                            inFlight = owner === 'placement';
                            alarm({name: 'session-refresh'});
                            alarm({name: 'keepalive'});
                            counts.push(refreshes);
                            probing = false; inFlight = false;
                            alarm({name: 'keepalive'});
                            await Promise.resolve(); await Promise.resolve();
                            counts.push(refreshes);
                            alarm({name: 'keepalive'});
                        }
                        return {counts, probes, due: sessionRefreshDue};
                    })()
                `);
        }""", source)
        assert result == {"counts": [0, 1, 1, 2], "probes": 2, "due": False}
    finally:
        page.close()


def test_runtime_click_point_scrolls_a_below_fold_control_into_view(browser):
    """Trusted clicks are dispatched at VIEWPORT coordinates. Tipsport's pregame
    match page now lands on the /co-se-sazi tab where the main market sits
    ~900-1100px down — below the fold of the actuator's ~920px-tall window
    (2026-09-20/23: two offer_gone rejections while the API still listed the
    price). clickPoint() must scroll the target on screen, measure AFTER the
    scroll, and return null for anything unsized or still off-screen."""
    page = browser.new_page(viewport={"width": 1280, "height": 600})
    try:
        page.set_content(
            '<div style="height:3000px"></div>'
            '<div data-atid="content||ODD||2681257493||2261996230||1.91||8355990||1"'
            ' style="width:158px;height:27px">1.91</div>'
            '<div data-atid="content||ODD||777||1||2.00||8355990||2" style="display:none">2.00</div>'
        )
        page.add_script_tag(path=SELECTORS)
        assert page.evaluate("window.scrollY") == 0
        found = page.evaluate("!!window.__ots.findOddButton('2681257493')")
        assert found is True
        pt = page.evaluate("window.__ots.clickPoint(window.__ots.findOddButton('2681257493'))")
        assert pt is not None and 0 <= pt["y"] < 600 and 0 <= pt["x"] < 1280, pt
        assert page.evaluate("window.scrollY") > 2000
        # unsized / hidden -> never a click point; missing element -> null, no throw
        assert page.evaluate("window.__ots.clickPoint(document.querectorAll ? null : document.querySelector('[data-atid^=\"content||ODD||777||\"]'))") is None
        assert page.evaluate("window.__ots.clickPoint(null)") is None
        diag = page.evaluate("window.__ots.oddDiagnostics('2681257493')")
        assert diag["nodes"] == 1 and diag["rects"][0]["h"] == 27, diag
        assert diag["viewport"] == [1280, 600] and diag["doc_height"] > 3000
        assert page.evaluate("window.__ots.oddDiagnostics('nope')")["nodes"] == 0
    finally:
        page.close()
