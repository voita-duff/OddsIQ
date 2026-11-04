"""Cloudflare JS challenge solver using Playwright headless browser.

Launches a stealth headless Chromium, solves Cloudflare's JS challenge,
then provides a session for making API requests from inside the browser
(sharing TLS fingerprint, cookies, and session state).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import async_playwright, BrowserContext, Page

from oddsiq import config

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

# Fake sec-ch-ua to hide HeadlessChrome — Tipsport checks this header.
# Brand list + GREASE value pinned to curl_cffi chrome146's actual format so
# the Playwright fallback and the curl_cffi fast path advertise identical
# Client Hints (eliminates a cross-session fingerprint discrepancy that
# could be flagged if Tipsport ever correlates).
_SEC_CH_UA = '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'

# Chromium launch args. --no-sandbox is required to run headless Chromium as
# root inside a container (the default deployment); harmless on local/macOS.
_CHROMIUM_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
]


class CloudflareSolveError(Exception):
    """Raised when Playwright fails to solve a Cloudflare challenge."""


class CloudflareSession:
    """A Playwright-backed session that can make API requests through
    a fully authenticated browser context.

    Solves Cloudflare JS challenges, waits for JSESSIONID, and patches
    client-hint headers to hide headless detection.

    Usage::

        async with CloudflareSession("https://example.com/page") as session:
            data = await session.post("https://example.com/api", json=body)
    """

    def __init__(self, warmup_url: str, timeout: float | None = None) -> None:
        self._warmup_url = warmup_url
        self._timeout = timeout or config.CF_SOLVE_TIMEOUT
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> CloudflareSession:
        """Launch browser, navigate to warmup URL, wait for session cookies."""
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=_CHROMIUM_LAUNCH_ARGS,
            )
            self._context = await self._browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1920, "height": 1080},
            )
            self._page = await self._context.new_page()

            # Hide webdriver flag
            await self._page.add_init_script(
                'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            )

            # Patch sec-ch-ua on all REST API requests to hide HeadlessChrome
            async def _fix_headers(route):
                headers = {**route.request.headers}
                headers["sec-ch-ua"] = _SEC_CH_UA
                await route.continue_(headers=headers)

            await self._page.route("**/rest/**", _fix_headers)

            logger.info("Cloudflare solver: navigating to %s", self._warmup_url)
            await self._page.goto(self._warmup_url, wait_until="domcontentloaded")

            # Wait for cf_clearance + JSESSIONID
            elapsed = 0.0
            poll_interval = 0.5
            while elapsed < self._timeout:
                cookies = {c["name"] for c in await self._context.cookies()}
                if "cf_clearance" in cookies and "JSESSIONID" in cookies:
                    logger.info(
                        "Cloudflare solver: session ready after %.1fs",
                        elapsed,
                    )
                    return self
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            # Show which cookies are missing for debugging
            cookies = {c["name"] for c in await self._context.cookies()}
            missing = {"cf_clearance", "JSESSIONID"} - cookies
            raise CloudflareSolveError(
                f"Cloudflare solver: missing cookies {missing} after {self._timeout}s"
            )

        except CloudflareSolveError:
            await self.close()
            raise
        except Exception as e:
            await self.close()
            raise CloudflareSolveError(f"Cloudflare solver: browser error — {e}") from e

    async def post(self, url: str, json: dict) -> dict[str, Any]:
        """Make a POST request from inside the browser page via fetch().

        Runs JavaScript in the page context, sharing the browser's full
        TLS fingerprint, cookies, and Cloudflare clearance.
        """
        if self._page is None:
            raise CloudflareSolveError("Session not started")
        result = await self._page.evaluate(
            """async ([url, body]) => {
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Accept': 'application/json',
                        'Content-Type': 'application/json;charset=utf-8',
                    },
                    body: JSON.stringify(body),
                    credentials: 'include',
                });
                if (!resp.ok) {
                    return { __error: true, status: resp.status };
                }
                return await resp.json();
            }""",
            [url, json],
        )
        if isinstance(result, dict) and result.get("__error"):
            status = result.get("status", "unknown")
            raise ConnectionError(
                f"Tipsport API request failed (HTTP {status}) via Playwright for {url}"
            )
        return result

    async def export_cookies(
        self, names: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return browser-context cookies, optionally filtered by name."""
        if self._context is None:
            raise CloudflareSolveError("Session not started")
        cookies = await self._context.cookies()
        if names is None:
            return cookies
        return [cookie for cookie in cookies if cookie.get("name") in names]

    async def close(self) -> None:
        """Shut down the browser. Safe to call even if already closed."""
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._context = None
        self._page = None

    async def __aenter__(self) -> CloudflareSession:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
