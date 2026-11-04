"""Read-only SQL queries powering the OddsIQ dashboard.

Each function takes the existing :class:`oddsiq.db.store.Store` and
returns a plain dict or list of dicts that the Jinja templates render
directly. Queries use ``store._db`` (the underlying ``aiosqlite``
connection) — matching the pattern in ``oddsiq/audit/bookmakers.py``.

All queries are read-only. The dashboard never writes to the DB.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from oddsiq import config
from oddsiq.pipeline import tiers as pipeline_tiers
from oddsiq.pipeline.betting_service import parse_betting_sports
from oddsiq.sports.profiles import ENABLED_SPORTS, get_profile
from oddsiq.db.store import Store
from oddsiq.dashboard.milestones import MILESTONES

# One row per value-bet OPPORTUNITY. The store's dedup key includes `tier`, so a
# single (game, bookmaker, market, selection) can have separate warm/hot/legacy
# rows — every PERFORMANCE aggregate (ROI, win-rate, P&L, calibration, per-book
# counts) must collapse them to one or it double-counts. Picks the latest
# observation per opportunity (tier-agnostic; also folds legacy `medium` rows).
# Read-only and column-preserving (SELECT *), so callers reference the same
# columns as a plain `value_bets` scan. Drop-in: `FROM {VALUE_BETS_DEDUPED}`.
# The inner scan drops rows stamped with an excluded_reason (known-bad data,
# e.g. the 2026-06 basketball matchView incident) so no aggregate built on
# this source can resurface them.
# Far-tier SHADOW rows (never bettable; NFL far-tier detection) are excluded
# too — they must not shape ROI/CLV (2026-09-06). The filter sits INSIDE the
# ROW_NUMBER input on purpose: a flexed kickoff can push a game back to far
# AFTER a hot detection, so the far row can be the LATEST for its selection —
# filtering outside would rank it rn=1 and drop the whole selection.
VALUE_BETS_DEDUPED = (
    "(SELECT * FROM (SELECT *, ROW_NUMBER() OVER ("
    "PARTITION BY game_id, bookmaker, market_type, selection, IFNULL(line, -999.0) "
    "ORDER BY flagged_at DESC, id DESC) AS _dedup_rn "
    f"FROM value_bets WHERE excluded_reason IS NULL AND tier != '{pipeline_tiers.SHADOW_TIER}') "
    "WHERE _dedup_rn = 1)"
)


def _sport_clause(sport: str | None, *, alias: str = "") -> tuple[str, list[str]]:
    """Return (sql_fragment, params) for an optional sport filter.

    When sport is None or "all", returns ('', []) so queries are unchanged.
    Otherwise returns (' AND <alias>.sport = ?', [sport]) to filter by sport.
    The `alias` qualifies the column in JOIN queries (e.g. 'vb', 'lec').
    """
    if not sport or sport == "all":
        return "", []
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}sport = ?", [sport]


# Bookmakers the dashboard surfaces: the sharp reference (Pinnacle) + the single
# soft book (Tipsport). Snapshot-driven aggregations filter to these so leftover
# rows from removed scrapers (Betano/Allwyn/Fortuna) can never resurface in the
# per-book scoreboard or the activity feed even if old rows linger in the DB.
_CURRENT_BOOKMAKERS: tuple[str, ...] = ("pinnacle", "tipsport")

# Soft books only — what the /analysis per-book scoreboard shows. The sharp
# reference (Pinnacle) never carries a value bet, so its row there was pure
# dead weight; its coverage lives on /live's coverage matrix.
_SOFT_BOOKMAKERS: tuple[str, ...] = ("tipsport",)

# Stored timestamps are UTC; the dashboard audience is CET/CEST, so human-facing
# tip-off times render in Prague local time. Mirrors server.py's _DISPLAY_TZ.
_DISPLAY_TZ = ZoneInfo("Europe/Prague")
_UPCOMING_HORIZON_HOURS = 24


def _book_clause() -> tuple[str, list[str]]:
    """Return (sql_fragment, params) restricting a query to current bookmakers."""
    placeholders = ", ".join("?" for _ in _CURRENT_BOOKMAKERS)
    return f" AND bookmaker IN ({placeholders})", list(_CURRENT_BOOKMAKERS)


async def status_strip(
    store: Store, today_iso: str | None = None, sport: str | None = None
) -> dict[str, Any]:
    """Aggregates for the ``/live`` status strip card.

    Args:
        today_iso: optional ``YYYY-MM-DD`` window for "today"; defaults to
            the current **UTC** date. SQLite evaluates ``date(flagged_at)``
            in UTC, so the default must be UTC too — using the operator's
            local date would misbucket flags near midnight in non-UTC
            timezones (e.g. CET). Injectable so tests avoid wall-clock
            flakiness.

    Returns:
        ``games_total``, ``games_scheduled``, ``games_live``,
        ``games_final``: counts from the ``games`` table.
        ``flags_today``: count of CLEAN, bettable-tier ``value_bets`` rows
        whose ``flagged_at`` date is today (UTC) — guard-stamped rows land
        in ``suspect_today`` and far-tier SHADOW rows in ``shadow_today``,
        so every one of today's detections shows in exactly one tile.
        ``avg_edge_today``: mean ``edge_pct`` over the ``flags_today`` rows;
        ``0.0`` when there are no clean flags today.
        ``budget_used`` / ``budget_remaining``: int values persisted in
        ``meta`` by the scheduler (keys ``odds_api_budget_used`` and
        ``odds_api_budget_remaining``); ``None`` when not yet persisted.
    """
    sclause, sparams = _sport_clause(sport)
    cursor = await store._db.execute(
        "SELECT status, COUNT(*) AS n FROM games WHERE 1=1"
        + sclause
        + " GROUP BY status",
        sparams,
    )
    by_status: dict[str, int] = {
        row["status"]: row["n"] for row in await cursor.fetchall()
    }

    if today_iso is None:
        today_iso = datetime.now(timezone.utc).date().isoformat()
    # Every one of today's rows is counted, split three ways: clean (bettable
    # tier, no guard stamp), guarded, and far-tier SHADOW (never bettable). A
    # shadow row is SHADOW even when it also carries a guard stamp, so it lands
    # in exactly one tile. Filtering shadow rows out entirely printed a big `0`
    # on a shadow-only day — the "detector looks dead" regression the 2026-08-14
    # overhaul fixed for guarded rows (2026-09-06).
    shadow = pipeline_tiers.SHADOW_TIER
    cursor = await store._db.execute(
        "SELECT "
        f"SUM(CASE WHEN excluded_reason IS NULL AND tier != '{shadow}' THEN 1 ELSE 0 END) AS flags, "
        f"SUM(CASE WHEN excluded_reason IS NOT NULL AND tier != '{shadow}' THEN 1 ELSE 0 END) AS suspect, "
        f"SUM(CASE WHEN tier = '{shadow}' THEN 1 ELSE 0 END) AS shadow, "
        "COALESCE(AVG(CASE WHEN excluded_reason IS NULL "
        f"AND tier != '{shadow}' THEN edge_pct END), 0) AS avg_edge "
        "FROM value_bets WHERE date(flagged_at) = ?" + sclause,
        [today_iso, *sparams],
    )
    row = await cursor.fetchone()
    flags_today = int(row["flags"] or 0)
    suspect_today = int(row["suspect"] or 0)
    shadow_today = int(row["shadow"] or 0)
    avg_edge_today = round(float(row["avg_edge"] or 0.0), 2)

    used = await store.get_meta("odds_api_budget_used")
    remaining = await store.get_meta("odds_api_budget_remaining")

    return {
        "games_total": sum(by_status.values()),
        "games_scheduled": by_status.get("scheduled", 0),
        "games_live": by_status.get("live", 0),
        "games_final": by_status.get("final", 0),
        "flags_today": flags_today,
        # Guard-stamped detections today (excluded_reason set at insert by the
        # pregame guards). The detector IS working when these are nonzero even
        # with 0 clean flags — hiding them made it look dead.
        "suspect_today": suspect_today,
        # Far-tier SHADOW detections today (recorded + CLV'd, never bettable).
        # Same reasoning as suspect_today: visible, but never counted as clean.
        "shadow_today": shadow_today,
        "avg_edge_today": avg_edge_today,
        "budget_used": int(used) if used is not None else None,
        "budget_remaining": int(remaining) if remaining is not None else None,
    }


# Dashboard freshness windows per pipeline tier (seconds since last matched
# snapshot before a coverage cell is flagged). Keyed by pipeline_tiers.tier_for(...).
_STALE_BY_TIER: dict[str, int] = {
    "hot": 5 * 60,  # hot (<60m to tipoff): warn after 5 min
    "warm": 15 * 60,  # warm (1-2h): warn after 15 min
}


async def active_game_tiers(store: Store, sport: str | None = None) -> set[str]:
    """Return the polling tiers currently relevant for active games.

    Uses pipeline tier boundaries (far/warm/hot) from
    ``oddsiq.pipeline.tiers.tier_for``:
    - far (>2h): excluded — lines aren't sharp yet.
    - warm (1-2h) and hot (<60m): included as-is.
    - post-tipoff: never included (the live tier was removed 2026-08-27;
      in-progress games are unpollable).
    """
    sclause, sparams = _sport_clause(sport)
    now = datetime.now(timezone.utc)
    cur = await store._db.execute(
        "SELECT scheduled_at, commence_time, status, sport FROM games "
        "WHERE status IN ('scheduled', 'live')" + sclause,
        sparams,
    )
    result: set[str] = set()
    for g in await cur.fetchall():
        tipoff = datetime.fromisoformat(g["commence_time"] or g["scheduled_at"])
        ttip_s = (tipoff - now).total_seconds()
        tier = pipeline_tiers.tier_for(ttip_s)
        if tier in ("hot", "warm"):
            result.add(tier)
    return result


async def coverage_matrix(
    store: Store, *, scrapers: list[str], sport: str | None = None
) -> list[dict[str, Any]]:
    """Per-(game, scraper) coverage rows for hot/warm active (PREGAME) games.

    Rows are emitted for the cartesian product of (active game) × (scraper).
    Far-tier games (>2h) and post-tipoff games are excluded — the live tier
    was removed 2026-08-27, so no scraper is expected to produce snapshots
    after tipoff (in-progress games would otherwise show permanent false
    "missing"). Live games surface via the status-strip count, the activity
    feed, and /game pages instead.

    Each row has:
        ``game_id``, ``home``, ``away``, ``tipoff`` (ISO),
        ``tier`` (``hot``/``warm``),
        ``status`` (``scheduled``/``live``),
        ``bookmaker``,
        ``state`` (``fresh`` / ``stale`` / ``missing``),
        ``age_s`` (seconds since last matched snapshot, or ``None`` for ``missing``).
    """
    sclause, sparams = _sport_clause(sport)
    now = datetime.now(timezone.utc)
    cur = await store._db.execute(
        "SELECT id, external_id, home_team, away_team, scheduled_at, "
        "commence_time, status, sport FROM games "
        "WHERE status IN ('scheduled', 'live')" + sclause + " "
        "ORDER BY COALESCE(commence_time, scheduled_at)",
        sparams,
    )
    games = await cur.fetchall()
    # Pre-fetch the latest captured_at per (game, scraper) in ONE grouped query,
    # instead of a SELECT MAX(...) per (game × scraper) inside the loop (an N+1
    # that ran on every 30s coverage-matrix refresh).
    last_ts_by_key: dict[tuple[int, str], str] = {}
    game_ids = [g["id"] for g in games]
    if game_ids and scrapers:
        gph = ",".join("?" * len(game_ids))
        bph = ",".join("?" * len(scrapers))
        cur_ts = await store._db.execute(
            f"SELECT game_id, bookmaker, MAX(captured_at) AS last_ts FROM odds_snapshots "
            f"WHERE game_id IN ({gph}) AND bookmaker IN ({bph}) "
            f"GROUP BY game_id, bookmaker",
            [*game_ids, *scrapers],
        )
        for r in await cur_ts.fetchall():
            last_ts_by_key[(r["game_id"], r["bookmaker"])] = r["last_ts"]
    rows: list[dict[str, Any]] = []
    for g in games:
        tipoff = datetime.fromisoformat(g["commence_time"] or g["scheduled_at"])
        ttip_s = (tipoff - now).total_seconds()
        tier = pipeline_tiers.tier_for(ttip_s)
        if tier not in ("hot", "warm"):
            continue  # far (not sharp yet) or post-tipoff (unpollable)
        stale_threshold = _STALE_BY_TIER[tier]
        row_scrapers = scrapers
        for scraper in row_scrapers:
            last_ts = last_ts_by_key.get((g["id"], scraper))
            if last_ts is None:
                state = "missing"
                age_s = None
            else:
                age_s = (now - datetime.fromisoformat(last_ts)).total_seconds()
                state = "fresh" if age_s <= stale_threshold else "stale"
            rows.append(
                {
                    "game_id": g["id"],
                    "home": g["home_team"],
                    "away": g["away_team"],
                    "tipoff": tipoff.isoformat(),
                    "tier": tier,
                    "status": g["status"],
                    "bookmaker": scraper,
                    "state": state,
                    "age_s": age_s,
                }
            )
    return rows


async def recent_db_events(
    store: Store, *, limit: int = 25, sport: str | None = None
) -> list[dict[str, Any]]:
    """Return recent activity events derivable from the DB schema only.

    Two event types qualify because they have real timestamps on the
    schema:

    - ``value_bet`` from ``value_bets.flagged_at``
    - ``snapshot`` from the latest ``captured_at`` per
      ``(bookmaker, market_type, selection)`` group in ``odds_snapshots``

    Other lifecycle events (``clv``, ``result``, ``game_live``,
    ``game_final``) have no schema timestamp — they come from
    :func:`oddsiq.dashboard.log_tail.parse_event_lines` instead and
    are NOT included here.

    Result is sorted descending by timestamp, limited to ``limit``.
    """
    sclause, sparams = _sport_clause(sport, alias="vb")
    cur = await store._db.execute(
        "SELECT 'value_bet' AS type, vb.flagged_at AS ts, vb.bookmaker, "
        "vb.market_type AS market, vb.selection, vb.edge_pct, vb.tier, "
        "g.home_team, g.away_team "
        "FROM value_bets vb JOIN games g ON vb.game_id = g.id "
        "WHERE vb.excluded_reason IS NULL"
        + sclause
        + " ORDER BY vb.flagged_at DESC LIMIT ?",
        [*sparams, limit],
    )
    vbs = [dict(r) for r in await cur.fetchall()]
    sclause, sparams = _sport_clause(sport)
    bclause, bparams = _book_clause()
    cur = await store._db.execute(
        "SELECT 'snapshot' AS type, MAX(captured_at) AS ts, "
        "bookmaker, market_type AS market, selection "
        "FROM odds_snapshots WHERE 1=1" + sclause + bclause + " "
        "GROUP BY bookmaker, market_type, selection "
        "ORDER BY ts DESC LIMIT ?",
        [*sparams, *bparams, limit],
    )
    snaps = [dict(r) for r in await cur.fetchall()]
    # Annotate snapshots with None edge_pct so the dict shapes match
    for s in snaps:
        s["edge_pct"] = None
    merged = vbs + snaps
    merged.sort(key=lambda e: e["ts"], reverse=True)
    return merged[:limit]


async def big_numbers(store: Store, sport: str | None = None) -> dict[str, Any]:
    """Aggregates for the ``/analysis`` big-numbers tiles.

    Sample-size labels are load-bearing: ``avg_edge_on_clv_subset`` is
    averaged ONLY over rows where ``clv_pct IS NOT NULL`` so the
    "edge ≈ CLV" comparison is mathematically valid (same subset).
    """
    sclause, sparams = _sport_clause(sport)
    cur = await store._db.execute(
        "SELECT COUNT(*) AS n_total, "
        "SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) AS n_resolved, "
        "SUM(CASE WHEN clv_pct IS NOT NULL THEN 1 ELSE 0 END) AS n_clv, "
        "SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) AS n_win, "
        "SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS n_loss, "
        "SUM(CASE WHEN result = 'draw' THEN 1 ELSE 0 END) AS n_draw, "
        "SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END) AS n_push, "
        "AVG(CASE WHEN clv_pct IS NOT NULL THEN clv_pct END) AS avg_clv, "
        "AVG(CASE WHEN clv_pct IS NOT NULL THEN edge_pct END) AS avg_edge_subset, "
        "AVG(CASE WHEN result IS NOT NULL THEN entry_odds END) AS avg_odds, "
        "SUM(CASE WHEN result = 'win' THEN entry_odds - 1.0 "
        "         WHEN result = 'loss' THEN -1.0 ELSE 0 END) AS flat_pl "
        f"FROM {VALUE_BETS_DEDUPED} WHERE 1=1" + sclause,
        sparams,
    )
    r = await cur.fetchone()
    n_total = int(r["n_total"] or 0)
    n_resolved = int(r["n_resolved"] or 0)
    n_clv = int(r["n_clv"] or 0)
    n_win = int(r["n_win"] or 0)
    n_loss = int(r["n_loss"] or 0)
    n_draw = int(r["n_draw"] or 0)
    n_push = int(r["n_push"] or 0)
    avg_clv = round(r["avg_clv"], 2) if r["avg_clv"] is not None else None
    avg_edge_subset = (
        round(r["avg_edge_subset"], 2) if r["avg_edge_subset"] is not None else None
    )
    delta = (
        round(avg_edge_subset - avg_clv, 2)
        if avg_clv is not None and avg_edge_subset is not None
        else None
    )
    # Win rate is over DECIDED bets only (win+loss). A draw/push returns the
    # stake, so counting it in the denominator would understate win-rate like a
    # loss. (NBA/NHL/MLB ties are vanishingly rare, but the math must be right.)
    n_decided = n_win + n_loss
    win_rate = round(100.0 * n_win / n_decided, 1) if n_decided else None
    # Flat-stake ROI over resolved bets (win → entry_odds−1, loss → −1, push/draw → 0)
    # — surfaced as the headline metric (mirrors audit.bookmakers.flat_stake_pl).
    flat_pl_raw = float(r["flat_pl"] or 0.0)
    flat_roi_pct = round(100.0 * flat_pl_raw / n_resolved, 2) if n_resolved else 0.0
    return {
        "bets_total": n_total,
        "bets_resolved": n_resolved,
        "bets_with_clv": n_clv,
        "n_win": n_win,
        "n_loss": n_loss,
        "n_draw": n_draw,
        "n_push": n_push,
        "win_rate": win_rate,
        "avg_clv": avg_clv,
        "avg_edge_on_clv_subset": avg_edge_subset,
        "delta": delta,
        "edge_approx_clv": (abs(delta) < 1.0) if delta is not None else None,
        # Avg entry odds over RESOLVED bets only (folded in from the removed
        # results-detail section, 2026-08-08).
        "avg_odds": round(r["avg_odds"], 2) if r["avg_odds"] is not None else None,
        "flat_pl": round(flat_pl_raw, 2),
        "flat_roi_pct": flat_roi_pct,
    }


async def monthly_performance(
    store: Store,
    *,
    month: str | None = None,
    sport: str | None = None,
) -> dict[str, Any]:
    """Per-calendar-month performance rollup for the ``/analysis`` monthly tabs.

    Months are UTC calendar months of ``flagged_at``. ``months`` lists every
    calendar month from the first value bet through the current month (empty
    months included), newest first. ``month`` is the selected
    month: the ``month`` argument when it is one of ``months``, the literal
    ``"all"`` for all-time aggregates (returned ``month`` is then ``None``),
    else the newest (``None`` only when no data).

    ``bets`` aggregates ``VALUE_BETS_DEDUPED`` (tier-collapsed, excluded rows
    dropped). Flat-stake convention throughout: win → entry_odds−1,
    loss → −1, draw/push → 0. (The live-scout column was removed with the
    live-edge subsystem, 2026-08-27.)
    """
    sclause, sparams = _sport_clause(sport)
    # Calendar-true month tabs: every month from the first value bet (clean OR
    # guarded) through the CURRENT month, newest first — a month with zero
    # clean bets must still exist as a tab showing zeros (2026-09-06: September
    # vanished from /analysis entirely because only months with deduped clean
    # rows were listed, which read as "the app stopped" rather than "no edges").
    cur = await store._db.execute(
        f"SELECT MIN(substr(flagged_at, 1, 7)) AS m FROM value_bets WHERE 1=1{sclause}",
        sparams,
    )
    row = await cur.fetchone()
    months = _month_range(row["m"] if row else None, datetime.now(timezone.utc))
    # month="all" -> all-time aggregates (selected None but data still runs);
    # an unknown/omitted month falls back to the newest data month.
    all_time = month == "all"
    selected = (
        None
        if all_time
        else (month if month in months else (months[0] if months else None))
    )
    bets: dict[str, Any] = {
        "n_total": 0,
        "n_resolved": 0,
        "n_win": 0,
        "n_loss": 0,
        "n_push": 0,
        "win_rate": None,
        "avg_edge": None,
        "avg_clv": None,
        "n_clv": 0,
        "flat_pl": 0.0,
        "flat_roi_pct": 0.0,
    }
    if selected is None and not all_time:
        return {"months": months, "month": None, "bets": bets}
    mclause_vb = "" if all_time else " AND substr(flagged_at, 1, 7) = ?"
    mparams: list[Any] = [] if all_time else [selected]

    cur = await store._db.execute(
        "SELECT COUNT(*) AS n_total, "
        "SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) AS n_resolved, "
        "SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) AS n_win, "
        "SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS n_loss, "
        "SUM(CASE WHEN clv_pct IS NOT NULL THEN 1 ELSE 0 END) AS n_clv, "
        "AVG(edge_pct) AS avg_edge, "
        "AVG(CASE WHEN clv_pct IS NOT NULL THEN clv_pct END) AS avg_clv, "
        "SUM(CASE WHEN result = 'win' THEN entry_odds - 1.0 "
        "         WHEN result = 'loss' THEN -1.0 ELSE 0 END) AS flat_pl "
        f"FROM {VALUE_BETS_DEDUPED} "
        f"WHERE 1=1{mclause_vb}{sclause}",
        [*mparams, *sparams],
    )
    r = await cur.fetchone()
    n_resolved = int(r["n_resolved"] or 0)
    n_win = int(r["n_win"] or 0)
    n_loss = int(r["n_loss"] or 0)
    n_decided = n_win + n_loss
    flat_pl = float(r["flat_pl"] or 0.0)
    bets.update(
        {
            "n_total": int(r["n_total"] or 0),
            "n_resolved": n_resolved,
            "n_win": n_win,
            "n_loss": n_loss,
            "n_push": n_resolved - n_win - n_loss,
            "win_rate": round(100.0 * n_win / n_decided, 1) if n_decided else None,
            "avg_edge": round(r["avg_edge"], 2) if r["avg_edge"] is not None else None,
            "avg_clv": round(r["avg_clv"], 2) if r["avg_clv"] is not None else None,
            "n_clv": int(r["n_clv"] or 0),
            "flat_pl": round(flat_pl, 2),
            "flat_roi_pct": round(100.0 * flat_pl / n_resolved, 2)
            if n_resolved
            else 0.0,
        }
    )

    return {"months": months, "month": selected, "bets": bets}


async def clv_trend(
    store: Store, *, days: int | None = None, sport: str | None = None
) -> dict[str, Any]:
    """Daily CLV averages (nightly raw + 7-day trailing rolling) for the
    ``/analysis`` hero chart.

    Args:
        store: Store handle.
        days: Window in days from today. ``None`` = all time.

    Returns dict with keys ``labels`` (ISO date strings), ``nightly``
    (per-day mean of ``clv_pct`` over rows where ``clv_pct IS NOT NULL``),
    ``rolling7`` (trailing 7-day mean over ``nightly``). All three lists
    are the same length, ordered ascending by date (oldest first).
    """
    sclause, sparams = _sport_clause(sport)
    where_extra = ""
    days_params: list[Any] = []
    if days is not None and days > 0:
        # days <= 0 is treated as all-time (no window) rather than building a
        # broken date('now', '-0 days')/'-negative days') modifier that empties
        # the chart.
        where_extra = " AND date(flagged_at) >= date('now', ?) "
        days_params.append(f"-{days} days")
    cur = await store._db.execute(
        f"SELECT date(flagged_at) AS d, AVG(clv_pct) AS avg_clv "
        f"FROM {VALUE_BETS_DEDUPED} WHERE clv_pct IS NOT NULL{sclause}{where_extra} "
        f"GROUP BY date(flagged_at) ORDER BY date(flagged_at)",
        [*sparams, *days_params],
    )
    rows = await cur.fetchall()
    data_labels = [r["d"] for r in rows]
    data_nightly = [round(r["avg_clv"], 2) for r in rows]
    # True trailing 7-CALENDAR-day mean (not last-7-data-points): on days with
    # no resolved bets there's no data, so a fixed point window would silently
    # span >7 days. Window each day over the actual dates present.
    label_dates = [datetime.fromisoformat(d).date() for d in data_labels]
    data_rolling: list[float | None] = []
    for i in range(len(data_nightly)):
        start = label_dates[i] - timedelta(days=6)
        window = [data_nightly[j] for j in range(i + 1) if label_dates[j] >= start]
        data_rolling.append(round(sum(window) / len(window), 2) if window else None)
    if not data_labels:
        return {"labels": [], "nightly": [], "rolling7": []}
    # Expand to the FULL calendar range with nulls on no-data days, so betting
    # gaps (e.g. the 2026-06-20..07-06 outage) render as visible holes instead
    # of a silently compressed x-axis. The chart draws nightly with
    # spanGaps=false (honest breaks) and the rolling line bridging.
    by_day = dict(zip(label_dates, zip(data_nightly, data_rolling)))
    labels: list[str] = []
    nightly: list[float | None] = []
    rolling7: list[float | None] = []
    day = label_dates[0]
    while day <= label_dates[-1]:
        labels.append(day.isoformat())
        n, r7 = by_day.get(day, (None, None))
        nightly.append(n)
        rolling7.append(r7)
        day += timedelta(days=1)
    return {"labels": labels, "nightly": nightly, "rolling7": rolling7}


async def profit_curve(store: Store, sport: str | None = None) -> dict[str, Any]:
    """Cumulative flat-1u P&L by flag date — the evaluation-mode equity curve.

    Returns ``labels`` (every calendar day from first to last resolved bet)
    and ``cumulative`` (running units; ``None`` on days with no resolved
    bets so the chart can bridge gaps without inventing data points).
    Flat-stake convention: win → entry_odds−1, loss → −1, draw/push → 0.
    """
    sclause, sparams = _sport_clause(sport)
    cur = await store._db.execute(
        f"SELECT date(flagged_at) AS d, "
        f"SUM(CASE WHEN result = 'win' THEN entry_odds - 1.0 "
        f"         WHEN result = 'loss' THEN -1.0 ELSE 0 END) AS pl "
        f"FROM {VALUE_BETS_DEDUPED} WHERE result IS NOT NULL{sclause} "
        f"GROUP BY date(flagged_at) ORDER BY date(flagged_at)",
        sparams,
    )
    rows = await cur.fetchall()
    if not rows:
        return {"labels": [], "cumulative": []}
    by_day: dict = {}
    running = 0.0
    for r in rows:
        running += float(r["pl"] or 0.0)
        by_day[datetime.fromisoformat(r["d"]).date()] = round(running, 2)
    labels: list[str] = []
    cumulative: list[float | None] = []
    day = min(by_day)
    last = max(by_day)
    while day <= last:
        labels.append(day.isoformat())
        cumulative.append(by_day.get(day))
        day += timedelta(days=1)
    return {"labels": labels, "cumulative": cumulative}


async def per_book_scoreboard(
    store: Store, sport: str | None = None
) -> list[dict[str, Any]]:
    """Per-book coverage + value-bet metrics — SOFT books only.

    Restricted to ``_SOFT_BOOKMAKERS`` (Tipsport): the Pinnacle reference row
    was dropped 2026-08-08 — it always showed 0 bets / — edge / — CLV, and its
    coverage stats belong to /live's coverage matrix. The allowlist also keeps
    leftover rows from removed scrapers (Betano/Allwyn/Fortuna) from
    resurfacing. A soft book with zero value bets still appears
    (``avg_edge``/``avg_clv`` = ``None``) so the operator can see coverage.

    Note: ``snapshots`` / ``games_matched`` count ALL ``odds_snapshots``
    rows, so they include post-tipoff live-scout snapshots — i.e. these are
    total (pregame + live) coverage, not pregame-only. The value-bet metrics
    (``bets``/``avg_edge``/``avg_clv``) read ``value_bets`` only and are
    unaffected by the live scout.
    """
    sclause, sparams = _sport_clause(sport)
    placeholders = ", ".join("?" for _ in _SOFT_BOOKMAKERS)
    bclause, bparams = f" AND bookmaker IN ({placeholders})", list(_SOFT_BOOKMAKERS)
    cur = await store._db.execute(
        "SELECT bookmaker, COUNT(*) AS snapshots, "
        "COUNT(DISTINCT game_id) AS games_matched, "
        "COUNT(DISTINCT date(captured_at)) AS days_active, "
        "MIN(date(captured_at)) AS first_seen, "
        "MAX(date(captured_at)) AS last_seen "
        "FROM odds_snapshots WHERE 1=1" + sclause + bclause + " GROUP BY bookmaker",
        [*sparams, *bparams],
    )
    coverage = {r["bookmaker"]: dict(r) for r in await cur.fetchall()}
    cur = await store._db.execute(
        "SELECT bookmaker, COUNT(*) AS bets, "
        "AVG(CASE WHEN clv_pct IS NOT NULL THEN edge_pct END) AS avg_edge, "
        "AVG(CASE WHEN clv_pct IS NOT NULL THEN clv_pct END) AS avg_clv "
        f"FROM {VALUE_BETS_DEDUPED} WHERE 1=1" + sclause + " GROUP BY bookmaker",
        sparams,
    )
    bets_by_book = {r["bookmaker"]: dict(r) for r in await cur.fetchall()}
    rows: list[dict[str, Any]] = []
    for book in sorted(coverage):
        c = coverage[book]
        b = bets_by_book.get(book, {})
        rows.append(
            {
                "bookmaker": book,
                "snapshots": c["snapshots"],
                "games_matched": c["games_matched"],
                "days_active": c["days_active"],
                "first_seen": c["first_seen"],
                "last_seen": c["last_seen"],
                "bets": int(b.get("bets") or 0),
                "avg_edge": round(b["avg_edge"], 2)
                if b.get("avg_edge") is not None
                else None,
                "avg_clv": round(b["avg_clv"], 2)
                if b.get("avg_clv") is not None
                else None,
            }
        )
    return rows


async def edge_distribution(
    store: Store, sport: str | None = None
) -> list[dict[str, Any]]:
    """Bucketed counts of ``edge_pct`` for the histogram.

    Bucket boundaries (half-open intervals):
      ``3-5%``  → ``3.0 <= edge_pct < 5.0``
      ``5-7%``  → ``5.0 <= edge_pct < 7.0``
      ``7-10%`` → ``7.0 <= edge_pct < 10.0``
      ``10%+``  → ``edge_pct >= 10.0``

    Always returns four buckets in order (zeros included), so the chart
    renders consistent x-axis labels even on an empty DB.
    """
    sclause, sparams = _sport_clause(sport)
    cur = await store._db.execute(
        "SELECT "
        "SUM(CASE WHEN edge_pct >= 3.0 AND edge_pct < 5.0  THEN 1 ELSE 0 END) AS b1, "
        "SUM(CASE WHEN edge_pct >= 5.0 AND edge_pct < 7.0  THEN 1 ELSE 0 END) AS b2, "
        "SUM(CASE WHEN edge_pct >= 7.0 AND edge_pct < 10.0 THEN 1 ELSE 0 END) AS b3, "
        "SUM(CASE WHEN edge_pct >= 10.0                    THEN 1 ELSE 0 END) AS b4 "
        f"FROM {VALUE_BETS_DEDUPED} WHERE edge_pct >= 3.0" + sclause,
        sparams,
    )
    r = await cur.fetchone()
    return [
        {"bucket": "3-5%", "count": int(r["b1"] or 0)},
        {"bucket": "5-7%", "count": int(r["b2"] or 0)},
        {"bucket": "7-10%", "count": int(r["b3"] or 0)},
        {"bucket": "10%+", "count": int(r["b4"] or 0)},
    ]


_THIN_N: int = 10


async def edge_calibration(
    store: Store, sport: str | None = None
) -> list[dict[str, Any]]:
    """Edge-bucket profitability: break-even win% vs actual win%, with a
    ``thin`` flag for buckets with fewer than ``_THIN_N`` resolved bets.

    ``expected_win`` is ``AVG(100 / entry_odds)`` — the break-even win rate
    at the price flagged, NOT the model's fair-odds probability. Actual win%
    above this means the bucket was profitable. (The column is surfaced as
    "Break-even win%" in the template to avoid implying model calibration.)
    """
    sclause, sparams = _sport_clause(sport)
    cur = await store._db.execute(
        "SELECT "
        "CASE "
        "  WHEN edge_pct >= 3.0 AND edge_pct < 5.0  THEN '3-5%' "
        "  WHEN edge_pct >= 5.0 AND edge_pct < 7.0  THEN '5-7%' "
        "  WHEN edge_pct >= 7.0 AND edge_pct < 10.0 THEN '7-10%' "
        "  WHEN edge_pct >= 10.0                    THEN '10%+' "
        "END AS bucket, "
        "COUNT(*) AS n, "
        "AVG(100.0 / entry_odds) AS expected_win, "
        "SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) * 100.0 / "
        "  NULLIF(SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END), 0) AS actual_win, "
        "MIN(edge_pct) AS min_edge "
        f"FROM {VALUE_BETS_DEDUPED} WHERE edge_pct >= 3.0 AND result IS NOT NULL"
        + sclause
        + " GROUP BY bucket ORDER BY min_edge",
        sparams,
    )
    rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        n = int(r["n"] or 0)
        expected = (
            round(r["expected_win"], 1) if r["expected_win"] is not None else None
        )
        actual = round(r["actual_win"], 1) if r["actual_win"] is not None else None
        delta = (
            round(actual - expected, 1)
            if expected is not None and actual is not None
            else None
        )
        out.append(
            {
                "bucket": r["bucket"],
                "n": n,
                "expected_win": expected,
                "actual_win": actual,
                "delta": delta,
                "thin": n < _THIN_N,
            }
        )
    return out


async def recent_value_bets(
    store: Store,
    *,
    limit: int = 10,
    sport: str | None = None,
    include_suspect: bool = False,
) -> list[dict[str, Any]]:
    """Return the last ``limit`` value bets (descending ``flagged_at``)
    joined to their game. Suspect (guard-stamped) rows are hidden by default;
    ``include_suspect`` adds them (with ``excluded_reason`` set) so the
    detections list can show WHY nothing has been bettable — without the
    toggle the list just ends at the last clean flag and looks dead.
    """
    sclause, sparams = _sport_clause(sport, alias="vb")
    suspect_clause = "" if include_suspect else " AND vb.excluded_reason IS NULL"
    cur = await store._db.execute(
        "SELECT vb.flagged_at, vb.bookmaker, vb.market_type, vb.selection, "
        "vb.line, vb.edge_pct, vb.entry_odds, vb.tier, vb.clv_pct, vb.result, "
        "vb.sport, vb.tipsport_url, vb.game_id, vb.excluded_reason, "
        "g.home_team, g.away_team "
        "FROM value_bets vb JOIN games g ON vb.game_id = g.id "
        "WHERE 1=1" + suspect_clause + sclause + " ORDER BY vb.flagged_at DESC LIMIT ?",
        [*sparams, limit],
    )
    return [dict(r) for r in await cur.fetchall()]


def _selection_team(selection: str, home_team: str, away_team: str) -> str:
    """Map a moneyline 'home'/'away' to the actual team name (mirrors the
    server-side helper); any other selection passes through unchanged."""
    if selection == "home":
        return home_team
    if selection == "away":
        return away_team
    return selection


# Runtime betting knobs live in the meta table (operator-set via `oddsiq
# betting` / the /betting dashboard controls). Defaults mirror BettingService.
_BETTING_META_DEFAULTS = {
    "betting_flat_stake_czk": 20.0,
    "betting_max_bets_per_day": 10,
    "betting_max_open_stakes_czk": 500.0,
    "betting_daily_loss_limit_czk": 300.0,
}


async def betting_overview(store: Store, sport: str | None = None) -> dict[str, Any]:
    """Money KPIs + engine config for the /betting tab.

    KPIs come from ``placed_bets``: at-risk (open stakes = pending/placed/
    unknown), total staked, realized P&L + ROI (settled), the latest known
    balance, and status counts. Config comes from the meta table. Engine
    liveness (off/disconnected/disarmed/armed) is read from the heartbeat log
    by the caller, not here (separate process)."""
    sclause, sparams = _sport_clause(sport)
    cur = await store._db.execute(
        "SELECT "
        "SUM(CASE WHEN status IN ('pending','placed','unknown') THEN stake_czk ELSE 0 END) AS at_risk, "
        "SUM(CASE WHEN status IN ('placed','unknown','settled') THEN stake_czk ELSE 0 END) AS staked, "
        "SUM(CASE WHEN status = 'settled' THEN settled_pl_czk ELSE 0 END) AS realized, "
        "SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END) AS n_settled, "
        "SUM(CASE WHEN status = 'placed' THEN 1 ELSE 0 END) AS n_open, "
        "COUNT(*) AS n_total "
        f"FROM placed_bets WHERE 1=1{sclause}",
        sparams,
    )
    r = await cur.fetchone()
    staked = float(r["staked"] or 0.0)
    realized = float(r["realized"] or 0.0)
    n_settled = int(r["n_settled"] or 0)
    # Latest known account balance = the most recent non-null balance_after_czk.
    cur = await store._db.execute(
        "SELECT balance_after_czk FROM placed_bets "
        f"WHERE balance_after_czk IS NOT NULL{sclause} "
        "ORDER BY resolved_at DESC, id DESC LIMIT 1",
        sparams,
    )
    brow = await cur.fetchone()

    async def _meta_num(key, default, maximum, minimum=0):
        raw = await store.get_meta(key)
        if raw is None:
            value = default
        else:
            try:
                value = type(default)(float(raw))
            except (ValueError, TypeError):
                value = default
        if not math.isfinite(value):
            value = default
        return max(minimum, min(value, maximum))

    # Today's caps usage + decision funnel (UTC day, matching BettingService's
    # cap accounting). The funnel answers "why hasn't it bet today" at a
    # glance: detections → guarded (pregame guards) → skipped (qualification)
    # → placed.
    today_iso = datetime.now(timezone.utc).date().isoformat()
    day = await store.betting_day_stats(today_iso)
    # Far-tier SHADOW rows are neither clean nor guarded — labelling them
    # "clean" in the funnel would promise a bet the engine can never place.
    shadow = pipeline_tiers.SHADOW_TIER
    cur = await store._db.execute(
        "SELECT "
        f"SUM(CASE WHEN excluded_reason IS NULL AND tier != '{shadow}' THEN 1 ELSE 0 END) AS clean, "
        f"SUM(CASE WHEN tier = '{shadow}' THEN 1 ELSE 0 END) AS shadow, "
        f"SUM(CASE WHEN excluded_reason IS NOT NULL AND tier != '{shadow}' THEN 1 ELSE 0 END) AS guarded "
        f"FROM value_bets WHERE date(flagged_at) = ?{sclause}",
        [today_iso, *sparams],
    )
    vt = await cur.fetchone()
    cur = await store._db.execute(
        "SELECT "
        "SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped, "
        "SUM(CASE WHEN status IN ('rejected','failed','unknown') THEN 1 ELSE 0 END) AS rejected, "
        "SUM(CASE WHEN status IN ('placed','settled','pending') THEN 1 ELSE 0 END) AS placed "
        f"FROM placed_bets WHERE date(created_at) = ?{sclause}",
        [today_iso, *sparams],
    )
    pt = await cur.fetchone()

    return {
        "betting": None,  # engine liveness filled by the route from the heartbeat
        "armed": (await store.get_meta("betting_armed")) == "1",
        "disarm_reason": await store.get_meta("betting_disarm_reason"),
        "balance_czk": brow["balance_after_czk"] if brow else None,
        "at_risk_czk": round(float(r["at_risk"] or 0.0), 2),
        "total_staked_czk": round(staked, 2),
        "realized_pl_czk": round(realized, 2),
        "roi_pct": round(100.0 * realized / staked, 2)
        if staked > 0 and n_settled
        else None,
        "n_open": int(r["n_open"] or 0),
        "n_settled": n_settled,
        "n_total": int(r["n_total"] or 0),
        "day": day,
        "today": {
            "detections": int(vt["clean"] or 0),
            "shadow": int(vt["shadow"] or 0),
            "guarded": int(vt["guarded"] or 0),
            "skipped": int(pt["skipped"] or 0),
            "rejected": int(pt["rejected"] or 0),
            "placed": int(pt["placed"] or 0),
        },
        "betting_sports": sorted(
            parse_betting_sports(await store.get_meta("betting_sports"))
        ),
        "flat_stake_czk": await _meta_num(
            "betting_flat_stake_czk",
            _BETTING_META_DEFAULTS["betting_flat_stake_czk"],
            config.BETTING_MAX_STAKE_CZK,
            1.0,
        ),
        "max_bets_per_day": await _meta_num(
            "betting_max_bets_per_day",
            _BETTING_META_DEFAULTS["betting_max_bets_per_day"],
            config.BETTING_MAX_BETS_PER_DAY,
        ),
        "max_open_stakes_czk": await _meta_num(
            "betting_max_open_stakes_czk",
            _BETTING_META_DEFAULTS["betting_max_open_stakes_czk"],
            config.BETTING_MAX_OPEN_STAKES_CZK,
        ),
        "daily_loss_limit_czk": await _meta_num(
            "betting_daily_loss_limit_czk",
            _BETTING_META_DEFAULTS["betting_daily_loss_limit_czk"],
            config.BETTING_MAX_DAILY_LOSS_CZK,
        ),
    }


async def placed_bets_table(
    store: Store, *, limit: int = 25, sport: str | None = None
) -> list[dict[str, Any]]:
    """Recent betting DECISIONS joined to game + value bet, newest first —
    skips included (with their qualification-failure reason): a skip must be
    as visible as a placement, or "why didn't it bet?" has no answer."""
    sclause, sparams = _sport_clause(sport, alias="pb")
    cur = await store._db.execute(
        "SELECT pb.status, pb.skip_reason, pb.error, pb.stake_czk, pb.requested_odds, "
        "pb.accepted_odds, pb.receipt_id, pb.settled_pl_czk, pb.latency_detect_ms, "
        "pb.latency_place_ms, pb.commanded_at, pb.created_at, pb.sport, "
        "vb.selection, vb.edge_pct, vb.tipsport_url, pb.game_id, "
        "g.home_team, g.away_team "
        "FROM placed_bets pb JOIN games g ON pb.game_id = g.id "
        "JOIN value_bets vb ON pb.value_bet_id = vb.id "
        "WHERE 1=1"
        + sclause
        + " ORDER BY COALESCE(pb.commanded_at, pb.created_at) DESC, pb.id DESC LIMIT ?",
        [*sparams, limit],
    )
    rows = []
    for r in await cur.fetchall():
        d = dict(r)
        d["selection_team"] = _selection_team(
            d["selection"], d["home_team"], d["away_team"]
        )
        rows.append(d)
    return rows


def _fmt_local_hm(dt: datetime) -> str:
    """Render a UTC-aware datetime as Prague-local HH:MM (24-hour)."""
    return dt.astimezone(_DISPLAY_TZ).strftime("%H:%M")


def _fmt_countdown(seconds: float) -> str:
    """Human countdown to tipoff: 'in 42m' under an hour, else 'in 6h 22m'."""
    total_min = int(seconds // 60)
    if total_min < 60:
        return f"in {total_min}m"
    return f"in {total_min // 60}h {total_min % 60}m"


def _window_label(tier: str, tipoff: datetime) -> str:
    """Bet-window badge text. warm/hot => edges are live now; far => the Prague
    local time the warm window opens (tipoff - 2h, matching tiers.tier_for)."""
    if tier == "hot":
        return "HOT"
    if tier == "warm":
        return "WARM"
    return f"opens {_fmt_local_hm(tipoff - timedelta(hours=2))}"


async def upcoming_games(
    store: Store,
    *,
    sport: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Schedule view for the dashboard Upcoming tab: scheduled games tipping off
    within the next 24h, shaped for "when should I be active to manually bet".

    Read-only. ``sport`` filters the AGENDA only — the per-sport summary tiles are
    always the full ``ENABLED_SPORTS`` overview. ``now`` is injectable for hermetic
    tests; defaults to ``datetime.now(timezone.utc)``. Times render in Europe/Prague.

    Returns ``{"tiles": [...], "groups": [...], "total": int}``:
      tiles  -- one dict per enabled sport (count incl. zero) + a TOTAL tile:
                {"sport", "display", "count", "next_local" (HH:MM or None)}
      groups -- ordered, non-empty only: open -> today -> tomorrow; each
                {"key", "label", "games": [game, ...]} with games tipoff-ascending.
      game   -- {"sport", "display", "away", "home", "tipoff_local", "countdown",
                 "tier", "window_label"}
    """
    if now is None:
        now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=_UPCOMING_HORIZON_HOURS)

    cur = await store._db.execute(
        "SELECT home_team, away_team, scheduled_at, commence_time, sport "
        "FROM games WHERE status = 'scheduled' "
        "ORDER BY COALESCE(commence_time, scheduled_at)"
    )
    rows = await cur.fetchall()

    enabled = set(ENABLED_SPORTS)
    windowed: list[dict[str, Any]] = []
    for g in rows:
        if g["sport"] not in enabled:
            continue
        tipoff = datetime.fromisoformat(g["commence_time"] or g["scheduled_at"])
        if not (now < tipoff <= horizon):
            continue
        windowed.append(
            {
                "sport": g["sport"],
                "home": g["home_team"],
                "away": g["away_team"],
                "tipoff": tipoff,
            }
        )

    # Tiles: always one per ENABLED sport (incl. zero), regardless of `sport`.
    tiles: list[dict[str, Any]] = []
    for key in ENABLED_SPORTS:
        sport_games = [w for w in windowed if w["sport"] == key]
        tiles.append(
            {
                "sport": key,
                "display": get_profile(key).display_name,
                "count": len(sport_games),
                "next_local": (
                    _fmt_local_hm(min(w["tipoff"] for w in sport_games))
                    if sport_games
                    else None
                ),
            }
        )
    # A TOTAL tile only earns its space with 2+ sports; single-sport it is a
    # duplicate of the one sport tile.
    if len(ENABLED_SPORTS) > 1:
        tiles.append(
            {
                "sport": "total",
                "display": "TOTAL",
                "count": len(windowed),
                "next_local": None,
            }
        )

    # Agenda: respect the `sport` filter, then group + format.
    agenda_src = [w for w in windowed if w["sport"] == sport] if sport else windowed
    buckets: dict[str, list[dict[str, Any]]] = {"open": [], "today": [], "tomorrow": []}
    today_local = now.astimezone(_DISPLAY_TZ).date()
    for w in agenda_src:
        ttip = (w["tipoff"] - now).total_seconds()
        tier = pipeline_tiers.tier_for(ttip)
        game = {
            "sport": w["sport"],
            "display": get_profile(w["sport"]).display_name,
            "away": w["away"],
            "home": w["home"],
            "tipoff_local": _fmt_local_hm(w["tipoff"]),
            "tipoff_iso": w["tipoff"].isoformat(),
            "countdown": _fmt_countdown(ttip),
            "tier": tier,
            "window_label": _window_label(tier, w["tipoff"]),
        }
        if tier in ("warm", "hot"):
            buckets["open"].append(game)
        elif w["tipoff"].astimezone(_DISPLAY_TZ).date() == today_local:
            buckets["today"].append(game)
        else:
            buckets["tomorrow"].append(game)

    labels = {"open": "Bet window open", "today": "Later today", "tomorrow": "Tomorrow"}
    groups = [
        {"key": k, "label": labels[k], "games": buckets[k]}
        for k in ("open", "today", "tomorrow")
        if buckets[k]
    ]
    # When the first bet window opens (Prague HH:MM): now if one is already
    # open, else earliest tipoff − 2h. The "when will the engine wake up" line.
    first_window_opens_local = None
    if buckets["open"]:
        first_window_opens_local = "now"
    elif agenda_src:
        earliest = min(w["tipoff"] for w in agenda_src)
        first_window_opens_local = _fmt_local_hm(earliest - timedelta(hours=2))
    return {
        "tiles": tiles,
        "groups": groups,
        "total": len(windowed),
        "first_window_opens_local": first_window_opens_local,
    }


async def next_game_window(
    store: Store,
    sport: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The NEXT scheduled game's bet window, for empty-state context.

    Returns ``{"away", "home", "sport", "tipoff_local", "opens_local"}`` for
    the earliest future scheduled game (Prague-local HH:MM strings;
    ``opens_local`` = tipoff − 2h, the warm-tier boundary), or ``None`` when
    nothing is scheduled. Powers the coverage-matrix empty state ("no active
    games" alone doesn't say whether that's normal)."""
    if now is None:
        now = datetime.now(timezone.utc)
    sclause, sparams = _sport_clause(sport)
    cur = await store._db.execute(
        "SELECT home_team, away_team, sport, scheduled_at, commence_time "
        "FROM games WHERE status = 'scheduled'" + sclause + " "
        "ORDER BY COALESCE(commence_time, scheduled_at)",
        sparams,
    )
    enabled = set(ENABLED_SPORTS)
    for g in await cur.fetchall():
        if g["sport"] not in enabled:
            continue
        tipoff = datetime.fromisoformat(g["commence_time"] or g["scheduled_at"])
        if tipoff <= now:
            continue
        return {
            "away": g["away_team"],
            "home": g["home_team"],
            "sport": g["sport"],
            "tipoff_local": _fmt_local_hm(tipoff),
            "opens_local": _fmt_local_hm(tipoff - timedelta(hours=2)),
        }
    return None


async def game_detail(store: Store, game_id: int) -> dict[str, Any] | None:
    """Everything the /game drill-down page needs, or ``None`` if unknown.

    ``labels`` is the unified, sorted list of snapshot times (Prague,
    ``DD.MM HH:MM``); ``series[selection][bookmaker]`` is odds aligned to
    those labels (``None`` where that book had no snapshot at that instant —
    the chart bridges with spanGaps). ``flags`` lists every value bet on the
    game, suspect included (marked), so "why did this flag?" reads off one
    page instead of ad-hoc SQL.
    """
    cur = await store._db.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    g = await cur.fetchone()
    if g is None:
        return None
    game = dict(g)
    tipoff_iso = game.get("commence_time") or game.get("scheduled_at")
    tipoff_local = None
    if tipoff_iso:
        tipoff_local = (
            datetime.fromisoformat(tipoff_iso)
            .astimezone(_DISPLAY_TZ)
            .strftime("%d.%m.%Y %H:%M")
        )

    bclause, bparams = _book_clause()
    cur = await store._db.execute(
        "SELECT bookmaker, selection, odds, captured_at, is_closing "
        "FROM odds_snapshots WHERE game_id = ? AND market_type = 'moneyline'"
        + bclause
        + " ORDER BY captured_at",
        [game_id, *bparams],
    )
    snaps = await cur.fetchall()
    instants: list[datetime] = []
    seen: set[str] = set()
    for s in snaps:
        if s["captured_at"] not in seen:
            seen.add(s["captured_at"])
            instants.append(datetime.fromisoformat(s["captured_at"]))
    instants.sort()
    index = {ts: i for i, ts in enumerate(instants)}
    labels = [t.astimezone(_DISPLAY_TZ).strftime("%d.%m %H:%M") for t in instants]
    series: dict[str, dict[str, list]] = {
        "home": {},
        "away": {},
    }
    for s in snaps:
        sel = s["selection"]
        if sel not in series:
            continue
        book_series = series[sel].setdefault(s["bookmaker"], [None] * len(instants))
        book_series[index[datetime.fromisoformat(s["captured_at"])]] = s["odds"]

    cur = await store._db.execute(
        "SELECT flagged_at, selection, entry_odds, fair_odds, edge_pct, tier, "
        "clv_pct, result, excluded_reason, tipsport_url "
        "FROM value_bets WHERE game_id = ? ORDER BY flagged_at",
        (game_id,),
    )
    flags = [dict(r) for r in await cur.fetchall()]
    for f in flags:
        f["flagged_label"] = (
            datetime.fromisoformat(f["flagged_at"])
            .astimezone(_DISPLAY_TZ)
            .strftime("%d.%m %H:%M")
        )

    return {
        "game": game,
        "tipoff_local": tipoff_local,
        "labels": labels,
        "series": series,
        "flags": flags,
        "n_snapshots": len(snaps),
    }


# ---------------------------------------------------------------------------
# Activity calendar (/analysis) — 2026-09-06
# ---------------------------------------------------------------------------


def _month_range(first_ym: str | None, now: datetime) -> list[str]:
    """Calendar months from ``first_ym`` ("YYYY-MM") through ``now``'s UTC
    month, newest first; empty months included. None/malformed -> []."""
    if not first_ym:
        return []
    try:
        y, m = int(first_ym[:4]), int(first_ym[5:7])
    except ValueError:
        return []
    cur_y, cur_m = now.year, now.month
    out: list[str] = []
    while (cur_y, cur_m) >= (y, m):
        out.append(f"{cur_y:04d}-{cur_m:02d}")
        cur_m -= 1
        if cur_m == 0:
            cur_y, cur_m = cur_y - 1, 12
    return out or [f"{now.year:04d}-{now.month:02d}"]


def _prague_day(ts_iso: str) -> date:
    return datetime.fromisoformat(ts_iso).astimezone(_DISPLAY_TZ).date()


def _prague_hm(ts_iso: str) -> str:
    return datetime.fromisoformat(ts_iso).astimezone(_DISPLAY_TZ).strftime("%H:%M")


def _empty_day(d: date, today: date) -> dict[str, Any]:
    return {
        "date": d.isoformat(),
        "dom": d.day,
        "clean": 0,
        "guarded": 0,
        "shadow": 0,
        "attempts": 0,
        "placed": 0,
        "skipped": 0,
        "pl_czk": 0.0,
        "flat_pl": 0.0,
        "first_time": None,
        "last_time": None,
        "milestones": [],
        "is_today": d == today,
        "future": d > today,
        "level": 0,
    }


async def activity_calendar(
    store: Store,
    *,
    sport: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Per-day activity from the first collected odds through today, bucketed
    by PRAGUE calendar day (a 23:30 UTC detection is "tonight" to the operator,
    not tomorrow), with system milestones merged in.

    Each day: clean/guarded/shadow detections (shadow = far-tier, recorded but
    never bettable), betting decisions (placed / attempts = rejected|failed|
    unknown / skipped), real settled P&L (Kč), paper flat P&L of clean bets,
    first/last detection time, milestone labels, and a 0-4 ``level``
    (0 nothing, 1 guarded/shadow only, 2 one clean, 3 two-four, 4 five+).
    Months are calendar-true — every month from data start through today
    renders, empty or not, so a quiet month reads as quiet rather than missing.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(_DISPLAY_TZ).date()
    sclause, sparams = _sport_clause(sport)
    days: dict[date, dict[str, Any]] = {}

    def bucket(d: date) -> dict[str, Any]:
        if d not in days:
            days[d] = _empty_day(d, today)
        return days[d]

    first_detection: date | None = None
    cur = await store._db.execute(
        f"SELECT flagged_at, excluded_reason, result, entry_odds, tier "
        f"FROM value_bets WHERE 1=1{sclause}",
        sparams,
    )
    for r in await cur.fetchall():
        d = _prague_day(r["flagged_at"])
        b = bucket(d)
        hm = _prague_hm(r["flagged_at"])
        if b["first_time"] is None or hm < b["first_time"]:
            b["first_time"] = hm
        if b["last_time"] is None or hm > b["last_time"]:
            b["last_time"] = hm
        # Anchors the calendar range before the shadow short-circuit, so a
        # shadow-only first day still starts the rendered month span.
        if first_detection is None or d < first_detection:
            first_detection = d
        if r["tier"] == pipeline_tiers.SHADOW_TIER:
            b["shadow"] += 1  # shadow detection: recorded, never bettable
            continue
        if r["excluded_reason"]:
            b["guarded"] += 1
        else:
            b["clean"] += 1
            if r["result"] == "win":
                b["flat_pl"] += (r["entry_odds"] or 1.0) - 1.0
            elif r["result"] == "loss":
                b["flat_pl"] -= 1.0

    first_attempt: date | None = None
    first_placed: date | None = None
    cur = await store._db.execute(
        f"SELECT status, created_at, settled_pl_czk FROM placed_bets WHERE 1=1{sclause}",
        sparams,
    )
    for r in await cur.fetchall():
        d = _prague_day(r["created_at"])
        b = bucket(d)
        st = r["status"]
        if st == "skipped":
            b["skipped"] += 1
            continue
        if st in ("rejected", "failed", "unknown"):
            b["attempts"] += 1
        elif st in ("pending", "placed", "settled"):
            b["placed"] += 1
            if first_placed is None or d < first_placed:
                first_placed = d
        if first_attempt is None or d < first_attempt:
            first_attempt = d
        if r["settled_pl_czk"] is not None:
            b["pl_czk"] += float(r["settled_pl_czk"])

    cur = await store._db.execute(
        f"SELECT MIN(captured_at) AS m FROM odds_snapshots WHERE 1=1{sclause}",
        sparams,
    )
    row = await cur.fetchone()
    first_odds = _prague_day(row["m"]) if row and row["m"] else None

    starts = [d for d in (first_odds, first_detection, first_attempt) if d is not None]
    if not starts:
        return {
            "months": [],
            "milestones": [],
            "today": today.isoformat(),
            "sport": sport,
        }
    start = min(starts).replace(day=1)

    milestones: list[dict[str, Any]] = [
        {"date": m.day, "label": m.label, "kind": m.kind}
        for m in MILESTONES
        if start <= m.day <= today
    ]
    for d, label in (
        (first_odds, "First odds collected"),
        (first_detection, "First value bet detected"),
        (first_attempt, "First real placement attempt"),
        (first_placed, "First bet placed"),
    ):
        if d is not None:
            milestones.append({"date": d, "label": label, "kind": "data"})
    milestones.sort(key=lambda m: m["date"])
    for m in milestones:
        bucket(m["date"])["milestones"].append(m["label"])

    for b in days.values():
        c = b["clean"]
        b["level"] = (
            4
            if c >= 5
            else 3
            if c >= 2
            else 2
            if c == 1
            else 1
            if (b["guarded"] or b["shadow"])
            else 0
        )

    months: list[dict[str, Any]] = []
    y, mo = start.year, start.month
    while (y, mo) <= (today.year, today.month):
        first = date(y, mo, 1)
        nxt = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
        cells: list[dict[str, Any] | None] = [
            None
        ] * first.weekday()  # Monday-first grid
        d = first
        while d < nxt:
            cells.append(days.get(d) or _empty_day(d, today))
            d += timedelta(days=1)
        while len(cells) % 7:
            cells.append(None)
        mdays = [c for c in cells if c is not None]
        months.append(
            {
                "ym": f"{y:04d}-{mo:02d}",
                "label": first.strftime("%b %Y"),
                "weeks": [cells[i : i + 7] for i in range(0, len(cells), 7)],
                "clean": sum(c["clean"] for c in mdays),
                "guarded": sum(c["guarded"] for c in mdays),
                "shadow": sum(c["shadow"] for c in mdays),
                "placed": sum(c["placed"] for c in mdays),
                "attempts": sum(c["attempts"] for c in mdays),
                "pl_czk": round(sum(c["pl_czk"] for c in mdays), 2),
                "milestones": [
                    m
                    for m in milestones
                    if (m["date"].year, m["date"].month) == (y, mo)
                ],
            }
        )
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return {
        "months": months,
        "milestones": milestones,
        "today": today.isoformat(),
        "sport": sport,
    }


async def day_detail(
    store: Store, day: date, *, sport: str | None = None
) -> dict[str, Any]:
    """Everything that happened on one PRAGUE calendar day: detections (with
    their betting decision) and the day's placed-bet outcomes, times in Prague."""
    start = datetime.combine(day, time(0), tzinfo=_DISPLAY_TZ).astimezone(timezone.utc)
    end = datetime.combine(
        day + timedelta(days=1), time(0), tzinfo=_DISPLAY_TZ
    ).astimezone(timezone.utc)
    sclause, sparams = _sport_clause(sport, alias="vb")
    cur = await store._db.execute(
        "SELECT vb.id, vb.game_id, vb.flagged_at, vb.tier, vb.selection, vb.entry_odds, "
        "vb.fair_odds, vb.edge_pct, vb.excluded_reason, vb.result, vb.clv_pct, vb.sport, "
        "g.home_team, g.away_team, "
        "pb.status AS dec_status, pb.skip_reason, pb.error, pb.stake_czk, "
        "pb.accepted_odds, pb.settled_pl_czk "
        "FROM value_bets vb JOIN games g ON g.id = vb.game_id "
        "LEFT JOIN placed_bets pb ON pb.value_bet_id = vb.id "
        f"WHERE vb.flagged_at >= ? AND vb.flagged_at < ?{sclause} "
        "ORDER BY vb.flagged_at",
        [start.isoformat(), end.isoformat(), *sparams],
    )
    detections = []
    for r in await cur.fetchall():
        d = dict(r)
        d["time"] = _prague_hm(d["flagged_at"])
        sel = (d["selection"] or "").lower()
        d["team"] = (
            d["home_team"]
            if sel == "home"
            else d["away_team"]
            if sel == "away"
            else d["selection"]
        )
        detections.append(d)
    # Far-tier rows are SHADOW detections (recorded, never bettable): they are
    # neither clean nor guarded, so they get their own count rather than
    # inflating the day's "clean" headline.
    shadow = pipeline_tiers.SHADOW_TIER
    n_clean = sum(
        1 for d in detections if not d["excluded_reason"] and d["tier"] != shadow
    )
    return {
        "day": day.isoformat(),
        "label": day.strftime("%A %d.%m.%Y"),
        "detections": detections,
        "n_clean": n_clean,
        # A far-tier row is SHADOW even when it also carries an excluded_reason
        # guard stamp — it must land in exactly one bucket (2026-09-06).
        "n_guarded": sum(
            1 for d in detections if d["excluded_reason"] and d["tier"] != shadow
        ),
        "n_shadow": sum(1 for d in detections if d["tier"] == shadow),
        "n_placed": sum(
            1 for d in detections if d["dec_status"] in ("pending", "placed", "settled")
        ),
        "n_attempts": sum(
            1
            for d in detections
            if d["dec_status"] in ("rejected", "failed", "unknown")
        ),
        "n_skipped": sum(1 for d in detections if d["dec_status"] == "skipped"),
        "pl_czk": round(sum(d["settled_pl_czk"] or 0.0 for d in detections), 2),
    }
