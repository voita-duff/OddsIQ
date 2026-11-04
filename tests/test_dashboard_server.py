"""Smoke tests for the OddsIQ dashboard FastAPI app.

These tests use FastAPI's ``TestClient`` to hit routes directly without
spinning up a uvicorn server. They focus on the wiring — status codes,
content-type, and the presence of structural markers (``data-page``
attributes) — not on the substantive content of each page, which is
filled in by Phases C/D/E.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from oddsiq.dashboard.log_tail import ScraperHealth
from oddsiq.dashboard.server import (
    create_app,
    _scraper_status_rows,
    _fmt_flagged_at,
    _selection_team,
)


@pytest.fixture(autouse=True)
def _hermetic_enabled_sports(monkeypatch):
    """Pin the full 4-sport list so tab-bar/board rendering tests are
    independent of the production enablement (MLB-only since 2026-08-08).
    server.py and queries.py bind ENABLED_SPORTS at import time, so patch
    both module bindings directly."""
    four = ["nba", "nhl", "mlb", "wnba"]
    monkeypatch.setattr("oddsiq.dashboard.server.ENABLED_SPORTS", four)
    monkeypatch.setattr("oddsiq.dashboard.queries.ENABLED_SPORTS", four)
    monkeypatch.setattr("oddsiq.sports.profiles.ENABLED_SPORTS", four)


def test_app_creates_and_redirects_root_to_live():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/live"


def test_live_route_returns_html():
    client = TestClient(create_app())
    resp = client.get("/live")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "OddsIQ" in resp.text
    assert 'data-page="live"' in resp.text


def test_live_sections_self_heal_after_failed_initial_load():
    """Regression: each live.html placeholder section must carry a recurring
    ``every Ns`` trigger so a dropped/failed initial ``load`` request retries
    instead of stranding the section on its 'loading…' placeholder forever.

    Root cause of the 'loading recent value bets…' hang: the sections relied
    on a one-shot ``hx-trigger="load"`` and the self-refresh ``every Ns`` poll
    lived only inside the swapped-in partial — so a single failed load (e.g.
    during a pipeline hot/live burst) left the section permanently loading.
    """
    import re

    client = TestClient(create_app())
    resp = client.get("/live")
    assert resp.status_code == 200
    triggers = re.findall(r'data-refresh-section[^>]*?hx-trigger="([^"]*)"', resp.text)
    assert len(triggers) >= 4, (
        f"expected the live refresh sections, found {len(triggers)}"
    )
    for t in triggers:
        assert "every" in t, f"section relies on one-shot load only (no retry): {t!r}"


def test_analysis_route_returns_html():
    client = TestClient(create_app())
    resp = client.get("/analysis")
    assert resp.status_code == 200
    assert 'data-page="analysis"' in resp.text


def test_settings_route_returns_html():
    client = TestClient(create_app())
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert 'data-page="settings"' in resp.text


from oddsiq import config as cfg


@pytest.fixture(autouse=True)
def _hermetic_dashboard_controls(monkeypatch):
    """Pin the write-control flag + credentials OFF.

    A developer's local ``.env`` (and the VM) runs the controls ON with
    credentials set, which now makes ``GET /betting`` challenge for Basic
    auth — every read-only route test would 401 depending on the machine.
    The control tests opt back in by monkeypatching after this fixture.
    """
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", False)
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_USERNAME", "")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_PASSWORD", "")


def test_status_strip_partial_loads(tmp_path, monkeypatch):
    # Use a tmp DB so the server's _store() context manager finds something queryable.
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    client = TestClient(create_app())
    resp = client.get("/live/partials/status-strip")
    assert resp.status_code == 200
    # Renders the expected scaffolding for an empty DB
    assert "Pipeline" in resp.text
    assert "Active scrapers" in resp.text
    assert "Odds API budget" in resp.text
    assert "Detections today" in resp.text
    # htmx attribute present so the section auto-refreshes
    assert 'hx-trigger="every 15s' in resp.text
    assert "refreshAll from:body" in resp.text


def test_status_strip_partial_shows_shadow_badge(tmp_path, monkeypatch):
    """A shadow-only day must not render a bare `0` — the far-tier detections
    get their own badge, mirroring the guarded one."""
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, ValueBet
    from oddsiq.db.store import Store

    db = tmp_path / "shadow.db"

    async def _seed():
        migrations.run(db)
        s = Store(db)
        await s.initialize()
        gid = await s.upsert_game(
            Game(
                external_id="nfl-shadow",
                home_team="Detroit Lions",
                away_team="New Orleans Saints",
                scheduled_at=datetime.now(timezone.utc) + timedelta(hours=5),
                status="scheduled",
                sport="nfl",
            )
        )
        await s.insert_value_bet(
            ValueBet(
                game_id=gid,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="home",
                entry_odds=1.4,
                fair_odds=1.34,
                edge_pct=4.5,
                flagged_at=datetime.now(timezone.utc),
                tier="far",
                sport="nfl",
            )
        )
        await s.close()

    asyncio.run(_seed())
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    client = TestClient(create_app())
    resp = client.get("/live/partials/status-strip")
    assert resp.status_code == 200
    assert "+1 shadow" in resp.text
    assert "guarded" not in resp.text  # badge is shadow-specific, not the guard one


def test_scraper_status_rows_use_warm_tier_freshness_window():
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    health = {
        "tipsport": ScraperHealth(
            timestamp=now - timedelta(minutes=10),
            name="tipsport",
            duration_ms=120,
            snapshots=4,
            matched_snapshots=4,
            matched_games=2,
            other_games=0,
            unmatched_snapshots=0,
            errors=0,
        )
    }
    rows = _scraper_status_rows(health, now, {"warm"})
    assert rows[0]["css_class"] == "ok"
    assert rows[0]["icon"] == "✓"


def test_scraper_status_rows_use_hot_tier_freshness_window():
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    health = {
        "tipsport": ScraperHealth(
            timestamp=now - timedelta(minutes=6),
            name="tipsport",
            duration_ms=120,
            snapshots=4,
            matched_snapshots=4,
            matched_games=2,
            other_games=0,
            unmatched_snapshots=0,
            errors=0,
        )
    }
    rows = _scraper_status_rows(health, now, {"hot"})
    assert rows[0]["css_class"] == "warn"
    assert rows[0]["icon"] == "⚠"


def test_fmt_flagged_at_converts_utc_to_prague_european_format():
    # flagged_at is stored UTC ISO; the dashboard shows European (CET/CEST)
    # date+time, day-first with dots and 24h clock. 2026-06-07 is CEST (UTC+2).
    assert _fmt_flagged_at("2026-06-07T14:30:00+00:00") == "07.06.2026 16:30"


def test_fmt_flagged_at_handles_winter_dst_and_date_rollover():
    # 2026-01-15 is CET (UTC+1); 23:30 UTC rolls over to 00:30 the next day.
    assert _fmt_flagged_at("2026-01-15T23:30:00+00:00") == "16.01.2026 00:30"


def test_fmt_flagged_at_handles_microsecond_iso_from_store():
    # store writes datetime.now(timezone.utc).isoformat() which carries micros.
    assert _fmt_flagged_at("2026-06-07T14:30:00.123456+00:00") == "07.06.2026 16:30"


def test_selection_team_maps_home_away_to_team_names():
    assert _selection_team("home", "Boston Celtics", "Miami Heat") == "Boston Celtics"
    assert _selection_team("away", "Boston Celtics", "Miami Heat") == "Miami Heat"
    # defensive: an unexpected selection (e.g. draw) passes through unchanged
    assert _selection_team("draw", "Boston Celtics", "Miami Heat") == "draw"


def test_recent_value_bets_partial_shows_team_name_not_home_away(tmp_path, monkeypatch):
    """The Selection column shows the actual team, not 'home'/'away', so the
    operator can instantly see which team to bet."""
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, ValueBet
    from oddsiq.db.store import Store

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    async def _seed():
        store = Store(db)
        await store.initialize()
        gid = await store.upsert_game(
            Game(
                external_id="ext-1",
                home_team="Boston Bruins",
                away_team="Montreal Canadiens",
                scheduled_at=datetime(2026, 6, 7, 18, 0, tzinfo=timezone.utc),
                status="scheduled",
                sport="nhl",
            )
        )
        await store.insert_value_bet(
            ValueBet(
                game_id=gid,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="away",
                entry_odds=2.05,
                fair_odds=1.95,
                edge_pct=5.0,
                tier="hot",
                sport="nhl",
            )
        )
        await store.close()

    asyncio.run(_seed())
    client = TestClient(create_app())
    resp = client.get("/betting/partials/value-bets")
    assert resp.status_code == 200
    assert "Montreal Canadiens" in resp.text  # away team name resolved
    assert ">away<" not in resp.text  # not the raw selection label


def test_coverage_matrix_header_says_start_not_tipoff(tmp_path, monkeypatch):
    """Multi-sport: the game-start column is labelled 'Start', not the
    basketball-specific 'Tipoff'."""
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.models import Game
    from oddsiq.db.store import Store

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    async def _seed():
        store = Store(db)
        await store.initialize()
        # A game ~30 min out is in the hot tier -> appears in the coverage matrix.
        await store.upsert_game(
            Game(
                external_id="ext-1",
                home_team="Boston Celtics",
                away_team="Miami Heat",
                scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                status="scheduled",
                sport="nba",
            )
        )
        await store.close()

    asyncio.run(_seed())
    client = TestClient(create_app())
    resp = client.get("/live/partials/coverage-matrix")
    assert resp.status_code == 200
    assert "<th>Start</th>" in resp.text
    assert "Tipoff" not in resp.text


def test_recent_value_bets_partial_shows_sport_and_european_datetime(
    tmp_path, monkeypatch
):
    """End-to-end: the partial renders a Sport column (not Book) and a
    European Prague-local date+time for flagged_at."""
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, ValueBet
    from oddsiq.db.store import Store

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    async def _seed():
        store = Store(db)
        await store.initialize()
        gid = await store.upsert_game(
            Game(
                external_id="ext-1",
                home_team="Boston Bruins",
                away_team="Montreal Canadiens",
                scheduled_at=datetime(2026, 6, 7, 18, 0, tzinfo=timezone.utc),
                status="scheduled",
                sport="nhl",
            )
        )
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=2.05,
            fair_odds=1.95,
            edge_pct=5.0,
            tier="hot",
            sport="nhl",
        )
        vb.flagged_at = datetime(2026, 6, 7, 14, 30, tzinfo=timezone.utc)
        await store.insert_value_bet(vb)
        await store.close()

    asyncio.run(_seed())

    client = TestClient(create_app())
    resp = client.get("/betting/partials/value-bets")
    assert resp.status_code == 200
    assert "<th>Tier</th>" in resp.text
    assert "<th>Book</th>" not in resp.text
    assert "07.06.2026 16:30" in resp.text  # 14:30 UTC -> 16:30 CEST, day-first


def test_scraper_status_rows_shows_only_tipsport():
    # The "Active scrapers" card shows only the soft book the operator bets
    # (Tipsport). Pinnacle (the sharp reference) and any legacy/removed-scraper
    # health lines are excluded.
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)

    def _h(name):
        return ScraperHealth(
            timestamp=now - timedelta(minutes=1),
            name=name,
            duration_ms=100,
            snapshots=2,
            matched_snapshots=2,
            matched_games=1,
            other_games=0,
            unmatched_snapshots=0,
            errors=0,
        )

    health = {
        "pinnacle": _h("pinnacle"),
        "tipsport": _h("tipsport"),
        "betano": _h("betano"),
    }
    rows = _scraper_status_rows(health, now, {"warm"})
    assert {r["name"] for r in rows} == {"tipsport"}


def test_status_strip_partial_shows_unknown_state_when_no_heartbeat(
    tmp_path, monkeypatch
):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    client = TestClient(create_app())
    resp = client.get("/live/partials/status-strip")
    assert resp.status_code == 200
    assert "no heartbeat" in resp.text  # pipeline state (humanized 'unknown')


def test_live_page_includes_status_strip_loader(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    client = TestClient(create_app())
    resp = client.get("/live")
    assert resp.status_code == 200
    # Page contains an htmx loader that hits the partial endpoint
    assert 'hx-get="/live/partials/status-strip"' in resp.text
    assert "refreshAll from:body" in resp.text


def test_coverage_matrix_partial_renders(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    client = TestClient(create_app())
    resp = client.get("/live/partials/coverage-matrix")
    assert resp.status_code == 200
    assert "coverage" in resp.text.lower()
    assert 'hx-trigger="every 30s' in resp.text


def test_recent_value_bets_partial_renders(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/betting/partials/value-bets")
    assert resp.status_code == 200
    assert "Recent value bets" in resp.text
    assert 'hx-trigger="every 30s' in resp.text


def test_activity_feed_partial_renders(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/live/partials/activity-feed")
    assert resp.status_code == 200
    assert "Activity feed" in resp.text
    assert 'hx-trigger="every 8s' in resp.text


def test_merge_activity_rows_orders_by_instant_across_timezones():
    """A DB event stored in UTC that is genuinely newer must sort ahead of a log
    event whose LOCAL-offset timestamp has a larger wall-clock string. Regression:
    a plain ISO-string sort misordered cross-source events."""
    from datetime import datetime, timezone, timedelta
    from oddsiq.dashboard.server import _merge_activity_rows
    from oddsiq.dashboard.log_tail import LogEvent

    db_events = [
        {
            "type": "snapshot",
            "ts": "2026-06-08T20:30:00+00:00",
            "bookmaker": "pinnacle",
            "market": "moneyline",
            "selection": "home",
            "edge_pct": None,
        }
    ]
    # 22:00 +02:00 == 20:00 UTC — 30 min OLDER than the DB event, yet its "22:00"
    # string sorts after "20:30".
    log_ts = datetime(2026, 6, 8, 22, 0, tzinfo=timezone(timedelta(hours=2)))
    log_events = [
        LogEvent(
            timestamp=log_ts,
            level="INFO",
            event_type="clv",
            fields={
                "bookmaker": "tipsport",
                "market": "moneyline",
                "selection": "home",
                "clv_pct": "4.20",
            },
            raw="x",
        )
    ]

    rows = _merge_activity_rows(db_events, log_events, limit=50)

    assert [r["src"] for r in rows] == ["DB", "LOG"]


def test_merge_activity_rows_skips_heartbeats():
    """Heartbeat liveness is surfaced by the status strip; heartbeat rows must NOT
    consume the activity-feed row budget (they would push out real events)."""
    from datetime import datetime, timezone, timedelta
    from oddsiq.dashboard.server import _merge_activity_rows
    from oddsiq.dashboard.log_tail import LogEvent

    base = datetime(2026, 6, 8, 20, 0, tzinfo=timezone.utc)
    log_events = [
        LogEvent(
            timestamp=base + timedelta(seconds=i),
            level="INFO",
            event_type="heartbeat",
            fields={"state": "polling", "games_tracked": "3"},
            raw="h",
        )
        for i in range(5)
    ]
    log_events.append(
        LogEvent(
            timestamp=base + timedelta(seconds=10),
            level="INFO",
            event_type="clv",
            fields={
                "bookmaker": "tipsport",
                "market": "moneyline",
                "selection": "home",
                "clv_pct": "4.20",
            },
            raw="c",
        )
    )

    rows = _merge_activity_rows([], log_events, limit=50)

    assert all(r["event_type"] != "heartbeat" for r in rows)
    assert any(r["event_type"] == "clv" for r in rows)


def test_activity_feed_renders_db_and_log_rows(tmp_path, monkeypatch):
    """When DB has a value_bet AND log has a clv: event, both appear in the feed."""
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, ValueBet
    from oddsiq.db.store import Store
    from datetime import datetime, timezone
    import asyncio

    db = tmp_path / "test.db"
    migrations.run(db)
    log = tmp_path / "x.log"
    log.write_text(
        "2026-05-28 02:00:00 +0000 [INFO] oddsiq.pipeline.settlement_service: clv: "
        "bookmaker=tipsport game_id=1 market=moneyline selection=home line= clv_pct=4.20\n"
    )
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", log)

    async def _seed():
        s = Store(db)
        await s.initialize()
        try:
            g = Game(
                external_id="ext1",
                home_team="Boston Celtics",
                away_team="Miami Heat",
                scheduled_at=datetime.now(timezone.utc),
                status="scheduled",
            )
            gid = await s.upsert_game(g)
            vb = ValueBet(
                game_id=gid,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="home",
                entry_odds=2.05,
                fair_odds=1.95,
                edge_pct=5.0,
                tier="hot",
            )
            await s.insert_value_bet(vb)
        finally:
            await s.close()

    asyncio.run(_seed())
    client = TestClient(create_app())
    resp = client.get("/live/partials/activity-feed")
    assert resp.status_code == 200
    # DB-sourced VALUE BET row
    assert "VALUE BET" in resp.text or "value_bet" in resp.text.lower()
    # LOG-sourced clv row
    assert "CLV" in resp.text or "clv" in resp.text.lower()
    # Both source tags appear
    assert "DB" in resp.text
    assert "LOG" in resp.text


def test_raw_logs_partial_returns_last_200_lines(tmp_path, monkeypatch):
    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {i}" for i in range(250)) + "\n")
    monkeypatch.setattr(cfg, "LOG_FILE", log)
    client = TestClient(create_app())
    resp = client.get("/live/partials/raw-logs")
    assert resp.status_code == 200
    # 200 lines tail — first 50 should be absent, last 200 present
    assert "line 249" in resp.text
    assert "line 50" in resp.text
    assert "line 49" not in resp.text


def test_raw_logs_partial_when_log_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/live/partials/raw-logs")
    assert resp.status_code == 200
    # Empty file or missing — still renders the wrapper
    assert "Raw logs" in resp.text
    assert 'hx-trigger="every 8s' in resp.text


def test_big_numbers_partial_renders(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/big-numbers")
    assert resp.status_code == 200
    assert "Value bets" in resp.text
    assert "Avg CLV" in resp.text
    assert "Win rate" in resp.text
    assert (
        "Overall ROI" in resp.text
    )  # ROI is the headline metric (replaced Avg edge card)
    assert 'hx-trigger="every 300s' in resp.text


def test_clv_trend_partial_renders(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/clv-trend")
    assert resp.status_code == 200
    assert "CLV trend" in resp.text
    assert 'hx-trigger="every 300s' in resp.text
    # Canvas + Chart.js init present
    assert "<canvas" in resp.text
    assert (
        "chart-box" in resp.text
    )  # fixed-height wrapper that prevents the flatten bug
    assert "height:240px" in resp.text  # inline height: immune to dashboard.css caching
    assert "oddsiqChart" in resp.text


def test_clv_trend_partial_accepts_days_param(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/clv-trend?days=7")
    assert resp.status_code == 200


def test_clv_trend_partial_has_period_selectors(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/clv-trend")
    assert resp.status_code == 200
    assert "All time" in resp.text
    assert "Last 30d" in resp.text
    assert "Last 7d" in resp.text
    # The 30d and 7d buttons must hit the endpoint with ?days=
    assert "days=30" in resp.text
    assert "days=7" in resp.text


def test_clv_trend_self_refresh_preserves_days_and_sport(tmp_path, monkeypatch):
    """The selected window must survive the every-300s auto-refresh.

    Regression: the self-refresh root ``hx-get`` threaded only ``sport``,
    not ``days``, so each auto-refresh silently re-fetched all-time data.
    The period-selector buttons also carry ``days``/``sport``, so this test
    isolates the root ``<section>`` (the one with the every-300s trigger)
    rather than asserting a plain substring that the buttons satisfy too.
    """
    import re

    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/clv-trend?days=7&sport=nhl")
    assert resp.status_code == 200

    # Grab the self-refresh root <section> opening tag (the every-300s one).
    m = re.search(r'<section\b[^>]*hx-trigger="every 300s[^>]*>', resp.text)
    assert m, "self-refresh root <section> with every-300s trigger not found"
    root_tag = m.group(0)
    hx = re.search(r'hx-get="([^"]*)"', root_tag)
    assert hx, "root hx-get not found"
    root_url = hx.group(1)
    assert "days=7" in root_url, f"days dropped from self-refresh hx-get: {root_url}"
    assert "sport=nhl" in root_url, (
        f"sport dropped from self-refresh hx-get: {root_url}"
    )


def test_scoreboard_partial_renders(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/scoreboard")
    assert resp.status_code == 200
    assert "No bookmaker coverage yet" in resp.text  # empty DB -> compact empty state
    assert 'hx-trigger="every 300s' in resp.text


def test_edge_buckets_partial_merges_volume_and_calibration(tmp_path, monkeypatch):
    """Edge distribution + calibration were the SAME buckets shown twice —
    now one table: volume bar + flagged count + break-even/actual/Δ columns."""
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/edge-buckets")
    assert resp.status_code == 200
    assert "Edge buckets" in resp.text
    assert "bar-fill" in resp.text  # volume bar
    assert "Break-even win%" in resp.text  # calibration columns
    assert 'hx-trigger="every 300s' in resp.text


def test_pages_use_versioned_asset_urls():
    # Cache-busting: CSS/JS links carry ?v=<token> so a changed asset gets a
    # new URL the browser must refetch (heuristic cache can't serve it stale).
    client = TestClient(create_app())
    resp = client.get("/analysis")
    assert resp.status_code == 200
    assert "/static/dashboard.css?v=" in resp.text
    assert "/static/charts.js?v=" in resp.text


def test_profit_curve_partial_renders(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/profit-curve")
    assert resp.status_code == 200
    assert "Profit curve" in resp.text
    assert "profit-curve-chart" in resp.text  # Chart.js canvas
    assert 'hx-trigger="every 300s' in resp.text


def test_results_detail_route_removed(tmp_path, monkeypatch):
    """The redundant results-detail section was removed 2026-08-08 — its only
    unique stat (avg odds) moved into the big-numbers win-rate tile."""
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    assert client.get("/analysis/partials/results-detail").status_code == 404
    assert "results-detail" not in client.get("/analysis").text


def test_settings_page_shows_config_paths(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    db.write_bytes(b"")  # zero-byte for size test
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "RAW_DATA_DIR", tmp_path / "raw")
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path / "backups")
    client = TestClient(create_app())
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "EDGE_THRESHOLD" in resp.text
    assert "Database" in resp.text
    assert "Bookmaker: tipsport" in resp.text  # folded into Pipeline & ops
    assert "Dashboard UI preferences" in resp.text
    # Restart note must be present
    assert "restart" in resp.text.lower()


def test_settings_never_shows_secret_fragments(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "RAW_DATA_DIR", tmp_path / "raw")
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(cfg, "ODDS_API_KEY", "aaaaaaaaaaaaaaaaSECRET1")
    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "1234:SECRETPAYLOAD")
    monkeypatch.setattr(cfg, "TELEGRAM_CHAT_ID", "1234567")
    client = TestClient(create_app())
    resp = client.get("/settings")
    body = resp.text
    # No fragments of any secret anywhere
    for forbidden in ("SECRET1", "SECRETPAYLOAD", "1234:"):
        assert forbidden not in body, f"forbidden token {forbidden!r} present"
    # But the meta info IS shown (the key VALUE is never rendered — only set/MISSING)
    assert "ODDS_API_KEY" in body  # the settings row name
    assert "set" in body  # key-set status, not the secret value
    assert "configured" in body  # Telegram status


def test_settings_shows_budget_from_meta(tmp_path, monkeypatch):
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.store import Store

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "RAW_DATA_DIR", tmp_path / "raw")
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path / "backups")

    async def _seed():
        s = Store(db)
        await s.initialize()
        try:
            await s.set_meta("odds_api_budget_used", "120")
            await s.set_meta("odds_api_budget_remaining", "380")
        finally:
            await s.close()

    asyncio.run(_seed())
    client = TestClient(create_app())
    resp = client.get("/settings")
    assert "120/380" in resp.text or "120 / 380" in resp.text


def test_settings_includes_ui_prefs_localstorage_script(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "RAW_DATA_DIR", tmp_path / "raw")
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path / "backups")
    client = TestClient(create_app())
    resp = client.get("/settings")
    assert "Auto-refresh" in resp.text
    assert "localStorage" in resp.text
    assert "autorefresh" in resp.text  # localStorage key name


def test_activity_feed_partial_has_filter_chips(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/live/partials/activity-feed")
    assert resp.status_code == 200
    # All six chip categories present
    for chip in (
        "warnings",
        "value bets",
        "CLV",
        "game lifecycle",
        "coverage gaps",
        "poll events",
    ):
        assert chip in resp.text
    # localStorage key present in inline JS
    assert "feed-filter-" in resp.text
    # data-evtype attribute used by the filter JS
    assert "data-evtype" in resp.text


def test_refresh_all_button_is_wired(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/live")
    assert resp.status_code == 200
    assert "Refresh all" in resp.text
    # The button emits a refreshAll event that initial loaders and swapped partials listen for.
    assert "htmx.trigger" in resp.text
    assert "document.body" in resp.text
    assert "refreshAll" in resp.text
    assert "main [data-refresh-section]" in resp.text


def test_autorefresh_preference_script_is_global(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/live")
    assert resp.status_code == 200
    assert "oddsiqSetAutoRefresh" in resp.text
    assert 'localStorage.getItem("autorefresh")' in resp.text


def test_charts_js_is_served_with_helper():
    client = TestClient(create_app())
    resp = client.get("/static/charts.js")
    assert resp.status_code == 200
    assert "oddsiqChart" in resp.text
    assert "oddsiqBarValueLabels" in resp.text
    assert "maintainAspectRatio" in resp.text


def test_static_assets_send_no_cache_header():
    client = TestClient(create_app())
    for path in ("/static/dashboard.css", "/static/charts.js"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("cache-control") == "no-cache", path


def test_sport_tab_bar_renders_enabled_sports():
    client = TestClient(create_app())
    body = client.get("/live").text
    assert "All" in body and ">NBA<" in body and ">NHL<" in body and ">MLB<" in body


def test_partials_accept_sport_param(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    assert client.get("/betting/partials/value-bets?sport=nhl").status_code == 200
    assert client.get("/analysis/partials/big-numbers?sport=mlb").status_code == 200
    assert client.get("/live/partials/status-strip?sport=nba").status_code == 200


def test_invalid_sport_is_treated_as_all(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    assert client.get("/live/partials/status-strip?sport=bogus").status_code == 200
    assert client.get("/live?sport=bogus").status_code == 200


def test_static_asset_304_revalidation_keeps_no_cache():
    # A revalidation (If-None-Match -> 304) must still carry no-cache so the
    # browser keeps revalidating instead of lapsing back to heuristic caching.
    client = TestClient(create_app())
    first = client.get("/static/dashboard.css")
    etag = first.headers["etag"]
    second = client.get("/static/dashboard.css", headers={"if-none-match": etag})
    assert second.status_code == 304
    assert second.headers.get("cache-control") == "no-cache"


def test_monthly_partial_self_refreshes_with_sport(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/monthly?sport=nhl")
    assert resp.status_code == 200
    assert "monthly?sport=nhl" in resp.text  # self-refresh sport preserved


def test_monthly_partial_month_chips_and_self_refresh(tmp_path, monkeypatch):
    """ONE month selector drives the bets rollup; chips + self-refresh keep the
    selected month. (The live tradeable block was removed 2026-08-27 with the
    live-edge subsystem.)"""
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, ValueBet
    from oddsiq.db.store import Store

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    async def _seed():
        store = Store(db)
        await store.initialize()
        at = datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc)
        gid = await store.upsert_game(
            Game(
                external_id="mp-1",
                home_team="New York Yankees",
                away_team="Boston Red Sox",
                scheduled_at=at,
                status="final",
                sport="mlb",
            )
        )
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=2.4,
            fair_odds=2.0,
            edge_pct=8.0,
            tier="hot",
            sport="mlb",
        )
        vb_id = await store.insert_value_bet(vb)
        await store._db.execute(
            "UPDATE value_bets SET result='win', flagged_at=? WHERE id=?",
            (at.isoformat(), vb_id),
        )
        await store._db.commit()
        await store.close()

    asyncio.run(_seed())
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/monthly")
    assert resp.status_code == 200
    assert "Jun 2026" in resp.text  # month chip
    assert "All time" in resp.text
    assert "tradeable set" not in resp.text  # live block removed 2026-08-27
    resp2 = client.get("/analysis/partials/monthly?month=2026-06")
    assert resp2.status_code == 200
    assert "+1.40" in resp2.text  # profit units of the won bet (June selected)
    assert "monthly?month=2026-06" in resp2.text  # self-refresh keeps month
    resp3 = client.get("/analysis/partials/monthly?month=all")
    assert resp3.status_code == 200
    assert "monthly?month=all" in resp3.text  # self-refresh keeps all-time


def test_settings_shows_budget_governor_section(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Budget governor" in resp.text


def test_partial_self_refresh_url_preserves_sport(tmp_path, monkeypatch):
    """The partial's own root hx-get (used for every-Ns self-refresh) must carry
    the sport param, else the filter is lost after the first refresh interval."""
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    client = TestClient(create_app())
    for url, frag in [
        ("/live/partials/status-strip?sport=nhl", "status-strip?sport=nhl"),
        ("/live/partials/coverage-matrix?sport=nhl", "coverage-matrix?sport=nhl"),
        ("/betting/partials/value-bets?sport=mlb", "value-bets?sport=mlb"),
        ("/live/partials/activity-feed?sport=mlb", "activity-feed?sport=mlb"),
        ("/analysis/partials/big-numbers?sport=nba", "big-numbers?sport=nba"),
        ("/analysis/partials/scoreboard?sport=nba", "scoreboard?sport=nba"),
        ("/analysis/partials/edge-buckets?sport=nhl", "edge-buckets?sport=nhl"),
        ("/analysis/partials/profit-curve?sport=nhl", "profit-curve?sport=nhl"),
        ("/analysis/partials/monthly?sport=mlb", "monthly?sport=mlb"),
    ]:
        body = client.get(url).text
        assert frag in body, (
            f"{url}: self-refresh hx-get missing sport ({frag} not in body)"
        )


def test_bets_redirects_to_betting():
    client = TestClient(create_app())
    resp = client.get("/bets", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/betting"


def test_betting_route_and_partials_render(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    page = client.get("/betting")
    assert page.status_code == 200 and 'data-page="betting"' in page.text
    for path in ("overview", "placed-bets", "value-bets"):
        assert client.get(f"/betting/partials/{path}").status_code == 200


def test_betting_controls_hidden_and_writes_forbidden_when_flag_off(
    tmp_path, monkeypatch
):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", False)
    client = TestClient(create_app())
    # No control affordances rendered.
    body = client.get("/betting/partials/overview").text
    assert "/betting/arm" not in body
    # Write endpoints refuse.
    assert client.post("/betting/arm").status_code == 403
    assert client.post("/betting/disarm").status_code == 403
    assert (
        client.post("/betting/config", data={"flat_stake_czk": "50"}).status_code == 403
    )


def test_betting_sports_gate_control(tmp_path, monkeypatch):
    # The betting-sports checkbox group (2026-08-25): the hidden
    # `betting_sports_present` marker distinguishes "all unchecked" from a
    # POST without the section; unknown keys drop; empty falls back to mlb.
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.store import Store

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", True)
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_USERNAME", "operator")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_PASSWORD", "secret")
    # Override the autouse 4-sport pin: the checkbox group renders one box per
    # ENABLED sport, and this test is about the NFL box.
    monkeypatch.setattr("oddsiq.dashboard.server.ENABLED_SPORTS", ["mlb", "nfl"])
    client = TestClient(
        create_app(),
        headers={
            "Authorization": "Basic b3BlcmF0b3I6c2VjcmV0",
            "Origin": "http://testserver",
        },
    )

    async def _meta():
        s = Store(db)
        await s.initialize()
        try:
            return await s.get_meta("betting_sports")
        finally:
            await s.close()

    # Checkboxes rendered in the config form.
    body = client.get("/betting/partials/overview").text
    assert 'name="betting_sports"' in body
    assert 'value="nfl"' in body

    # Tick mlb + nfl.
    assert (
        client.post(
            "/betting/config",
            data={
                "betting_sports": ["mlb", "nfl"],
                "betting_sports_present": "1",
            },
        ).status_code
        == 200
    )
    assert asyncio.run(_meta()) == "mlb,nfl"

    # Unknown key dropped; all-invalid falls back to mlb.
    assert (
        client.post(
            "/betting/config",
            data={
                "betting_sports": ["xxx"],
                "betting_sports_present": "1",
            },
        ).status_code
        == 200
    )
    assert asyncio.run(_meta()) == "mlb"

    # A profiled-but-DISABLED sport is dropped too (validated against
    # ENABLED_SPORTS): a persisted "nba" would otherwise go live at the
    # October re-enable without a fresh operator tick.
    assert (
        client.post(
            "/betting/config",
            data={
                "betting_sports": ["nba", "mlb"],
                "betting_sports_present": "1",
            },
        ).status_code
        == 200
    )
    assert asyncio.run(_meta()) == "mlb"

    # All unchecked -> conservative mlb, never empty.
    assert (
        client.post(
            "/betting/config",
            data={
                "betting_sports_present": "1",
            },
        ).status_code
        == 200
    )
    assert asyncio.run(_meta()) == "mlb"

    # A POST without the section leaves the meta untouched.
    assert (
        client.post(
            "/betting/config",
            data={
                "betting_sports": ["mlb", "nfl"],
                "betting_sports_present": "1",
            },
        ).status_code
        == 200
    )
    assert (
        client.post("/betting/config", data={"flat_stake_czk": "10"}).status_code == 200
    )
    assert asyncio.run(_meta()) == "mlb,nfl"


def test_betting_sports_checkboxes_hidden_when_controls_off(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", False)
    client = TestClient(create_app())
    body = client.get("/betting/partials/overview").text
    assert 'name="betting_sports"' not in body
    # The banner still shows the active gate read-only.
    assert "bets:" in body and "MLB" in body


def test_betting_controls_mutate_meta_when_flag_on(tmp_path, monkeypatch):
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.store import Store

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", True)
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_USERNAME", "operator")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_PASSWORD", "secret")
    monkeypatch.setattr(cfg, "BETTING_MAX_STAKE_CZK", 500.0)
    monkeypatch.setattr(cfg, "BETTING_MAX_BETS_PER_DAY", 10)
    monkeypatch.setattr(cfg, "BETTING_MAX_OPEN_STAKES_CZK", 500.0)
    monkeypatch.setattr(cfg, "BETTING_MAX_DAILY_LOSS_CZK", 300.0)
    client = TestClient(
        create_app(),
        headers={
            "Authorization": "Basic b3BlcmF0b3I6c2VjcmV0",
            "Origin": "http://testserver",
        },
    )

    # Controls now rendered.
    assert "/betting/arm" in client.get("/betting/partials/overview").text

    assert client.post("/betting/arm").status_code == 200
    assert (
        client.post(
            "/betting/config",
            data={
                "flat_stake_czk": "9999",  # over the hard ceiling -> clamped
                "max_bets_per_day": "9999",
                "max_open_stakes_czk": "999999",
                "daily_loss_limit_czk": "999999",
            },
        ).status_code
        == 200
    )

    async def _read():
        s = Store(db)
        await s.initialize()
        try:
            return (
                await s.get_meta("betting_armed"),
                await s.get_meta("betting_flat_stake_czk"),
                await s.get_meta("betting_max_bets_per_day"),
                await s.get_meta("betting_max_open_stakes_czk"),
                await s.get_meta("betting_daily_loss_limit_czk"),
            )
        finally:
            await s.close()

    armed, stake, per_day, max_open, loss_limit = asyncio.run(_read())
    assert armed == "1"
    assert float(stake) == 500.0  # clamped to BETTING_MAX_STAKE_CZK
    assert per_day == "10"
    assert float(max_open) == 500.0
    assert float(loss_limit) == 300.0

    assert client.post("/betting/disarm").status_code == 200

    async def _armed():
        s = Store(db)
        await s.initialize()
        try:
            return await s.get_meta("betting_armed")
        finally:
            await s.close()

    assert asyncio.run(_armed()) == "0"


def test_live_page_no_longer_has_value_bets_or_live_edges():
    client = TestClient(create_app())
    resp = client.get("/live")
    assert "/live/partials/recent-value-bets" not in resp.text
    assert "/live/partials/live-edges" not in resp.text


def test_bets_page_sections_self_heal():
    import re

    client = TestClient(create_app())
    resp = client.get("/bets")
    triggers = re.findall(r'data-refresh-section[^>]*?hx-trigger="([^"]*)"', resp.text)
    assert len(triggers) >= 2
    for t in triggers:
        assert "every" in t


def test_settings_has_no_live_scout_knobs(tmp_path, monkeypatch):
    """The live-edge subsystem was fully removed 2026-08-27 — /settings must
    show none of its knobs."""
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "LIVE_EDGE_ALERT_THRESHOLD" not in resp.text
    assert "LIVE_MARKOUT" not in resp.text
    assert "LIVE_EDGE_THRESHOLD" not in resp.text
    assert "Live scout" not in resp.text


def test_settings_shows_grouped_config_with_betting_and_sports(tmp_path, monkeypatch):
    """The config list is registry-driven and truthful: live-tier cadence,
    pregame guards, schedule cadences, betting gates, and enabled sports all
    appear (they were missing while the inert alert knob was shown)."""
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/settings")
    assert resp.status_code == 200
    for key in (
        "POLL_TIER_FAR / WARM / HOT",
        "PREGAME_MAX_PLAUSIBLE_EDGE",
        "PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS",
        "SCHEDULE_COMPLETION_CHECK_INTERVAL",
        "BETTING_ENABLED",
        "BETTING_CONFIRM_ENABLED",
        "ENABLED_SPORTS",
        "HEARTBEAT_PING_URL",
        "BACKUP_INTERVAL_HOURS",
    ):
        assert key in resp.text, f"{key} missing from /settings"


def test_settings_shows_effective_throttled_cadence(tmp_path, monkeypatch):
    """'hot 90s' at throttle L2 was a lie — the governor actually polls hot at
    120s. Settings must show the EFFECTIVE cadence for the current level."""
    from datetime import datetime, timedelta, timezone
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    log = tmp_path / "oddsiq.log"
    ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
        "%Y-%m-%d %H:%M:%S +0000"
    )
    log.write_text(
        f"{ts} [INFO] oddsiq.pipeline.scheduler: heartbeat: "
        "state=polling games_tracked=5 seconds_since_last_poll=10 budget_throttle=2\n"
    )
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", log)
    client = TestClient(create_app())
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "hot 120s" in resp.text  # L2 effective, not the base 90s
    assert "live 300s" not in resp.text  # live tier removed 2026-08-27


def test_run_dashboard_applies_migrations(tmp_path, monkeypatch):
    """The dashboard must apply pending migrations on launch — /betting selects
    placed_bets columns, so a dashboard pointed at an un-migrated DB would
    otherwise 500."""
    import sqlite3
    import uvicorn
    from oddsiq.dashboard import server

    db = tmp_path / "fresh.db"  # never migrated
    monkeypatch.setattr(server.config, "DATABASE_PATH", db)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)  # don't block on serving
    rc = server.run_dashboard(port=0)
    assert rc == 0
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(placed_bets)").fetchall()}
    finally:
        conn.close()
    assert "receipt_id" in cols  # migrations (through 011) ran at launch


def test_upcoming_route_returns_html():
    client = TestClient(create_app())
    resp = client.get("/upcoming")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert 'data-page="upcoming"' in resp.text


def test_upcoming_section_self_heals_with_recurring_trigger():
    """The board section must carry an `every Ns` trigger (not just `load`) so a
    failed initial load retries instead of stranding on the placeholder."""
    import re

    client = TestClient(create_app())
    resp = client.get("/upcoming")
    triggers = re.findall(r'data-refresh-section[^>]*?hx-trigger="([^"]*)"', resp.text)
    assert triggers, "no refresh section found on /upcoming"
    for t in triggers:
        assert "every" in t, f"section relies on one-shot load only (no retry): {t!r}"


def test_nav_includes_upcoming_link():
    client = TestClient(create_app())
    resp = client.get("/live")
    assert 'href="/upcoming"' in resp.text


def test_upcoming_board_partial_loads_empty_db(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    client = TestClient(create_app())
    resp = client.get("/upcoming/partials/board")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "No games scheduled in the next 24h." in resp.text  # empty agenda
    assert "TOTAL" in resp.text  # tiles still render (4 sports pinned => TOTAL shown)
    assert 'hx-trigger="every 300s' in resp.text  # self-heal trigger
    assert "refreshAll from:body" in resp.text


def test_upcoming_board_partial_renders_a_scheduled_game(tmp_path, monkeypatch):
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.models import Game
    from oddsiq.db.store import Store

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    async def _seed():
        store = Store(db)
        await store.initialize()
        tip = datetime.now(timezone.utc) + timedelta(
            minutes=30
        )  # ~30m out -> hot -> open
        await store.upsert_game(
            Game(
                external_id="up-1",
                home_team="Boston Celtics",
                away_team="Miami Heat",
                scheduled_at=tip,
                commence_time=tip,
                status="scheduled",
                sport="nba",
            )
        )
        await store.close()

    asyncio.run(_seed())
    client = TestClient(create_app())
    resp = client.get("/upcoming/partials/board")
    assert resp.status_code == 200
    assert "Miami Heat @ Boston Celtics" in resp.text
    assert "Bet window open" in resp.text
    assert "HOT" in resp.text


def test_analysis_page_has_monthly_section(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get("/analysis")
    assert resp.status_code == 200
    assert "/analysis/partials/monthly" in resp.text


def test_monthly_partial_renders_month_tabs_and_profit(tmp_path, monkeypatch):
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, ValueBet
    from oddsiq.db.store import Store

    db = tmp_path / "test.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")

    async def _seed():
        store = Store(db)
        await store.initialize()
        gid = await store.upsert_game(
            Game(
                external_id="monthly-1",
                home_team="New York Yankees",
                away_team="Boston Red Sox",
                scheduled_at=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
                status="final",
                sport="mlb",
            )
        )
        vb = ValueBet(
            game_id=gid,
            bookmaker="tipsport",
            market_type="moneyline",
            selection="home",
            entry_odds=2.5,
            fair_odds=2.3,
            edge_pct=5.0,
            tier="hot",
            sport="mlb",
        )
        vb.flagged_at = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        bet_id = await store.insert_value_bet(vb)
        await store.update_value_bet_result(bet_id, "win")
        await store.close()

    asyncio.run(_seed())
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/monthly")
    assert resp.status_code == 200
    assert 'class="card-title">Performance' in resp.text
    assert "Jun 2026" in resp.text  # month tab label
    # Tabs are calendar-true through the current month (2026-09-06 fix: a
    # month with no clean bets used to vanish), and the current month is the
    # default view — so June's profit shows when June is selected.
    assert datetime.now(timezone.utc).strftime("%b %Y") in resp.text
    resp = client.get("/analysis/partials/monthly?month=2026-06")
    assert "+1.50" in resp.text  # exact profit units for the month
    # Explicit month selection keeps working (tab click URL).
    resp2 = client.get("/analysis/partials/monthly?month=2026-06")
    assert resp2.status_code == 200
    assert "+1.50" in resp2.text


def test_merge_activity_rows_shows_prague_clock_with_date_for_old_events():
    """One clock everywhere: the feed used to print raw UTC (DB events) next
    to local-offset log times, with no dates — a 4-day-old bet looked like
    tonight's. Rows must render Prague time, dated when not 'today'."""
    from datetime import datetime, timezone
    from oddsiq.dashboard.server import _merge_activity_rows

    now = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
    db_events = [
        {  # 2026-08-06T23:15:30Z == 07.08. 01:15:30 Prague (CEST)
            "type": "value_bet",
            "ts": "2026-08-06T23:15:30+00:00",
            "bookmaker": "tipsport",
            "market": "moneyline",
            "selection": "home",
            "edge_pct": 4.3,
        },
        {  # today: 05:05:10Z == 07:05:10 Prague
            "type": "snapshot",
            "ts": "2026-08-11T05:05:10+00:00",
            "bookmaker": "pinnacle",
            "market": "moneyline",
            "selection": "away",
        },
    ]
    rows = _merge_activity_rows(db_events, [], limit=10, now=now)
    today_row = next(r for r in rows if r["event_type"] == "snapshot")
    old_row = next(r for r in rows if r["event_type"] == "value_bet")
    assert today_row["ts_short"] == "07:05:10"
    assert old_row["ts_short"] == "07.08 01:15"


def test_merge_activity_rows_prague_consistent_across_sources():
    """A DB event (UTC ISO) and a log event (local offset) at the SAME instant
    must render the SAME display time."""
    from datetime import datetime, timezone, timedelta
    from oddsiq.dashboard.log_tail import LogEvent
    from oddsiq.dashboard.server import _merge_activity_rows

    now = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
    db_events = [
        {
            "type": "value_bet",
            "ts": "2026-08-11T05:05:10+00:00",
            "bookmaker": "tipsport",
            "market": "moneyline",
            "selection": "home",
            "edge_pct": 3.5,
        }
    ]
    log_events = [
        LogEvent(
            timestamp=datetime(
                2026, 8, 11, 7, 5, 10, tzinfo=timezone(timedelta(hours=2))
            ),
            level="INFO",
            event_type="clv",
            fields={
                "bookmaker": "tipsport",
                "market": "moneyline",
                "selection": "home",
                "clv_pct": "2.1",
            },
            raw="",
        )
    ]
    rows = _merge_activity_rows(db_events, log_events, limit=10, now=now)
    assert len(rows) == 2
    assert rows[0]["ts_short"] == rows[1]["ts_short"] == "07:05:10"


def test_merge_activity_rows_formats_suspect_value_bets():
    """Guard-stamped detections appear in the feed, visibly marked with the
    guard reason (the 'why is nothing betting' signal)."""
    from datetime import datetime, timezone
    from oddsiq.dashboard.log_tail import LogEvent
    from oddsiq.dashboard.server import _merge_activity_rows

    log_events = [
        LogEvent(
            timestamp=datetime(2026, 8, 11, 5, 10, tzinfo=timezone.utc),
            level="INFO",
            event_type="value_bet_suspect",
            fields={
                "bookmaker": "tipsport",
                "market": "moneyline",
                "selection": "home",
                "edge_pct": "12.30",
                "reason": "incoherent_book_pregame",
                "tier": "hot",
            },
            raw="",
        )
    ]
    rows = _merge_activity_rows([], log_events, limit=10)
    assert len(rows) == 1
    assert "guarded" in rows[0]["text"].lower()
    assert "incoherent_book_pregame" in rows[0]["text"]


def test_nav_hides_sport_tabs_for_single_enabled_sport(monkeypatch):
    """With one enabled sport the All|MLB toggle is dead weight — hide it."""
    monkeypatch.setattr("oddsiq.dashboard.server.ENABLED_SPORTS", ["mlb"])
    client = TestClient(create_app())
    resp = client.get("/live")
    assert resp.status_code == 200
    assert "sport-tabs" not in resp.text


def test_base_page_has_favicon_and_theme_color():
    client = TestClient(create_app())
    resp = client.get("/live")
    assert 'rel="icon"' in resp.text
    assert 'name="theme-color"' in resp.text


def test_scraper_rows_show_neutral_idle_when_no_active_games():
    """Red ✗ during a normal overnight idle is a false alarm (the 485m-stale
    tipsport badge with zero pollable games). With no active tiers the row is
    neutral 'idle', never warn/err — red is reserved for 'games needed polling
    and didn't get it'."""
    now = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
    health = {
        "tipsport": ScraperHealth(
            timestamp=now - timedelta(minutes=485),
            name="tipsport",
            duration_ms=100,
            snapshots=1,
            matched_snapshots=1,
            matched_games=1,
            other_games=0,
            unmatched_snapshots=0,
            errors=0,
        )
    }
    rows = _scraper_status_rows(health, now, active_tiers=set())
    assert rows[0]["css_class"] == "na"
    assert rows[0]["icon"] == "idle"
    assert rows[0]["age"] == "8h"


def test_scraper_rows_still_alarm_when_games_are_active():
    now = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
    health = {
        "tipsport": ScraperHealth(
            timestamp=now - timedelta(minutes=30),
            name="tipsport",
            duration_ms=100,
            snapshots=1,
            matched_snapshots=1,
            matched_games=1,
            other_games=0,
            unmatched_snapshots=0,
            errors=0,
        )
    }
    rows = _scraper_status_rows(health, now, active_tiers={"hot"})
    assert rows[0]["css_class"] == "err"
    assert rows[0]["icon"] == "✗"


def test_budget_context_projects_month_end_and_reset():
    """The budget cell must answer 'am I going to run out': % used, reset
    date (ODDS_API_QUOTA_RESET_DAY), burn/day since reset, and the projected
    month-end total at that burn."""
    from oddsiq.dashboard.server import _budget_context

    now = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
    ctx = _budget_context(8630, 11370, now=now, reset_day=2)
    assert ctx["total"] == 20000
    assert ctx["pct"] == 43
    assert ctx["resets_on"] == "02.09"
    assert ctx["burn_per_day"] == round(8630 / 9)  # reset Aug 2 -> 9 days elapsed
    assert ctx["days_left"] == 22  # Aug 11 -> Sep 2
    assert ctx["projected"] == 8630 + ctx["burn_per_day"] * 22
    assert ctx["over_budget"] is True


def test_budget_context_handles_unknown_budget():
    from oddsiq.dashboard.server import _budget_context

    now = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
    assert _budget_context(None, None, now=now, reset_day=2) is None


def test_budget_context_trailing_burn_from_history():
    """With a daily used-history, the projection uses the TRAILING burn (last
    ~3 complete days), not the period average — a period average bakes in
    regime changes forever (the 2026-08-27 live-tier removal cut ~200/day but
    the average kept projecting the old rate until the next reset)."""
    from oddsiq.dashboard.server import _budget_context

    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    # 15,314 used over 25 days = 613/day avg, but the last 3 days burned only
    # 15,314 - 14,090 = 1,224 => 408/day trailing.
    history = [
        {"d": "2026-08-23", "u": 13680},
        {"d": "2026-08-24", "u": 14090},
        {"d": "2026-08-25", "u": 14510},
        {"d": "2026-08-26", "u": 14904},
    ]
    ctx = _budget_context(15314, 4686, now=now, reset_day=2, history=history)
    assert ctx["burn_basis"] == "3d"
    assert ctx["burn_per_day"] == round((15314 - 14090) / 3)  # 408
    assert ctx["projected"] == 15314 + ctx["burn_per_day"] * ctx["days_left"]
    assert ctx["over_budget"] is False


def test_budget_context_falls_back_to_period_average():
    from oddsiq.dashboard.server import _budget_context

    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    # No history -> period average, labeled as such.
    ctx = _budget_context(15314, 4686, now=now, reset_day=2, history=None)
    assert ctx["burn_basis"] == "avg"
    assert ctx["burn_per_day"] == round(15314 / 25)
    # History from before the quota reset (used DECREASED since) -> fallback,
    # never a negative trailing burn.
    stale = [{"d": "2026-08-26", "u": 19500}]
    ctx2 = _budget_context(
        300,
        19700,
        now=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        reset_day=2,
        history=stale,
    )
    assert ctx2["burn_basis"] == "avg"
    assert ctx2["burn_per_day"] >= 0


def test_raw_logs_partial_colors_warning_and_error_lines(tmp_path, monkeypatch):
    from oddsiq import config

    p = tmp_path / "oddsiq.log"
    p.write_text(
        "2026-08-11 07:00:00 +0000 [INFO] oddsiq.pipeline.scheduler: all fine\n"
        "2026-08-11 07:00:01 +0000 [WARNING] oddsiq.pipeline.scheduler: something odd\n"
        "2026-08-11 07:00:02 +0000 [ERROR] oddsiq.pipeline.scheduler: something broke\n"
    )
    monkeypatch.setattr(config, "LOG_FILE", p)
    client = TestClient(create_app())
    resp = client.get("/live/partials/raw-logs")
    assert resp.status_code == 200
    assert 'class="logline-warn"' in resp.text
    assert 'class="logline-err"' in resp.text


def test_format_db_event_includes_matchup_and_tier():
    from oddsiq.dashboard.server import _format_db_event

    text = _format_db_event(
        {
            "type": "value_bet",
            "bookmaker": "tipsport",
            "market": "moneyline",
            "selection": "home",
            "edge_pct": 4.3,
            "tier": "hot",
            "home_team": "Boston Celtics",
            "away_team": "Miami Heat",
        }
    )
    assert "Miami Heat @ Boston Celtics" in text
    assert "Boston Celtics" in text  # selection resolved to the team
    assert "hot" in text


def _seed_upcoming_game(db_path, *, hours_from_now=1.5, sport="mlb"):
    import asyncio as _asyncio
    from oddsiq.db import migrations as _migrations
    from oddsiq.db.models import Game
    from oddsiq.db.store import Store

    async def _seed():
        _migrations.run(db_path)
        s = Store(db_path)
        await s.initialize()
        g = Game(
            external_id=f"u-{hours_from_now}",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=hours_from_now),
            status="scheduled",
            sport=sport,
        )
        await s.upsert_game(g)
        await s.close()

    _asyncio.run(_seed())


def test_upcoming_board_has_table_headers_and_no_duplicate_title(tmp_path, monkeypatch):
    from oddsiq import config

    db = tmp_path / "d.db"
    _seed_upcoming_game(db, hours_from_now=9.0)
    monkeypatch.setattr(config, "DATABASE_PATH", db)
    client = TestClient(create_app())
    resp = client.get("/upcoming/partials/board")
    assert resp.status_code == 200
    assert "<thead>" in resp.text
    assert "Bet window" in resp.text
    # The page bar already titles the page; the partial must not repeat it.
    assert "Upcoming — next 24h" not in resp.text


def test_upcoming_rows_highlight_open_windows_and_tick(tmp_path, monkeypatch):
    from oddsiq import config

    db = tmp_path / "d.db"
    _seed_upcoming_game(db, hours_from_now=1.5)  # warm => window open
    monkeypatch.setattr(config, "DATABASE_PATH", db)
    client = TestClient(create_app())
    resp = client.get("/upcoming/partials/board")
    assert 'class="window-open"' in resp.text
    assert "data-tipoff=" in resp.text


def _write_betting_log(p, *, betting="armed", logged_in=True):
    # Timestamps must be wall-clock-fresh: _engine_state downgrades a
    # heartbeat older than 3 intervals to "unknown".
    def _ts(seconds_ago):
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
            "%Y-%m-%d %H:%M:%S +0000"
        )

    p.write_text(
        f"{_ts(90)} [INFO] oddsiq.pipeline.betting_bridge: "
        "betting_bridge: extension connected (version=0.2.0 logged_in=False)\n"
        f"{_ts(60)} [INFO] oddsiq.pipeline.betting_bridge: "
        f"betting_bridge: status logged_in={logged_in}\n"
        f"{_ts(30)} [INFO] oddsiq.pipeline.scheduler: heartbeat: "
        f"state=polling games_tracked=5 seconds_since_last_poll=10 "
        f"budget_throttle=2 betting={betting}\n"
    )


def test_betting_overview_renders_banner_chain_and_caps(tmp_path, monkeypatch):
    """The money page leads with a first-class state banner and the 6-link
    safety chain (enabled → bridge → extension → login → armed → confirm)."""
    from oddsiq.db import migrations

    db = tmp_path / "d.db"
    migrations.run(db)
    log = tmp_path / "oddsiq.log"
    _write_betting_log(log, betting="armed", logged_in=True)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", log)
    monkeypatch.setattr(cfg, "BETTING_ENABLED", True)
    monkeypatch.setattr(cfg, "BETTING_CONFIRM_ENABLED", True)
    client = TestClient(create_app())
    resp = client.get("/betting/partials/overview")
    assert resp.status_code == 200
    assert "engine-banner armed" in resp.text
    assert 'class="chain' in resp.text
    for label in (
        "enabled",
        "bridge",
        "extension",
        "logged in",
        "armed",
        "confirm gate",
    ):
        assert label in resp.text, f"chain link {label!r} missing"
    assert "0/10" in resp.text  # bets-per-day cap progress


def test_state_dot_partial_renders_engine_state(tmp_path, monkeypatch):
    log = tmp_path / "oddsiq.log"
    _write_betting_log(log, betting="armed")
    monkeypatch.setattr(cfg, "LOG_FILE", log)
    client = TestClient(create_app())
    resp = client.get("/betting/partials/state-dot")
    assert resp.status_code == 200
    assert "armed" in resp.text
    # No heartbeat at all -> renders nothing (dot hidden on fresh installs)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    resp = client.get("/betting/partials/state-dot")
    assert resp.text.strip() == ""


def test_value_bets_partial_suspect_toggle(tmp_path, monkeypatch):
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.models import Game, ValueBet
    from oddsiq.db.store import Store

    db = tmp_path / "d.db"

    async def _seed():
        migrations.run(db)
        s = Store(db)
        await s.initialize()
        g = Game(
            external_id="g1",
            home_team="New York Yankees",
            away_team="Boston Red Sox",
            scheduled_at=datetime.now(timezone.utc),
            status="scheduled",
            sport="mlb",
        )
        gid = await s.upsert_game(g)
        await s.insert_value_bet(
            ValueBet(
                game_id=gid,
                bookmaker="tipsport",
                market_type="moneyline",
                selection="home",
                entry_odds=2.05,
                fair_odds=1.95,
                edge_pct=5.0,
                flagged_at=datetime.now(timezone.utc),
                tier="hot",
                sport="mlb",
                excluded_reason="incoherent_book_pregame",
            )
        )
        await s.close()

    asyncio.run(_seed())
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    client = TestClient(create_app())
    default = client.get("/betting/partials/value-bets")
    assert "No value bets flagged yet" in default.text
    shown = client.get("/betting/partials/value-bets?show=suspect")
    assert 'class="suspect"' in shown.text
    assert "incoherent_book_pregame" in shown.text


def test_betting_config_post_shows_save_flash(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "d.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", True)
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_USERNAME", "operator")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_PASSWORD", "secret")
    client = TestClient(
        create_app(),
        headers={
            "Authorization": "Basic b3BlcmF0b3I6c2VjcmV0",
            "Origin": "http://testserver",
        },
    )
    resp = client.post("/betting/config", data={"flat_stake_czk": 10})
    assert resp.status_code == 200
    assert "save-flash" in resp.text


def test_betting_controls_require_credentials_and_same_origin(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "auth.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", True)
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_USERNAME", "operator")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_PASSWORD", "secret")
    client = TestClient(create_app())
    assert (
        client.post("/betting/arm", headers={"Origin": "http://testserver"}).status_code
        == 401
    )
    auth = {"Authorization": "Basic b3BlcmF0b3I6c2VjcmV0"}
    assert (
        client.post(
            "/betting/arm",
            headers={**auth, "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/betting/arm",
            headers={**auth, "Origin": "http://testserver"},
        ).status_code
        == 200
    )


def test_betting_controls_fail_closed_when_credentials_missing(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "missing-auth.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", True)
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_USERNAME", "")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_PASSWORD", "")
    client = TestClient(create_app())
    assert "/betting/arm" not in client.get("/betting/partials/overview").text
    assert client.post("/betting/arm").status_code == 503
    # The page itself stays public read-only — nothing to authenticate against.
    assert client.get("/betting").status_code == 200
    # ...and the in-app hint names the ACTUAL blocker (flag on, creds missing).
    overview = client.get("/betting/partials/overview").text
    assert "DASHBOARD_CONTROLS_USERNAME" in overview
    assert "DASHBOARD_CONTROLS_ENABLED=1" not in overview


def test_betting_page_challenges_for_basic_auth_when_controls_credentialed(
    tmp_path,
    monkeypatch,
):
    """The control POSTs are htmx XHRs and htmx does not swap a non-2xx
    response, so an unauthenticated first Arm click would silently no-op.
    Challenging on the PAGE (a top-level navigation) makes the browser prompt
    and then attach the credentials to the same-origin htmx requests."""
    from oddsiq.db import migrations

    db = tmp_path / "page-auth.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", True)
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_USERNAME", "operator")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_PASSWORD", "secret")
    client = TestClient(create_app())

    unauth = client.get("/betting")
    assert unauth.status_code == 401
    assert unauth.headers["WWW-Authenticate"] == 'Basic realm="OddsIQ betting controls"'

    assert client.get("/betting", auth=("operator", "wrong")).status_code == 401
    ok = client.get("/betting", auth=("operator", "secret"))
    assert ok.status_code == 200 and 'data-page="betting"' in ok.text

    # Only the page is gated: the read-only tabs, the /betting partials and
    # the legacy redirect stay open so nothing else starts prompting.
    for path in ("/live", "/upcoming", "/analysis", "/settings"):
        assert client.get(path).status_code == 200, path
    for path in ("overview", "placed-bets", "value-bets"):
        assert client.get(f"/betting/partials/{path}").status_code == 200, path
    assert client.get("/bets", follow_redirects=False).status_code in (307, 308)

    # The overview PARTIAL stays public (read-only data) but must not render
    # the arm/disarm + caps FORM to an unauthenticated viewer; the operator,
    # whose browser now carries the credentials, still gets it.
    anon = client.get("/betting/partials/overview")
    assert anon.status_code == 200 and "/betting/arm" not in anon.text
    op = client.get("/betting/partials/overview", auth=("operator", "secret"))
    assert op.status_code == 200 and "/betting/arm" in op.text


def test_read_only_tabs_need_no_auth_when_controls_disabled(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "public.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    for path in ("/live", "/upcoming", "/betting", "/analysis", "/settings"):
        assert client.get(path).status_code == 200, path


def test_control_same_origin_check_honours_forwarded_host(tmp_path, monkeypatch):
    """Behind a reverse proxy Host is the upstream bind while Origin carries
    the public hostname — comparing against Host alone 403s every POST."""
    from oddsiq.db import migrations

    db = tmp_path / "proxy.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_ENABLED", True)
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_USERNAME", "operator")
    monkeypatch.setattr(cfg, "DASHBOARD_CONTROLS_PASSWORD", "secret")
    client = TestClient(create_app())
    auth = {"Authorization": "Basic b3BlcmF0b3I6c2VjcmV0"}
    proxied = {
        **auth,
        "Origin": "https://oddsiq.example",
        "Host": "127.0.0.1:8080",
        "X-Forwarded-Host": "oddsiq.example, edge.internal",
    }
    assert client.post("/betting/arm", headers=proxied).status_code == 200
    # A foreign Origin is still rejected against the forwarded host.
    assert (
        client.post(
            "/betting/arm",
            headers={
                **proxied,
                "Origin": "https://evil.example",
            },
        ).status_code
        == 403
    )


def test_base_template_surfaces_htmx_errors():
    client = TestClient(create_app())
    body = client.get("/live").text
    assert 'id="htmx-error"' in body
    assert "htmx:responseError" in body
    assert "htmx:sendError" in body


def test_status_strip_flags_unsafe_start_snapshots(tmp_path, monkeypatch):
    """`unsafe_start_snapshots > 0` means Tipsport listings arrived without a
    usable start time — matching fails closed, so this is the tripwire for a
    payload change silently killing every match."""
    from oddsiq.db import migrations

    db = tmp_path / "unsafe.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)

    def _health_line(unsafe: int) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %z")
        return (
            f"{stamp} [INFO] oddsiq.pipeline.poll_coordinator: Scraper health: "
            "name=tipsport duration_ms=120 snapshots=8 matched_snapshots=6 "
            "matched_games=3 other_games=1 unmatched_snapshots=2 errors=0 "
            f"unsafe_start_snapshots={unsafe}\n"
        )

    log = tmp_path / "oddsiq.log"
    log.write_text(_health_line(3), encoding="utf-8")
    monkeypatch.setattr(cfg, "LOG_FILE", log)
    client = TestClient(create_app())
    assert "unsafe start: 3" in client.get("/live/partials/status-strip").text

    log.write_text(_health_line(0), encoding="utf-8")
    assert "unsafe start" not in client.get("/live/partials/status-strip").text


def test_coverage_matrix_surfaces_pinnacle_provenance_warnings(tmp_path, monkeypatch):
    from oddsiq.db import migrations

    db = tmp_path / "sharp-health.db"
    migrations.run(db)
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %z")
    log = tmp_path / "oddsiq.log"
    log.write_text(
        f"{stamp} [INFO] oddsiq.pipeline.poll_coordinator: Scraper health: "
        "name=pinnacle duration_ms=120 snapshots=8 matched_snapshots=6 "
        "matched_games=3 other_games=1 unmatched_snapshots=1 errors=0 "
        "unsafe_start_snapshots=0 sharp_source_age_max_ms=85000 "
        "missing_source_updates=1 missing_event_ids=2 event_id_mismatches=3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "LOG_FILE", log)
    body = TestClient(create_app()).get("/live/partials/coverage-matrix").text
    assert "source=85s" in body
    assert "no timestamp=1" in body
    assert "no id=2" in body
    assert "id miss=3" in body


def test_game_page_renders_and_404s(tmp_path, monkeypatch):
    import asyncio
    from oddsiq.db import migrations
    from oddsiq.db.models import Game
    from oddsiq.db.store import Store

    db = tmp_path / "d.db"

    async def _seed():
        migrations.run(db)
        s = Store(db)
        await s.initialize()
        gid = await s.upsert_game(
            Game(
                external_id="gd-1",
                home_team="New York Yankees",
                away_team="Boston Red Sox",
                scheduled_at=datetime.now(timezone.utc),
                status="scheduled",
                sport="mlb",
            )
        )
        await s.close()
        return gid

    gid = asyncio.run(_seed())
    monkeypatch.setattr(cfg, "DATABASE_PATH", db)
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "absent.log")
    client = TestClient(create_app())
    resp = client.get(f"/game/{gid}")
    assert resp.status_code == 200
    assert "Boston Red Sox @ New York Yankees" in resp.text
    assert "game-chart-home" in resp.text  # per-selection odds chart canvases
    assert "game-chart-away" in resp.text
    assert client.get("/game/999999").status_code == 404


def test_analysis_calendar_partial_renders():
    client = TestClient(create_app())
    resp = client.get("/analysis/partials/calendar")
    assert resp.status_code == 200
    assert "Activity calendar" in resp.text


def test_analysis_day_partial_validates_date():
    client = TestClient(create_app())
    assert client.get("/analysis/partials/day?date=2026-09-04").status_code == 200
    assert client.get("/analysis/partials/day?date=not-a-date").status_code == 400


def test_analysis_page_loads_calendar_section():
    client = TestClient(create_app())
    resp = client.get("/analysis")
    assert resp.status_code == 200
    assert 'hx-get="/analysis/partials/calendar"' in resp.text
