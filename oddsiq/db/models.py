from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Game:
    external_id: str
    home_team: str
    away_team: str
    scheduled_at: datetime
    commence_time: datetime | None = None
    status: str = "scheduled"
    home_score: int | None = None
    away_score: int | None = None
    sport: str = "nba"
    id: int | None = None


@dataclass
class OddsSnapshot:
    game_id: int
    bookmaker: str
    market_type: str  # "moneyline", "spread", "total"
    selection: str  # "home", "away", "over", "under"
    odds: float
    line: float | None = None
    is_closing: bool = False
    raw_response_path: str | None = None
    # Upstream market freshness (The Odds API/Pinnacle), distinct from
    # captured_at which is OddsIQ's local observation time. Tipsport doesn't
    # expose an equivalent timestamp; historical rows remain NULL.
    source_updated_at: datetime | None = None
    sport: str = "nba"
    id: int | None = None
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Transient fields for game matching — not persisted to the database.
    _home_team: str | None = field(default=None, repr=False)
    _away_team: str | None = field(default=None, repr=False)
    _commence_time: datetime | None = field(default=None, repr=False)
    # Transient provider identity used for exact schedule↔odds attribution.
    # game_id already persists the relationship, so this field need not.
    _source_event_id: str | None = field(default=None, repr=False)
    # True only when a real upstream payload omitted/malformed its freshness
    # field. Hand-built legacy/test snapshots leave this False and use their
    # captured_at as a compatibility fallback in the edge engine.
    _source_freshness_missing: bool = field(default=False, repr=False)
    # Transient Tipsport execution identity (match id, deep-link url,
    # opportunity id) — captured by the parser, threaded through find_edges,
    # and persisted ONLY on value_bets (migration 009). odds_snapshots stays
    # schema-identical so its dedup key / "last seen" semantics don't change.
    _tipsport_match_id: int | None = field(default=None, repr=False)
    _tipsport_opp_id: int | None = field(default=None, repr=False)
    _tipsport_url: str | None = field(default=None, repr=False)


@dataclass
class ValueBet:
    game_id: int
    bookmaker: str
    market_type: str
    selection: str
    entry_odds: float
    fair_odds: float
    edge_pct: float
    line: float | None = None
    closing_odds: float | None = None
    pinnacle_closing: float | None = None
    clv_pct: float | None = None
    result: str | None = None
    stake: float | None = None
    # `tier` is the polling tier at detection time: 'hot', 'warm', or 'far'
    # (far = shadow detection for sports with SportProfile.shadow_far_detection,
    # never bet). It's part of the value_bets unique key so a warm-tier
    # observation and a later hot-tier observation for the same selection
    # coexist as separate rows — each gets its own CLV computation. Defaults
    # to 'hot' because every value bet captured before migration 003 was
    # hot-tier-detected. Note: historical rows may be 'medium' from before
    # the cadence rename.
    tier: str = "hot"
    sport: str = "nba"
    # Non-NULL marks a known-bad row every reporting surface filters out while
    # settlement/CLV still process it. Set at insert time by the pregame
    # plausibility/coherence guards ('implausible_edge_pregame',
    # 'incoherent_book_pregame') or retroactively by `oddsiq mark-excluded`.
    excluded_reason: str | None = None
    # Tipsport execution identity captured at detection (migration 009):
    # the book's own match id, deep-link url, and opportunity id for the
    # detected selection. Lets the P0 availability probe (and any later
    # placement) address the exact offer instead of re-matching by team
    # name. NULL on historical rows and non-Tipsport sources.
    tipsport_match_id: int | None = None
    tipsport_opp_id: int | None = None
    tipsport_url: str | None = None
    # Immutable freshness evidence for the Pinnacle pair behind this edge.
    # Snapshot rows can receive later "last seen" bumps; this value cannot.
    sharp_source_updated_at: datetime | None = None
    id: int | None = None
    flagged_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PlacedBet:
    """One auto-betting DECISION on a value bet (migration 010).

    Either a `skipped` row carrying the qualification failure reason, or an
    execution attempt progressing `pending -> placed | rejected | failed |
    unknown -> settled`. An operator may reconcile pending/unknown to placed
    or rejected after checking book history; this never retries the wager.
    `value_bet_id` is UNIQUE, so a second decision is impossible.
    Money columns are CZK (Tipsport account currency).
    """
    value_bet_id: int
    game_id: int
    sport: str
    status: str                          # skipped|pending|placed|rejected|failed|unknown|settled
    skip_reason: str | None = None
    stake_czk: float | None = None
    requested_odds: float | None = None
    min_odds: float | None = None        # extension aborts confirm below this price
    accepted_odds: float | None = None
    receipt_id: str | None = None
    error: str | None = None
    balance_after_czk: float | None = None
    commanded_at: datetime | None = None
    resolved_at: datetime | None = None  # last state transition (placement result / settlement)
    latency_detect_ms: int | None = None  # flagged_at -> command dispatch
    latency_place_ms: int | None = None   # command dispatch -> extension result
    settled_pl_czk: float | None = None
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
