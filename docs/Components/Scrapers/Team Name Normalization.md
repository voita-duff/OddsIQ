---
tags:
  - component/scraper
  - status/complete
  - phase/p0
  - layer/data-ingestion
  - type/component
source-files:
  - oddsiq/scrapers/normalize.py
  - oddsiq/sports/profiles.py
  - oddsiq/sports/teams.py
  - tests/test_normalize.py
related:
  - "[[PinnacleScraper]]"
  - "[[TipsportScraper]]"
  - "[[Scheduler]]"
  - "[[SportProfile]]"
  - "[[Failure Modes]]"
---

# Team Name Normalization

## Purpose

Resolves bookmaker-specific team name variations to canonical names for the active sport. Without normalisation, [[PinnacleScraper]] snapshots (using The Odds API names) could not be matched against [[TipsportScraper]] snapshots (using Tipsport's Czech abbreviations) by the [[Scheduler]]. A three-tier matching strategy handles exact matches, known bookmaker aliases, and fuzzy fallback for unknown variations.

The canonical set and alias mappings are per-sport, sourced from the [[SportProfile]] registry (`oddsiq/sports/profiles.py` + `oddsiq/sports/teams.py`).

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/scrapers/normalize.py` | `normalize_team(name, bookmaker, sport)` | Tier 0 (blank) → 1 (canonical) → 2 (alias mapping) → 3 (fuzzy) |
| `oddsiq/scrapers/normalize.py` | `FUZZY_THRESHOLD` | `85` — minimum WRatio score to accept a fuzzy match |
| `oddsiq/sports/teams.py` | `NBA_TEAMS`, `NHL_TEAMS`, `MLB_TEAMS`, `WNBA_TEAMS` | Per-sport `frozenset` of canonical team names |
| `oddsiq/sports/teams.py` | `NBA_TIPSPORT_ALIASES`, `NHL_TIPSPORT_ALIASES`, `MLB_TIPSPORT_ALIASES`, `WNBA_TIPSPORT_ALIASES` | Per-sport Tipsport label → canonical name dicts (WNBA's is generated: `"<Name> Ž"` → `"<Name>"`) |
| `oddsiq/sports/profiles.py` | `SportProfile.canonical_teams` | `frozenset` used as the exact-match set and fuzzy corpus |
| `oddsiq/sports/profiles.py` | `SportProfile.team_mappings` | `dict[str, str]` of Tipsport label → canonical name for the sport |
| `tests/test_normalize.py` | — | Tests: canonical, alias mappings, fuzzy, unknown, blank guard |

## Inputs

- `name: str` — raw team name from bookmaker (e.g. `"LA Lakers"`, `"Los Angeles Lakers"`)
- `bookmaker: str` — identifier (`"pinnacle"`, `"tipsport"`) to select the alias dict
- `sport: str` — sport key (`"nba"`, `"nhl"`, `"mlb"`, `"wnba"`) to load the correct [[SportProfile]]

## Outputs

- `str | None` — canonical team name (e.g. `"Los Angeles Lakers"`) or `None` if unrecognisable
- When `None` is returned, the calling scraper skips the match (debug-logged in [[TipsportScraper]])

## Interactions

- [[PinnacleScraper]] — Odds API names are generally canonical; they pass through tier 1 without alias lookup. Both `parse_response` and `OddsApiScheduleSource._parse_scores_event` call `normalize_team(name, "pinnacle", sport)`.
- [[TipsportScraper]] — calls `normalize_team(raw_name, "tipsport", profile.key)` for each match's `participantHome` and `participantVisiting`. NBA has 10 known abbreviation aliases in `NBA_TIPSPORT_ALIASES`; WNBA has 15 generated aliases (Tipsport suffixes every women's-league team with `" Ž"`); NHL and MLB aliases are empty dicts (Tipsport labels match Odds API names for those sports).
- [[Scheduler]] / [[Pipeline Services]] — after scrapers produce `OddsSnapshot` objects with `_home_team`/`_away_team`, the `PollCoordinator` matches them to `Game` objects by comparing normalised names.
- [[SportProfile]] — `get_profile(sport)` supplies `canonical_teams` and `team_mappings` per call.
- [[Failure Modes]] — unrecognised team names cause silent match skipping. A fuzzy match resolving to the wrong team would pair odds with the wrong game, producing false edges.

## Internal Logic

### Three-tier matching

`normalize_team(name, bookmaker, sport)`:

```python
profile = get_profile(sport)
canonical = profile.canonical_teams  # frozenset
# Tier 0: blank guard
if not name or not name.strip():
    return None
# Tier 1: exact canonical match
if name in canonical:
    return name
# Tier 2: alias mapping for this sport
mapped = profile.team_mappings.get(name)
if mapped is not None:
    return mapped
# Tier 3: fuzzy fallback
result = process.extractOne(name, canonical, scorer=fuzz.WRatio)
if result and result[1] >= FUZZY_THRESHOLD:
    logger.warning("Fuzzy matched '%s' (%s/%s) -> '%s' (score: %d). Add to the sport's team_mappings.", ...)
    return result[0]
logger.error("Could not normalize team name '%s' from %s (%s)", name, bookmaker, sport)
return None
```

**Tier 0 — blank guard**: Tipsport returns placeholder entries with blank participant fields on every poll. The guard short-circuits silently so the caller can skip without log noise.

**Tier 1 — exact canonical match**: O(1) `frozenset` lookup against `profile.canonical_teams`. Common path for [[PinnacleScraper]] data and any Tipsport name that happens to match exactly.

**Tier 2 — alias mapping**: looks up the name in `profile.team_mappings`. Current state per sport:

| Sport | Tipsport aliases | Notes |
|-------|-----------------|-------|
| NBA | 10 entries (`LA Lakers`, `LA Clippers`, `GS Warriors`, `SA Spurs`, `NY Knicks`, `NO Pelicans`, `OKC Thunder`, `Portland Blazers`, `Phila 76ers`, `Minnesota Wolves`) | Abbreviations used by Tipsport |
| NHL | empty dict | Tipsport labels match Odds API names; fuzzy covers minor drift |
| MLB | empty dict | All 30 team names verified matching 2026-06-03 |
| WNBA | 15 entries (generated `"<Name> Ž"` → `"<Name>"`) | Tipsport suffixes every women's-league team with `" Ž"` (ženy); dict comprehension over `WNBA_TEAMS` (verified live 2026-06-12) |

**Tier 3 — fuzzy fallback**: `rapidfuzz.process.extractOne` with `WRatio` scorer and threshold 85. `WRatio` tries multiple strategies (ratio, partial, token sort, token set) and returns the best. Fires a WARNING to prompt adding the match to tier 2. If the score is below 85, logs an ERROR and returns `None`.

### `use_canonical_set` escape hatch

`SportProfile.use_canonical_set` defaults to `True` for all current sports. When `False` (intended for college sports, not yet active), the normaliser would trust source names and fall through to fuzzy without the exact-match guard — the field is reserved for future use.

## Configuration

| Constant | Value | Location |
|----------|-------|----------|
| `FUZZY_THRESHOLD` | 85 | `normalize.py` |
| Per-sport canonical sets | `NBA_TEAMS`, `NHL_TEAMS`, `MLB_TEAMS`, `WNBA_TEAMS` | `oddsiq/sports/teams.py` |
| Per-sport alias dicts | `NBA_TIPSPORT_ALIASES`, `NHL_TIPSPORT_ALIASES`, `MLB_TIPSPORT_ALIASES`, `WNBA_TIPSPORT_ALIASES` | `oddsiq/sports/teams.py` |

## Error Handling

- **Unrecognisable name**: returns `None`, logged at ERROR. Match skipped by the calling scraper.
- **Fuzzy match used**: logged at WARNING with score and suggested action. The match still proceeds; the warning signals a mapping gap to close.
- **Wrong fuzzy match**: no automated detection. Review fuzzy WARNING logs and add confirmed mappings to the sport's alias dict.

## Known Issues

- **Fuzzy match risk**: a bookmaker abbreviation that fuzzy-matches above 85 to the wrong team would pair odds with the wrong game. Mitigate by reviewing WARNING logs and promoting matches to tier 2.
- **Static team lists**: if a sport adds/removes/renames a team, the relevant `*_TEAMS` frozenset must be updated manually. No dynamic discovery.

## Tests

`tests/test_normalize.py` covers canonical pass-through (tier 1), alias resolution (tier 2), fuzzy handling (tier 3), unknown name returning `None`, and the blank/whitespace guard.
