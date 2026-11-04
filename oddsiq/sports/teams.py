"""Per-sport team data — the source of truth the registry and normalizer use."""
from __future__ import annotations

NBA_TEAMS: frozenset[str] = frozenset({
    "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets",
    "Chicago Bulls", "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets",
    "Detroit Pistons", "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
    "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies", "Miami Heat",
    "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans",
    "New York Knicks", "Oklahoma City Thunder", "Orlando Magic",
    "Philadelphia 76ers", "Phoenix Suns", "Portland Trail Blazers",
    "Sacramento Kings", "San Antonio Spurs", "Toronto Raptors", "Utah Jazz",
    "Washington Wizards",
})

# Tipsport abbreviation -> canonical name (NBA).
NBA_TIPSPORT_ALIASES: dict[str, str] = {
    "LA Lakers": "Los Angeles Lakers",
    "LA Clippers": "Los Angeles Clippers",
    "GS Warriors": "Golden State Warriors",
    "SA Spurs": "San Antonio Spurs",
    "NY Knicks": "New York Knicks",
    "NO Pelicans": "New Orleans Pelicans",
    "OKC Thunder": "Oklahoma City Thunder",
    "Portland Blazers": "Portland Trail Blazers",
    "Phila 76ers": "Philadelphia 76ers",
    "Minnesota Wolves": "Minnesota Timberwolves",
}

NHL_TEAMS: frozenset[str] = frozenset({
    "Anaheim Ducks", "Boston Bruins", "Buffalo Sabres", "Calgary Flames",
    "Carolina Hurricanes", "Chicago Blackhawks", "Colorado Avalanche",
    "Columbus Blue Jackets", "Dallas Stars", "Detroit Red Wings",
    "Edmonton Oilers", "Florida Panthers", "Los Angeles Kings", "Minnesota Wild",
    "Montreal Canadiens", "Nashville Predators", "New Jersey Devils",
    "New York Islanders", "New York Rangers", "Ottawa Senators",
    "Philadelphia Flyers", "Pittsburgh Penguins", "San Jose Sharks",
    "Seattle Kraken", "St. Louis Blues", "Tampa Bay Lightning",
    "Toronto Maple Leafs", "Utah Mammoth", "Vancouver Canucks",
    "Vegas Golden Knights", "Washington Capitals", "Winnipeg Jets",
})

# Tipsport labels match the Odds API names for NHL (verified: Carolina
# Hurricanes, Vegas Golden Knights). No aliases needed; fuzzy match covers
# minor drift. Confirm full-league names + Utah Mammoth at season start.
NHL_TIPSPORT_ALIASES: dict[str, str] = {}

MLB_TEAMS: frozenset[str] = frozenset({
    "Arizona Diamondbacks", "Athletics", "Atlanta Braves", "Baltimore Orioles",
    "Boston Red Sox", "Chicago Cubs", "Chicago White Sox", "Cincinnati Reds",
    "Cleveland Guardians", "Colorado Rockies", "Detroit Tigers", "Houston Astros",
    "Kansas City Royals", "Los Angeles Angels", "Los Angeles Dodgers",
    "Miami Marlins", "Milwaukee Brewers", "Minnesota Twins", "New York Mets",
    "New York Yankees", "Philadelphia Phillies", "Pittsburgh Pirates",
    "San Diego Padres", "San Francisco Giants", "Seattle Mariners",
    "St. Louis Cardinals", "Tampa Bay Rays", "Texas Rangers",
    "Toronto Blue Jays", "Washington Nationals",
})

# Tipsport labels match the Odds API names for MLB (all 30 verified 2026-06-03).
MLB_TIPSPORT_ALIASES: dict[str, str] = {}

# 2026 season roster: the 13 incumbents + expansion Toronto Tempo and Portland
# Fire. All 15 observed in Odds API events/scores data on 2026-06-11.
WNBA_TEAMS: frozenset[str] = frozenset({
    "Atlanta Dream", "Chicago Sky", "Connecticut Sun", "Dallas Wings",
    "Golden State Valkyries", "Indiana Fever", "Las Vegas Aces",
    "Los Angeles Sparks", "Minnesota Lynx", "New York Liberty",
    "Phoenix Mercury", "Portland Fire", "Seattle Storm", "Toronto Tempo",
    "Washington Mystics",
})

# Tipsport suffixes every women's-league team with " Ž" (ženy), e.g.
# "Indiana Fever Ž" (verified on the WNBA listing 2026-06-11). Map all 15
# explicitly so normalization is deterministic and silent; a clean
# (unsuffixed) label passes the canonical-set check before aliases apply.
WNBA_TIPSPORT_ALIASES: dict[str, str] = {
    f"{name} Ž": name for name in sorted(WNBA_TEAMS)
}

# Extracted verbatim from The Odds API americanfootball_nfl event data
# (2026-08-25, all 32 distinct home/away names across the posted season).
NFL_TEAMS: frozenset[str] = frozenset({
    "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills",
    "Carolina Panthers", "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns",
    "Dallas Cowboys", "Denver Broncos", "Detroit Lions", "Green Bay Packers",
    "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars",
    "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers",
    "Los Angeles Rams", "Miami Dolphins", "Minnesota Vikings",
    "New England Patriots", "New Orleans Saints", "New York Giants",
    "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers",
    "San Francisco 49ers", "Seattle Seahawks", "Tampa Bay Buccaneers",
    "Tennessee Titans", "Washington Commanders",
})

# Tipsport labels match the Odds API names for NFL (futures recon 2026-08-25:
# "Kansas City Chiefs", "Philadelphia Eagles", ... full names). Verify all 32
# via `oddsiq tipsport-smoke --sport nfl` once game markets post (~Sep); any
# drift becomes an explicit alias here.
NFL_TIPSPORT_ALIASES: dict[str, str] = {}
