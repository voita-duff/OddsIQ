"""Startup browser preflight.

Playwright's headless Chromium is required by the browser-based scraper
(Tipsport). The bundled browser-build version is coupled to the
installed ``playwright`` package version, so a package upgrade without a
matching ``playwright install`` leaves the binary missing — which otherwise
surfaces only as a per-poll ``BrowserType.launch`` failure storm (the
2026-05-30 outage ran ~18h fully degraded before anyone noticed). This
one-shot probe lets the scheduler surface that loudly at boot instead.
"""

from __future__ import annotations


async def chromium_available() -> tuple[bool, str | None]:
    """Launch and immediately close a headless Chromium.

    Returns ``(True, None)`` on success, or ``(False, <first error line>)``
    when Playwright cannot launch the browser (missing binary, version drift,
    etc.). Never raises — the caller decides whether to warn or abort.
    """
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        try:
            # --no-sandbox: preflight must succeed in the container (root) too.
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            await browser.close()
        finally:
            await pw.stop()
        return True, None
    except Exception as e:  # noqa: BLE001 — preflight must never raise
        text = str(e).strip()
        first_line = text.splitlines()[0] if text else type(e).__name__
        return False, first_line
