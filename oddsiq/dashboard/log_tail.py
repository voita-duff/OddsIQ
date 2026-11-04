"""Log-file helpers for the OddsIQ dashboard.

The dashboard tails ``logs/oddsiq.log`` to (a) show recent lines in the
``/live`` raw-logs panel and (b) parse the latest scheduler heartbeat
for the pipeline-state determination. The log file is shared by every
OddsIQ process (main pipeline, ``oddsiq dashboard``, smoke subcommands,
audit), so heartbeat parsing must filter on any oddsiq pipeline logger's
``heartbeat:`` line (the emitter moved to ``oddsiq.pipeline.scheduler``
in the P4 decomposition) rather than trusting the last line.

This module is read-only and side-effect-free. It owns no file handles;
each call opens, reads, and closes.
"""

from __future__ import annotations

import ast
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_HEARTBEAT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+\-]\d{4}) \[(?P<level>\w+)\] "
    r"oddsiq\.[\w.]+: heartbeat: state=(?P<state>\S+) "
    r"games_tracked=(?P<games>\d+) seconds_since_last_poll=(?P<secs>-?\d+)"
    r"(?: budget_throttle=(?P<throttle>\d+))?"
    r"(?: betting=(?P<betting>\S+))?"
)
_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S %z"


@dataclass(frozen=True)
class Heartbeat:
    """Parsed fields from a scheduler ``heartbeat:`` log line."""
    timestamp: datetime
    state: str
    games_tracked: int
    seconds_since_last_poll: int
    budget_throttle: int | None = None
    betting: str | None = None   # off | disconnected | disarmed | armed (Phase 1+)


def tail_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path``.

    Returns ``[]`` if the file is missing or empty. Trailing newlines
    are stripped from each returned line.
    """
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return list(deque((line.rstrip("\n") for line in fh), maxlen=n))


def _parse_heartbeat_line(line: str) -> Heartbeat | None:
    """Parse one ``heartbeat:`` log line, or None if it doesn't match."""
    match = _HEARTBEAT_RE.match(line)
    if not match:
        return None
    throttle_raw = match.group("throttle")
    return Heartbeat(
        timestamp=datetime.strptime(match.group("ts"), _TIMESTAMP_FMT),
        state=match.group("state"),
        games_tracked=int(match.group("games")),
        seconds_since_last_poll=int(match.group("secs")),
        budget_throttle=int(throttle_raw) if throttle_raw is not None else None,
        betting=match.group("betting"),
    )


def latest_heartbeat(path: Path) -> Heartbeat | None:
    """Scan ``path`` from the bottom for the most recent
    ``oddsiq.pipeline.scheduler: heartbeat: ...`` line (or any other
    oddsiq pipeline logger's ``heartbeat:`` line) and parse it.

    Returns ``None`` when the file is missing or contains no pipeline
    heartbeat lines. Other loggers writing to the same file (dashboard
    process, audit subcommand, etc.) are ignored.
    """
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for line in reversed(lines):
        hb = _parse_heartbeat_line(line)
        if hb is not None:
            return hb
    return None


@dataclass(frozen=True)
class ScraperHealth:
    """Parsed fields from a scheduler ``Scraper health:`` log line."""
    timestamp: datetime
    name: str
    duration_ms: int
    snapshots: int
    matched_snapshots: int
    matched_games: int
    other_games: int
    unmatched_snapshots: int
    errors: int
    unsafe_start_snapshots: int = 0
    sharp_source_age_max_ms: int = 0
    missing_source_updates: int = 0
    missing_event_ids: int = 0
    event_id_mismatches: int = 0


_SCRAPER_HEALTH_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+\-]\d{4}) \[\w+\] "
    r"oddsiq\.[\w.]+: Scraper health: name=(?P<name>\S+) "
    r"duration_ms=(?P<dur>\d+) snapshots=(?P<sn>\d+) "
    r"matched_snapshots=(?P<ms>\d+) matched_games=(?P<mg>\d+) "
    r"other_games=(?P<og>\d+) unmatched_snapshots=(?P<us>\d+) errors=(?P<err>\d+)"
    r"(?: unsafe_start_snapshots=(?P<unsafe>\d+))?"
    r"(?: sharp_source_age_max_ms=(?P<source_age>\d+)"
    r" missing_source_updates=(?P<missing_source>\d+)"
    r" missing_event_ids=(?P<missing_ids>\d+)"
    r" event_id_mismatches=(?P<id_mismatch>\d+))?"
)


def _parse_health_line(line: str) -> ScraperHealth | None:
    """Parse one ``Scraper health:`` log line, or None if it doesn't match."""
    match = _SCRAPER_HEALTH_RE.match(line)
    if not match:
        return None
    return ScraperHealth(
        timestamp=datetime.strptime(match.group("ts"), _TIMESTAMP_FMT),
        name=match.group("name"),
        duration_ms=int(match.group("dur")),
        snapshots=int(match.group("sn")),
        matched_snapshots=int(match.group("ms")),
        matched_games=int(match.group("mg")),
        other_games=int(match.group("og")),
        unmatched_snapshots=int(match.group("us")),
        errors=int(match.group("err")),
        unsafe_start_snapshots=int(match.group("unsafe") or 0),
        sharp_source_age_max_ms=int(match.group("source_age") or 0),
        missing_source_updates=int(match.group("missing_source") or 0),
        missing_event_ids=int(match.group("missing_ids") or 0),
        event_id_mismatches=int(match.group("id_mismatch") or 0),
    )


def latest_scraper_health_by_name(path: Path) -> dict[str, ScraperHealth]:
    """Walk the log bottom-up and return the most recent
    ``Scraper health:`` line per scraper name.

    Empty dict when the file is missing or contains no scraper-health
    lines. The dashboard's status-strip card and coverage-matrix header
    both query this.
    """
    out: dict[str, ScraperHealth] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for line in reversed(lines):
        health = _parse_health_line(line)
        if health is not None and health.name not in out:
            out[health.name] = health
    return out


def scan_log_state(path: Path) -> tuple[Heartbeat | None, dict[str, ScraperHealth]]:
    """Single bottom-up pass returning ``(latest_heartbeat,
    latest_scraper_health_by_name)``.

    Equivalent to calling the two parsers separately, but reads the file ONCE.
    The ``/live`` status strip needs both (plus the heartbeat again for the
    pipeline-state derivation) every refresh, so this collapses 2-3 full reads of
    a multi-MB log into one — and the caller offloads it via ``asyncio.to_thread``
    to keep the blocking read off the event loop.
    """
    heartbeat: Heartbeat | None = None
    health: dict[str, ScraperHealth] = {}
    if not path.exists():
        return heartbeat, health
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for line in reversed(lines):
        if heartbeat is None:
            hb = _parse_heartbeat_line(line)
            if hb is not None:
                heartbeat = hb
                continue
        h = _parse_health_line(line)
        if h is not None and h.name not in health:
            health[h.name] = h
    return heartbeat, health


@dataclass(frozen=True)
class LogEvent:
    """A parsed log line with one of the known event prefixes.

    ``event_type`` is the prefix normalised: lowercase, spaces replaced
    by underscores, trailing colon stripped (e.g. ``Scraper health:`` →
    ``scraper_health``).

    ``fields`` is a best-effort extraction of ``key=value`` pairs from
    the body. Quoted values are unquoted so team names such as
    ``away="Miami Heat"`` survive as ``Miami Heat``.
    """
    timestamp: datetime
    level: str
    event_type: str
    fields: dict[str, str]
    raw: str


_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+\-]\d{4}) "
    r"\[(?P<level>\w+)\] (?P<logger>\S+): (?P<rest>.*)$"
)
_KV_RE = re.compile(r"(\w+)=((?:\"(?:\\.|[^\"])*\")|(?:'(?:\\.|[^'])*')|\S*)")


def _parse_kv_value(raw: str) -> str:
    if raw.startswith(("'", '"')):
        try:
            parsed = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return raw.strip("\"'")
        return parsed if isinstance(parsed, str) else str(parsed)
    return raw

# Prefixes recognised by the activity-feed parser. Order matters for
# longest-match-first (e.g. ``value_bet_suspect:`` before ``value_bet:``).
_EVENT_PREFIXES: tuple[str, ...] = (
    "value_bet_suspect:", "value_bet:", "clv:", "result:",
    "game_live:", "game_final:",
    "heartbeat:",
    "Scraper health:", "Coverage gap:",
)


def parse_event_lines(path: Path, *, n: int = 200) -> list[LogEvent]:
    """Parse the last ``n`` lines of ``path``, returning one
    :class:`LogEvent` per line whose body starts with a recognised
    event prefix. Other lines (info noise, dashboard HTTP requests,
    audit output) are skipped.
    """
    out: list[LogEvent] = []
    for line in tail_lines(path, n):
        match = _LINE_RE.match(line)
        if not match:
            continue
        rest = match.group("rest")
        prefix = None
        for candidate in _EVENT_PREFIXES:
            if rest.startswith(candidate):
                prefix = candidate
                break
        if prefix is None:
            continue
        event_type = prefix.rstrip(":").lower().replace(" ", "_")
        body = rest[len(prefix):].strip()
        fields = {key: _parse_kv_value(value) for key, value in _KV_RE.findall(body)}
        out.append(LogEvent(
            timestamp=datetime.strptime(match.group("ts"), _TIMESTAMP_FMT),
            level=match.group("level"),
            event_type=event_type,
            fields=fields,
            raw=line,
        ))
    return out


@dataclass(frozen=True)
class BridgeState:
    """Last-known betting-bridge/extension state, scraped from the shared log.

    The dashboard runs in a separate process from the pipeline, so the
    ``/betting`` safety-chain widget cannot ask the live ``BettingBridge``
    object — it reconstructs the extension's state from the bridge's own
    log lines instead (connected / status / disconnected / supersede).
    """
    last_connected: datetime | None = None
    version: str | None = None
    logged_in: bool | None = None
    logged_in_at: datetime | None = None
    last_disconnected: datetime | None = None
    last_supersede: datetime | None = None


_BRIDGE_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+\-]\d{4}) \[\w+\] "
    r"oddsiq\.[\w.]+: betting_bridge: (?P<body>.*)$"
)
_BRIDGE_CONNECTED_RE = re.compile(
    r"^extension connected \(version=(?P<version>\S+) logged_in=(?P<li>True|False)\)"
)
_BRIDGE_STATUS_RE = re.compile(r"^status logged_in=(?P<li>True|False)")


def latest_bridge_state(path: Path) -> BridgeState | None:
    """Scan the log bottom-up for the latest betting-bridge facts.

    Returns ``None`` when the file is missing or contains no
    ``betting_bridge:`` lines at all (bridge never ran). ``logged_in``
    reflects the LATEST of a ``status logged_in=`` update or the hello's
    ``extension connected (... logged_in=...)`` — whichever appears last.
    """
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    seen_any = False
    last_connected = version = None
    logged_in = logged_in_at = None
    last_disconnected = last_supersede = None
    for line in reversed(lines):
        m = _BRIDGE_LINE_RE.match(line)
        if not m:
            continue
        seen_any = True
        ts = datetime.strptime(m.group("ts"), _TIMESTAMP_FMT)
        body = m.group("body")
        cm = _BRIDGE_CONNECTED_RE.match(body)
        if cm is not None:
            if last_connected is None:
                last_connected = ts
                version = cm.group("version")
            if logged_in is None:
                logged_in = cm.group("li") == "True"
                logged_in_at = ts
            continue
        sm = _BRIDGE_STATUS_RE.match(body)
        if sm is not None:
            if logged_in is None:
                logged_in = sm.group("li") == "True"
                logged_in_at = ts
            continue
        if body.startswith("extension disconnected") and last_disconnected is None:
            last_disconnected = ts
            continue
        if body.startswith("new client supersedes") and last_supersede is None:
            last_supersede = ts
    if not seen_any:
        return None
    return BridgeState(
        last_connected=last_connected,
        version=version,
        logged_in=logged_in,
        logged_in_at=logged_in_at,
        last_disconnected=last_disconnected,
        last_supersede=last_supersede,
    )
