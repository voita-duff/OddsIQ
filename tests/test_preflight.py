import asyncio
import logging
from types import SimpleNamespace

from oddsiq.scrapers import preflight


# ---------------------------------------------------------------------------
# chromium_available() — the launch probe
# ---------------------------------------------------------------------------


def test_chromium_available_true_when_launch_succeeds(monkeypatch):
    """Happy path: launches and cleanly tears down browser + runtime."""
    closed = {"browser": False, "pw": False}

    async def fake_launch(**_kwargs):
        async def close():
            closed["browser"] = True

        return SimpleNamespace(close=close)

    async def fake_stop():
        closed["pw"] = True

    async def fake_start():
        return SimpleNamespace(
            chromium=SimpleNamespace(launch=fake_launch), stop=fake_stop
        )

    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: SimpleNamespace(start=fake_start),
    )

    ok, err = asyncio.run(preflight.chromium_available())

    assert ok is True
    assert err is None
    assert closed["browser"] and closed["pw"], "must close browser and stop runtime"


def test_chromium_available_false_returns_first_error_line(monkeypatch):
    """Missing-binary failure (the 2026-05-30 case) returns a one-line reason."""

    async def fake_start():
        raise RuntimeError(
            "BrowserType.launch: Executable doesn't exist at /…/chrome-headless-shell\n"
            "Please run: playwright install"
        )

    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: SimpleNamespace(start=fake_start),
    )

    ok, err = asyncio.run(preflight.chromium_available())

    assert ok is False
    assert err is not None
    assert "BrowserType.launch" in err
    assert "\n" not in err, "only the first error line should be surfaced"


# ---------------------------------------------------------------------------
# Scheduler wiring — gating + loud warning
# ---------------------------------------------------------------------------


def test_scheduler_preflight_warns_when_browser_unavailable(monkeypatch, caplog):
    from oddsiq.pipeline.scheduler import Scheduler

    sch = Scheduler(SimpleNamespace())
    sch._scrapers = [SimpleNamespace(name="tipsport"), SimpleNamespace(name="pinnacle")]

    async def fake_unavailable():
        return False, "BrowserType.launch: Executable doesn't exist"

    monkeypatch.setattr("oddsiq.pipeline.scheduler.chromium_available", fake_unavailable)

    with caplog.at_level(logging.WARNING, logger="oddsiq.pipeline.scheduler"):
        asyncio.run(sch._check_browser_preflight())

    assert any(
        "Browser preflight FAILED" in r.getMessage() for r in caplog.records
    ), f"expected a loud preflight warning, got: {[r.getMessage() for r in caplog.records]}"


def test_scheduler_preflight_skipped_without_browser_books(monkeypatch):
    from oddsiq.pipeline.scheduler import Scheduler

    sch = Scheduler(SimpleNamespace())
    sch._scrapers = [
        SimpleNamespace(name="pinnacle"),
    ]
    called = {"v": False}

    async def fake_probe():
        called["v"] = True
        return True, None

    monkeypatch.setattr("oddsiq.pipeline.scheduler.chromium_available", fake_probe)

    asyncio.run(sch._check_browser_preflight())

    assert called["v"] is False, "preflight must be skipped when no browser book is enabled"
