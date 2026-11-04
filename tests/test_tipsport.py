import asyncio
from datetime import date, datetime, timezone

import pytest
from curl_cffi.requests.cookies import Cookies

from oddsiq.scrapers.cloudflare import CloudflareSolveError
from oddsiq.scrapers.tipsport import (
    HEADERS, TIPSPORT_IMPERSONATE, TipsportScraper, TipsportMarketUnavailable,
    _warmup_url,
)
from oddsiq.sports.profiles import NBA, NHL, MLB, WNBA, NFL

# Basketball mirrors hockey: the default WINNER_WHOLE_MATCH tab carries the
# regulation 3-way legs (1/1x/x/x2/2) whose 1/2 prices are NOT the moneyline
# (verified 2026-06-12: live WHOLE legs sum under 100% two-way implied). The
# true incl-OT winner lives in WINNER_FINAL_DECISION; the parser must select
# it and never fall back to the regulation tab.
SAMPLE_MONEYLINE_RESPONSE = {
    "offerSuperSports": [{
        "id": {"superSportId": 7},
        "name": "Basketbal",
        "tabs": [
            {
                "matchView": "WINNER_WHOLE_MATCH",
                "name": "Zápas",
                "subTabs": [],
                "offerCompetitionAnnuals": [{
                    "id": 249904,
                    "competitionId": 23,
                    "name": "NBA, Basketbal - muži",
                    "matches": [
                        {
                            "id": 7432495,
                            "nameFull": "Toronto Raptors - Miami Heat",
                            "participantHome": "Toronto Raptors",
                            "participantVisiting": "Miami Heat",
                            "oppRows": [{
                                "oppsTab": [
                                    {"id": 1, "eventId": 100, "label": "1", "type": "1", "odd": 1.58, "bettingEnabled": True, "dateClosed": "2026-04-10T01:10:00.000+02:00"},
                                    {"id": 2, "eventId": 101, "label": "10", "type": "1x", "odd": 1.45, "bettingEnabled": True, "dateClosed": "2026-04-10T01:10:00.000+02:00"},
                                    {"id": 3, "eventId": 100, "label": "0", "type": "x", "odd": 14.8, "bettingEnabled": True, "dateClosed": "2026-04-10T01:10:00.000+02:00"},
                                    {"id": 4, "eventId": 101, "label": "02", "type": "x2", "odd": 2.35, "bettingEnabled": True, "dateClosed": "2026-04-10T01:10:00.000+02:00"},
                                    {"id": 5, "eventId": 100, "label": "2", "type": "2", "odd": 2.75, "bettingEnabled": True, "dateClosed": "2026-04-10T01:10:00.000+02:00"},
                                ]
                            }]
                        }
                    ]
                }]
            },
            {
                "matchView": "WINNER_FINAL_DECISION",
                "name": "Vítěz do rozhodnutí",
                "subTabs": [],
                "offerCompetitionAnnuals": [{
                    "id": 249904,
                    "competitionId": 23,
                    "name": "NBA, Basketbal - muži",
                    "matches": [
                        {
                            "id": 7432495,
                            "nameFull": "Toronto Raptors - Miami Heat",
                            "url": "/kurzy/zapas/basketbal-toronto-raptors-miami-heat/7432495",
                            "participantHome": "Toronto Raptors",
                            "participantVisiting": "Miami Heat",
                            "oppRows": [{
                                "oppsTab": [
                                    {"id": 6, "eventId": 102, "label": "Toronto", "type": "1", "odd": 1.50, "bettingEnabled": True, "dateClosed": "2026-04-10T01:10:00.000+02:00"},
                                    {"id": 7, "eventId": 102, "label": "Miami", "type": "2", "odd": 2.59, "bettingEnabled": True, "dateClosed": "2026-04-10T01:10:00.000+02:00"},
                                ]
                            }]
                        }
                    ]
                }]
            },
        ]
    }]
}


@pytest.fixture
def scraper():
    return TipsportScraper()


@pytest.fixture(autouse=True)
def _raw_responses_to_tmp(monkeypatch, tmp_path):
    """Redirect _save_raw_response writes away from the real ./data/raw.

    fetch_odds persists every payload via _save_raw_response, which reads
    config.RAW_DATA_DIR at call time — without this patch, a local pytest run
    drops SAMPLE_MONEYLINE_RESPONSE fixture files into the operator's
    raw-response archive. config loads .env at import time, so the config
    attribute must be patched, not the env var.
    """
    monkeypatch.setattr("oddsiq.config.RAW_DATA_DIR", tmp_path)


def _browser_jsessionid_cookie() -> list[dict]:
    return [{
        "name": "JSESSIONID",
        "value": "browser-session",
        "domain": ".tipsport.cz",
        "path": "/",
        "secure": True,
    }]


def test_parse_moneyline(scraper):
    snapshots = scraper.parse_response(SAMPLE_MONEYLINE_RESPONSE, NBA)
    # Must read the WINNER_FINAL_DECISION prices (1.50/2.59), NOT the regulation
    # 1/2 legs (1.58/2.75) from the WHOLE_MATCH tab.
    assert len(snapshots) == 2
    home = [s for s in snapshots if s.selection == "home"]
    away = [s for s in snapshots if s.selection == "away"]
    assert len(home) == 1
    assert home[0].odds == 1.50
    assert home[0].line is None
    assert home[0].market_type == "moneyline"
    assert len(away) == 1
    assert away[0].odds == 2.59
    assert home[0].sport == "nba"


def test_parse_captures_tipsport_ids(scraper):
    """Execution identity for the P0 probe / P1 placement: Tipsport's own
    match id, deep-link url, and opportunity id ride along as TRANSIENT
    snapshot fields (never persisted on odds_snapshots — the dedup key and
    schema stay untouched); EdgeService copies them onto value_bets."""
    snapshots = scraper.parse_response(SAMPLE_MONEYLINE_RESPONSE, NBA)
    home = next(s for s in snapshots if s.selection == "home")
    away = next(s for s in snapshots if s.selection == "away")
    assert home._tipsport_match_id == 7432495
    assert away._tipsport_match_id == 7432495
    assert home._tipsport_opp_id == 6
    assert away._tipsport_opp_id == 7
    assert home._tipsport_url == "/kurzy/zapas/basketbal-toronto-raptors-miami-heat/7432495"


def test_parse_nba_fails_closed_without_final_decision(scraper):
    # Only the regulation WHOLE_MATCH tab present -> market unavailable, never
    # read the 3-way legs as moneyline (same contract as NHL).
    only_regulation = {"offerSuperSports": [{"tabs": [
        SAMPLE_MONEYLINE_RESPONSE["offerSuperSports"][0]["tabs"][0]
    ]}]}
    with pytest.raises(TipsportMarketUnavailable):
        scraper.parse_response(only_regulation, NBA)


def test_parse_skips_disabled_odds(scraper):
    data = {
        "offerSuperSports": [{
            "id": {"superSportId": 7},
            "name": "Basketbal",
            "tabs": [{
                "matchView": "WINNER_FINAL_DECISION",
                "name": "Vítěz do rozhodnutí",
                "subTabs": [],
                "offerCompetitionAnnuals": [{
                    "id": 249904,
                    "competitionId": 23,
                    "name": "NBA",
                    "matches": [{
                        "id": 1,
                        "nameFull": "Toronto Raptors - Miami Heat",
                        "participantHome": "Toronto Raptors",
                        "participantVisiting": "Miami Heat",
                        "oppRows": [{
                            "oppsTab": [
                                {"id": 1, "eventId": 100, "label": "1", "type": "1", "odd": 1.58, "bettingEnabled": False, "dateClosed": "2026-04-10T01:10:00.000+02:00"},
                                {"id": 2, "eventId": 100, "label": "2", "type": "2", "odd": 2.75, "bettingEnabled": True, "dateClosed": "2026-04-10T01:10:00.000+02:00"},
                            ]
                        }]
                    }]
                }]
            }]
        }]
    }
    snapshots = scraper.parse_response(data, NBA)
    assert len(snapshots) == 1
    assert snapshots[0].selection == "away"


@pytest.mark.asyncio
async def test_fetch_returns_empty_when_playwright_fails(scraper, monkeypatch):
    """A 403 from curl_cffi + Playwright failure returns empty."""

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status
            self.text = "ok"

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()

        async def get(self, url, **kwargs):
            return FakeResponse(200)  # warm-up OK

        async def post(self, url, **kwargs):
            return FakeResponse(403)  # API blocked

        async def close(self):
            pass

    class FailingCfSession:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            raise CloudflareSolveError("timeout")

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)
    monkeypatch.setattr("oddsiq.scrapers.tipsport.CloudflareSession", FailingCfSession)

    odds = await scraper.fetch_odds(date.today(), NBA)
    assert odds == []


@pytest.mark.asyncio
async def test_close_closes_cf_session_even_if_client_close_raises(scraper):
    """A failing curl client.close() must not skip the Playwright/CF shutdown —
    otherwise a Chromium process leaks on exit."""
    cf_closed = {"v": False}

    class BoomClient:
        async def close(self):
            raise RuntimeError("curl close boom")

    class FakeCf:
        async def close(self):
            cf_closed["v"] = True

    scraper._client = BoomClient()
    scraper._cf_session = FakeCf()

    await scraper.close()  # must not raise

    assert cf_closed["v"] is True
    assert scraper._client is None and scraper._cf_session is None


@pytest.mark.asyncio
async def test_session_auto_recovery_after_consecutive_failures(scraper, monkeypatch):
    """After MAX_CONSECUTIVE_FAILURES, both sessions are destroyed."""
    monkeypatch.setattr("oddsiq.config.MAX_CONSECUTIVE_FAILURES", 3)

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status
            self.text = "ok"

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()

        async def get(self, url, **kwargs):
            return FakeResponse(200)

        async def post(self, url, **kwargs):
            return FakeResponse(403)

        async def close(self):
            pass

    class FakeCfSession:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            return self

        async def export_cookies(self, names=None):
            return _browser_jsessionid_cookie()

        async def post(self, url, **kwargs):
            raise ConnectionError("Playwright also 403")

        async def close(self):
            pass

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)
    monkeypatch.setattr("oddsiq.scrapers.tipsport.CloudflareSession", FakeCfSession)

    # Three consecutive failures trigger session reset
    await scraper.fetch_odds(date.today(), NBA)
    assert scraper._consecutive_failures == 1

    await scraper.fetch_odds(date.today(), NBA)
    assert scraper._consecutive_failures == 2

    await scraper.fetch_odds(date.today(), NBA)
    assert scraper._consecutive_failures == 0  # reset
    assert scraper._client is None
    assert scraper._cf_session is None


@pytest.mark.asyncio
async def test_get_client_does_warmup_get(scraper, monkeypatch):
    """Creating the session triggers a GET to the NBA page for Cloudflare cookies."""
    warmup_called = False
    created_sessions = []

    class FakeResponse:
        status_code = 200
        text = "<html>ok</html>"

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.cookies = Cookies()
            created_sessions.append(self)

        async def get(self, url, **kwargs):
            nonlocal warmup_called
            warmup_called = True
            assert "nba-23" in url
            return FakeResponse()

        async def post(self, url, **kwargs):
            return FakeResponse()

        async def close(self):
            pass

    monkeypatch.setattr(
        "oddsiq.scrapers.tipsport.AsyncSession",
        FakeSession,
    )
    await scraper._get_client(NBA)
    assert warmup_called, "Warm-up GET was not called when creating session"
    assert created_sessions[0].kwargs["impersonate"] == TIPSPORT_IMPERSONATE
    # Session-level headers are deliberately empty: chrome146's impersonate
    # owns the warmup GET wire shape, and POST-time HEADERS (Accept/
    # Content-Type/Referer) are applied per-call inside _fetch_moneyline.
    assert "headers" not in created_sessions[0].kwargs
    # Confirm HEADERS does NOT leak the manual User-Agent that previously
    # truncated chrome146's full UA string.
    assert "User-Agent" not in HEADERS


@pytest.mark.asyncio
async def test_401_syncs_browser_jsessionid_then_retries_curl(scraper, monkeypatch):
    """A fresh curl session gets API 401 until it borrows browser JSESSIONID."""

    class FakeResponse:
        def __init__(self, status, json_data=None):
            self.status_code = status
            self._json = json_data
            self.text = ""

        def json(self):
            return self._json

    sessions = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()
            self.post_count = 0
            sessions.append(self)

        async def get(self, url, **kwargs):
            return FakeResponse(200)

        async def post(self, url, **kwargs):
            self.post_count += 1
            if not any(name == "JSESSIONID" for name in self.cookies.keys()):
                return FakeResponse(401)
            return FakeResponse(200, SAMPLE_MONEYLINE_RESPONSE)

        async def close(self):
            pass

    cf_post_called = False

    class FakeCfSession:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            return self

        async def export_cookies(self, names=None):
            assert names == {"JSESSIONID"}
            return _browser_jsessionid_cookie()

        async def post(self, url, **kwargs):
            nonlocal cf_post_called
            cf_post_called = True
            raise AssertionError("Playwright POST should not be used after cookie sync")

        async def close(self):
            pass

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)
    monkeypatch.setattr("oddsiq.scrapers.tipsport.CloudflareSession", FakeCfSession)

    odds = await scraper.fetch_odds(date.today(), NBA)

    assert len(odds) == 2
    assert sessions[0].post_count == 2
    assert not cf_post_called


@pytest.mark.asyncio
async def test_stale_jsessionid_forces_browser_restart(scraper, monkeypatch):
    """When curl_cffi already carries a JSESSIONID and STILL gets 401, the
    server has invalidated that session — the scraper must close the
    existing Playwright session and start a new one to mint a fresh
    JSESSIONID, then retry curl. Pins the `force_new_browser=True` branch
    in _copy_browser_api_session_to_client.
    """

    class FakeResponse:
        def __init__(self, status, json_data=None):
            self.status_code = status
            self._json = json_data
            self.text = ""

        def json(self):
            return self._json

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()

        async def get(self, url, **kwargs):
            return FakeResponse(200)

        async def post(self, url, **kwargs):
            # 401 until cookies carry the FRESH JSESSIONID minted by the
            # NEW Playwright session — stale JSESSIONID is rejected.
            value = self.cookies.get("JSESSIONID")
            if value == "FRESH-SESSION-B":
                return FakeResponse(200, SAMPLE_MONEYLINE_RESPONSE)
            return FakeResponse(401)

        async def close(self):
            pass

    cf_instances: list[object] = []
    cf_closes: list[int] = []
    cf_starts: list[int] = []

    class FakeCfSession:
        def __init__(self, *a, **kw):
            self._idx = len(cf_instances)
            cf_instances.append(self)

        async def start(self):
            cf_starts.append(self._idx)
            return self

        async def export_cookies(self, names=None):
            # First instance (idx=0) is the pre-planted "stale" session;
            # second instance (idx=1) is the freshly minted one after the
            # force_new_browser close+restart.
            value = "FRESH-SESSION-B" if self._idx > 0 else "STALE-SESSION-A"
            return [{
                "name": "JSESSIONID",
                "value": value,
                "domain": ".tipsport.cz",
                "path": "/",
                "secure": True,
            }]

        async def post(self, url, **kwargs):
            raise AssertionError(
                "Playwright POST should not be used — fresh-JSESSIONID curl retry must succeed"
            )

        async def close(self):
            cf_closes.append(self._idx)

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)
    monkeypatch.setattr("oddsiq.scrapers.tipsport.CloudflareSession", FakeCfSession)

    # Pre-plant: simulate the mid-window state where prior sync succeeded
    # but the server has since invalidated the JSESSIONID.
    client = await scraper._get_client(NBA)
    client.cookies.set(
        "JSESSIONID", "STALE-SESSION-A",
        domain=".tipsport.cz", path="/", secure=True,
    )
    scraper._cf_session = FakeCfSession()
    await scraper._cf_session.start()

    odds = await scraper.fetch_odds(date.today(), NBA)

    assert len(odds) == 2, "fetch should succeed after force-new-browser restart"
    # Two FakeCfSession instances were created: the pre-planted stale one
    # and the post-restart fresh one.
    assert len(cf_instances) == 2, "expected force_new_browser to spawn a second Playwright session"
    # The pre-planted (idx=0) was closed before the restart.
    assert 0 in cf_closes
    # The fresh session (idx=1) was started.
    assert 1 in cf_starts
    # And the client now carries the fresh JSESSIONID — not the stale one.
    assert client.cookies.get("JSESSIONID") == "FRESH-SESSION-B"


@pytest.mark.asyncio
async def test_get_client_warmup_403_log_matches_deferred_fallback(
    scraper, monkeypatch, caplog
):
    """Warmup 403 only announces possible POST fallback, not immediate Playwright."""

    class FakeResponse:
        status_code = 403
        text = "<html>blocked</html>"

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()

        async def get(self, url, **kwargs):
            return FakeResponse()

        async def post(self, url, **kwargs):
            return FakeResponse()

        async def close(self):
            pass

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)

    with caplog.at_level("INFO", logger="oddsiq.scrapers.tipsport"):
        await scraper._get_client(NBA)

    assert "warmup blocked; will retry POST and may fall back" in caplog.text
    assert "curl_cffi blocked" not in caplog.text


def test_parse_skips_invalid_odds_values(scraper):
    data = {
        "offerSuperSports": [{
            "id": {"superSportId": 7},
            "name": "Basketbal",
            "tabs": [{
                "matchView": "WINNER_FINAL_DECISION",
                "offerCompetitionAnnuals": [{
                    "matches": [{
                        "id": 1,
                        "nameFull": "Toronto Raptors - Miami Heat",
                        "participantHome": "Toronto Raptors",
                        "participantVisiting": "Miami Heat",
                        "oppRows": [{"oppsTab": [
                            {"type": "1", "label": "1", "odd": 1.58, "bettingEnabled": True},
                            {"type": "2", "label": "2", "odd": None, "bettingEnabled": True},
                            {"type": "2", "label": "2", "odd": True, "bettingEnabled": True},
                            {"type": "2", "label": "2", "odd": "bad", "bettingEnabled": True},
                            {"type": "2", "label": "2", "odd": 0, "bettingEnabled": True},
                            {"type": "2", "label": "2", "odd": -1.0, "bettingEnabled": True},
                        ]}],
                    }],
                }],
            }],
        }],
    }

    snapshots = scraper.parse_response(data, NBA)

    assert len(snapshots) == 1
    assert snapshots[0].selection == "home"
    assert snapshots[0].odds == 1.58


def test_team_names_attached(scraper):
    """Verify _home_team and _away_team are set on snapshots."""
    snapshots = scraper.parse_response(SAMPLE_MONEYLINE_RESPONSE, NBA)
    for snap in snapshots:
        assert snap._home_team == "Toronto Raptors"
        assert snap._away_team == "Miami Heat"


def test_parse_response_raises_on_missing_offer_super_sports(scraper):
    """An unexpected 200 response missing the top-level offerSuperSports key
    used to log a warning and return [] — i.e. health logs showed errors=0
    while the parse silently failed. Now it must propagate so fetch_odds
    counts the market as failed via last_fetch_error_count."""
    with pytest.raises((KeyError, IndexError, TypeError)):
        scraper.parse_response({}, NBA)


def test_parse_response_raises_on_empty_offer_super_sports(scraper):
    """Same contract for an empty list — IndexError on [0] must propagate."""
    with pytest.raises((KeyError, IndexError)):
        scraper.parse_response({"offerSuperSports": []}, NBA)


def test_parse_response_raises_on_non_dict_root(scraper):
    """A JSON list at the root would TypeError on dict-style access; this
    must also propagate rather than silently return []."""
    with pytest.raises((KeyError, IndexError, TypeError)):
        scraper.parse_response([], NBA)


def test_parse_fails_closed_when_moneyline_tab_absent(scraper):
    # Well-formed envelope, but the sport's moneyline matchView tab is absent.
    # Still fail closed (never read another tab), but with a DISTINCT type so the
    # fetch layer can tell "market not offered" from a transport/parse failure.
    data = {"offerSuperSports": [{"tabs": [{"matchView": "GOAL_SCORERS", "offerCompetitionAnnuals": []}]}]}
    with pytest.raises(TipsportMarketUnavailable):
        scraper.parse_response(data, NBA)


@pytest.mark.asyncio
async def test_fetch_odds_does_not_count_market_unavailable_as_failure(scraper, monkeypatch):
    """A well-formed response missing the moneyline tab (market not offered) must
    NOT count as a consecutive failure — a session reset can't conjure a market,
    and the shared Cloudflare session is used by the other sports in the same
    cycle. Returns [] without incrementing _consecutive_failures."""
    wellformed_no_ml = {"offerSuperSports": [{"tabs": [
        {"matchView": "GOAL_SCORERS", "offerCompetitionAnnuals": []}]}]}

    class FakeResp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self.text = "ok"
            self._payload = payload

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()

        async def get(self, url, **kwargs):
            return FakeResp(200)

        async def post(self, url, **kwargs):
            return FakeResp(200, wellformed_no_ml)

        async def close(self):
            pass

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)
    monkeypatch.setattr(scraper, "_save_raw_response", lambda *a, **k: None)

    result = await scraper.fetch_odds(date.today(), NBA)

    assert result == []
    assert scraper._consecutive_failures == 0


@pytest.mark.asyncio
async def test_fetch_odds_counts_playwright_error_as_failure(scraper, monkeypatch):
    """A raw playwright.async_api.Error from the Cloudflare fallback path
    (page.evaluate / context.cookies on a destroyed context) must be caught by
    fetch_odds and routed through failure accounting + session reset — not
    escape uncounted and leave a wedged CF session limping indefinitely."""
    from playwright.async_api import Error as PlaywrightError

    async def boom(*a, **k):
        raise PlaywrightError("Execution context was destroyed")

    monkeypatch.setattr(scraper, "_fetch_moneyline", boom)

    result = await scraper.fetch_odds(date.today(), NBA)

    assert result == []
    assert scraper.last_fetch_error_count == 1
    assert scraper._consecutive_failures == 1


# --- Playwright fallback integration tests ---


@pytest.mark.asyncio
async def test_403_on_post_falls_back_to_playwright(scraper, monkeypatch):
    """POST 403 from curl_cffi → Playwright session used for API call."""

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status
            self.text = "ok"

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()

        async def get(self, url, **kwargs):
            return FakeResponse(200)  # warm-up OK

        async def post(self, url, **kwargs):
            return FakeResponse(403)  # curl_cffi always blocked

        async def close(self):
            pass

    cf_post_called = False

    class FakeCfSession:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            return self

        async def export_cookies(self, names=None):
            return _browser_jsessionid_cookie()

        async def post(self, url, **kwargs):
            nonlocal cf_post_called
            cf_post_called = True
            return SAMPLE_MONEYLINE_RESPONSE

        async def close(self):
            pass

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)
    monkeypatch.setattr("oddsiq.scrapers.tipsport.CloudflareSession", FakeCfSession)

    odds = await scraper.fetch_odds(date.today(), NBA)
    assert len(odds) > 0, "Should have fetched odds via Playwright fallback"
    assert cf_post_called, "Playwright session should have been used"
    assert scraper._cf_session is not None


@pytest.mark.asyncio
async def test_captured_at_stamped_at_request_initiation_not_parse_time(
    scraper, monkeypatch, tmp_path
):
    """Regression: captured_at must reflect when the HTTP request was sent,
    not when parsing finished. Otherwise a poll initiated 5-15s pre-tipoff
    can produce snapshots stamped post-tipoff because latency pushes parse-time
    well past the request time.
    """

    class FakeResponse:
        def __init__(self, status, json_data=None):
            self.status_code = status
            self._json = json_data

        def json(self):
            return self._json

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()

        async def get(self, url, **kwargs):
            return FakeResponse(200)  # warm-up OK

        async def post(self, url, **kwargs):
            # Simulate slow API response: sleep 1.5s before returning.
            await asyncio.sleep(1.5)
            return FakeResponse(200, SAMPLE_MONEYLINE_RESPONSE)

        async def close(self):
            pass

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)
    monkeypatch.setattr("oddsiq.config.RAW_DATA_DIR", tmp_path)

    pre_call = datetime.now(timezone.utc)
    snapshots = await scraper.fetch_odds(date.today(), NBA)
    post_call = datetime.now(timezone.utc)

    assert len(snapshots) > 0
    # The HTTP simulated 1.5s delay => >=1.5s elapsed.
    elapsed = (post_call - pre_call).total_seconds()
    assert elapsed >= 1.0, "Test setup invalid: HTTP delay didn't fire"

    for snap in snapshots:
        # captured_at must be near pre_call (request initiation), not post_call (parse done).
        # Allow 200ms slack for setup overhead (rate-limit-zero, fixture init, etc.).
        delta_from_pre = abs((snap.captured_at - pre_call).total_seconds())
        delta_from_post = abs((snap.captured_at - post_call).total_seconds())
        assert delta_from_pre < delta_from_post, (
            f"captured_at {snap.captured_at} is closer to post-call {post_call} "
            f"than pre-call {pre_call}; should reflect request initiation, "
            f"deltas: pre={delta_from_pre:.3f}s, post={delta_from_post:.3f}s"
        )


@pytest.mark.asyncio
async def test_raw_response_path_propagates_to_snapshots(scraper, monkeypatch, tmp_path):
    """Regression: raw_response_path must be set on every returned snapshot,
    pointing at the JSON file actually written to disk."""

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()

        async def get(self, url, **kwargs):
            return FakeResponse(200)

        async def post(self, url, **kwargs):
            return FakeResponse(403)

        async def close(self):
            pass

    class FakeCfSession:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            return self

        async def export_cookies(self, names=None):
            return _browser_jsessionid_cookie()

        async def post(self, url, **kwargs):
            return SAMPLE_MONEYLINE_RESPONSE

        async def close(self):
            pass

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)
    monkeypatch.setattr("oddsiq.scrapers.tipsport.CloudflareSession", FakeCfSession)
    monkeypatch.setattr("oddsiq.config.RAW_DATA_DIR", tmp_path)

    snapshots = await scraper.fetch_odds(date.today(), NBA)
    assert len(snapshots) > 0
    for snap in snapshots:
        assert snap.raw_response_path is not None, (
            f"raw_response_path is None on {snap.market_type}/{snap.selection} snapshot"
        )
        from pathlib import Path
        assert Path(snap.raw_response_path).exists(), (
            f"raw_response_path {snap.raw_response_path} does not exist on disk"
        )


@pytest.mark.asyncio
async def test_playwright_session_reused_across_fetch_calls(scraper, monkeypatch):
    """The same Playwright session is reused across two fetch_odds calls."""
    start_count = 0

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status

    class FakeSession:
        def __init__(self, **kwargs):
            self.cookies = Cookies()

        async def get(self, url, **kwargs):
            return FakeResponse(200)

        async def post(self, url, **kwargs):
            return FakeResponse(403)

        async def close(self):
            pass

    class FakeCfSession:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            nonlocal start_count
            start_count += 1
            return self

        async def export_cookies(self, names=None):
            return _browser_jsessionid_cookie()

        async def post(self, url, **kwargs):
            return SAMPLE_MONEYLINE_RESPONSE

        async def close(self):
            pass

    monkeypatch.setattr("oddsiq.scrapers.tipsport.AsyncSession", FakeSession)
    monkeypatch.setattr("oddsiq.scrapers.tipsport.CloudflareSession", FakeCfSession)

    await scraper.fetch_odds(date.today(), NBA)
    await scraper.fetch_odds(date.today(), NBA)
    assert start_count == 1, "Playwright should start only once, reused across fetch calls"


# ---------------------------------------------------------------------------
# NHL + MLB parse fixtures
# ---------------------------------------------------------------------------

# NHL 2-way incl-OT lives in WINNER_FINAL_DECISION (type 1/2). The default
# WINNER_WHOLE_MATCH tab is the 3-way regulation (1/x/2 + double chance) and
# MUST be excluded — reading it would fabricate edges.
SAMPLE_NHL_RESPONSE = {
    "offerSuperSports": [{
        "id": {"superSportId": 23},
        "tabs": [
            {"matchView": "WINNER_WHOLE_MATCH", "offerCompetitionAnnuals": [{
                "matches": [{
                    "participantHome": "Carolina Hurricanes",
                    "participantVisiting": "Vegas Golden Knights",
                    "nameFull": "Carolina Hurricanes - Vegas Golden Knights",
                    "oppRows": [{"oppsTab": [
                        {"label": "1", "type": "1", "odd": 2.04, "bettingEnabled": True},
                        {"label": "10", "type": "1x", "odd": 1.32, "bettingEnabled": True},
                        {"label": "0", "type": "x", "odd": 3.83, "bettingEnabled": True},
                        {"label": "02", "type": "x2", "odd": 1.69, "bettingEnabled": True},
                        {"label": "2", "type": "2", "odd": 3.07, "bettingEnabled": True},
                    ]}],
                }],
            }]},
            {"matchView": "WINNER_FINAL_DECISION", "offerCompetitionAnnuals": [{
                "matches": [{
                    "participantHome": "Carolina Hurricanes",
                    "participantVisiting": "Vegas Golden Knights",
                    "nameFull": "Carolina Hurricanes - Vegas Golden Knights",
                    "oppRows": [{"oppsTab": [
                        {"label": "Carolina", "type": "1", "odd": 1.56, "bettingEnabled": True},
                        {"label": "Vegas", "type": "2", "odd": 2.27, "bettingEnabled": True},
                    ]}],
                }],
            }]},
        ],
    }]
}

SAMPLE_MLB_RESPONSE = {
    "offerSuperSports": [{
        "id": {"superSportId": 6},
        "tabs": [{"matchView": "WINNER_WHOLE_MATCH", "offerCompetitionAnnuals": [{
            "matches": [{
                "participantHome": "Washington Nationals",
                "participantVisiting": "Miami Marlins",
                "nameFull": "Washington Nationals - Miami Marlins",
                "oppRows": [{"oppsTab": [
                    {"label": "1", "type": "1", "odd": 1.92, "bettingEnabled": True},
                    {"label": "2", "type": "2", "odd": 1.94, "bettingEnabled": True},
                ]}],
            }],
        }]}],
    }]
}


def test_parse_nhl_uses_final_decision_not_regulation(scraper):
    snaps = scraper.parse_response(SAMPLE_NHL_RESPONSE, NHL)
    by_sel = {s.selection: s for s in snaps}
    assert set(by_sel) == {"home", "away"}
    # The incl-OT prices (1.56/2.27), NOT the regulation 2.04/3.07.
    assert by_sel["home"].odds == 1.56
    assert by_sel["away"].odds == 2.27
    assert by_sel["home"].sport == "nhl"
    assert by_sel["home"]._home_team == "Carolina Hurricanes"


def test_parse_nhl_fails_closed_without_final_decision(scraper):
    only_regulation = {"offerSuperSports": [{"tabs": [
        SAMPLE_NHL_RESPONSE["offerSuperSports"][0]["tabs"][0]  # WINNER_WHOLE_MATCH only
    ]}]}
    # Still fail closed (never read the 3-way regulation tab as moneyline), but
    # as the distinct market-unavailable type rather than a bare KeyError.
    with pytest.raises(TipsportMarketUnavailable):
        scraper.parse_response(only_regulation, NHL)


def test_parse_mlb_two_way(scraper):
    snaps = scraper.parse_response(SAMPLE_MLB_RESPONSE, MLB)
    by_sel = {s.selection: s for s in snaps}
    assert by_sel["home"].odds == 1.92 and by_sel["away"].odds == 1.94
    assert by_sel["home"].sport == "mlb"
    assert by_sel["home"]._home_team == "Washington Nationals"


def test_moneyline_filters_per_sport():
    from oddsiq.scrapers.tipsport import _moneyline_filters
    # MLB is the only WHOLE_MATCH (true 2-way; baseball cannot tie) -> default [].
    assert _moneyline_filters(MLB) == []
    # Hockey and basketball request their 2-way incl-OT tab explicitly.
    assert _moneyline_filters(NHL) == [
        {"matchView": "WINNER_FINAL_DECISION", "id": {"superSportId": 23}}
    ]
    assert _moneyline_filters(NBA) == [
        {"matchView": "WINNER_FINAL_DECISION", "id": {"superSportId": 7}}
    ]
    assert _moneyline_filters(WNBA) == [
        {"matchView": "WINNER_FINAL_DECISION", "id": {"superSportId": 7}}
    ]
    # American football is a FINAL_DECISION sport too (WHOLE_MATCH = regulation
    # 3-way legs, recon 2026-08-25); superSportId 2 captured from the live API.
    assert _moneyline_filters(NFL) == [
        {"matchView": "WINNER_FINAL_DECISION", "id": {"superSportId": 2}}
    ]


# ---------------------------------------------------------------------------
# WNBA parse fixture
# ---------------------------------------------------------------------------

# WNBA: same basketball engine as NBA — the 2-way incl-OT moneyline lives in
# WINNER_FINAL_DECISION (WHOLE_MATCH carries the regulation 3-way legs). Tipsport
# labels carry the women's " Ž" suffix and the league lives under the
# basketbal-zeny category (competition id 10). Odds mirror the live A/B capture
# of 2026-06-12 (FINAL_DECISION 1.25/3.70 vs regulation legs 1.28/3.84).
SAMPLE_WNBA_RESPONSE = {
    "offerSuperSports": [{
        "id": {"superSportId": 7},
        "tabs": [
            {"matchView": "WINNER_WHOLE_MATCH", "offerCompetitionAnnuals": [{
                "matches": [{
                    "participantHome": "Indiana Fever Ž",
                    "participantVisiting": "Chicago Sky Ž",
                    "nameFull": "Indiana Fever Ž - Chicago Sky Ž",
                    "oppRows": [{"oppsTab": [
                        {"label": "1", "type": "1", "odd": 1.28, "bettingEnabled": True},
                        {"label": "10", "type": "1x", "odd": 1.20, "bettingEnabled": True},
                        {"label": "0", "type": "x", "odd": 20.0, "bettingEnabled": True},
                        {"label": "02", "type": "x2", "odd": 3.17, "bettingEnabled": True},
                        {"label": "2", "type": "2", "odd": 3.84, "bettingEnabled": True},
                    ]}],
                }],
            }]},
            {"matchView": "WINNER_FINAL_DECISION", "offerCompetitionAnnuals": [{
                "matches": [{
                    "participantHome": "Indiana Fever Ž",
                    "participantVisiting": "Chicago Sky Ž",
                    "nameFull": "Indiana Fever Ž - Chicago Sky Ž",
                    "oppRows": [{"oppsTab": [
                        {"label": "Indiana", "type": "1", "odd": 1.25, "bettingEnabled": True},
                        {"label": "Chicago", "type": "2", "odd": 3.70, "bettingEnabled": True},
                    ]}],
                }],
            }]},
        ],
    }]
}


def test_parse_wnba_two_way_normalizes_z_suffix(scraper):
    snaps = scraper.parse_response(SAMPLE_WNBA_RESPONSE, WNBA)
    by_sel = {s.selection: s for s in snaps}
    assert set(by_sel) == {"home", "away"}
    # FINAL_DECISION prices, not the regulation 1.28/3.84.
    assert by_sel["home"].odds == 1.25 and by_sel["away"].odds == 3.70
    assert by_sel["home"].sport == "wnba"
    assert by_sel["home"]._home_team == "Indiana Fever"
    assert by_sel["home"]._away_team == "Chicago Sky"


def test_parse_wnba_fails_closed_without_final_decision(scraper):
    only_regulation = {"offerSuperSports": [{"tabs": [
        SAMPLE_WNBA_RESPONSE["offerSuperSports"][0]["tabs"][0]
    ]}]}
    with pytest.raises(TipsportMarketUnavailable):
        scraper.parse_response(only_regulation, WNBA)


# ---------------------------------------------------------------------------
# NFL parse fixture
# ---------------------------------------------------------------------------

# NFL: american football shares the basketball/hockey shape — WHOLE_MATCH is
# the regulation 3-way legs (a pre-OT draw is real; observed draw ~14.6 on live
# NCAAF games 2026-08-25), so the 2-way incl-OT moneyline is
# WINNER_FINAL_DECISION ("Vítěz do rozhodnutí"). Shape + identity fields mirror
# the real offer response captured at recon (match id/url + opp id/eventId feed
# the migration-009 execution-identity capture the betting engine addresses).
SAMPLE_NFL_RESPONSE = {
    "offerSuperSports": [{
        "id": {"superSportId": 2},
        "name": "Americký fotbal",
        "tabs": [
            {"matchView": "WINNER_WHOLE_MATCH", "offerCompetitionAnnuals": [{
                "matches": [{
                    "id": 9000001,
                    "participantHome": "Kansas City Chiefs",
                    "participantVisiting": "Buffalo Bills",
                    "nameFull": "Kansas City Chiefs - Buffalo Bills",
                    "url": "/kurzy/zapas/americky-fotbal-kc-buf/9000001",
                    "oppRows": [{"oppsTab": [
                        {"id": 91, "eventId": 81, "label": "1", "type": "1", "odd": 1.62, "bettingEnabled": True},
                        {"id": 92, "eventId": 82, "label": "10", "type": "1x", "odd": 1.55, "bettingEnabled": True},
                        {"id": 93, "eventId": 83, "label": "0", "type": "x", "odd": 15.0, "bettingEnabled": True},
                        {"id": 94, "eventId": 84, "label": "02", "type": "x2", "odd": 2.6, "bettingEnabled": True},
                        {"id": 95, "eventId": 85, "label": "2", "type": "2", "odd": 2.95, "bettingEnabled": True},
                    ]}],
                }],
            }]},
            {"matchView": "WINNER_FINAL_DECISION", "offerCompetitionAnnuals": [{
                "matches": [{
                    "id": 9000001,
                    "participantHome": "Kansas City Chiefs",
                    "participantVisiting": "Buffalo Bills",
                    "nameFull": "Kansas City Chiefs - Buffalo Bills",
                    "url": "/kurzy/zapas/americky-fotbal-kc-buf/9000001",
                    "oppRows": [{"oppsTab": [
                        {"id": 2670000001, "eventId": 2257000001,
                         "label": "Kansas City Chiefs", "type": "1", "odd": 1.55, "bettingEnabled": True},
                        {"id": 2670000002, "eventId": 2257000002,
                         "label": "Buffalo Bills", "type": "2", "odd": 2.72, "bettingEnabled": True},
                    ]}],
                }],
            }]},
        ],
    }]
}


def test_parse_nfl_uses_final_decision_not_regulation(scraper):
    snaps = scraper.parse_response(SAMPLE_NFL_RESPONSE, NFL)
    by_sel = {s.selection: s for s in snaps}
    assert set(by_sel) == {"home", "away"}
    # The incl-OT prices (1.55/2.72), NOT the regulation legs 1.62/2.95.
    assert by_sel["home"].odds == 1.55 and by_sel["away"].odds == 2.72
    assert by_sel["home"].sport == "nfl"
    assert by_sel["home"]._home_team == "Kansas City Chiefs"
    assert by_sel["home"]._away_team == "Buffalo Bills"
    # Execution identity (migration 009) — the betting engine addresses the
    # exact offer by these.
    assert by_sel["home"]._tipsport_match_id == 9000001
    assert by_sel["home"]._tipsport_opp_id == 2670000001
    assert by_sel["away"]._tipsport_opp_id == 2670000002
    assert by_sel["home"]._tipsport_url == "/kurzy/zapas/americky-fotbal-kc-buf/9000001"


def test_parse_nfl_fails_closed_without_final_decision(scraper):
    # The exact pre-September reality observed live 2026-08-25: only the
    # WHOLE_MATCH tab (futures/3-way) present -> market unavailable, never a
    # fallback to the regulation legs.
    only_regulation = {"offerSuperSports": [{"tabs": [
        SAMPLE_NFL_RESPONSE["offerSuperSports"][0]["tabs"][0]
    ]}]}
    with pytest.raises(TipsportMarketUnavailable):
        scraper.parse_response(only_regulation, NFL)


def test_warmup_url_uses_gender_slug():
    # The category segment between sport and league is muzi for the men's
    # leagues and zeny for WNBA — a hardcoded -muzi here would point WNBA's
    # warmup/Referer at a nonexistent page.
    assert _warmup_url(NBA).endswith("/kurzy/basketbal/basketbal-muzi/nba-23")
    assert _warmup_url(WNBA).endswith("/kurzy/basketbal/basketbal-zeny/wnba-10")


# ---------------------------------------------------------------------------
# Listing start time (_commence_time) — the wrong-game guard input
# (2026-08-30: Tipsport posts the NEXT game of an MLB series while today's is
# pregame; names-only matching pinned tomorrow's opener on today's game).
# ---------------------------------------------------------------------------

def test_parse_response_carries_listing_start_from_opp_dateclosed(scraper):
    """SAMPLE fixture has no match-level dateClosed — the per-opp fallback
    still stamps every snapshot with the listing's own start."""
    snapshots = scraper.parse_response(SAMPLE_MONEYLINE_RESPONSE, NBA)
    assert snapshots, "fixture must parse"
    expected = datetime(2026, 4, 9, 23, 10, tzinfo=timezone.utc)  # 01:10+02:00
    assert all(s._commence_time == expected for s in snapshots)


def test_parse_response_prefers_match_level_dateclosed(scraper):
    data = {
        "offerSuperSports": [{
            "id": {"superSportId": 7},
            "name": "Basketbal",
            "tabs": [{
                "matchView": "WINNER_FINAL_DECISION",
                "name": "Vítěz do rozhodnutí",
                "subTabs": [],
                "offerCompetitionAnnuals": [{
                    "id": 249904, "competitionId": 23,
                    "name": "NBA, Basketbal - muži",
                    "matches": [{
                        "id": 8442783,
                        "nameFull": "Toronto Raptors - Miami Heat",
                        "url": "/kurzy/zapas/basketbal-toronto-raptors-miami-heat/8442783",
                        "participantHome": "Toronto Raptors",
                        "participantVisiting": "Miami Heat",
                        # Match-level start (the real payload shape) wins over
                        # the per-opp fallback.
                        "dateClosed": "2026-08-30T22:07:00.000+02:00",
                        "oppRows": [{"oppsTab": [
                            {"id": 1, "eventId": 9, "label": "Toronto", "type": "1",
                             "odd": 2.58, "bettingEnabled": True,
                             "dateClosed": "2026-08-31T00:00:00.000+02:00"},
                            {"id": 2, "eventId": 9, "label": "Miami", "type": "2",
                             "odd": 1.53, "bettingEnabled": True,
                             "dateClosed": "2026-08-31T00:00:00.000+02:00"},
                        ]}],
                    }],
                }],
            }],
        }],
    }
    snapshots = scraper.parse_response(data, NBA)
    expected = datetime(2026, 8, 30, 20, 7, tzinfo=timezone.utc)  # 22:07+02:00
    assert len(snapshots) == 2
    assert all(s._commence_time == expected for s in snapshots)


def test_parse_match_start_malformed_is_none(scraper):
    assert scraper._parse_match_start({"dateClosed": "not-a-date"}) is None
    assert scraper._parse_match_start({"dateClosed": "2026-08-30T22:07:00"}) is None
    assert scraper._parse_match_start({}) is None
    assert scraper._parse_match_start({"dateClosed": None, "oppRows": []}) is None
