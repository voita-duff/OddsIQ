// OddsIQ Tipsport bet-slip selectors — PURE page-context DOM helpers.
//
// Injected into the tipsport.cz page by the extension (via CDP Runtime.evaluate)
// and exposed as window.__ots. Every selector keys on the site's stable
// `data-atid` attributes — NEVER the rotating styled-components class hashes
// (the random-looking suffixes that change on every Tipsport build). The
// data-atid grammar was verified live against the offer API 2026-08-09
// (see tests/test_extension_selectors.py + the Phase-2 plan).
//
// No chrome.* APIs here so the module is trivially injectable and unit-lockable.
(function () {
  // Odd button: content || ODD || <oppId> || <eventId> || <odd> || <matchId> || <type>
  function parseAtid(atid) {
    if (!atid || atid.indexOf("content||ODD||") !== 0) return null;
    const p = atid.split("||");
    if (p.length < 7) return null;
    return {
      oppId: p[2],
      eventId: p[3],
      odd: parseFloat(p[4]),
      matchId: p[5],
      type: p[6], // "1" = home, "2" = away
    };
  }

  // Resolve by parsing the stable grammar and comparing the opp id exactly.
  // A substring CSS selector can match an id occurring in another field
  // (event/match id) and would trusted-click the wrong price.
  // Every node carrying this opp id, ANY size — the raw material for both the
  // locator and the offer_gone diagnostics.
  function oddCandidates(oppId) {
    const wanted = String(oppId);
    return Array.from(document.querySelectorAll('[data-atid^="content||ODD||"]')).filter(
      (el) => {
        const meta = parseAtid(el.getAttribute("data-atid"));
        return !!meta && String(meta.oppId) === wanted;
      }
    );
  }
  const sized = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

  function findOddButton(oppId) {
    const all = oddCandidates(oppId);
    // Prefer a sized candidate (a hidden breakpoint duplicate of the same offer
    // measures 0x0): find() is document-order, so a hidden node BEFORE the live
    // one would otherwise win every poll and turn the selection into a
    // permanent offer_gone.
    let el = all.find(sized) || null;
    if (!el && all.length) {
      // Every node unsized: a block far below the fold may simply not be laid
      // out yet (lazy render / content-visibility). Bring the first on screen
      // and look once more before calling the offer absent.
      try { all[0].scrollIntoView({ block: "center", inline: "nearest" }); } catch (e) {}
      el = all.find(sized) || null;
    }
    return el;
  }

  // Trusted clicks are dispatched at VIEWPORT coordinates, so a target must be
  // ON SCREEN when it is measured. Tipsport's pregame match page now lands on
  // the /co-se-sazi tab where the main market sits ~900-1100px down — below the
  // fold of the actuator's window (2026-09-20/23: two offer_gone rejections
  // while the offer API still listed the price). Scroll first, measure after,
  // and refuse (null) anything unsized or still off-screen — the caller must
  // never click a coordinate that lands on nothing, or on something else.
  function clickPoint(el) {
    if (!el) return null;
    try { el.scrollIntoView({ block: "center", inline: "nearest" }); } catch (e) {}
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return null;
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return null;
    return { x, y };
  }

  // Why an offer_gone happened: absent node, hidden node, or a node the
  // viewport never reached — plus the page geometry to judge it by.
  function oddDiagnostics(oppId) {
    const els = oddCandidates(oppId);
    const txt = document.body ? document.body.innerText : "";
    return {
      nodes: els.length,
      rects: els.slice(0, 4).map((el) => {
        const r = el.getBoundingClientRect();
        return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                 display: getComputedStyle(el).display };
      }),
      main_market_heading: /Hlavní příležitosti/.test(txt),
      headings: Array.from(document.querySelectorAll("h1,h2,h3,h4"))
        .map((h) => h.textContent.trim().slice(0, 30)).filter(Boolean).slice(0, 12),
      scroll_y: Math.round(window.scrollY),
      viewport: [window.innerWidth, window.innerHeight],
      doc_height: document.documentElement.scrollHeight,
    };
  }

  // Returns the parsed meta for a bet's opp_id, or null if the button is gone
  // (offer pulled / not on this page).
  function oddMeta(oppId) {
    const el = findOddButton(oppId);
    return el ? parseAtid(el.getAttribute("data-atid")) : null;
  }

  const stakeInput = () => document.querySelector('[data-atid="txtAmountPaid"]');
  const submitButton = () => document.querySelector('[data-atid="btn-betTicket"]');
  const removeButton = () =>
    document.querySelector('[data-atid="ticketBuilderRemoveOppButton"]');

  // True once the given opp_id is actually in the slip (the site echoes our id
  // back as ticketBuilderChangeBet-<oppId>) — the post-add confirmation hook.
  function selectionInSlip(oppId) {
    return !!document.querySelector(
      '[data-atid="ticketBuilderChangeBet-' + oppId + '"]'
    );
  }

  // Number of DISTINCT legs on the bet slip, keyed on the oppId suffix of the
  // `ticketBuilderChangeBet-<oppId>` control (one per leg — verified in the
  // captured live slip fixture, one leg = 1). Distinct, not `.length`: the
  // fixture is a hand-trimmed fragment and cannot rule out the site rendering
  // the slip twice (e.g. one node per breakpoint layout). Duplicated markup
  // would read 0 on a clean slip but 2 once OUR single leg exists, tripping
  // slip_not_singleton on every placement — two consecutive failures, and the
  // engine auto-disarms. Counting ids collapses duplicates of the same leg and
  // still sees two different ones. Returns -1 when the DOM cannot be
  // inspected; the actuator treats that sentinel as a hard stop.
  function slipLegCount() {
    try {
      const PREFIX = "ticketBuilderChangeBet-";
      const ids = new Set();
      document.querySelectorAll('[data-atid^="' + PREFIX + '"]').forEach(
        (el) => ids.add((el.getAttribute("data-atid") || "").slice(PREFIX.length))
      );
      return ids.size;
    } catch (e) { return -1; }
  }

  // Live total odds shown in the slip ("Celkový kurz" row) — the value the
  // min_odds guard compares against immediately before submitting. Returns a
  // float or null when the row isn't present.
  function totalOdds() {
    const label = Array.from(document.querySelectorAll("div")).find(
      (d) => d.children.length === 0 && d.textContent.trim() === "Celkový kurz"
    );
    if (!label || !label.nextElementSibling) return null;
    const v = parseFloat(label.nextElementSibling.textContent.trim().replace(",", "."));
    return isNaN(v) ? null : v;
  }

  // The live value in the React-controlled stake input, normalized from Czech
  // decimal/grouping notation. Null means unreadable and must block submit —
  // an EMPTY field is unreadable too, not zero: Number("") is 0, which would
  // read as a real (and wrong) stake if the trusted input never landed.
  function stakeAmount() {
    const input = stakeInput();
    if (!input) return null;
    const raw = String(input.value || "").replace(/\s/g, "").replace(",", ".");
    if (!raw) return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  }

  // The field text exactly as rendered, for diagnosing a stake_mismatch:
  // stakeAmount() collapses every unreadable form to the same null ("10 Kč",
  // "", outright garbage), so only the raw string says WHICH one happened.
  function stakeRaw() {
    const input = stakeInput();
    return input ? String(input.value) : null;
  }

  // Login detection, verified against the real logged-in DOM (2026-08-10,
  // Phase-4 login characterization; fixture tipsport_logged_in_header.html):
  // the header carries data-atid="profile-avatar" and a session timer
  // ("Doba přihlášení:&nbsp;00:05:43" — note the &nbsp;, so match the bare
  // label). Logged out, neither exists and the header shows "Přihlásit".
  // Fail-safe default is "logged out" so the engine never places while
  // state is unknown.
  // The ONLY trustworthy marker is the session timer ("Doba přihlášení"),
  // which Tipsport renders solely for an authenticated session. The avatar
  // atid must NOT count: the pinned /kurzy probe page carries a feed of OTHER
  // users' avatars (20+ `data-atid="profile-avatar"` nodes on 2026-09-09), so
  // an avatar-based probe read "logged in" for days while the session was
  // dead — the 0.8.0 incident found by the operator over VNC.
  function loggedIn() {
    return /Doba přihlášení/.test(document.body ? document.body.innerText : "");
  }

  // Diagnostics for the status frame / logs: which markers the page shows.
  function loginMarkers() {
    const txt = document.body ? document.body.innerText : "";
    return {
      timer: /Doba přihlášení/.test(txt),
      avatar_nodes: document.querySelectorAll('[data-atid="profile-avatar"]').length,
      prihlasit: /Přihlásit/.test(txt),
    };
  }

  window.__ots = {
    parseAtid,
    oddCandidates,
    findOddButton,
    clickPoint,
    oddDiagnostics,
    oddMeta,
    stakeInput,
    submitButton,
    removeButton,
    selectionInSlip,
    slipLegCount,
    totalOdds,
    stakeAmount,
    stakeRaw,
    loggedIn,
    loginMarkers,
  };
})();
