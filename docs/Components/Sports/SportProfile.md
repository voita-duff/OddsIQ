---
tags:
  - component/sports
  - status/current
  - layer/data
  - type/component
source-files:
  - oddsiq/sports/profiles.py
  - oddsiq/sports/teams.py
related:
  - "[[System Overview]]"
  - "[[Architecture Decisions]]"
  - "[[TipsportScraper]]"
  - "[[PinnacleScraper]]"
  - "[[Team Name Normalization]]"
  - "[[Polling Tiers]]"
  - "[[Multi-Sport Expansion]]"
---

# SportProfile

`SportProfile` (`oddsiq/sports/profiles.py`) is a frozen dataclass that bundles every sport-specific constant. Scrapers, the engine, and the pipeline become sport-agnostic by taking a profile argument; there are no hardcoded NBA constants outside the registry.

## Profile Fields

```python
@dataclass(frozen=True)
class SportProfile:
    key: str                          # internal id + DB value ("nba", "nhl", "mlb", "wnba", "nfl")
    display_name: str                 # "NBA", "NHL", "MLB", "WNBA", "NFL"
    odds_api_key: str                 # Odds API sport key for /odds and /scores
    tipsport_competition_id: int      # Tipsport offer-API competition id
    tipsport_sport_slug: str          # Tipsport URL sport slug
    tipsport_super_sport_id: int      # Tipsport superSportId; required when the
                                      # moneyline matchView needs an explicit filter
    canonical_teams: frozenset[str]   # canonical team names for this sport
    team_mappings: dict[str, str]     # Tipsport label -> canonical name
    live_backstop_hours: float        # game-LIFECYCLE window after tipoff (zombie detection,
                                      # /scores refresh scoping) — post-tipoff games are never polled
    market_rule: str                  # reserved descriptor ("winner_incl_ot_2way")
    tipsport_league_slug: str         # Tipsport URL league slug
    tipsport_moneyline_matchview: str # Tipsport tab for the 2-way moneyline
    tipsport_gender_slug: str         # Tipsport URL category segment ({sport}-muzi /
                                      # {sport}-zeny); default "muzi", only the
                                      # warmup/Referer URL uses it (WNBA: "zeny")
    shadow_far_detection: bool        # default False; True -> far-tier (2–6h) Pinnacle
                                      # fetch + edge detection as SHADOW data (NFL only)
    use_canonical_set: bool           # RESERVED (not yet read by normalize_team):
                                      # False -> trust source names + fuzzy (college, later)
```

(The `live_tipsport_coverage` field was removed 2026-08-27 with the live-edge
subsystem — post-tipoff games are no longer polled at all.)

`shadow_far_detection` (`bool`, default `False`) — far-tier (2–6h) Pinnacle fetch + edge detection as SHADOW data (tier `far`, CLV at kickoff, `tier_not_bettable` in the ledger, excluded from performance aggregates). NFL only (2026-09-06) — its lines move all week; evidence for widening the betting window.

## Registered Sports

### NBA

| Field | Value |
|---|---|
| `key` | `"nba"` |
| `odds_api_key` | `"basketball_nba"` |
| `tipsport_competition_id` | `23` |
| `tipsport_super_sport_id` | `7` (required for the `WINNER_FINAL_DECISION` filter) |
| `tipsport_moneyline_matchview` | `"WINNER_FINAL_DECISION"` |
| `live_backstop_hours` | `3.0` |
| Team set | 30 teams |
| Aliases | 10 Tipsport abbreviations (e.g. `"LA Lakers"` → `"Los Angeles Lakers"`) |

**Corrected 2026-06-12 (was `WINNER_WHOLE_MATCH` since collection began):** basketball's `WINNER_WHOLE_MATCH` tab carries the regulation 3-way legs (`1/1x/x/x2/2`); its 1/2 prices are **not** the moneyline — a live same-book A/B showed the legs summing under 100% two-way implied (NBA Finals G5: WHOLE 1.51/3.00 = 99.6%, vs `WINNER_FINAL_DECISION` 1.43/2.84 = 105.1%, the true incl-OT market that aligns with Pinnacle h2h). The legacy pre-refactor scraper read the same tab, so **all NBA Tipsport moneyline rows from April 2026 until the fix deploy — `value_bets` AND `live_edge_candidates` (markout confirmation re-checks Pinnacle fair and cannot catch the structural mislabel) — carry a systematic positive edge/CLV bias**. Remediation: `oddsiq mark-excluded` on the VM stamps the window's rows out of every reporting surface (see [[Data Model]] `excluded_reason`).

### NHL

| Field | Value |
|---|---|
| `key` | `"nhl"` |
| `odds_api_key` | `"icehockey_nhl"` |
| `tipsport_competition_id` | `22` |
| `tipsport_super_sport_id` | `23` |
| `tipsport_moneyline_matchview` | `"WINNER_FINAL_DECISION"` |
| `live_backstop_hours` | `2.5` |
| Team set | 32 teams |
| Aliases | None (NHL labels match Odds API names) |

**Critical:** NHL uses `WINNER_FINAL_DECISION` (2-way incl. OT/shootout), **not** `WINNER_WHOLE_MATCH`. `WINNER_WHOLE_MATCH` for hockey is the regulation 3-way market (home/draw/away), which is explicitly excluded by the `WINNER_FINAL_DECISION` filter. The `tipsport_super_sport_id=23` is required to surface the correct market. Fetching the wrong market and mislabeling 3-way prices as moneyline is a silent data-corruption risk; the scraper is fail-closed (rows not matching the 2-way filter are dropped, not emitted).

### MLB

| Field | Value |
|---|---|
| `key` | `"mlb"` |
| `odds_api_key` | `"baseball_mlb"` |
| `tipsport_competition_id` | `5795` |
| `tipsport_super_sport_id` | `6` (unused for the moneyline request) |
| `tipsport_moneyline_matchview` | `"WINNER_WHOLE_MATCH"` |
| `live_backstop_hours` | `4.0` |
| Team set | 30 teams |
| Aliases | None (Tipsport labels match the Odds API names; fuzzy fallback covers drift) |

`WINNER_WHOLE_MATCH` is a clean 2-way market for baseball (cannot tie in the final result). The 4.0 h backstop covers extra-inning games (lifecycle window only — post-tipoff games are not polled since the 2026-08-27 live-edge removal).

### WNBA

| Field | Value |
|---|---|
| `key` | `"wnba"` |
| `odds_api_key` | `"basketball_wnba"` |
| `tipsport_competition_id` | `10` |
| `tipsport_super_sport_id` | `7` (basketball, same as NBA; required for the `WINNER_FINAL_DECISION` filter) |
| `tipsport_moneyline_matchview` | `"WINNER_FINAL_DECISION"` |
| `tipsport_gender_slug` | `"zeny"` — the league lives under `/kurzy/basketbal/basketbal-zeny/wnba-10` |
| `live_backstop_hours` | `2.5` |
| Team set | 15 teams (2026 roster incl. expansion Toronto Tempo + Portland Fire) |
| Aliases | 15 — Tipsport suffixes every women's team with `" Ž"` (e.g. `"Indiana Fever Ž"` → `"Indiana Fever"`) |

Added 2026-06-11. Same basketball engine as NBA — the enablement smoke + raw-response inspection caught that the default `WINNER_WHOLE_MATCH` tab is the regulation 3-way for basketball, which led to the NBA correction above; both basketball profiles use the incl-OT `WINNER_FINAL_DECISION` tab (A/B price-verified at enablement: FD 1.25/3.70 vs regulation legs 1.28/3.84, with FD aligning to Pinnacle's incl-OT h2h). The 2.5 h backstop reflects the 4×10 min format (~2 h real time, NHL-style buffer). WNBA is the only profile with a non-default `tipsport_gender_slug` — the men's leagues sit under `{sport}-muzi`.

### NFL

| Field | Value |
|---|---|
| `key` | `"nfl"` |
| `odds_api_key` | `"americanfootball_nfl"` (regular season + playoffs; preseason is a separate Odds API key and can never leak into the slate) |
| `tipsport_competition_id` | `5748` (verified in-browser 2026-08-25: `/kurzy/americky-fotbal/americky-fotbal-muzi/nfl-5748`) |
| `tipsport_super_sport_id` | `2` (captured from Tipsport's own offer API 2026-08-25; required for the `WINNER_FINAL_DECISION` filter) |
| `tipsport_moneyline_matchview` | `"WINNER_FINAL_DECISION"` ("Vítěz do rozhodnutí") |
| `live_backstop_hours` | `3.5` (games run ~3h10m; OT buffer) |
| `shadow_far_detection` | `True` (2026-09-06) — far-tier shadow detection; the other four profiles keep the default `False` |
| Team set | 32 teams, extracted verbatim from Odds API event data 2026-08-25 |
| Aliases | None pending the market-post smoke (Tipsport futures labels matched the Odds API names exactly) |

Added 2026-08-25 (design spec: `docs/Design/2026-08-25-nfl-fifth-sport-autobetting-design.md`). American football repeats the basketball shape: the `WINNER_WHOLE_MATCH` tab carries the regulation 3-way legs (`1/1x/x/x2/2` — a pre-OT draw is real), so the 2-way incl-OT moneyline is `WINNER_FINAL_DECISION`, **price-verified on live NCAAF games** (same superSport engine, games available at recon time): FD rows carried a proper standalone 105.8–109.4% two-way implied, coherent A/B vs the regulation legs (TCU 1.28 FD vs 1.32 regulation-1). NFL ties (rare, post-OT) settle as `draw` → push. At enablement Tipsport had posted only futures — game moneylines post ~early Sep; until then the FD tab is absent and every fetch takes the graceful `TipsportMarketUnavailable` path. Unlike the other sports, NFL also participates in **auto-betting** once the operator ticks it into the meta `betting_sports` gate (see [[Data Model]] meta keys; decided 2026-08-25: bet from the first slate, shared caps).

## Registry API

```python
SPORTS: dict[str, SportProfile]    # all registered profiles (5)
ENABLED_SPORTS: list[str]          # currently ["mlb", "nfl"] (2026-08-25)

def get_profile(key: str) -> SportProfile: ...
def enabled_profiles() -> list[SportProfile]: ...
```

`enabled_profiles()` drives both `ScheduleService.fetch_fresh_games()` (one `/scores` call per profile) and `Scheduler._compute_pollable()` (filters pollable games per sport). Every per-sport path in the pipeline iterates this list.

## Adding a Sport

1. Add team data to `oddsiq/sports/teams.py` (canonical set + Tipsport aliases).
2. Add a `SportProfile` instance to `oddsiq/sports/profiles.py` with the recon-locked competition ID, the correct `tipsport_moneyline_matchview`, and the right `tipsport_gender_slug` (women's leagues live under `{sport}-zeny`).
3. Append the sport's `key` to `ENABLED_SPORTS`.
4. Run the Tipsport smoke test (`oddsiq tipsport-smoke --sport <key>`) to confirm the moneyline market returns data in the expected shape.
5. **Price-verify the matchView, don't trust structure alone** (the 2026-06-12 basketball lesson): inspect the saved raw response for the tab's full `oppsTab` (a 5-outcome `1/1x/x/x2/2` row set means regulation 3-way legs), and A/B the candidate tab's prices against `WINNER_FINAL_DECISION` and Pinnacle's incl-OT h2h. A standalone 2-way market must carry positive vig (implied sum meaningfully above 100%); legs that sum near or below 100% belong to a 3-way book.

Deferred sports (NCAAF / NCAAB) with their competition IDs are tracked in [[Multi-Sport Expansion]]. (NFL graduated from that list 2026-08-25.)

## Live-tier Pinnacle gating (REMOVED 2026-08-27)

The `live_tipsport_coverage` field and the live-tier Pinnacle poll it gated were
removed with the whole live-edge subsystem (2026-08-27). Post-tipoff games are
unpollable; `live_backstop_hours` survives purely as the game-lifecycle window
(zombie detection, /scores refresh scoping). Archive: kept offline, outside this repository.

## Team Name Normalization

`normalize_team(name, book, sport)` (`oddsiq/scrapers/normalize.py`) selects the canonical set and aliases from the named sport's profile. The `use_canonical_set` field is **reserved** (not yet read by `normalize_team`); when activated, `False` would trust source names and skip the canonical-set guard (intended for future college sports) — setting it `False` currently has no effect. See [[Team Name Normalization]] for the 3-tier matching logic (canonical → alias → fuzzy).
