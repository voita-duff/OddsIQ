"""Tests for the dashboard's log-file tailing + heartbeat parser.

The dashboard reads ``logs/oddsiq.log`` to (a) tail the last N lines for
the raw-logs panel and (b) parse the latest scheduler heartbeat line for
the pipeline-state determination. Both must be robust against:
- Missing file (pre-start, deleted)
- Empty file
- Interleaved lines from other CLI subcommands or the dashboard process
  itself writing to the same log
"""

from datetime import datetime, timezone

from oddsiq.dashboard.log_tail import tail_lines, latest_heartbeat


def test_tail_lines_returns_last_n_lines(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    lines = tail_lines(p, 5)
    assert lines == [f"line {i}" for i in range(45, 50)]


def test_tail_lines_returns_all_when_fewer_than_n(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("a\nb\nc\n")
    assert tail_lines(p, 10) == ["a", "b", "c"]


def test_tail_lines_handles_missing_file(tmp_path):
    assert tail_lines(tmp_path / "absent.log", 10) == []


def test_tail_lines_handles_empty_file(tmp_path):
    p = tmp_path / "empty.log"
    p.touch()
    assert tail_lines(p, 10) == []


def test_latest_heartbeat_finds_most_recent_scheduler_heartbeat(tmp_path):
    p = tmp_path / "x.log"
    p.write_text(
        "2026-05-28 01:00:00 +0000 [INFO] oddsiq.pipeline.scheduler: heartbeat: "
        "state=idle-no-games games_tracked=0 seconds_since_last_poll=-1\n"
        "2026-05-28 01:00:30 +0000 [INFO] oddsiq.dashboard.server: GET /live\n"
        "2026-05-28 01:01:00 +0000 [INFO] oddsiq.pipeline.scheduler: heartbeat: "
        "state=polling games_tracked=3 seconds_since_last_poll=12\n"
    )
    hb = latest_heartbeat(p)
    assert hb is not None
    assert hb.state == "polling"
    assert hb.games_tracked == 3
    assert hb.seconds_since_last_poll == 12
    assert hb.timestamp == datetime(2026, 5, 28, 1, 1, 0, tzinfo=timezone.utc)


def test_latest_heartbeat_ignores_non_scheduler_lines(tmp_path):
    """The dashboard and other CLI subcommands write to the same log file.

    We can't trust just the last line — must filter on the
    ``<oddsiq pipeline logger>: heartbeat:`` shape specifically (the emitter
    is ``oddsiq.pipeline.scheduler`` post-decomposition; the parser matches any
    ``oddsiq.*`` logger so it survives the emitter moving between modules).
    """
    p = tmp_path / "x.log"
    p.write_text(
        "2026-05-28 02:00:00 +0000 [INFO] oddsiq.pipeline.scheduler: heartbeat: "
        "state=alive games_tracked=1 seconds_since_last_poll=5\n"
        "2026-05-28 02:01:00 +0000 [INFO] oddsiq.dashboard.server: GET /live\n"
        "2026-05-28 02:02:00 +0000 [INFO] oddsiq.audit.bookmakers: query ran\n"
    )
    hb = latest_heartbeat(p)
    assert hb is not None
    assert hb.timestamp == datetime(2026, 5, 28, 2, 0, 0, tzinfo=timezone.utc)


def test_latest_heartbeat_returns_none_when_no_heartbeat_lines(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("no heartbeats here\nanother line\n")
    assert latest_heartbeat(p) is None


def test_latest_heartbeat_returns_none_when_file_missing(tmp_path):
    assert latest_heartbeat(tmp_path / "absent.log") is None


def test_parse_scraper_health_returns_latest_per_name(tmp_path):
    p = tmp_path / "x.log"
    p.write_text(
        "2026-05-28 02:14:33 +0000 [INFO] oddsiq.pipeline.poll_coordinator: Scraper health: "
        "name=pinnacle duration_ms=412 snapshots=18 matched_snapshots=18 "
        "matched_games=9 other_games=0 unmatched_snapshots=0 errors=0\n"
        "2026-05-28 02:14:34 +0000 [INFO] oddsiq.pipeline.poll_coordinator: Scraper health: "
        "name=tipsport duration_ms=187 snapshots=18 matched_snapshots=18 "
        "matched_games=9 other_games=0 unmatched_snapshots=0 errors=0\n"
        "2026-05-28 02:16:33 +0000 [INFO] oddsiq.pipeline.poll_coordinator: Scraper health: "
        "name=pinnacle duration_ms=523 snapshots=20 matched_snapshots=20 "
        "matched_games=10 other_games=0 unmatched_snapshots=0 errors=0\n"
    )
    from oddsiq.dashboard.log_tail import latest_scraper_health_by_name
    health = latest_scraper_health_by_name(p)
    assert set(health) == {"pinnacle", "tipsport"}
    # pinnacle has TWO entries; the latest (02:16:33) wins
    assert health["pinnacle"].duration_ms == 523
    assert health["pinnacle"].matched_games == 10
    assert health["pinnacle"].errors == 0
    assert health["tipsport"].duration_ms == 187


def test_parse_scraper_health_reads_sharp_provenance_metrics(tmp_path):
    p = tmp_path / "x.log"
    p.write_text(
        "2026-09-06 12:00:00 +0000 [INFO] oddsiq.pipeline.poll_coordinator: "
        "Scraper health: name=pinnacle duration_ms=300 snapshots=4 "
        "matched_snapshots=4 matched_games=2 other_games=0 "
        "unmatched_snapshots=0 errors=0 unsafe_start_snapshots=0 "
        "sharp_source_age_max_ms=81234 missing_source_updates=1 "
        "missing_event_ids=2 event_id_mismatches=3\n",
        encoding="utf-8",
    )
    from oddsiq.dashboard.log_tail import latest_scraper_health_by_name
    h = latest_scraper_health_by_name(p)["pinnacle"]
    assert h.sharp_source_age_max_ms == 81234
    assert h.missing_source_updates == 1
    assert h.missing_event_ids == 2
    assert h.event_id_mismatches == 3


def test_parse_scraper_health_handles_missing_file(tmp_path):
    from oddsiq.dashboard.log_tail import latest_scraper_health_by_name
    assert latest_scraper_health_by_name(tmp_path / "absent.log") == {}


def test_parse_event_lines_recognises_all_prefixes(tmp_path):
    p = tmp_path / "x.log"
    p.write_text(
        "2026-05-28 02:00:00 +0000 [INFO] oddsiq.pipeline.edge_service: value_bet: bookmaker=tipsport game_id=1 market=moneyline selection=home line= edge_pct=5.31 entry_odds=2.05 fair_odds=1.95 tier=hot\n"
        "2026-05-28 02:01:00 +0000 [INFO] oddsiq.pipeline.settlement_service: clv: bookmaker=tipsport game_id=1 market=moneyline selection=away line= clv_pct=3.20\n"
        "2026-05-28 02:02:00 +0000 [INFO] oddsiq.pipeline.settlement_service: result: bookmaker=tipsport game_id=1 market=moneyline selection=away line= result=loss clv_pct=3.20\n"
        "2026-05-28 02:03:00 +0000 [INFO] oddsiq.pipeline.settlement_service: game_live: game_id=1 away=Boston home=Miami\n"
        "2026-05-28 02:04:00 +0000 [INFO] oddsiq.pipeline.schedule_service: game_final: game_id=1 away=Boston home=Miami away_score=108 home_score=112\n"
        "2026-05-28 02:05:00 +0000 [INFO] oddsiq.pipeline.scheduler: heartbeat: state=polling games_tracked=3 seconds_since_last_poll=12\n"
        "2026-05-28 02:06:00 +0000 [INFO] oddsiq.pipeline.poll_coordinator: Scraper health: name=pinnacle duration_ms=412 snapshots=18 matched_snapshots=18 matched_games=9 other_games=0 unmatched_snapshots=0 errors=0\n"
        "2026-05-28 02:07:00 +0000 [WARNING] oddsiq.pipeline.coverage_monitor: Coverage gap: game_id=1 scraper=tipsport tier=hot minutes_stale=12\n"
    )
    from oddsiq.dashboard.log_tail import parse_event_lines
    events = parse_event_lines(p, n=200)
    types = [e.event_type for e in events]
    assert "value_bet" in types
    assert "clv" in types
    assert "result" in types
    assert "game_live" in types
    assert "game_final" in types
    assert "heartbeat" in types
    assert "scraper_health" in types
    assert "coverage_gap" in types
    # Check fields extraction for a few specific event types
    vb = next(e for e in events if e.event_type == "value_bet")
    assert vb.fields["bookmaker"] == "tipsport"
    assert vb.fields["edge_pct"] == "5.31"
    cg = next(e for e in events if e.event_type == "coverage_gap")
    assert cg.fields["scraper"] == "tipsport"
    assert cg.level == "WARNING"


def test_parse_event_lines_preserves_quoted_team_names(tmp_path):
    p = tmp_path / "x.log"
    p.write_text(
        '2026-05-28 02:03:00 +0000 [INFO] oddsiq.pipeline.settlement_service: '
        'game_live: game_id=1 away="Los Angeles Lakers" home="Boston Celtics"\n'
    )
    from oddsiq.dashboard.log_tail import parse_event_lines
    event = parse_event_lines(p, n=200)[0]
    assert event.fields["away"] == "Los Angeles Lakers"
    assert event.fields["home"] == "Boston Celtics"


def test_parse_event_lines_ignores_unknown_prefixes(tmp_path):
    p = tmp_path / "x.log"
    p.write_text(
        "2026-05-28 02:00:00 +0000 [INFO] oddsiq.pipeline.scheduler: starting scheduler\n"
        "2026-05-28 02:00:01 +0000 [INFO] oddsiq.dashboard.server: GET /live\n"
        "2026-05-28 02:00:02 +0000 [INFO] oddsiq.pipeline.scheduler: heartbeat: state=polling games_tracked=0 seconds_since_last_poll=-1\n"
    )
    from oddsiq.dashboard.log_tail import parse_event_lines
    events = parse_event_lines(p, n=200)
    assert len(events) == 1
    assert events[0].event_type == "heartbeat"


def test_parse_event_lines_handles_missing_file(tmp_path):
    from oddsiq.dashboard.log_tail import parse_event_lines
    assert parse_event_lines(tmp_path / "absent.log", n=200) == []


def test_heartbeat_parses_budget_throttle(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("2026-06-04 12:00:00 +0000 [INFO] oddsiq.pipeline.scheduler: "
                 "heartbeat: state=polling games_tracked=3 seconds_since_last_poll=12 budget_throttle=2\n")
    hb = latest_heartbeat(p)
    assert hb is not None and hb.budget_throttle == 2


def test_heartbeat_without_throttle_still_parses(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("2026-06-04 12:00:00 +0000 [INFO] oddsiq.pipeline.scheduler: "
                 "heartbeat: state=polling games_tracked=3 seconds_since_last_poll=12\n")
    hb = latest_heartbeat(p)
    assert hb is not None and hb.budget_throttle is None


def test_heartbeat_parses_betting_state(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("2026-08-09 12:00:00 +0000 [INFO] oddsiq.pipeline.scheduler: "
                 "heartbeat: state=polling games_tracked=3 seconds_since_last_poll=12 "
                 "budget_throttle=3 betting=disarmed\n")
    hb = latest_heartbeat(p)
    assert hb is not None and hb.budget_throttle == 3 and hb.betting == "disarmed"


def test_heartbeat_without_betting_field_is_none(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("2026-06-04 12:00:00 +0000 [INFO] oddsiq.pipeline.scheduler: "
                 "heartbeat: state=polling games_tracked=3 seconds_since_last_poll=12 budget_throttle=2\n")
    hb = latest_heartbeat(p)
    assert hb is not None and hb.betting is None


def test_parse_event_lines_respects_n_window(tmp_path):
    p = tmp_path / "x.log"
    lines = []
    for i in range(20):
        lines.append(f"2026-05-28 02:{i:02d}:00 +0000 [INFO] oddsiq.pipeline.scheduler: heartbeat: state=polling games_tracked=0 seconds_since_last_poll=-1")
    p.write_text("\n".join(lines) + "\n")
    from oddsiq.dashboard.log_tail import parse_event_lines
    events = parse_event_lines(p, n=5)
    assert len(events) == 5


def test_scan_log_state_matches_individual_parsers(tmp_path):
    """scan_log_state does ONE pass returning the same (heartbeat, health) as the
    two single-purpose parsers (used by the status strip to avoid 3 reads/req)."""
    from oddsiq.dashboard import log_tail
    p = tmp_path / "x.log"
    p.write_text(
        "2026-06-08 20:00:00 +0000 [INFO] oddsiq.pipeline.scheduler: heartbeat: "
        "state=polling games_tracked=3 seconds_since_last_poll=12\n"
        "2026-06-08 20:00:05 +0000 [INFO] oddsiq.pipeline.poll_coordinator: Scraper health: "
        "name=pinnacle duration_ms=120 snapshots=4 matched_snapshots=4 matched_games=2 "
        "other_games=0 unmatched_snapshots=0 errors=0\n"
    )
    hb, health = log_tail.scan_log_state(p)
    assert hb == log_tail.latest_heartbeat(p)
    assert health == log_tail.latest_scraper_health_by_name(p)
    assert hb is not None and "pinnacle" in health


def test_scan_log_state_missing_file_returns_empty(tmp_path):
    from oddsiq.dashboard import log_tail
    hb, health = log_tail.scan_log_state(tmp_path / "nope.log")
    assert hb is None and health == {}


def test_parse_event_lines_recognizes_value_bet_suspect(tmp_path):
    """Suspect (guard-stamped) detections must surface in the activity feed:
    since 2026-08-07 EVERY detection has been guard-stamped, and with no
    ``value_bet_suspect`` event the dashboard showed a dead detector."""
    from oddsiq.dashboard.log_tail import parse_event_lines
    p = tmp_path / "x.log"
    p.write_text(
        "2026-08-11 03:10:00 +0000 [INFO] oddsiq.pipeline.edge_service: "
        "value_bet_suspect: bookmaker=tipsport game_id=42 market=moneyline "
        "selection=home edge_pct=12.30 entry_odds=3.10 fair_odds=2.76 "
        "tier=hot reason=incoherent_book_pregame\n"
    )
    events = parse_event_lines(p, n=10)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "value_bet_suspect"
    assert ev.fields["reason"] == "incoherent_book_pregame"
    assert ev.fields["edge_pct"] == "12.30"
    assert ev.fields["tier"] == "hot"


def test_latest_bridge_state_parses_connect_status_and_supersede(tmp_path):
    """The /betting safety-chain widget needs the extension's last-known
    state: connected-at + version, logged_in (latest of hello and status
    lines), and the last supersede/disconnect for storm visibility."""
    from oddsiq.dashboard.log_tail import latest_bridge_state
    p = tmp_path / "x.log"
    p.write_text(
        "2026-08-10 17:53:01 +0000 [WARNING] oddsiq.pipeline.betting_bridge: "
        "betting_bridge: new client supersedes existing one\n"
        "2026-08-10 22:39:44 +0000 [INFO] oddsiq.pipeline.betting_bridge: "
        "betting_bridge: extension connected (version=0.2.0 logged_in=False)\n"
        "2026-08-10 22:40:14 +0000 [INFO] oddsiq.pipeline.betting_bridge: "
        "betting_bridge: status logged_in=True\n"
    )
    st = latest_bridge_state(p)
    assert st is not None
    assert st.version == "0.2.0"
    assert st.last_connected is not None
    assert st.last_connected.strftime("%H:%M:%S") == "22:39:44"
    assert st.logged_in is True
    assert st.logged_in_at is not None and st.logged_in_at.strftime("%H:%M:%S") == "22:40:14"
    assert st.last_supersede is not None
    assert st.last_supersede.strftime("%H:%M") == "17:53"
    assert st.last_disconnected is None


def test_latest_bridge_state_disconnected_after_connect(tmp_path):
    from oddsiq.dashboard.log_tail import latest_bridge_state
    p = tmp_path / "x.log"
    p.write_text(
        "2026-08-10 22:39:44 +0000 [INFO] oddsiq.pipeline.betting_bridge: "
        "betting_bridge: extension connected (version=0.2.0 logged_in=True)\n"
        "2026-08-10 23:01:00 +0000 [WARNING] oddsiq.pipeline.betting_bridge: "
        "betting_bridge: extension disconnected\n"
    )
    st = latest_bridge_state(p)
    assert st.last_disconnected is not None
    assert st.last_disconnected > st.last_connected
    # hello carried logged_in=True and no later status line contradicts it
    assert st.logged_in is True


def test_latest_bridge_state_missing_file(tmp_path):
    from oddsiq.dashboard.log_tail import latest_bridge_state
    st = latest_bridge_state(tmp_path / "absent.log")
    assert st is None
