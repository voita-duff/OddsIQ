# WNBA as the Fourth Sport — Design

> Spec for adding WNBA to the [[SportProfile]] registry alongside NBA/NHL/MLB,
> per the enablement checklist in `docs/Decisions/Multi-Sport Expansion.md`.
> User direction (2026-06-11): "implement WNBA, same as other sports."

## Why WNBA, why now

- Fills the summer slate gap: NBA/NHL finals end mid-June, leaving MLB as the
  only active sport until late September. WNBA runs **now → September**, with
  playoffs into October.
- Same shape as NBA: US pro league, overnight-CET tipoffs (low Tipsport trader
  attention — the softness thesis), clean 2-way basketball moneyline, small
  fixed team set.
- All three coverage legs verified live on 2026-06-11 (see Recon below).
- Budget: ~+250–350 Odds API credits/day during the season — fits the
  post-finals headroom under the [[Budget Governor]] (measured 2026-06-11:
  ~525/day total burn with NBA/NHL finals still running, 14.1k of 20k
  remaining).

## Recon evidence (2026-06-11, all live-verified)

| Leg | Finding |
|---|---|
| Odds API odds | `basketball_wnba` active; Pinnacle posts 2-outcome h2h (4/6 events, median overround 3.95%) |
| Odds API scores | `/scores` works (6 upcoming, 14 with `daysFrom=3`) — schedule + settlement source OK |
| Team universe | Exactly **15 teams** observed across events+scores: the 13 incumbents + 2026 expansion **Toronto Tempo** and **Portland Fire**; Connecticut Sun unchanged |
| Tipsport league | `https://www.tipsport.cz/kurzy/basketbal/basketbal-zeny/wnba-10` → competition id **10**, category segment **`basketbal-zeny`** (women, not `-muzi`) |
| Tipsport labels | Match listings append **" Ž"** to every team: `Indiana Fever Ž - Chicago Sky Ž` |
| Tipsport markets | Same tab structure as NBA (Zápas / Vítěz do rozhodnutí / Handicap / Počet bodů) |

## Profile decisions

```python
WNBA = SportProfile(
    key="wnba",
    display_name="WNBA",
    odds_api_key="basketball_wnba",
    tipsport_competition_id=10,        # recon 2026-06-11: /kurzy/.../wnba-10
    tipsport_sport_slug="basketbal",
    tipsport_super_sport_id=7,         # basketball superSport (same as NBA); unused for the [] moneyline request
    canonical_teams=WNBA_TEAMS,        # 15 teams, Odds API naming
    team_mappings=WNBA_TIPSPORT_ALIASES,  # "<Name> Ž" -> "<Name>" for all 15
    live_backstop_hours=2.5,
    market_rule="winner_incl_ot_2way",
    tipsport_league_slug="wnba",
    tipsport_moneyline_matchview="WINNER_WHOLE_MATCH",
    tipsport_gender_slug="zeny",       # NEW FIELD — see below
    use_canonical_set=True,
)
```

- **`tipsport_moneyline_matchview`** — designed as `WINNER_WHOLE_MATCH` (NBA
  precedent), **corrected to `WINNER_FINAL_DECISION` during verification** —
  see the Verification outcome section below. The design's own contingency
  check (inspect the raw response for 2-outcome `oppsTab` rows) fired: the NHL
  lesson applied to basketball too.
- **`live_backstop_hours=2.5`** — WNBA plays 4×10min; real elapsed time ≈2h.
  Mirrors NHL's 2.5 (similar real duration + buffer). NBA's 3.0 covers a
  longer 48-min format.
- **New profile field `tipsport_gender_slug: str = "muzi"`** —
  `_warmup_url()` currently hardcodes the `{sport}-muzi` category segment;
  WNBA lives under `basketbal-zeny`. The field (default `"muzi"`, so
  NBA/NHL/MLB behavior is bit-identical) parameterizes the warmup/Referer URL:
  `/kurzy/{slug}/{slug}-{gender_slug}/{league}-{competition_id}`. All four
  profiles set it explicitly.
- **Aliases**: all 15 `"<Name> Ž" -> "<Name>"` entries. Fuzzy would match the
  Ž-suffixed labels anyway (score ≈96, unambiguous) but would WARN on every
  poll; explicit aliases are deterministic and silent. Clean (unsuffixed)
  labels pass through the canonical-set check unchanged, so either API label
  shape works.

## Touchpoints

Code:
- `oddsiq/sports/teams.py` — `WNBA_TEAMS` (15) + `WNBA_TIPSPORT_ALIASES` (15)
- `oddsiq/sports/profiles.py` — dataclass field `tipsport_gender_slug`,
  `WNBA` profile, `SPORTS`, `ENABLED_SPORTS` (+ explicit `tipsport_gender_slug="muzi"` on NBA/NHL/MLB)
- `oddsiq/sports/__init__.py` — export `WNBA`
- `oddsiq/scrapers/tipsport.py` — `_warmup_url()` uses the gender slug
- `oddsiq/notifications/telegram.py` — `_SPORT_EMOJI["wnba"] = 🏀`
- `oddsiq/dashboard/templates/partials/upcoming_board.html` — emoji map + `wnba`
- `oddsiq/cli.py` — `--sport` help strings mention `wnba`
- `oddsiq/pipeline/schedule_service.py` — docstrings (NBA/NHL/MLB → enabled profiles)
- `pyproject.toml` — project description

Tests (mirror existing per-sport patterns):
- `tests/test_sports_profiles.py` — four sports enabled, WNBA field pins
  (competition id 10, WHOLE_MATCH, gender slug zeny, 15 teams, backstop 2.5)
- `tests/test_normalize.py` — Ž-suffix alias cases + clean-name passthrough
- `tests/test_tipsport.py` — WNBA parse fixture with Ž labels; `_warmup_url`
  gender-segment assertions (muzi for NBA, zeny for WNBA)
- `tests/test_telegram.py` — `wnba` sport header case
- Any test asserting exactly three sports (dashboard/scheduler/schedule
  service) — extend to four

Docs (after code; `make audit` is the drift gate):
- the project notes — sport lists, backstop list, status, test counts
- `docs/Components/Sports/SportProfile.md` — WNBA table + gender-slug field
- `docs/Decisions/Multi-Sport Expansion.md` — WNBA row (id 10) + scope update
- `docs/00 - Map of Content.md`, `HANDBOOK.md`, architecture pages with
  enabled-sport lists, `docs/Testing/Test Suite Overview.md` counts

## Verification plan

1. `pytest tests/` — full suite green
2. `ruff check oddsiq/ tests/ scripts/`
3. `make audit` — docs-drift checker green
4. **Live smoke**: `oddsiq tipsport-smoke --sport wnba` (real Cloudflare +
   offer API) — must pass its acceptance criteria (even moneyline pair count,
   sane odds, no internal errors) AND the raw response must show 2-outcome
   `oppsTab` rows under the WHOLE_MATCH tab
5. `oddsiq live-discover --sport wnba` if a game is live at verification time
6. Pinnacle side needs no smoke (same `/odds` path; verified by recon probe)

## Verification outcome (2026-06-12)

All gates ran; one changed the design and uncovered a production bug:

1. Suite green (655), ruff clean, docs audit clean.
2. First live smoke (still on `WINNER_WHOLE_MATCH`) passed its acceptance
   criteria but the **raw-response inspection showed 5-outcome rows**
   (`1/1x/x/x2/2` with draw at 20.0) — basketball's WHOLE_MATCH tab is the
   regulation 3-way, exactly the corruption mode the checklist anticipated.
3. Same-book A/B (`WINNER_WHOLE_MATCH` vs `WINNER_FINAL_DECISION`, all 4 WNBA
   games + NBA Finals G5) settled the semantics: WHOLE legs sum below 100%
   two-way implied (NBA: 1.51/3.00 = 99.6% — impossible standalone), while
   FINAL_DECISION carries normal 5–7% vig and aligns with Pinnacle's incl-OT
   h2h. **Both basketball profiles switched to `WINNER_FINAL_DECISION`
   (superSportId 7), fail-closed like NHL.**
4. Consequence beyond WNBA: **NBA had been recording regulation prices as
   moneyline since 2026-06-02** — pre-fix NBA value bets / CLV carry a
   systematic positive bias (e.g. a fake +5.1% Knicks "edge" at the time of
   verification). Historical-data cleanup is tracked as a follow-up decision.
5. Post-fix smokes: WNBA 8/8 moneyline snapshots (1.25/3.70 …) and NBA 2/2
   (1.43/2.84) — true incl-OT prices over the wire, Ž-suffix labels
   normalized, gender-slug warmup URL exercised by the live Cloudflare solve.

## Rollout

Detection-only, evaluation mode — WNBA rides the existing passive-CLV
collection. Deploy is the standard VM flow (`make push` → `compose pull`),
left to the operator. Watch the first night's `Scraper health:` lines for
`unmatched_snapshots=0` on wnba and the heartbeat's `budget_throttle` for
governor escalation.
