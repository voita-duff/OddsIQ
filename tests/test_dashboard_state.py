"""Tests for the dashboard's pipeline-state derivation.

``derive_pipeline_state`` combines log freshness (newest scheduler
heartbeat timestamp) with the heartbeat's self-reported state to
produce one of four user-facing states. The rules are documented in
the P3 design spec under "Pipeline alive determination".
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from oddsiq.dashboard.state import derive_pipeline_state


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def _heartbeat_line(when: datetime, *, state: str, games: int = 0, secs: int = -1) -> str:
    ts = when.strftime("%Y-%m-%d %H:%M:%S %z")
    return (
        f"{ts} [INFO] oddsiq.pipeline.scheduler: heartbeat: state={state} "
        f"games_tracked={games} seconds_since_last_poll={secs}"
    )


def test_alive_when_heartbeat_recent_and_polling(tmp_path):
    p = tmp_path / "x.log"
    now = datetime.now(timezone.utc)
    _write_log(p, [_heartbeat_line(now, state="polling", games=2, secs=5)])
    assert derive_pipeline_state(p, now=now) == "alive"


def test_alive_when_heartbeat_recent_and_between_game_windows(tmp_path):
    p = tmp_path / "x.log"
    now = datetime.now(timezone.utc)
    _write_log(p, [_heartbeat_line(now, state="between-game-windows", games=3, secs=120)])
    assert derive_pipeline_state(p, now=now) == "alive"


def test_idle_no_games_when_heartbeat_says_so(tmp_path):
    p = tmp_path / "x.log"
    now = datetime.now(timezone.utc)
    _write_log(p, [_heartbeat_line(now, state="idle-no-games")])
    assert derive_pipeline_state(p, now=now) == "idle-no-games"


def test_stale_when_heartbeat_5min_old(tmp_path):
    p = tmp_path / "x.log"
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=5)
    _write_log(p, [_heartbeat_line(past, state="polling", games=2, secs=5)])
    assert derive_pipeline_state(p, now=now) == "stale"


def test_stale_when_heartbeat_just_over_2min_old(tmp_path):
    p = tmp_path / "x.log"
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=2, seconds=30)
    _write_log(p, [_heartbeat_line(past, state="polling", games=2, secs=5)])
    assert derive_pipeline_state(p, now=now) == "stale"


def test_unknown_when_heartbeat_15min_old(tmp_path):
    p = tmp_path / "x.log"
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=15)
    _write_log(p, [_heartbeat_line(past, state="polling", games=2, secs=5)])
    assert derive_pipeline_state(p, now=now) == "unknown"


def test_unknown_when_log_has_no_heartbeat_lines(tmp_path):
    p = tmp_path / "x.log"
    _write_log(p, [
        "2026-05-28 01:00:00 +0000 [INFO] oddsiq.dashboard.server: GET /",
        "2026-05-28 01:00:01 +0000 [INFO] oddsiq.audit: query ran",
    ])
    assert derive_pipeline_state(p, now=datetime.now(timezone.utc)) == "unknown"


def test_unknown_when_log_file_missing(tmp_path):
    assert derive_pipeline_state(tmp_path / "absent.log",
                                  now=datetime.now(timezone.utc)) == "unknown"
