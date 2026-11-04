# Multi-Sport Expansion — Tipsport Coverage & Deferred Sports

> Accumulated reference + decision log for expanding OddsIQ beyond NBA. Captures
> the Tipsport coverage recon (2026-06-02) and the staged scope decision so the
> deferred sports (NFL, NCAAF, NCAAB) can be enabled as config-only adds once
> they are in season and the core multi-sport pipeline is polished.

---

## TL;DR

All six major US sports we care about are bettable on Tipsport. The multi-sport
refactor wires **NBA (baseline) + NHL + MLB** now; **WNBA was enabled as the
fourth sport 2026-06-11** (in season May–Oct, fills the summer slate gap);
**NFL was enabled as the fifth sport 2026-08-25** (incl. auto-betting via the
meta `betting_sports` gate — see the scope update below). **NCAAF / NCAAB stay
deferred** to their seasons and to a decision on college team-name
normalization.

---

## Tipsport coverage map (recon 2026-06-02)

URL pattern: `https://www.tipsport.cz/kurzy/{sportSlug}/{sportSlug}-{genderSlug}/{leagueSlug}-{competitionId}` — `genderSlug` is `muzi` for the men's leagues and `zeny` for women's (WNBA); it became the `SportProfile.tipsport_gender_slug` field when WNBA shipped.

| Sport | Tipsport sport slug | League slug | Competition ID | Odds API sport key | In season (2026-06-02) |
|---|---|---|---|---|---|
| NBA   | `basketbal`       | `nba`        | **23**    | `basketball_nba`        | Finals, ending |
| NHL   | `ledni-hokej`     | `nhl`        | **22**    | `icehockey_nhl`         | Finals, ending ~mid-June |
| MLB   | `baseball`        | `mlb`        | **5795**  | `baseball_mlb`          | ✅ Daily, Apr–Oct |
| WNBA  | `basketbal` (`basketbal-zeny`) | `wnba` | **10** | `basketball_wnba`  | ✅ Enabled 2026-06-11; May–Oct |
| NFL   | `americky-fotbal` | `nfl`        | **5748**  | `americanfootball_nfl`  | Offseason → Sept |
| NCAAF | `americky-fotbal` | `usa-ncaaf`  | **67562** | `americanfootball_ncaaf`| Offseason → Aug/Sept |
| NCAAB | `basketbal`       | `usa-ncaa`   | **80576** | `basketball_ncaab`      | Offseason → Nov |

`competitionId` is the per-sport equivalent of `SportProfile.tipsport_competition_id`
(NBA = 23; formerly the `config.TIPSPORT_NBA_COMPETITION_ID` constant, removed in the
2026-06-02 refactor when per-sport constants moved onto the [[SportProfile]] registry).

### Verification method & caveats

- Plain `WebFetch` on tipsport.cz returns **HTTP 403** (Cloudflare) — the same
  wall the scraper bypasses via Playwright. This map was built from
  search-surfaced tipsport.cz league URLs + Czech betting-info sites, **not** a
  live click-through. Competition IDs were extracted from those URLs.
- Confirms the **pages + IDs exist**. NOT yet confirmed:
  1. Each league currently returns live games with moneyline in the parser shape
     we handle — verify per sport during implementation (the smoke).
  2. `superSportId` per sport (basketball = 7, hockey = 23) — **REQUIRED**
     whenever the sport's 2-way moneyline is the `WINNER_FINAL_DECISION` tab
     (all basketball and hockey leagues): `_moneyline_filters()` sends it in
     the explicit `matchViewFilters` entry, and the tab only populates when
     requested with it. Only `WINNER_WHOLE_MATCH` sports (baseball) ride the
     default `[]` request. (Corrected 2026-06-12 — this caveat originally
     claimed superSportId was spread/total-only; see the basketball 3-way
     update below.)

---

## Scope decision (2026-06-02)

- **Now:** NBA (baseline) + NHL + MLB. Both new sports are in season and have
  clean ~30-team pro rosters (NHL 32, MLB 30) analogous to NBA, so they validate
  the generic sport layer without the college team-set problem.
- **Deferred:** NFL, NCAAF, NCAAB — enable as config-only adds once (a) their
  seasons open and (b) the core multi-sport pipeline is polished. No rush: all
  three are offseason.

## Scope update (2026-08-25): NFL enabled (fifth sport, incl. auto-betting)

NFL became the fifth sport on explicit user direction — the first sport add
commissioned **with auto-betting** (gated by the new meta `betting_sports`
key; decided: bet from the first week-1 slate, shared caps). Live recon
2026-08-25 closed every open checklist item except the live probe:
`superSportId=2` captured from Tipsport's own offer API; american football's
`WINNER_WHOLE_MATCH` confirmed as the regulation 3-way legs (the basketball
trap), with the 2-way incl-OT moneyline in `WINNER_FINAL_DECISION`
("Vítěz do rozhodnutí") **price-verified on live NCAAF games** (FD implied
105.8–109.4%, coherent A/B vs the regulation legs); 32 canonical team names
extracted from Odds API event data (272 events with Pinnacle h2h already
posted; preseason is a separate key); Tipsport futures labels matched the
Odds API names exactly (aliases start empty, the market-post smoke verifies
all 32). Game moneylines post ~early Sep — until then the FD tab is absent
and the scraper idles on `TipsportMarketUnavailable`. Design + decisions:
`docs/Design/2026-08-25-nfl-fifth-sport-autobetting-design.md`.

## Scope update (2026-06-11): WNBA enabled

WNBA became the fourth sport on explicit user direction, after a live recon of
all three coverage legs (Odds API odds + scores active with Pinnacle posting
2-way h2h; Tipsport carries the league at `basketbal-zeny/wnba-10` with
`" Ž"`-suffixed team labels; 15-team 2026 roster incl. expansion Toronto Tempo
and Portland Fire observed in Odds API data). Two code changes fell out:
parameterizing the hardcoded `-muzi` segment in the Tipsport warmup URL into
`SportProfile.tipsport_gender_slug`, and — via the enablement smoke's
raw-response check — the basketball `WINNER_FINAL_DECISION` correction described
in the 3-way note above (which also fixed NBA).
Design + verification record: `docs/Design/2026-06-11-wnba-fourth-sport-design.md`.

---

## Per-sport enablement checklist (for the deferred sports)

NFL status shown; NCAAF/NCAAB remain fully open (plus the college
normalization decision below).

- [x] Tipsport competition ID (above)
- [x] Tipsport `superSportId` — NFL: `2` (captured from the live offer API 2026-08-25; required when the moneyline lives in `WINNER_FINAL_DECISION` — all basketball/hockey/american-football leagues)
- [x] Canonical team-name set + bookmaker normalization mappings — NFL: 32 names from Odds API event data; aliases empty pending the market-post smoke
- [x] Odds API scores endpoint sport key verified — NFL: `americanfootball_nfl` active (272 events w/ Pinnacle h2h at recon)
- [x] Odds API sport key (above)
- [x] Market check — NFL: `WINNER_FINAL_DECISION` price-verified via live NCAAF A/B 2026-08-25; `WINNER_WHOLE_MATCH` is the regulation 3-way for american football
- [x] ~~live-coverage probe~~ — OBSOLETE: the live-edge subsystem (and the `live_tipsport_coverage` field) was fully removed 2026-08-27; there is no live tier to decide coverage for.

---

## College normalization caveat (pro is fine; college is not)

`oddsiq/scrapers/normalize.py` uses a hardcoded canonical team set (30 NBA names)
plus fuzzy match. Pro leagues fit this shape (NHL 32, MLB 30, NFL 32). College
does **not**: NCAAF has ~130 FBS teams, NCAAB has 350+ D1 teams. Before enabling
NCAAF/NCAAB, decide a different normalization strategy (e.g. trust the
schedule/Odds-API canonical names + fuzzy, drop the hardcoded set for those
sports). Flagged here so it is not discovered mid-implementation.

---

## Draw / 3-way market note

`resolve_bet_result()` returns `"draw"` on equal scores (shipped with the multi-sport refactor; see `Fixed - Moneyline Tie Not Handled.md`). NHL/MLB do
not tie in the *final* result, but Tipsport lists a regulation-time 3-way market
(home/draw/away) beside the winner-incl-OT 2-way. Scrape only the 2-way (matches
Pinnacle `h2h`) and add defensive draw handling. The fail-closed 2-way tab selection is implemented for NHL in `TipsportScraper.parse_response()` via `profile.tipsport_moneyline_matchview`.

**Update 2026-06-12:** the same applies to **basketball**. The WNBA enablement
smoke revealed that Tipsport's `WINNER_WHOLE_MATCH` tab for basketball carries
the regulation 3-way legs (`1/1x/x/x2/2`) whose 1/2 prices are not the
moneyline (live A/B: NBA WHOLE legs 1.51/3.00 sum to 99.6% two-way implied —
impossible for a standalone 2-way book — vs `WINNER_FINAL_DECISION` 1.43/2.84
at a normal 5.1% vig, aligning with Pinnacle h2h). NBA and WNBA now use
`WINNER_FINAL_DECISION` with `superSportId=7`; **only MLB** rides the default
`WINNER_WHOLE_MATCH` tab (baseball cannot tie). The contamination predates the
refactor: the legacy NBA-only scraper also read `WINNER_WHOLE_MATCH` (verified
against the pre-refactor code), so **every NBA Tipsport moneyline row
since collection began (April 2026) — `value_bets` AND `live_edge_candidates`
(markout confirmation re-checks Pinnacle fair, so it cannot catch this
structural mislabel) — carries the systematic positive bias** until the fix is
deployed. Remediation: stamp the window with `oddsiq mark-excluded` on the VM
(reporting surfaces then hide the rows; see [[Data Model]] `excluded_reason`).

---

## Related

- `docs/Decisions/Markets.md` — Pinnacle market scope (moneyline-only)
- The multi-book validation gate this refactor supersedes — doc removed in the 2026-06-02 cleanup; the full pre-refactor state is preserved in the pre-release history
- `docs/Known Issues/Fixed - Moneyline Tie Not Handled.md` — draw/tie handling (shipped)
- `oddsiq/scrapers/normalize.py`, `oddsiq/scrapers/tipsport.py`, `oddsiq/config.py`
