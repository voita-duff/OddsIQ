"""Per-bookmaker audit aggregations used by `oddsiq audit-bookmakers`.

All section queries are pure read-only SQL run through `store._db`. The
audit module is a sibling to Store (not a consumer of its higher-level
API) because most sections need bespoke aggregations that would bloat
Store with one-off methods.

`--since YYYY-MM-DD` semantics: section 1 (snapshots) filters on `captured_at`
directly. The value-bet sections (2-5) filter on the value bet's own
`flagged_at` column — its real creation time. (An earlier version derived a
proxy from `MIN(odds_snapshots.captured_at)` under the false belief that
`value_bets` had no creation timestamp; that proxy dropped bets with no matching
snapshot and could skew forward on dedup "last-seen" captured_at bumps.)

Because `tier` is part of the value-bet dedup key, one (game, bookmaker, market,
selection) opportunity can have separate warm/hot/legacy rows; the value-bet
sections read a deduped derived table (one row per opportunity) so counts and
CLV don't double-count.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from oddsiq.db.store import Store
from oddsiq.pipeline.tiers import SHADOW_TIER

logger = logging.getLogger(__name__)

# Edge-percentage buckets, shared by the per-book audit and the live-scout
# report so the boundaries live in one place. (label, lo_inclusive, hi_exclusive)
EDGE_BUCKETS: list[tuple[str, float, float]] = [
    ("3-5%", 3.0, 5.0),
    ("5-7%", 5.0, 7.0),
    ("7-10%", 7.0, 10.0),
    ("10%+", 10.0, float("inf")),
]


def flat_stake_pl(result: str | None, entry_odds: float) -> float:
    """Profit/loss of a 1-unit flat stake on a settled bet.

    Canonical convention shared with the dashboard (`dashboard/queries.py`,
    which recomputes it in SQL — keep the CASE there in sync with this):
        win → entry_odds - 1   loss → -1   push/draw/unsettled → 0
    """
    if result == "win":
        return entry_odds - 1.0
    if result == "loss":
        return -1.0
    return 0.0


async def run_audit(
    store: Store,
    *,
    bookmaker: str | None = None,
    since: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Run all five audit sections and return them as a dict of section-name -> rows."""
    return {
        "snapshot_volumes": await _snapshot_volumes(store, bookmaker=bookmaker, since=since),
        "value_bets": await _value_bets(store, bookmaker=bookmaker, since=since),
        "clv_by_bucket": await _clv_by_bucket(store, bookmaker=bookmaker, since=since),
        "closing_quality": await _closing_quality(store, bookmaker=bookmaker, since=since),
        "coverage_gap_proxy": await _coverage_gap_proxy(store, bookmaker=bookmaker, since=since),
    }


async def _snapshot_volumes(
    store: Store, *, bookmaker: str | None, since: str | None
) -> list[dict[str, Any]]:
    """Per-bookmaker snapshot count, distinct games matched, total games in window, match rate.

    Counts ALL ``odds_snapshots`` rows with no pre-tipoff filter, so for the
    live-scouted books (``pinnacle``, ``tipsport``) these now include
    post-tipoff live-scout snapshots — `snapshots` and `match_rate` are
    total (pregame + live) coverage, not pregame-only. Interpret cross-book
    comparisons accordingly (only Tipsport/Pinnacle get live rows today).
    """
    where_book = "AND bookmaker = ?" if bookmaker else ""
    where_since_snap = "AND captured_at >= ?" if since else ""
    where_since_game = "AND scheduled_at >= ?" if since else ""

    params_book: list[Any] = [bookmaker] if bookmaker else []
    params_since_snap: list[Any] = [since] if since else []
    params_since_game: list[Any] = [since] if since else []

    sql = f"""
        WITH total_games AS (
            SELECT COUNT(*) AS n FROM games
            WHERE 1=1 {where_since_game}
        )
        SELECT
            os.bookmaker AS bookmaker,
            COUNT(*) AS snapshots,
            COUNT(DISTINCT os.game_id) AS games_matched,
            (SELECT n FROM total_games) AS total_games
        FROM odds_snapshots os
        WHERE 1=1 {where_book} {where_since_snap}
        GROUP BY os.bookmaker
        ORDER BY snapshots DESC
    """
    params = params_since_game + params_book + params_since_snap
    cursor = await store._db.execute(sql, params)
    rows = await cursor.fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        total = row["total_games"]
        rate = 100.0 * row["games_matched"] / total if total else 0.0
        out.append({
            "bookmaker": row["bookmaker"],
            "snapshots": row["snapshots"],
            "games_matched": row["games_matched"],
            "total_games": total,
            "match_rate": round(rate, 1),
        })
    return out


# One row per value-bet OPPORTUNITY, aliased ``vb``. ``tier`` is in the dedup
# key, so warm/hot/legacy rows of a single (game, book, market, selection) must
# collapse or every per-bet aggregate double-counts. Picks the latest
# observation per opportunity (and folds legacy `medium` rows). Column-preserving
# (SELECT *), so callers reference vb.* exactly as a plain value_bets scan would.
# The inner scan drops rows stamped with an excluded_reason (known-bad data,
# e.g. the 2026-06 basketball matchView incident) from every audit section.
# far-tier SHADOW rows (never bettable) are excluded, mirroring the dashboard view (2026-09-06)
_VALUE_BETS_DEDUPED = (
    "(SELECT * FROM (SELECT *, ROW_NUMBER() OVER ("
    "PARTITION BY game_id, bookmaker, market_type, selection, IFNULL(line, -999.0) "
    "ORDER BY flagged_at DESC, id DESC) AS _dedup_rn "
    f"FROM value_bets WHERE excluded_reason IS NULL AND tier != '{SHADOW_TIER}') "
    "WHERE _dedup_rn = 1) AS vb"
)


def _value_bets_source(since: str | None) -> tuple[str, str, list[Any]]:
    """Return ``(from_source, since_where, params)`` for the value-bet sections.

    ``from_source`` is the deduped derived table (one row per opportunity).
    ``since_where`` filters on the bet's own ``flagged_at`` (its creation time),
    appended into the section's WHERE clause; empty when ``since`` is None.
    """
    if not since:
        return _VALUE_BETS_DEDUPED, "", []
    return _VALUE_BETS_DEDUPED, " AND vb.flagged_at >= ?", [since]


async def _value_bets(
    store: Store, *, bookmaker: str | None, since: str | None
) -> list[dict[str, Any]]:
    """Per-book bet counts and CLV stats."""
    src, since_where, since_params = _value_bets_source(since)
    where_book = "AND vb.bookmaker = ?" if bookmaker else ""
    book_params: list[Any] = [bookmaker] if bookmaker else []

    sql = f"""
        SELECT
            vb.bookmaker AS bookmaker,
            COUNT(*) AS total,
            SUM(CASE WHEN vb.clv_pct IS NOT NULL THEN 1 ELSE 0 END) AS with_clv,
            SUM(CASE WHEN vb.result IS NOT NULL THEN 1 ELSE 0 END) AS with_result,
            AVG(CASE WHEN vb.clv_pct IS NOT NULL THEN vb.clv_pct END) AS avg_clv,
            SUM(CASE WHEN vb.clv_pct IS NOT NULL AND vb.clv_pct > 0 THEN 1 ELSE 0 END) AS pos_clv_count
        FROM {src}
        WHERE 1=1 {where_book}{since_where}
        GROUP BY vb.bookmaker
        ORDER BY total DESC
    """
    params = book_params + since_params
    cursor = await store._db.execute(sql, params)
    rows = await cursor.fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        # Median CLV: pull all clv_pct values for this book and compute server-side.
        median_sql = f"""
            SELECT vb.clv_pct AS clv_pct
            FROM {src}
            WHERE vb.bookmaker = ? AND vb.clv_pct IS NOT NULL{since_where}
            ORDER BY vb.clv_pct
        """
        median_params = [row["bookmaker"]] + since_params
        clv_cursor = await store._db.execute(median_sql, median_params)
        clv_values = [r["clv_pct"] for r in await clv_cursor.fetchall()]
        if clv_values:
            mid = len(clv_values) // 2
            median = clv_values[mid] if len(clv_values) % 2 else (clv_values[mid - 1] + clv_values[mid]) / 2.0
        else:
            median = None

        pos_pct = (
            100.0 * row["pos_clv_count"] / row["with_clv"]
            if row["with_clv"]
            else 0.0
        )
        out.append({
            "bookmaker": row["bookmaker"],
            "total": row["total"],
            "with_clv": row["with_clv"],
            "with_result": row["with_result"],
            "avg_clv": round(row["avg_clv"], 2) if row["avg_clv"] is not None else None,
            "median_clv": round(median, 2) if median is not None else None,
            "pos_clv_pct": round(pos_pct, 1),
        })
    return out


async def _clv_by_bucket(
    store: Store, *, bookmaker: str | None, since: str | None
) -> list[dict[str, Any]]:
    """Edge-bucket breakdown per book.

    Buckets: [3,5)/[5,7)/[7,10)/[10,inf). Bets with edge_pct < 3 are
    excluded (they wouldn't have been flagged under EDGE_THRESHOLD).
    """
    src, since_where, since_params = _value_bets_source(since)
    where_book = "AND vb.bookmaker = ?" if bookmaker else ""
    book_params: list[Any] = [bookmaker] if bookmaker else []

    sql = f"""
        SELECT
            vb.bookmaker AS bookmaker,
            CASE
                WHEN vb.edge_pct >= 3.0 AND vb.edge_pct < 5.0  THEN '3-5%'
                WHEN vb.edge_pct >= 5.0 AND vb.edge_pct < 7.0  THEN '5-7%'
                WHEN vb.edge_pct >= 7.0 AND vb.edge_pct < 10.0 THEN '7-10%'
                WHEN vb.edge_pct >= 10.0                       THEN '10%+'
            END AS edge_bucket,
            COUNT(*) AS bets,
            AVG(vb.edge_pct) AS avg_edge,
            AVG(CASE WHEN vb.clv_pct IS NOT NULL THEN vb.clv_pct END) AS avg_clv,
            SUM(CASE WHEN vb.clv_pct IS NOT NULL AND vb.clv_pct > 0 THEN 1 ELSE 0 END) AS pos_count,
            SUM(CASE WHEN vb.clv_pct IS NOT NULL THEN 1 ELSE 0 END) AS clv_count
        FROM {src}
        WHERE vb.edge_pct >= 3.0 {where_book}{since_where}
        GROUP BY vb.bookmaker, edge_bucket
        ORDER BY vb.bookmaker, MIN(vb.edge_pct)
    """
    params = book_params + since_params
    cursor = await store._db.execute(sql, params)
    rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        pos_pct = 100.0 * row["pos_count"] / row["clv_count"] if row["clv_count"] else 0.0
        out.append({
            "bookmaker": row["bookmaker"],
            "edge_bucket": row["edge_bucket"],
            "bets": row["bets"],
            "avg_edge": round(row["avg_edge"], 2),
            "avg_clv": round(row["avg_clv"], 2) if row["avg_clv"] is not None else None,
            "pos_clv_pct": round(pos_pct, 1),
        })
    return out


async def _closing_quality(
    store: Store, *, bookmaker: str | None, since: str | None
) -> list[dict[str, Any]]:
    """Per-book closing-snapshot freshness.

    bets_resolved = bets with result populated.
    with_closing = bets resolved AND clv_pct populated (we had a valid closing line).
    stale_skipped = bets resolved AND clv_pct NULL (closing too stale, see
    `CLOSING_FRESHNESS_MINUTES` gate).
    """
    src, since_where, since_params = _value_bets_source(since)
    where_book = "AND vb.bookmaker = ?" if bookmaker else ""
    book_params: list[Any] = [bookmaker] if bookmaker else []

    sql = f"""
        SELECT
            vb.bookmaker AS bookmaker,
            SUM(CASE WHEN vb.result IS NOT NULL THEN 1 ELSE 0 END) AS bets_resolved,
            SUM(CASE WHEN vb.result IS NOT NULL AND vb.clv_pct IS NOT NULL THEN 1 ELSE 0 END) AS with_closing,
            SUM(CASE WHEN vb.result IS NOT NULL AND vb.clv_pct IS NULL THEN 1 ELSE 0 END) AS stale_skipped
        FROM {src}
        WHERE 1=1 {where_book}{since_where}
        GROUP BY vb.bookmaker
        HAVING bets_resolved > 0
        ORDER BY bets_resolved DESC
    """
    params = book_params + since_params
    cursor = await store._db.execute(sql, params)
    rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        pct = 100.0 * row["with_closing"] / row["bets_resolved"] if row["bets_resolved"] else 0.0
        out.append({
            "bookmaker": row["bookmaker"],
            "bets_resolved": row["bets_resolved"],
            "with_closing": row["with_closing"],
            "stale_skipped": row["stale_skipped"],
            "pct_clean": round(pct, 1),
        })
    return out


async def _coverage_gap_proxy(
    store: Store, *, bookmaker: str | None, since: str | None
) -> list[dict[str, Any]]:
    """Proxy: count resolved bets where CLV could not be computed (NULL clv_pct).

    These bets had their closing snapshot rejected by the freshness gate
    in mark_closing_snapshots(), which usually means the scraper went
    silent during the hot tier. P2.2 uses this as a stand-in for proper
    coverage-gap event persistence (deferred to P3 dashboard).
    """
    src, since_where, since_params = _value_bets_source(since)
    where_book = "AND vb.bookmaker = ?" if bookmaker else ""
    book_params: list[Any] = [bookmaker] if bookmaker else []

    sql = f"""
        SELECT
            vb.bookmaker AS bookmaker,
            COUNT(*) AS resolved_without_clv
        FROM {src}
        WHERE vb.result IS NOT NULL AND vb.clv_pct IS NULL {where_book}{since_where}
        GROUP BY vb.bookmaker
        HAVING resolved_without_clv > 0
        ORDER BY resolved_without_clv DESC
    """
    params = book_params + since_params
    cursor = await store._db.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {"bookmaker": row["bookmaker"], "resolved_without_clv": row["resolved_without_clv"]}
        for row in rows
    ]


_SECTION_TITLES = [
    ("snapshot_volumes",   "Section 1 — Snapshot volumes per book"),
    ("value_bets",         "Section 2 — Value bets per book"),
    ("clv_by_bucket",      "Section 3 — CLV by edge bucket"),
    ("closing_quality",    "Section 4 — Closing snapshot quality"),
    ("coverage_gap_proxy", "Section 5 — Coverage gap proxy"),
]


def _format_row(row: dict[str, Any]) -> str:
    """Render a row dict as 'key=value  key=value ...' joined with two spaces."""
    parts: list[str] = []
    for k, v in row.items():
        if isinstance(v, list):
            parts.append(f"{k}=[{len(v)} items]")
        elif v is None:
            parts.append(f"{k}=-")
        elif isinstance(v, float):
            parts.append(f"{k}={v:.2f}")
        else:
            parts.append(f"{k}={v}")
    return "  ".join(parts)


def format_text(audit: dict[str, list[dict[str, Any]]]) -> str:
    """Human-readable formatter. One section per block, blank line between."""
    blocks: list[str] = []
    for key, title in _SECTION_TITLES:
        rows = audit.get(key, [])
        block = [title, "─" * len(title)]
        if not rows:
            block.append("(no rows)")
        else:
            for row in rows:
                block.append(_format_row(row))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks) + "\n"


def format_json(audit: dict[str, list[dict[str, Any]]]) -> str:
    """Machine-readable formatter. Stable schema: top-level keys are the
    five section names, values are arrays of row objects."""
    return json.dumps(audit, indent=2, default=str)
