"""Pipeline-state derivation for the OddsIQ dashboard.

Combines the latest scheduler heartbeat line (from the log file) with
its self-reported state into one of four user-facing states rendered by
the ``/live`` status strip. See the P3 design spec under
"Pipeline alive determination" for the rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from oddsiq.dashboard.log_tail import latest_heartbeat

_FRESH_SECONDS = 2 * 60
_UNKNOWN_SECONDS = 10 * 60
_UNSET = object()


def derive_pipeline_state(
    log_path: Path, *, now: datetime | None = None, heartbeat=_UNSET
) -> str:
    """Return one of: ``alive`` / ``idle-no-games`` / ``stale`` / ``unknown``.

    Reads the latest ``oddsiq.pipeline.scheduler: heartbeat:`` line (or
    any other oddsiq pipeline logger's ``heartbeat:`` line) from
    ``log_path`` (via :func:`oddsiq.dashboard.log_tail.latest_heartbeat`)
    and applies the four-state mapping:

    | Heartbeat age   | Heartbeat state                                | Result          |
    | --------------- | ---------------------------------------------- | --------------- |
    | within 2 min    | ``polling`` or ``between-game-windows``        | ``alive``       |
    | within 2 min    | ``idle-no-games``                              | ``idle-no-games`` |
    | 2–10 min        | (any)                                          | ``stale``       |
    | > 10 min        | (any)                                          | ``unknown``     |
    | (no heartbeat)  | —                                              | ``unknown``     |

    Other CLI subcommands and the dashboard process itself also write
    to ``logs/oddsiq.log``; the underlying heartbeat parser filters on
    any oddsiq pipeline logger's ``heartbeat:`` prefix so this function
    is immune to log-file contamination.

    ``now`` is injectable for testing; defaults to ``datetime.now(UTC)``.
    ``heartbeat`` is injectable so a caller that already scanned the log (e.g.
    the status strip via ``scan_log_state``) can avoid a second read; when not
    provided it reads the latest heartbeat itself.
    """
    now = now or datetime.now(timezone.utc)
    hb = latest_heartbeat(log_path) if heartbeat is _UNSET else heartbeat
    if hb is None:
        return "unknown"
    age_s = (now - hb.timestamp).total_seconds()
    if age_s > _UNKNOWN_SECONDS:
        return "unknown"
    if age_s > _FRESH_SECONDS:
        return "stale"
    if hb.state == "idle-no-games":
        return "idle-no-games"
    if hb.state in ("polling", "between-game-windows"):
        return "alive"
    return "unknown"
