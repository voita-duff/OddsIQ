"""FastAPI dashboard server for OddsIQ.

The dashboard is a read-only web UI on top of the existing SQLite DB and
the rotating log file. It runs as a separate process (started via
``oddsiq dashboard``). See ``docs/Components/Dashboard.md``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import math
import secrets
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from oddsiq import config
from oddsiq.notifications import telegram
from oddsiq.db.store import Store
from oddsiq.dashboard import queries, state as state_mod, log_tail
from oddsiq.sports.profiles import SPORTS, ENABLED_SPORTS


# Timestamps are stored UTC; the dashboard audience is CET/CEST, so
# human-facing times render in Prague local time (matches the console
# log convention of showing local time, but explicit + DST-correct).
_DISPLAY_TZ = ZoneInfo("Europe/Prague")
logger_dash = logging.getLogger("oddsiq.dashboard")


def _fmt_flagged_at(value: str) -> str:
    """Render a stored UTC ISO ``flagged_at`` as European (Prague) local
    ``DD.MM.YYYY HH:MM`` — day-first, dotted, 24-hour. The column is written
    as ``datetime.now(timezone.utc).isoformat()``, so convert before display."""
    return (
        datetime.fromisoformat(value).astimezone(_DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")
    )


def _selection_team(selection: str, home_team: str, away_team: str) -> str:
    """Map a moneyline ``selection`` ('home'/'away') to the actual team name so
    the dashboard shows which team to bet on instead of a side label. Any
    unexpected selection (e.g. 'draw') passes through unchanged."""
    if selection == "home":
        return home_team
    if selection == "away":
        return away_team
    return selection


def _human_size(n: int) -> str:
    """Render bytes as a 1-decimal human-readable string (B/KB/MB/GB/TB)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _safe_size(p: Path) -> str:
    try:
        return _human_size(p.stat().st_size)
    except OSError:
        return "—"


def _dir_size(p: Path) -> str:
    if not p.exists():
        return "—"
    try:
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return _human_size(total)
    except OSError:
        return "—"


def _count_files(p: Path, glob: str) -> int:
    if not p.exists():
        return 0
    return len(list(p.glob(glob)))


def _flag(value: bool) -> str:
    return "1 (on)" if value else "0 (off)"


def _settings_config_sections() -> list[dict]:
    """Grouped, registry-style config display for /settings.

    One place, grouped by concern, built fresh per request so it can't drift
    from ``config.py`` values. (The live-scout section was removed with the
    whole live-edge subsystem, 2026-08-27.)
    """
    from oddsiq.sports.profiles import ENABLED_SPORTS as _sports

    telegram_recipients = sum(
        1
        for token, chat in (
            (config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID),
            (config.TELEGRAM_BOT_TOKEN_2, config.TELEGRAM_CHAT_ID_2),
        )
        if token and chat
    )
    return [
        {
            "title": "Detection",
            "rows": [
                {"name": "EDGE_THRESHOLD", "value": str(config.EDGE_THRESHOLD)},
                {
                    "name": "PREGAME_MAX_PLAUSIBLE_EDGE",
                    "value": str(config.PREGAME_MAX_PLAUSIBLE_EDGE),
                },
                {
                    "name": "PREGAME_MIN_BOOK_OVERROUND",
                    "value": str(config.PREGAME_MIN_BOOK_OVERROUND),
                },
                {
                    "name": "PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS",
                    "value": str(config.PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS),
                },
                {
                    "name": "PREGAME_REQUIRE_COMPLETE_BOOK_MARKET",
                    "value": str(config.PREGAME_REQUIRE_COMPLETE_BOOK_MARKET),
                },
                {
                    "name": "CLOSING_FRESHNESS_MINUTES",
                    "value": str(config.CLOSING_FRESHNESS_MINUTES),
                },
                {"name": "PINNACLE_MARKETS", "value": config.PINNACLE_MARKETS},
            ],
        },
        {
            "title": "Polling & schedule",
            "rows": [
                {
                    "name": "POLL_TIER_FAR / WARM / HOT",
                    "value": (
                        f"{config.POLL_TIER_FAR} / {config.POLL_TIER_WARM} / "
                        f"{config.POLL_TIER_HOT} s"
                    ),
                },
                {
                    "name": "SCHEDULE_GAME_WINDOW_REFRESH",
                    "value": f"{config.SCHEDULE_GAME_WINDOW_REFRESH}s",
                },
                {
                    "name": "SCHEDULE_COMPLETION_CHECK_INTERVAL",
                    "value": f"{config.SCHEDULE_COMPLETION_CHECK_INTERVAL}s",
                },
                {
                    "name": "SCHEDULE_REFRESH_INTERVAL",
                    "value": f"{config.SCHEDULE_REFRESH_INTERVAL}s",
                },
                {
                    "name": "SCHEDULER_HEARTBEAT_SECONDS",
                    "value": str(config.SCHEDULER_HEARTBEAT_SECONDS),
                },
            ],
        },
        {
            "title": "Auto-betting",
            "rows": [
                {"name": "BETTING_ENABLED", "value": _flag(config.BETTING_ENABLED)},
                {
                    "name": "BETTING_CONFIRM_ENABLED (env)",
                    "value": _flag(config.BETTING_CONFIRM_ENABLED),
                },
                {
                    "name": "BETTING_MAX_STAKE_CZK (hard ceiling)",
                    "value": f"{config.BETTING_MAX_STAKE_CZK:.0f}",
                },
                {
                    "name": "BETTING_MAX_BETS_PER_DAY (hard ceiling)",
                    "value": str(config.BETTING_MAX_BETS_PER_DAY),
                },
                {
                    "name": "BETTING_MAX_OPEN_STAKES_CZK (hard ceiling)",
                    "value": f"{config.BETTING_MAX_OPEN_STAKES_CZK:.0f}",
                },
                {
                    "name": "BETTING_MAX_DAILY_LOSS_CZK (hard ceiling)",
                    "value": f"{config.BETTING_MAX_DAILY_LOSS_CZK:.0f}",
                },
                {
                    "name": "BETTING_BRIDGE",
                    "value": f"{config.BETTING_BRIDGE_HOST}:{config.BETTING_BRIDGE_PORT}",
                },
                {
                    "name": "BETTING_PLACEMENT_TIMEOUT_SECONDS",
                    "value": str(config.BETTING_PLACEMENT_TIMEOUT_SECONDS),
                },
                {
                    "name": "BETTING_FAIR_MAX_AGE_SECONDS",
                    "value": str(config.BETTING_FAIR_MAX_AGE_SECONDS),
                },
                {
                    "name": "DASHBOARD_CONTROLS_ENABLED",
                    "value": _flag(config.DASHBOARD_CONTROLS_ENABLED),
                },
                {
                    "name": "DASHBOARD_CONTROLS_AUTH",
                    "value": (
                        "configured"
                        if config.DASHBOARD_CONTROLS_USERNAME
                        and config.DASHBOARD_CONTROLS_PASSWORD
                        else "MISSING"
                    ),
                },
            ],
        },
        {
            "title": "Pipeline & ops",
            "rows": [
                {"name": "ENABLED_SPORTS", "value": ", ".join(_sports) or "none"},
                {
                    "name": "Bookmaker: tipsport",
                    "value": "enabled"
                    if config.BOOKMAKER_TIPSPORT_ENABLED
                    else "disabled",
                },
                {
                    "name": "ODDS_API_KEY",
                    "value": "set" if config.ODDS_API_KEY else "MISSING",
                },
                {"name": "Telegram recipients", "value": str(telegram_recipients)},
                {
                    "name": "HEARTBEAT_PING_URL",
                    "value": "set" if config.HEARTBEAT_PING_URL else "not set",
                },
                {
                    "name": "BACKUP_INTERVAL_HOURS",
                    "value": str(config.BACKUP_INTERVAL_HOURS),
                },
                {
                    "name": "RAW_RESPONSE_KEEP_DAYS",
                    "value": str(config.RAW_RESPONSE_KEEP_DAYS),
                },
            ],
        },
    ]


def _controls_auth_configured() -> bool:
    return bool(
        config.DASHBOARD_CONTROLS_USERNAME and config.DASHBOARD_CONTROLS_PASSWORD
    )


def _controls_available() -> bool:
    return bool(config.DASHBOARD_CONTROLS_ENABLED and _controls_auth_configured())


# One realm for the page challenge AND the control POSTs: browsers cache Basic
# credentials per (origin, realm), so a mismatch would make the prompt on
# ``GET /betting`` useless for the htmx POSTs it exists to authorize.
_CONTROLS_REALM = 'Basic realm="OddsIQ betting controls"'


def _basic_auth_ok(request: Request) -> bool:
    """True when the request carries the configured Basic credentials."""
    username = config.DASHBOARD_CONTROLS_USERNAME
    password = config.DASHBOARD_CONTROLS_PASSWORD
    if not username or not password:
        return False
    auth = request.headers.get("authorization", "")
    try:
        scheme, encoded = auth.split(" ", 1)
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        supplied_user, supplied_password = decoded.split(":", 1)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    return (
        scheme.lower() == "basic"
        and secrets.compare_digest(supplied_user.encode(), username.encode())
        and secrets.compare_digest(supplied_password.encode(), password.encode())
    )


def _expected_origin_host(request: Request) -> str:
    """The host the browser actually addressed.

    Behind a reverse proxy ``Host`` is the upstream bind (``127.0.0.1:8080``)
    while ``Origin`` carries the public hostname — comparing against ``Host``
    alone would 403 every control POST. Prefer the proxy's
    ``X-Forwarded-Host`` (first hop when chained) when present.

    Trusting it does not weaken the CSRF check: a cross-site page cannot set a
    custom header on a request to this app (that needs a CORS preflight, and
    no CORS middleware is installed to approve one), so an attacker's browser
    can never supply a forged ``X-Forwarded-Host`` to match its own Origin.
    """
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.headers.get("host", "")


@asynccontextmanager
async def _store():
    """Open the Store on the configured DB path and guarantee ``close()`` on
    exit (including on error). Use as ``async with _store() as store:``."""
    store = Store(config.DATABASE_PATH)
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


def _scraper_freshness_thresholds(active_tiers: set[str]) -> tuple[int, int]:
    if "hot" in active_tiers:
        return 5 * 60, 10 * 60
    if "warm" in active_tiers:
        return 15 * 60, 25 * 60
    return 60 * 60, 6 * 60 * 60


# The "Active scrapers" card lists only the soft book(s) the operator bets —
# currently just Tipsport. Pinnacle (the sharp reference) and any legacy/removed
# scraper health lines are excluded here; Pinnacle health stays visible via the
# Odds API budget card and the coverage matrix.
_ACTIVE_SCRAPER_NAMES: frozenset[str] = frozenset({"tipsport"})


def _fmt_age(age_s: float) -> str:
    """Compact age: '45s' under 2 min, '38m' under 2 h, '8h' beyond."""
    if age_s < 120:
        return f"{int(age_s)}s"
    if age_s < 2 * 3600:
        return f"{int(age_s / 60)}m"
    return f"{int(age_s / 3600)}h"


def _scraper_status_rows(
    health: dict[str, "log_tail.ScraperHealth"],
    now: datetime,
    active_tiers: set[str],
) -> list[dict]:
    """Render per-scraper status: ✓/⚠/✗ + relative time string.

    Restricted to ``_ACTIVE_SCRAPER_NAMES`` (the soft book(s) the operator
    bets) so the card stays focused on Tipsport. With NO active tiers there
    is nothing the scraper should be polling, so the row is a neutral
    ``idle`` — red is reserved for "games needed polling and didn't get it"
    (an 8h-stale badge during a normal overnight idle is a false alarm).
    """
    ok_s, warn_s = _scraper_freshness_thresholds(active_tiers)
    rows: list[dict] = []
    for name in sorted(health):
        if name not in _ACTIVE_SCRAPER_NAMES:
            continue
        h = health[name]
        age_s = (now - h.timestamp).total_seconds()
        if not active_tiers:
            icon, css = "idle", "na"
        elif age_s <= ok_s:
            icon, css = "✓", "ok"
        elif age_s <= warn_s:
            icon, css = "⚠", "warn"
        else:
            icon, css = "✗", "err"
        rows.append(
            {
                "name": name,
                "icon": icon,
                "css_class": css,
                "age": _fmt_age(age_s),
                # Tripwire: snapshots dropped because Tipsport's listing carried no
                # usable start time. Non-zero means a payload change is silently
                # killing game matching (the fail-closed path), so it gets its own
                # badge rather than hiding inside the log line.
                "unsafe_start": h.unsafe_start_snapshots,
            }
        )
    return rows


def _parse_budget_history(raw: str | None) -> list | None:
    """The governor's `odds_api_budget_history` meta (JSON list) -> list, or
    None on absent/malformed (the context then falls back to period average)."""
    import json

    if not raw:
        return None
    try:
        history = json.loads(raw)
        return history if isinstance(history, list) else None
    except ValueError:
        return None


def _budget_context(
    used: int | None,
    remaining: int | None,
    *,
    now: datetime,
    reset_day: int | None = None,
    history: list | None = None,
) -> dict | None:
    """Everything the budget cell needs to answer "am I going to run out":
    percent used, the next reset date (``ODDS_API_QUOTA_RESET_DAY``), burn/day,
    and the projected total at that burn. ``None`` when the governor hasn't
    persisted budget numbers yet.

    Burn/day prefers the TRAILING rate over the last ~3 complete days (from
    the governor's ``odds_api_budget_history`` meta) — the period average is
    permanently poisoned by mid-period regime changes (the 2026-08-27
    live-tier removal cut ~200/day but the average kept projecting the old
    rate until the next reset). Falls back to the period average when the
    history is missing, predates the last reset, or crosses a quota rollover
    (``burn_basis``: ``"3d"`` vs ``"avg"``)."""
    if used is None or remaining is None:
        return None
    if reset_day is None:
        reset_day = config.ODDS_API_QUOTA_RESET_DAY
    total = used + remaining
    today = now.date()
    # Last reset: this month's reset day if already passed, else last month's.
    if today.day >= reset_day:
        last_reset = today.replace(day=reset_day)
    else:
        first_of_month = today.replace(day=1)
        prev_month_end = first_of_month - timedelta(days=1)
        last_reset = prev_month_end.replace(day=min(reset_day, prev_month_end.day))
    next_reset = (last_reset.replace(day=1) + timedelta(days=32)).replace(day=reset_day)
    days_elapsed = max((today - last_reset).days, 1)
    days_left = (next_reset - today).days

    burn = round(used / days_elapsed)
    burn_basis = "avg"
    if history:
        # Anchor on the entry closest to 3 days back that is still inside the
        # current quota period and not past a rollover (u must not exceed the
        # current cumulative `used`).
        window_start = today - timedelta(days=3)
        candidates = []
        for e in history:
            try:
                d = date_cls.fromisoformat(e["d"])
                u = int(e["u"])
            except (KeyError, TypeError, ValueError):
                continue
            if last_reset <= d < today and d >= window_start and u <= used:
                candidates.append((d, u))
        if candidates:
            anchor_d, anchor_u = min(candidates)  # oldest usable in the window
            span_days = max((today - anchor_d).days, 1)
            burn = round((used - anchor_u) / span_days)
            burn_basis = "3d"

    projected = used + burn * days_left
    return {
        "total": total,
        "pct": (used * 100 // total) if total else 0,
        "resets_on": next_reset.strftime("%d.%m"),
        "days_left": days_left,
        "burn_per_day": burn,
        "burn_basis": burn_basis,
        "projected": projected,
        "over_budget": projected > total,
    }


def _format_db_event(e: dict) -> str:
    if e["type"] == "value_bet":
        team = _selection_team(
            e.get("selection", "?"), e.get("home_team", "?"), e.get("away_team", "?")
        )
        matchup = f"{e.get('away_team', '?')} @ {e.get('home_team', '?')}"
        tier = f" ({e['tier']})" if e.get("tier") else ""
        return f"VALUE BET · {matchup} · {team} {e['edge_pct']:+.2f}%{tier}"
    if e["type"] == "snapshot":
        return f"snapshot · {e['bookmaker']} {e['market']} {e['selection']}"
    return e["type"]


def _format_log_event(e: "log_tail.LogEvent") -> str:
    f = e.fields
    if e.event_type == "value_bet_suspect":
        return (
            f"guarded detection · {f.get('reason', '?')} · {f.get('bookmaker', '?')} "
            f"{f.get('market', '?')} {f.get('selection', '?')} +{f.get('edge_pct', '?')}%"
        )
    if e.event_type == "clv":
        return f"CLV · {f.get('bookmaker', '?')} {f.get('market', '?')} {f.get('selection', '?')} {f.get('clv_pct', '?')}%"
    if e.event_type == "result":
        return f"RESULT {f.get('result', '?')} · {f.get('bookmaker', '?')} {f.get('market', '?')} {f.get('selection', '?')}"
    if e.event_type == "game_live":
        return f"game live · {f.get('away', '?')} @ {f.get('home', '?')}"
    if e.event_type == "game_final":
        return (
            f"game final · {f.get('away', '?')} {f.get('away_score', '?')} - "
            f"{f.get('home', '?')} {f.get('home_score', '?')}"
        )
    if e.event_type == "coverage_gap":
        return (
            f"⚠ coverage gap · {f.get('scraper', '?')} "
            f"game={f.get('game_id', '?')} stale={f.get('minutes_stale', '?')}m"
        )
    if e.event_type == "scraper_health":
        return (
            f"poll · {f.get('name', '?')} matched_games={f.get('matched_games', '?')} "
            f"errors={f.get('errors', '?')}"
        )
    if e.event_type == "heartbeat":
        return f"heartbeat · state={f.get('state', '?')} games={f.get('games_tracked', '?')}"
    return e.event_type


def _as_instant(ts: str) -> datetime:
    """Parse an ISO timestamp string to a tz-aware datetime (naive assumed UTC).
    Unparseable values sort oldest."""
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _fmt_feed_ts(instant: datetime, now: datetime) -> str:
    """Feed timestamp in Prague local time — one clock for the whole dashboard.

    Today (Prague date) renders as ``HH:MM:SS``; anything older carries its
    date (``DD.MM HH:MM``) so a 4-day-old event can't masquerade as tonight's
    (the feed used to print raw UTC with no date at all).
    """
    local = instant.astimezone(_DISPLAY_TZ)
    if local.date() == now.astimezone(_DISPLAY_TZ).date():
        return local.strftime("%H:%M:%S")
    return local.strftime("%d.%m %H:%M")


def _merge_activity_rows(
    db_events,
    log_events,
    *,
    limit: int = 50,
    now: datetime | None = None,
) -> list[dict]:
    """Merge DB-sourced and log-sourced activity events into one newest-first feed.

    Sort by the parsed tz-aware INSTANT, not the raw ISO string: DB events are
    stored UTC (``+00:00``) while log events carry the pipeline's LOCAL offset
    (e.g. ``+02:00``), so a lexicographic string sort would misorder them across
    sources. Display times go through :func:`_fmt_feed_ts` (Prague, dated when
    not today) for BOTH sources. The ``value_bet`` log line is skipped because
    the DB already supplies a formatted value-bet row (avoids listing each bet
    twice); ``value_bet_suspect`` is kept — guarded detections exist ONLY in
    the log, and hiding them made the detector look dead.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    rows: list[tuple[datetime, dict]] = []
    for e in db_events:
        ts = e["ts"]
        instant = _as_instant(ts)
        rows.append(
            (
                instant,
                {
                    "src": "DB",
                    "ts": ts,
                    "ts_short": _fmt_feed_ts(instant, now),
                    "event_type": e["type"],
                    "level": "INFO",
                    "text": _format_db_event(e),
                },
            )
        )
    for e in log_events:
        # Skip value_bet (the DB already supplies a formatted row) and heartbeat
        # (liveness is shown by the status strip) so neither consumes the row
        # budget and pushes real events out of the feed.
        if e.event_type in ("value_bet", "heartbeat"):
            continue
        rows.append(
            (
                e.timestamp,
                {
                    "src": "LOG",
                    "ts": e.timestamp.isoformat(),
                    "ts_short": _fmt_feed_ts(e.timestamp, now),
                    "event_type": e.event_type,
                    "level": e.level,
                    "text": _format_log_event(e),
                },
            )
        )
    rows.sort(key=lambda r: r[0], reverse=True)
    return [row for _, row in rows[:limit]]


def _engine_state(hb, now: datetime) -> str:
    """Effective engine state for the banner/dot: the heartbeat's ``betting=``
    field, downgraded to ``unknown`` when the heartbeat is missing or stale
    (a 3-heartbeat-old 'armed' proves nothing)."""
    if hb is None or hb.betting is None:
        return "off" if hb is not None else "unknown"
    if (now - hb.timestamp).total_seconds() > 3 * config.SCHEDULER_HEARTBEAT_SECONDS:
        return "unknown"
    return hb.betting


def _betting_chain(
    hb, bridge, engine_state: str, *, armed_meta: bool, now: datetime
) -> list[dict]:
    """The 6-link safety chain for /betting: enabled → bridge → extension →
    logged in → armed → confirm gate. Each link is ok/fail/warn/na with a
    short detail, so a broken deploy (extension didn't re-dial, login
    dropped, gate off) is diagnosable at a glance instead of hiding behind a
    one-word pill."""
    links: list[dict] = []
    links.append(
        {
            "label": "enabled",
            "css": "ok" if config.BETTING_ENABLED else "na",
            "detail": "" if config.BETTING_ENABLED else "BETTING_ENABLED=0",
        }
    )
    if hb is None:
        links.append({"label": "bridge", "css": "na", "detail": "no heartbeat"})
    elif hb.betting is None or engine_state == "off":
        links.append({"label": "bridge", "css": "fail", "detail": "engine off"})
    else:
        links.append(
            {
                "label": "bridge",
                "css": "ok",
                "detail": f"hb {_fmt_age((now - hb.timestamp).total_seconds())}",
            }
        )
    ext_detail = ""
    if bridge is not None and bridge.last_connected is not None:
        ext_detail = (
            f"seen {_fmt_age((now - bridge.last_connected).total_seconds())} ago"
        )
    if engine_state in ("disarmed", "armed"):
        links.append({"label": "extension", "css": "ok", "detail": ext_detail})
    elif engine_state == "disconnected":
        links.append(
            {
                "label": "extension",
                "css": "fail",
                "detail": ext_detail or "not connected",
            }
        )
    else:
        links.append({"label": "extension", "css": "na", "detail": ext_detail})
    if bridge is not None and bridge.logged_in is not None:
        li_detail = ""
        if bridge.logged_in_at is not None:
            li_detail = (
                f"as of {_fmt_age((now - bridge.logged_in_at).total_seconds())} ago"
            )
        links.append(
            {
                "label": "logged in",
                "css": "ok" if bridge.logged_in else "fail",
                "detail": li_detail
                if bridge.logged_in
                else (li_detail or "session down"),
            }
        )
    else:
        links.append({"label": "logged in", "css": "na", "detail": "no bridge log"})
    if engine_state == "armed":
        links.append({"label": "armed", "css": "ok", "detail": ""})
    elif armed_meta:
        links.append(
            {"label": "armed", "css": "warn", "detail": "meta armed, engine not"}
        )
    else:
        links.append({"label": "armed", "css": "fail", "detail": "disarmed"})
    links.append(
        {
            "label": "confirm gate",
            "css": "ok" if config.BETTING_CONFIRM_ENABLED else "warn",
            "detail": "money ON (env)"
            if config.BETTING_CONFIRM_ENABLED
            else "OFF — dry-run (env)",
        }
    )
    # A recent supersede is the reconnect-storm signature — surface it even
    # after the storm settles so the operator knows to check the extension.
    if bridge is not None and bridge.last_supersede is not None:
        storm_age = (now - bridge.last_supersede).total_seconds()
        if storm_age < 1800:
            links.append(
                {
                    "label": "reconnect storm",
                    "css": "warn",
                    "detail": f"supersede {_fmt_age(storm_age)} ago",
                }
            )
    return links


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles that disables heuristic browser caching.

    Default ``StaticFiles`` sends only ``ETag``/``Last-Modified`` and no
    ``Cache-Control``, so browsers may serve a stale ``dashboard.css`` /
    ``charts.js`` across reloads — masking template/CSS edits until a hard
    refresh. ``no-cache`` lets the browser keep a copy but forces revalidation
    on every request (cheap 304 when unchanged). Appropriate because this
    dashboard is local-only.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _norm_sport(sport: str | None) -> str | None:
    """Return a valid registered sport key, or None (= All / unfiltered)."""
    if sport and sport in SPORTS:
        return sport
    return None


def _asset_version(static_dir: Path) -> str:
    """Cache-busting token = newest static-file mtime (integer seconds).

    Appended to CSS/JS URLs in ``base.html`` as ``?v=<token>``. A changed
    asset gets a new URL, so the browser fetches it fresh instead of serving
    a heuristically-cached stale copy — the failure mode ``no-cache`` alone
    can't fix, since a browser may serve an already-cached copy without ever
    revalidating. The token only changes when files do, so unchanged assets
    still cache. Recomputed per ``create_app()`` (i.e. per dashboard restart).
    """
    try:
        mtimes = [f.stat().st_mtime for f in static_dir.glob("*") if f.is_file()]
        return str(int(max(mtimes))) if mtimes else "0"
    except OSError:
        return "0"


def create_app() -> FastAPI:
    """Build and return the FastAPI app. Each call returns a distinct
    instance; templates and static-files objects are constructed per-app
    so tests can spin up isolated instances."""
    app = FastAPI(title="OddsIQ Dashboard", docs_url=None, redoc_url=None)
    dashboard_dir = Path(__file__).parent
    templates = Jinja2Templates(directory=str(dashboard_dir / "templates"))
    templates.env.globals["asset_version"] = _asset_version(dashboard_dir / "static")
    templates.env.filters["flagged_at"] = _fmt_flagged_at
    templates.env.filters["team"] = _selection_team
    app.mount(
        "/static",
        _NoCacheStaticFiles(directory=str(dashboard_dir / "static")),
        name="static",
    )

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/live", status_code=307)

    @app.get("/upcoming", response_class=HTMLResponse)
    async def upcoming(request: Request, sport: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "upcoming.html",
            {
                "page": "upcoming",
                "sport": _norm_sport(sport),
                "enabled_sports": ENABLED_SPORTS,
            },
        )

    @app.get("/upcoming/partials/board", response_class=HTMLResponse)
    async def upcoming_board_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            board = await queries.upcoming_games(store, sport=s)
        return templates.TemplateResponse(
            request,
            "partials/upcoming_board.html",
            {"board": board, "sport": s},
        )

    @app.get("/live", response_class=HTMLResponse)
    async def live(request: Request, sport: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "live.html",
            {
                "page": "live",
                "sport": _norm_sport(sport),
                "enabled_sports": ENABLED_SPORTS,
            },
        )

    @app.get("/live/partials/status-strip", response_class=HTMLResponse)
    async def status_strip_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            summary = await queries.status_strip(store, sport=s)
            active_tiers = await queries.active_game_tiers(store, sport=s)
            budget_history = _parse_budget_history(
                await store.get_meta("odds_api_budget_history")
            )
        now = datetime.now(timezone.utc)
        # One offloaded scan: derive heartbeat + scraper health in a single pass
        # and reuse the heartbeat for the state derivation (avoids 3 blocking
        # full-file reads per status-strip refresh).
        hb, health = await asyncio.to_thread(log_tail.scan_log_state, config.LOG_FILE)
        return templates.TemplateResponse(
            request,
            "partials/status_strip.html",
            {
                "summary": summary,
                "state": state_mod.derive_pipeline_state(
                    config.LOG_FILE, now=now, heartbeat=hb
                ),
                "heartbeat": hb,
                "heartbeat_age_s": int((now - hb.timestamp).total_seconds())
                if hb
                else None,
                "scrapers": _scraper_status_rows(health, now, active_tiers),
                "api_key_set": bool(config.ODDS_API_KEY),
                "budget": _budget_context(
                    summary["budget_used"],
                    summary["budget_remaining"],
                    now=now,
                    history=budget_history,
                ),
                "updated": now.astimezone(_DISPLAY_TZ).strftime("%H:%M:%S"),
                "sport": s,
            },
        )

    @app.get("/live/partials/coverage-matrix", response_class=HTMLResponse)
    async def coverage_matrix_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        enabled_pairs = [
            ("pinnacle", True),
            ("tipsport", config.BOOKMAKER_TIPSPORT_ENABLED),
        ]
        enabled = [name for name, on in enabled_pairs if on]
        async with _store() as store:
            rows = await queries.coverage_matrix(store, scrapers=enabled, sport=s)
            next_window = (
                None if rows else await queries.next_game_window(store, sport=s)
            )
        health = await asyncio.to_thread(
            log_tail.latest_scraper_health_by_name, config.LOG_FILE
        )
        return templates.TemplateResponse(
            request,
            "partials/coverage_matrix.html",
            {
                "rows": rows,
                "scrapers": enabled,
                "health": health,
                "next_window": next_window,
                "sport": s,
            },
        )

    @app.get("/bets")
    async def bets_redirect() -> RedirectResponse:
        # /bets was replaced by /betting (P1 Phase 3). Keep old bookmarks working.
        return RedirectResponse("/betting")

    @app.get("/betting", response_class=HTMLResponse)
    async def betting(request: Request, sport: str | None = None) -> HTMLResponse:
        # When the write controls are live (flag on AND credentials set), the
        # PAGE itself challenges. It is a top-level navigation, so the browser
        # prompts and then attaches the credentials to every same-origin htmx
        # request from it — including the control POSTs. Without this the first
        # Arm click is an XHR that 401s, and htmx does not swap a non-2xx
        # response: the button would silently do nothing. Every other GET
        # (other tabs, the /betting partials, the /bets redirect) stays open,
        # and with no credentials configured this page stays public read-only.
        if _controls_available() and not _basic_auth_ok(request):
            raise HTTPException(
                status_code=401,
                detail="dashboard control authentication required",
                headers={"WWW-Authenticate": _CONTROLS_REALM},
            )
        return templates.TemplateResponse(
            request,
            "betting.html",
            {
                "page": "betting",
                "sport": _norm_sport(sport),
                "enabled_sports": ENABLED_SPORTS,
            },
        )

    async def _overview_response(
        request: Request, sport: str | None, *, saved: bool = False
    ):
        s = _norm_sport(sport)
        async with _store() as store:
            overview = await queries.betting_overview(store, sport=s)
        now = datetime.now(timezone.utc)
        hb, bridge = await asyncio.to_thread(
            lambda: (
                log_tail.latest_heartbeat(config.LOG_FILE),
                log_tail.latest_bridge_state(config.LOG_FILE),
            )
        )
        engine_state = _engine_state(hb, now)
        overview["betting"] = engine_state
        return templates.TemplateResponse(
            request,
            "partials/betting_overview.html",
            {
                "o": overview,
                "sport": s,
                "engine_state": engine_state,
                "chain": _betting_chain(
                    hb, bridge, engine_state, armed_meta=overview["armed"], now=now
                ),
                "saved": saved,
                "updated": now.astimezone(_DISPLAY_TZ).strftime("%H:%M:%S"),
                # Render the arm/disarm + caps FORM only to an authenticated viewer:
                # the overview partial itself stays public (read-only data), so an
                # unauthenticated stranger must not even see the controls.
                "controls_enabled": _controls_available() and _basic_auth_ok(request),
                "controls_flag_enabled": config.DASHBOARD_CONTROLS_ENABLED,
                "controls_auth_configured": _controls_auth_configured(),
                "gate_sports": list(ENABLED_SPORTS),
                "max_stake_czk": config.BETTING_MAX_STAKE_CZK,
                "max_bets_per_day_ceiling": config.BETTING_MAX_BETS_PER_DAY,
                "max_open_stakes_czk_ceiling": config.BETTING_MAX_OPEN_STAKES_CZK,
                "daily_loss_limit_czk_ceiling": config.BETTING_MAX_DAILY_LOSS_CZK,
            },
        )

    @app.get("/betting/partials/overview", response_class=HTMLResponse)
    async def betting_overview_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        return await _overview_response(request, sport)

    @app.get("/betting/partials/state-dot", response_class=HTMLResponse)
    async def betting_state_dot(request: Request) -> HTMLResponse:
        # Nav-wide engine dot. Empty response when no heartbeat has ever
        # carried a betting field (engine never ran) — no dot, no noise.
        hb = await asyncio.to_thread(log_tail.latest_heartbeat, config.LOG_FILE)
        if hb is None or hb.betting is None:
            return HTMLResponse("")
        state = _engine_state(hb, datetime.now(timezone.utc))
        return templates.TemplateResponse(
            request, "partials/state_dot.html", {"state": state}
        )

    @app.get("/betting/partials/placed-bets", response_class=HTMLResponse)
    async def betting_placed_bets_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            rows = await queries.placed_bets_table(store, limit=25, sport=s)
        return templates.TemplateResponse(
            request, "partials/placed_bets.html", {"rows": rows, "sport": s}
        )

    @app.get("/betting/partials/value-bets", response_class=HTMLResponse)
    async def betting_value_bets_partial(
        request: Request, sport: str | None = None, show: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        include_suspect = show == "suspect"
        base = [f"sport={s}"] if s else []
        self_params = base + (["show=suspect"] if include_suspect else [])
        self_qs = ("?" + "&".join(self_params)) if self_params else ""
        toggle_params = (base + ["show=suspect"]) if not include_suspect else list(base)
        toggle_qs = ("?" + "&".join(toggle_params)) if toggle_params else ""
        toggle_label = "show guarded too" if not include_suspect else "clean only"
        async with _store() as store:
            rows = await queries.recent_value_bets(
                store, limit=15, sport=s, include_suspect=include_suspect
            )
        return templates.TemplateResponse(
            request,
            "partials/recent_value_bets.html",
            {
                "rows": rows,
                "sport": s,
                "show": show,
                "self_qs": self_qs,
                "toggle_qs": toggle_qs,
                "toggle_label": toggle_label,
            },
        )

    def _require_controls(request: Request) -> None:
        """Authorize one money-affecting POST and reject cross-site submits."""
        if not config.DASHBOARD_CONTROLS_ENABLED:
            raise HTTPException(status_code=403, detail="dashboard controls disabled")
        if not _controls_auth_configured():
            raise HTTPException(
                status_code=503, detail="dashboard control credentials not configured"
            )
        if not _basic_auth_ok(request):
            raise HTTPException(
                status_code=401,
                detail="dashboard control authentication required",
                headers={"WWW-Authenticate": _CONTROLS_REALM},
            )
        source = request.headers.get("origin") or request.headers.get("referer")
        if not source or urlsplit(source).netloc != _expected_origin_host(request):
            raise HTTPException(
                status_code=403, detail="cross-origin control request rejected"
            )

    def _bounded_number(value: float, maximum: float, *, minimum: float = 0.0) -> float:
        if not math.isfinite(value):
            raise HTTPException(status_code=422, detail="control value must be finite")
        if maximum < minimum:
            raise HTTPException(status_code=503, detail="invalid hard control ceiling")
        return max(minimum, min(value, maximum))

    @app.post("/betting/arm", response_class=HTMLResponse)
    async def betting_arm(request: Request, sport: str | None = None) -> HTMLResponse:
        _require_controls(request)
        async with _store() as store:
            await store.set_meta("betting_armed", "1")
        logger_dash.info("betting_armed: via dashboard")
        return await _overview_response(request, sport)

    @app.post("/betting/disarm", response_class=HTMLResponse)
    async def betting_disarm(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        _require_controls(request)
        async with _store() as store:
            await store.set_meta("betting_armed", "0")
            await store.set_meta(
                "betting_disarm_reason",
                f"operator_dashboard @ {datetime.now(timezone.utc).isoformat()}",
            )
        logger_dash.info("betting_disarmed: via dashboard")
        return await _overview_response(request, sport)

    @app.post("/betting/config", response_class=HTMLResponse)
    async def betting_config(
        request: Request,
        sport: str | None = None,
        flat_stake_czk: float = Form(None),
        max_bets_per_day: int = Form(None),
        max_open_stakes_czk: float = Form(None),
        daily_loss_limit_czk: float = Form(None),
        betting_sports: list[str] = Form(None),
        betting_sports_present: str = Form(None),
    ) -> HTMLResponse:
        _require_controls(request)
        async with _store() as store:
            if betting_sports_present is not None:
                # Checkbox group: unchecked boxes aren't submitted, so the
                # hidden marker distinguishes "all unchecked" from a POST
                # without the section. Validate against ENABLED sports (the
                # rendered boxes), NOT all profiles — a profiled-but-disabled
                # key (e.g. nba) persisted now would silently become live at
                # its seasonal re-enable without an operator tick. Unknown
                # keys drop; empty falls back to mlb — the gate must never go
                # silently empty (disarm is the off switch, not this).
                sel = sorted(
                    {
                        s2.strip().lower()
                        for s2 in (betting_sports or [])
                        if s2.strip().lower() in ENABLED_SPORTS
                    }
                )
                await store.set_meta("betting_sports", ",".join(sel) if sel else "mlb")
            if flat_stake_czk is not None:
                # Clamp to the env hard ceiling — the dashboard can never set a
                # stake above BETTING_MAX_STAKE_CZK.
                stake = _bounded_number(
                    float(flat_stake_czk),
                    config.BETTING_MAX_STAKE_CZK,
                    minimum=1.0,
                )
                await store.set_meta("betting_flat_stake_czk", str(stake))
            if max_bets_per_day is not None:
                max_day = max(
                    0,
                    min(
                        int(max_bets_per_day),
                        config.BETTING_MAX_BETS_PER_DAY,
                    ),
                )
                await store.set_meta("betting_max_bets_per_day", str(max_day))
            if max_open_stakes_czk is not None:
                max_open = _bounded_number(
                    float(max_open_stakes_czk),
                    config.BETTING_MAX_OPEN_STAKES_CZK,
                )
                await store.set_meta("betting_max_open_stakes_czk", str(max_open))
            if daily_loss_limit_czk is not None:
                loss_limit = _bounded_number(
                    float(daily_loss_limit_czk),
                    config.BETTING_MAX_DAILY_LOSS_CZK,
                )
                await store.set_meta("betting_daily_loss_limit_czk", str(loss_limit))
        logger_dash.info("betting_config_updated: via dashboard")
        return await _overview_response(request, sport, saved=True)

    @app.get("/live/partials/activity-feed", response_class=HTMLResponse)
    async def activity_feed_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            db_events = await queries.recent_db_events(store, limit=25, sport=s)
        log_events = await asyncio.to_thread(
            log_tail.parse_event_lines, config.LOG_FILE, n=200
        )
        rows = _merge_activity_rows(db_events, log_events, limit=50)
        return templates.TemplateResponse(
            request,
            "partials/activity_feed.html",
            {"rows": rows, "sport": s},
        )

    @app.get("/live/partials/raw-logs", response_class=HTMLResponse)
    async def raw_logs_partial(request: Request) -> HTMLResponse:
        lines = log_tail.tail_lines(config.LOG_FILE, 200)
        return templates.TemplateResponse(
            request,
            "partials/raw_logs.html",
            {"lines": lines, "log_path": str(config.LOG_FILE)},
        )

    @app.get("/analysis/partials/big-numbers", response_class=HTMLResponse)
    async def big_numbers_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            n = await queries.big_numbers(store, sport=s)
        return templates.TemplateResponse(
            request,
            "partials/big_numbers.html",
            {"n": n, "sport": s},
        )

    @app.get("/analysis/partials/monthly", response_class=HTMLResponse)
    async def monthly_partial(
        request: Request,
        month: str | None = None,
        sport: str | None = None,
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            data = await queries.monthly_performance(store, month=month, sport=s)
        # "2026-06" → "Jun 2026" tab labels; the ym string stays the URL param.
        months = [
            (ym, datetime.strptime(ym, "%Y-%m").strftime("%b %Y"))
            for ym in data["months"]
        ]
        return templates.TemplateResponse(
            request,
            "partials/monthly_performance.html",
            {
                "months": months,
                "month": data["month"],
                "is_all": month == "all",
                "bets": data["bets"],
                "sport": s,
            },
        )

    @app.get("/analysis/partials/calendar", response_class=HTMLResponse)
    async def calendar_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            cal = await queries.activity_calendar(store, sport=s)
        return templates.TemplateResponse(
            request,
            "partials/activity_calendar.html",
            {"cal": cal, "sport": s},
        )

    @app.get("/analysis/partials/day", response_class=HTMLResponse)
    async def day_partial(
        request: Request,
        date: str,
        sport: str | None = None,
    ) -> HTMLResponse:
        try:
            day = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        s = _norm_sport(sport)
        async with _store() as store:
            d = await queries.day_detail(store, day, sport=s)
        return templates.TemplateResponse(
            request,
            "partials/day_detail.html",
            {"d": d, "sport": s},
        )

    @app.get("/analysis/partials/profit-curve", response_class=HTMLResponse)
    async def profit_curve_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            curve = await queries.profit_curve(store, sport=s)
        return templates.TemplateResponse(
            request,
            "partials/profit_curve.html",
            {"curve": curve, "sport": s},
        )

    @app.get("/analysis/partials/clv-trend", response_class=HTMLResponse)
    async def clv_trend_partial(
        request: Request,
        days: int | None = None,
        sport: str | None = None,
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            series = await queries.clv_trend(store, days=days, sport=s)
        return templates.TemplateResponse(
            request,
            "partials/clv_trend.html",
            {"series": series, "days": days, "sport": s},
        )

    @app.get("/analysis/partials/scoreboard", response_class=HTMLResponse)
    async def scoreboard_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        s = _norm_sport(sport)
        async with _store() as store:
            rows = await queries.per_book_scoreboard(store, sport=s)
        return templates.TemplateResponse(
            request,
            "partials/scoreboard.html",
            {"rows": rows, "sport": s},
        )

    @app.get("/analysis/partials/edge-buckets", response_class=HTMLResponse)
    async def edge_buckets_partial(
        request: Request, sport: str | None = None
    ) -> HTMLResponse:
        # One table for what used to be two sections over the SAME buckets:
        # the volume histogram (all flagged) + the calibration columns
        # (resolved-only break-even vs actual win rate).
        s = _norm_sport(sport)
        async with _store() as store:
            dist = await queries.edge_distribution(store, sport=s)
            cal = await queries.edge_calibration(store, sport=s)
        cal_by_bucket = {c["bucket"]: c for c in cal}
        max_count = max((d["count"] for d in dist), default=0) or 1
        rows = [
            {
                **d,
                "bar_pct": d["count"] * 100 // max_count,
                **{
                    k: cal_by_bucket.get(d["bucket"], {}).get(k)
                    for k in ("n", "expected_win", "actual_win", "delta", "thin")
                },
            }
            for d in dist
        ]
        return templates.TemplateResponse(
            request,
            "partials/edge_buckets.html",
            {"rows": rows, "sport": s},
        )

    @app.get("/analysis", response_class=HTMLResponse)
    async def analysis(request: Request, sport: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "analysis.html",
            {
                "page": "analysis",
                "sport": _norm_sport(sport),
                "enabled_sports": ENABLED_SPORTS,
            },
        )

    @app.get("/game/{game_id}", response_class=HTMLResponse)
    async def game_page(request: Request, game_id: int) -> HTMLResponse:
        async with _store() as store:
            detail = await queries.game_detail(store, game_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="unknown game")
        return templates.TemplateResponse(
            request,
            "game.html",
            {
                "page": "game",
                "sport": None,
                "enabled_sports": ENABLED_SPORTS,
                "d": detail,
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request, sport: str | None = None) -> HTMLResponse:
        # Guard against a corrupt/zero-byte DB file: `meta` normally exists after
        # migrations.run() (dashboard launch) and get_meta() returns None for
        # absent keys, but a truncated DB raises OperationalError — keep /settings
        # rendering its config paths regardless (see test_settings_page_shows_config_paths).
        import sqlite3 as _sqlite3
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _dist_version
        from oddsiq.pipeline.budget_governor import throttle_intervals

        try:
            _pkg_version = _dist_version("oddsiq")
        except PackageNotFoundError:
            _pkg_version = "unknown"
        used = None
        remaining = None
        schema_version = None
        budget_history = None
        async with _store() as store:
            try:
                used = await store.get_meta("odds_api_budget_used")
                remaining = await store.get_meta("odds_api_budget_remaining")
                budget_history = _parse_budget_history(
                    await store.get_meta("odds_api_budget_history")
                )
                cur = await store._db.execute(
                    "SELECT MAX(version) AS v FROM schema_version"
                )
                row = await cur.fetchone()
                schema_version = row["v"] if row else None
            except _sqlite3.OperationalError:
                pass
        now = datetime.now(timezone.utc)
        hb = await asyncio.to_thread(log_tail.latest_heartbeat, config.LOG_FILE)
        paths = {
            "db": str(config.DATABASE_PATH),
            "db_size": _safe_size(config.DATABASE_PATH),
            "log": str(config.LOG_FILE),
            "log_size": _safe_size(config.LOG_FILE),
            "raw": str(config.RAW_DATA_DIR),
            "raw_size": _dir_size(config.RAW_DATA_DIR),
            "backup": str(config.BACKUP_DIR),
            "backup_count": _count_files(config.BACKUP_DIR, "*.db"),
        }
        level = (
            hb.budget_throttle
            if hb is not None and hb.budget_throttle is not None
            else 0
        )
        eff = throttle_intervals(level)
        governor = {
            "budget": _budget_context(
                int(used) if used is not None else None,
                int(remaining) if remaining is not None else None,
                now=now,
                history=budget_history,
            ),
            "budget_used": int(used) if used is not None else None,
            "budget_remaining": int(remaining) if remaining is not None else None,
            "throttle_level": hb.budget_throttle if hb is not None else None,
            # The cadence ACTUALLY running right now (base config throttled by
            # the governor level) — showing base "hot 90s" at L2 was a lie.
            "effective_cadence": (f"warm {eff['warm']}s / hot {eff['hot']}s"),
            "base_cadence": (
                f"warm {config.POLL_TIER_WARM}s / hot {config.POLL_TIER_HOT}s"
            ),
        }
        import platform as _platform

        system = {
            "version": _pkg_version,
            "python": _platform.python_version(),
            "schema_version": schema_version,
            "heartbeat_state": hb.state if hb is not None else None,
            "heartbeat_age_s": int((now - hb.timestamp).total_seconds())
            if hb
            else None,
        }
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "page": "settings",
                "sport": _norm_sport(sport),
                "enabled_sports": ENABLED_SPORTS,
                "config_sections": _settings_config_sections(),
                "telegram_configured": telegram.is_configured(),
                "paths": paths,
                "governor": governor,
                "system": system,
            },
        )

    return app


def run_dashboard(*, host: str = "127.0.0.1", port: int = 8080) -> int:
    """Start the dashboard on ``host:port`` via uvicorn.

    Blocks until the server exits. Returns 0 on a clean shutdown.
    Intended to be called from the ``oddsiq dashboard`` CLI subcommand.
    """
    import uvicorn  # local import keeps test_cli.py argparse tests fast
    from oddsiq.db import migrations

    # Apply pending migrations so the dashboard is self-sufficient even if the
    # pipeline hasn't run since the last schema change — /betting selects
    # placed_bets (migration 010) and every reporting query filters
    # excluded_reason (007); both would 500 against an un-migrated DB.
    migrations.run(config.DATABASE_PATH)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0
