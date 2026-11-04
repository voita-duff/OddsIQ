from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oddsiq.scrapers.cloudflare import CloudflareSession, CloudflareSolveError


def _make_fake_playwright(cookies_sequence, page_eval_result=None):
    """Build a mock playwright stack.

    *cookies_sequence*: list of lists of cookie dicts returned by
    successive ``context.cookies()`` calls.
    """
    page = AsyncMock()
    page.goto = AsyncMock()
    page.add_init_script = AsyncMock()
    page.route = AsyncMock()
    if page_eval_result is not None:
        page.evaluate = AsyncMock(return_value=page_eval_result)
    else:
        page.evaluate = AsyncMock(return_value={"data": "test"})

    context = AsyncMock()
    context.cookies = AsyncMock(side_effect=cookies_sequence)
    context.new_page = AsyncMock(return_value=page)
    context.pages = [page]

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    chromium = AsyncMock()
    chromium.launch = AsyncMock(return_value=browser)

    pw = AsyncMock()
    pw.chromium = chromium
    pw.stop = AsyncMock()

    pw_ctx = MagicMock()
    pw_ctx.start = AsyncMock(return_value=pw)
    factory = MagicMock(return_value=pw_ctx)

    return factory, browser, page


@pytest.mark.asyncio
async def test_session_starts_when_cookies_present():
    """Session starts successfully when cf_clearance + JSESSIONID appear."""
    cookies_poll = [
        [{"name": "__cf_bm", "value": "abc"}],
        [
            {"name": "__cf_bm", "value": "abc"},
            {"name": "cf_clearance", "value": "xyz123"},
            {"name": "JSESSIONID", "value": "sess1"},
        ],
    ]
    factory, browser, _ = _make_fake_playwright(cookies_poll)

    with patch("oddsiq.scrapers.cloudflare.async_playwright", factory):
        session = CloudflareSession("https://example.com", timeout=5.0)
        await session.start()
        assert session._page is not None
        await session.close()

    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_raises_on_timeout():
    """Session raises CloudflareSolveError when required cookies never appear."""
    no_clearance = [{"name": "__cf_bm", "value": "abc"}]
    factory, browser, _ = _make_fake_playwright([no_clearance] * 20)

    with patch("oddsiq.scrapers.cloudflare.async_playwright", factory):
        session = CloudflareSession("https://example.com", timeout=1.0)
        with pytest.raises(CloudflareSolveError, match="missing cookies"):
            await session.start()

    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_post_uses_page_evaluate():
    """Session.post() delegates to page.evaluate() with fetch()."""
    cookies_poll = [
        [
            {"name": "cf_clearance", "value": "ok"},
            {"name": "JSESSIONID", "value": "sess1"},
        ],
    ]
    api_response = {"offerSuperSports": []}
    factory, browser, page = _make_fake_playwright(
        cookies_poll, page_eval_result=api_response
    )

    with patch("oddsiq.scrapers.cloudflare.async_playwright", factory):
        session = CloudflareSession("https://example.com", timeout=5.0)
        await session.start()
        result = await session.post("https://example.com/api", json={"key": "val"})
        assert result == api_response
        page.evaluate.assert_awaited_once()
        await session.close()


@pytest.mark.asyncio
async def test_export_cookies_filters_by_name():
    """Session.export_cookies() returns only requested browser cookies."""
    cookies = [
        [
            {"name": "cf_clearance", "value": "ok"},
            {"name": "JSESSIONID", "value": "sess1"},
            {"name": "__cf_bm", "value": "browser-cookie"},
        ],
    ]
    cookies_poll = cookies * 2
    factory, _, _ = _make_fake_playwright(cookies_poll)

    with patch("oddsiq.scrapers.cloudflare.async_playwright", factory):
        session = CloudflareSession("https://example.com", timeout=5.0)
        await session.start()
        result = await session.export_cookies({"JSESSIONID"})
        assert result == [{"name": "JSESSIONID", "value": "sess1"}]
        await session.close()


@pytest.mark.asyncio
async def test_post_raises_on_error_response():
    """Session.post() raises ConnectionError when fetch returns an error."""
    cookies_poll = [
        [
            {"name": "cf_clearance", "value": "ok"},
            {"name": "JSESSIONID", "value": "sess1"},
        ],
    ]
    error_result = {"__error": True, "status": 403}
    factory, browser, _ = _make_fake_playwright(
        cookies_poll, page_eval_result=error_result
    )

    with patch("oddsiq.scrapers.cloudflare.async_playwright", factory):
        session = CloudflareSession("https://example.com", timeout=5.0)
        await session.start()
        with pytest.raises(ConnectionError, match="HTTP 403"):
            await session.post("https://example.com/api", json={})
        await session.close()


@pytest.mark.asyncio
async def test_browser_closed_on_error():
    """Browser is closed even when navigation raises."""
    page = AsyncMock()
    page.add_init_script = AsyncMock()
    page.route = AsyncMock()
    page.goto = AsyncMock(side_effect=RuntimeError("browser crashed"))

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    chromium = AsyncMock()
    chromium.launch = AsyncMock(return_value=browser)

    pw = AsyncMock()
    pw.chromium = chromium
    pw.stop = AsyncMock()

    pw_ctx = MagicMock()
    pw_ctx.start = AsyncMock(return_value=pw)
    factory = MagicMock(return_value=pw_ctx)

    with patch("oddsiq.scrapers.cloudflare.async_playwright", factory):
        session = CloudflareSession("https://example.com", timeout=5.0)
        with pytest.raises(CloudflareSolveError, match="browser crashed"):
            await session.start()

    browser.close.assert_awaited_once()
