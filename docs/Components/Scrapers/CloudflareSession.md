---
tags:
  - component/scraper
  - status/complete
  - phase/p0
  - layer/data-ingestion
  - type/component
source-files:
  - oddsiq/scrapers/cloudflare.py
  - oddsiq/scrapers/preflight.py
  - tests/test_cloudflare.py
related:
  - "[[TipsportScraper]]"
  - "[[Mitigated - Cloudflare Session Lifecycle]]"
  - "[[First Game Night]]"
  - "[[Observed Latency]]"
---

# CloudflareSession

## Purpose

Playwright-based Cloudflare JS challenge solver and browser-session bootstrapper, used exclusively by [[TipsportScraper]]. Pinnacle and The Odds API are pure `httpx` — no browser involved.

`CloudflareSession` launches a disguised headless Chromium, solves Cloudflare's challenge, acquires `cf_clearance` + `JSESSIONID`, and exports the browser-created `JSESSIONID` for handoff to the curl_cffi fast path. When curl still fails after JSESSIONID sync, `post()` executes the API request from inside the browser context as a last resort.

This file also covers `chromium_available()` from `oddsiq/scrapers/preflight.py` — the startup probe that guards against missing Playwright binaries.

## Source Files

| File | Symbol | Description |
|------|--------|-------------|
| `oddsiq/scrapers/cloudflare.py` | `CloudflareSession` | Full implementation; async context manager |
| `oddsiq/scrapers/cloudflare.py` | `CloudflareSession.start()` | Browser launch, anti-detection patches, `cf_clearance` + `JSESSIONID` polling |
| `oddsiq/scrapers/cloudflare.py` | `CloudflareSession.post(url, json)` | API request via `page.evaluate(fetch(...))` inside browser context |
| `oddsiq/scrapers/cloudflare.py` | `CloudflareSession.export_cookies(names)` | Browser-context cookie export, optionally filtered by name; used to hand `JSESSIONID` to curl_cffi |
| `oddsiq/scrapers/cloudflare.py` | `CloudflareSession.close()` | Browser cleanup, exception-suppressed; safe to call repeatedly |
| `oddsiq/scrapers/cloudflare.py` | `CloudflareSolveError` | Raised when CF challenge fails or browser dies during startup |
| `oddsiq/scrapers/cloudflare.py` | `_USER_AGENT`, `_SEC_CH_UA` | Chrome 146 on macOS UA + client-hint patch string |
| `oddsiq/scrapers/preflight.py` | `chromium_available()` | One-shot startup probe: launches and immediately closes Chromium; returns `(True, None)` on success or `(False, error_line)` on failure |
| `tests/test_cloudflare.py` | — | 6 tests: cookie wait, timeout, `page.evaluate`, cookie export, error handling, cleanup |

## Inputs

- **`warmup_url`**: Tipsport sport page URL, built from [[SportProfile]] slugs by `_warmup_url(profile)` in `tipsport.py`
- **`timeout`**: max seconds to wait for CF cookies (default `CF_SOLVE_TIMEOUT=30.0s`)
- `post(url, json)`: URL and JSON body for the API request

## Outputs

- Browser context with `cf_clearance` + `JSESSIONID` cookies
- `export_cookies({"JSESSIONID"})` → list of cookie dicts for [[TipsportScraper]] to copy into curl_cffi
- `post()` → parsed JSON `dict` from the in-browser `fetch()` call
- `chromium_available()` → `(bool, str | None)` — never raises

## Interactions

- [[TipsportScraper]] — sole consumer. Uses `export_cookies` to copy `JSESSIONID` into the curl jar; falls back to in-page `post()` only when curl still fails after that sync. Proactively refreshes sessions at `SESSION_MAX_AGE` (20 min) via `_maybe_refresh_session()`.
- [[Scheduler]] / [[Pipeline Services]] — `Scheduler._check_browser_preflight()` calls `chromium_available()` once at boot when Tipsport is enabled; logs a WARNING with the `playwright install chromium` remediation on failure, then continues (REST books are unaffected).
- [[Mitigated - Cloudflare Session Lifecycle]] — proactive refresh at `SESSION_MAX_AGE` prevents the expired-cookie dead cycle.
- [[Observed Latency]] — initial CF solve ~2s typical, up to 30s (timeout); API requests via Playwright 2-10s vs ~200ms for curl_cffi.

## Internal Logic

### Headless disguise (load-bearing)

Playwright 1.60 `headless=True` runs `chrome-headless-shell`, whose user-agent contains `"HeadlessChrome"` and leaves `navigator.webdriver=true`. Tipsport (and Cloudflare) detect both signals. Three patches are applied in `start()`:

**1. Hide `navigator.webdriver`**:

```python
await self._page.add_init_script(
    'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
)
```

**2. Patch `sec-ch-ua` on REST routes** — replaces `"HeadlessChrome"` in Client Hints with a legitimate Chrome 146 string on all `**/rest/**` routes:

```python
_SEC_CH_UA = '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'
```

The patch is pinned to match `curl_cffi chrome146`'s actual Client Hints — eliminating a cross-session fingerprint discrepancy that could be flagged if Tipsport correlates the two.

**3. Real desktop-Chrome UA + `--disable-blink-features=AutomationControlled`**:

```python
self._browser = await self._pw.chromium.launch(
    headless=True,
    args=["--disable-blink-features=AutomationControlled"],
)
self._context = await self._browser.new_context(
    user_agent=_USER_AGENT,   # Chrome 146 on macOS
    viewport={"width": 1920, "height": 1080},
)
```

Stripping any of these three patches causes Tipsport/Cloudflare to block the session.

### Cookie polling

After navigating to `warmup_url`, polls every 0.5 s until `cf_clearance` and `JSESSIONID` are both present, or `timeout` (30s) expires. On timeout raises `CloudflareSolveError` with details on which cookies are missing.

### `export_cookies` for JSESSIONID handoff

```python
cookies = await session.export_cookies({"JSESSIONID"})
```

[[TipsportScraper]] copies only `JSESSIONID` into its curl_cffi cookie jar. Playwright's Cloudflare cookies are deliberately not copied — they are bound to the browser TLS fingerprint and are rejected on a different TLS stack.

### In-browser API requests (last-resort fallback)

`post()` runs `fetch()` from inside the live browser page via `page.evaluate(...)`:
- TLS fingerprint matches a real Chrome browser
- `cf_clearance` and `JSESSIONID` are sent automatically (`credentials: 'include'`)
- The `sec-ch-ua` route interceptor applies

Non-OK responses are signaled via an `__error` sentinel dict and raised as `ConnectionError`. [[TipsportScraper]] wraps this call in `asyncio.wait_for(..., timeout=60)` to prevent a `page.evaluate(fetch(...))` that never resolves from freezing the event loop (the pattern that caused a 17-min hang on [[First Game Night]]).

### Startup Chromium preflight (`chromium_available`)

```python
async def chromium_available() -> tuple[bool, str | None]:
    ...
    browser = await pw.chromium.launch(headless=True)
    await browser.close()
    return True, None
```

Launches and immediately closes Chromium. Returns `(False, error_line)` on any exception, never raises. [[Scheduler]] calls this once at boot when Tipsport is enabled; on failure it logs a WARNING with the `playwright install chromium` command but does not abort — REST books (Pinnacle) are unaffected. This probe closed the silent gap that took Tipsport/Betano down ~18h on 2026-05-30 when a Playwright package bump left the browser binary missing.

## Configuration

| Constant | Value | Source |
|----------|-------|--------|
| `CF_SOLVE_TIMEOUT` | 30.0s | `config.py` |
| `_USER_AGENT` | Chrome 146 on macOS | `cloudflare.py` |
| `_SEC_CH_UA` | `"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"` | `cloudflare.py` |
| `SESSION_MAX_AGE` | 1200s (20 min) | `tipsport.py` — lifecycle managed by [[TipsportScraper]] |
| Playwright pinned at | `~=1.60.0` | `pyproject.toml` — must be followed by `playwright install` on any bump |

## Error Handling

- **`CloudflareSolveError`**: cookie polling timeout or browser crash during startup. `start()` calls `close()` before re-raising (no resource leaks).
- **Other browser errors during startup**: wrapped in `CloudflareSolveError`.
- **`post()` on unstarted session**: raises `CloudflareSolveError("Session not started")`.
- **`export_cookies()` on unstarted session**: raises `CloudflareSolveError("Session not started")`.
- **Non-OK API response from `post()`**: detected via `__error` sentinel, raised as `ConnectionError`.
- **Cleanup failures in `close()`**: silently swallowed.

## Known Issues

- **No internal cookie refresh**: cookie expiry tracking and refresh is entirely managed by [[TipsportScraper]]'s `_maybe_refresh_session()` and `MAX_CONSECUTIVE_FAILURES` safety net. `CloudflareSession` is created, used, and destroyed by its consumer.
- **Playwright resource weight**: each instance runs a full Chromium browser. Only one instance exists at a time.

## Tests

`tests/test_cloudflare.py` — 6 tests: cookie wait loop, timeout when cookies never arrive, `page.evaluate` fetch execution, cookie export for selected names, error handling for non-OK responses, and cleanup on failure.
