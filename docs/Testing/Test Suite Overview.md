---
tags:
  - type/component
  - status/complete
  - phase/p0
  - layer/operations
---
# Test Suite Overview

878 tests across 50 test files. Mostly unit tests with mocked dependencies, plus an end-to-end lifecycle integration suite and real-Chromium behavior checks for the extension's page-context selectors. Canonical count: `pytest --collect-only -q`.

**CI & lint:** `.github/workflows/ci.yml` installs Playwright Chromium, then runs `ruff check` + this suite + `scripts/docs-audit.py` on every push to `main` and every PR (Python 3.11 and 3.12). Locally: `make lint` / `make test` / `make audit`.

## Test Coverage

Counts are kept rough — run `pytest --collect-only -q tests/<file>.py` for the exact current number, and `scripts/docs-audit.py` to detect drift in this table.

| File | Tests | Coverage |
| --- | --- | --- |
| `test_scheduler.py` | 19 | Polling tier intervals, concurrent scraper fetch fan-out with ordered processing, shared schedule/completion refresh throttle, terminal result score order and timestamp/game-id labels, hot+warm-tier edge detection, Pinnacle budget gating + forced shutdown persistence, commence_time reconciliation, per-scraper health log emission, heartbeat state derivation (idle-no-games / polling / between-game-windows), throttle-level interval widening, per-sport poll gating, per-sport poll-failure isolation (one sport's error can't starve later sports), run-loop stop-wake, startup schedule health-check WARNING log (was a Telegram alert until the 2026-08-10 rebrand), periodic backups (fires only past `BACKUP_INTERVAL_HOURS`, the moment literal pinned to `backup._VALID_MOMENTS`, and a raising backup never escapes `_maybe_periodic_backup` — it logs, the clock still advances, so a dead disk stays once-per-interval instead of costing a poll cycle) |
| `test_integration_lifecycle.py` | 7 | End-to-end against real Scheduler + Store (only scraper HTTP mocked): hot-tier poll → edge detection → closing-snapshot mark → CLV + win/loss resolution on final game; total-market over/under/push resolution; scraper-error isolation (one scraper raising never aborts the cycle) |
| `test_store.py` | 39 | Game upsert, snapshot batch insert + line-aware dedup (incl. aligned raw/source provenance and never bumping a closing row), value-bet duplicate handling under line/tier/exclusion identity, freshness-gated closing marks, recovery helpers, meta, and zombie cascade |
| `test_backup.py` | 28 | Filename formatting, `PRAGMA integrity_check` on valid/corrupt DBs, `sqlite3.Connection.backup()` captures WAL data, retention pruning (daily + monthly) + protect-just-written under 0/0 retention, failure modes, plus the `periodic` moment (2026-09-06): it is accepted and written as `-periodic.db`, retention pools periodic files with startup/shutdown in the one daily/monthly budget, the timestamp-collision tiebreak is a three-way total order (shutdown > periodic > startup) with the `shutdown > periodic` rung covered on its own, `_MOMENT_RANK` is the single source both the validation set and the retention filename regex derive from (the regex matches exactly those three moments and nothing else), and unknown moments still raise `ValueError` |
| `test_tipsport.py` | 35 | Moneyline parsing per sport (NBA/NHL/MLB/WNBA/NFL fixtures), curl fingerprint headers, 401 browser `JSESSIONID` sync + curl retry, 4xx fallback after retry, 3-consecutive-failure session-reset path, warm-up flow, invalid-odds hardening, team attachment, Playwright reuse, tab-aware `_count_matches`, missing moneyline matchView tab raises `TipsportMarketUnavailable` (market-not-offered, NOT counted as a session failure), `close()` guards each session independently, unexpected response structure propagates instead of silent `[]`, WNBA 2-way parse with Ž-suffix label normalization, gender-slug warmup URL (`-muzi` vs `-zeny`), basketball (NBA + WNBA) fail-closed without `WINNER_FINAL_DECISION` (the WHOLE_MATCH tab is the regulation 3-way for basketball), NFL FD 2-way parse incl. execution-identity fields + fail-closed without the FD tab + per-sport `_moneyline_filters` (NFL superSportId 2) |
| `test_migrations.py` | 21 | Discovery + sort order, schema_version bootstrap, idempotence, transaction rollback on failure, concurrent-runner re-check skip, baseline + 001-004 application (tier column + unique index), migration 011 drops live_edge_candidates (idempotent; post-chain table absent), creates missing DB parent dirs, ≤100 ms up-to-date overhead |
| `test_telegram.py` | 31 | Bettor-facing formatters only (2026-08-10 rebrand): bet-placed (money-first: stake/odds/payout/balance), win/loss (real-Kč P&L from the placed bet + cumulative `Total:`, score-with-pick line, accepted-odds not entry), auto-disarm (slim, humanized reason), rejected/failed attempt formatter (2026-08-25), login lost/restored + not-logged-in-skip formatters (2026-08-30) + NFL 🏈 emoji. Plus HTML escaping, and the send infra: disabled-when-unconfigured, API payload, success-send audit log (`telegram_sent:` on 200 only), error swallowing, tag-safe truncation (no mid-tag split, dangling `<b>` re-balanced), dual-recipient fan-out (mirror to account 2, independent send, one-failure-doesn't-block-other), `is_configured()`. The cut formatters (value-bet, CLV, coverage-gap, session-summary, live-edge, health-ping) were deleted with their send-sites |
| `test_cli.py` | 55 | `--check-budget` output, probe-failure exit, missing API key, arg parsing, `cleanup-closing-flags` dry-run, `delete-zombie-games` (parse_args, backstop+grace+status selection, cascade delete scoped to zombies, dry-run no-write), missed-gameday audit (WARNING log, no Telegram since the 2026-08-10 rebrand), backup-failure WARNING log, raw-response retention toggle, audit-bookmakers + parse_args + filter round-trip + malformed-`--since` rejection + `--since` UTC offset normalization, all four `*-smoke` commands (parse_args + healthy/fail modes incl. the two-way overround line), `oddsiq betting` status printing the EFFECTIVE caps clamped to the env ceilings (the printed value matches the engine's own clamp) and `--reconcile` reporting whether it disarmed, logging hygiene (httpx/httpcore/urllib3 silenced + secret-redaction filter + idempotent install) |
| `test_clv.py` | 15 | CLV calculation, bet resolution (moneyline/spread/total, win/loss/push), unknown market + unknown selection raise `ValueError` |
| `test_pinnacle.py` | 25 | Parse response (incl. team-name normalization to canonical), exact event-id/source-timestamp provenance, missing-id and malformed/naive/future-`last_update` handling, `probe_budget` (success, missing headers, HTTP error, malformed headers), `_parse_commence_time`, captured_at threading, single-key construction, budget-exhaustion skip (not counted as a fetch error), fail-soft `[]` on HTTP error with `last_fetch_error_count=1` surfaced to the health log, profile-driven URL + sport stamping (NBA/NHL), per-fetch `last_fetch_error_count` (reset per call), invalid-price outcome skipping (non-numeric / non-finite / ≤1.0) |
| `test_edge.py` | 8 | Edge calculation, line matching, mismatches, spread strict same-side line check |
| `test_normalize.py` | 11 | Canonical passthrough (NBA + WNBA + NFL), Tipsport NBA alias mapping, WNBA Ž-suffix alias mapping, fuzzy fallback, empty/whitespace guard (silent), unknown team returns None + logs error |
| `test_audit_bookmakers.py` | 16 | Five audit sections (multi-book overlap removed), --bookmaker/--since filters (flagged_at-based, incl. snapshotless bets), warm/hot tier dedup, text and JSON formatters, orphan-bet edge case, far-tier shadow rows excluded from the audit ROI |
| `test_dashboard_queries.py` | 96 | Read-only dashboard SQL including performance/shadow exclusions, schedule/coverage, activity, placed-bet KPIs/tables, and effective hard-clamped betting cap display. |
| `test_dashboard_server.py` | 104 | FastAPI routes/partials, dashboard state/rendering, sharp-provenance warning badges, betting controls and sport gate, immutable cap clamps, fail-closed app Basic auth (incl. the `GET /betting` page challenge and the `X-Forwarded-Host` same-origin path), and same-origin mutation enforcement. |
| `test_log_tail.py` | 26 | Structured log-line parsing, heartbeat/scraper-health extraction including sharp provenance, event tail, malformed-line tolerance, suspect events, and bridge-state scanning |
| `test_dashboard_state.py` | 8 | Four-state pipeline derivation (`alive` / `idle-no-games` / `stale` / `unknown`) from heartbeat age + self-reported state |
| `test_preflight.py` | 4 | `chromium_available()` startup browser preflight: launch success, missing-binary / version-drift failure, error message includes the `playwright install chromium` remediation |
| `test_cloudflare.py` | 6 | Cookie wait, timeout, `page.evaluate`, filtered cookie export, error handling, cleanup |
| `test_vig.py` | 6 | Implied probability, vig removal (balanced/typical/favorite), invalid-odds `ValueError` |
| `test_models.py` | 4 | Dataclass creation; `_commence_time` field on `OddsSnapshot`; `LiveEdgeCandidate` shape |
| `test_base_scraper.py` | 5 | Raw-response filename uniqueness at microsecond precision, retention pruner (delete old / keep recent / ignore non-date dirs / no-op on missing root / keep-days zero) |
| `test_config.py` | 5 | Schedule-refresh cadence defaults (`SCHEDULE_GAME_WINDOW_REFRESH` 900s, `SCHEDULE_REFRESH_INTERVAL` 10800s, `SCHEDULE_COMPLETION_CHECK_INTERVAL` 900s) + env overrides, and the placement-timeout invariant (`BETTING_PLACEMENT_TIMEOUT_SECONDS` must clear the extension's 44s of enforced waits with ≥10s headroom for the uncapped receipt read) |
| `test_poll_coordinator.py` | 40 | Polling tier routing per-game (far/warm/hot; post-tipoff skipped entirely — no snapshots, no detection, no Pinnacle), exact Pinnacle event-id matching, commence_time reclassification (a shared team pair never corrupts it), snapshot dedup on unchanged odds, the ±2h Tipsport commence-time window and the fail-closed start-time rule (a listing with no trustworthy start is DROPPED rather than attributed, counted as `unsafe_start_snapshots`) while a doubleheader whose legs each carry their own listing time still detects, provenance health metrics on the `Scraper health:` line, scraper fan-out isolation (incl. a task-level fetch exception not sinking siblings), far-tier shadow fetch/detect for `shadow_far_detection` sports, and the two-phase collect/dispatch handoff with per-game `detect_failed:` isolation |
| `test_schedule_service.py` | 22 | Schedule refresh throttling, per-sport active-window fetch (idle sports skipped) + periodic full sweep + partial-refresh slate merge, completion-check fetch scoped to live sports + config-driven throttle window (`SCHEDULE_COMPLETION_CHECK_INTERVAL`), game-completion checks, live-transition handling (isolation + closing-snapshot marking), historical-bet recovery after restart (incl. per-game recovery-failure isolation), final-game log emission |
| `test_settlement_service.py` | 14 | CLV backfill (spread opposite-signed Pinnacle pairing, already-resolved bets), result resolution and structured log, excluded-bet silent settlement (CLV/result written, no Telegram/stats), historical-bet recovery, and shadow (`tier='far'`) settlement — CLV is written but kept out of `SessionStats` and logged `clv_shadow:`, while the result still resolves and logs normally |
| `test_budget_governor.py` | 22 | Budget-state persistence to `meta`, throttle levels 0-3 incl. level-1 boundary (warm-only widening), reserve-floor override, <1-day-into-period guard, non-calendar + reset-day-boundary projection, unknown-tier fallback, skip-when-unprobed default, Pinnacle snapshot accessors, rollover + exhaustion-recovery re-probes (hourly-until-refill, interval throttle, period not consumed) |
| `test_coverage_monitor.py` | 10 | Coverage-gap detection (stale Pinnacle, never-succeeded scraper, extra scrapers), alert deduplication per (game, scraper) pair, dedup-key + match-timestamp pruning for games dropped from the slate (per-scraper last-seen survives) |
| `test_edge_service.py` | 27 | Pregame detection/persistence, all plausibility/coherence/in-play/completeness/source-freshness guards, clean recovery after suspect evidence, provenance handoff, ordered dispatch isolation, shadow stats, and real signature binding |
| `test_odds_api_schedule.py` | 7 | Scores-endpoint parsing (final/live/upcoming), `ScheduleFetchError` on 4xx, 5xx retried with backoff then succeeds, health check, malformed-JSON body raises `ScheduleFetchError` |
| `test_tiers.py` | 6 | Tier boundary classification (far/warm/hot, `None` post-tipoff), `get_poll_interval` cadence per tier, Pinnacle-needed flag per tier incl. the `shadow_far=True` far-tier case, per-sport live backstop |
| `test_sports.py` | 3 | `SportProfile` fields, registry lookup by key, frozen-dataclass constraint |
| `test_sports_profiles.py` | 11 | Enabled-sports set (mlb+nfl enabled, five profiled), team-set sizes, per-sport market config (MLB whole-match; NHL/NBA/WNBA/NFL final-decision), WNBA profile pins (competition 10, `zeny` gender slug, 2.5h backstop), NFL profile pins (competition 5748, superSportId 2, 3.5h backstop), the `shadow_far_detection` flag (NFL `True`, the other four `False`), muzi gender-slug default, two-way market rule |
| `test_migration_005.py` | 2 | Migration 005 adds `sport` column + backfills existing rows to `nba`; idempotent on re-run |
| `test_migration_007.py` | 3 | Migration 007 adds nullable `excluded_reason` to `value_bets` + `live_edge_candidates`; fresh rows default NULL; idempotent on re-run |
| `test_migration_009.py` | 2 | Migration 009 adds nullable Tipsport execution-identity columns (`tipsport_match_id`/`tipsport_opp_id`/`tipsport_url`) to `value_bets` only (odds_snapshots untouched); fresh rows default NULL |
| `test_migration_010.py` | 2 | Migration 010 creates the `placed_bets` decision ledger; `value_bet_id` UNIQUE (double-bet guard lives in the schema) |
| `test_migration_012.py` | 1 | Migration 012 adds nullable upstream source timestamps to snapshots and value bets |
| `test_migration_013.py` | 1 | Migration 013 scopes the value-bet unique index by normalized exclusion reason |
| `test_betting_store.py` | 8 | placed_bets CRUD, decision dedup, cap aggregates, settlement P&L including requested-odds fallback, evidence-required pending/unknown reconciliation, and zombie cascade. |
| `test_betting_service.py` | 39 | Qualification chain including upstream sharp-source age, immutable money/count ceilings, stake hardening, circuit breaker, timeout/unknown disarm, sport/tipoff/cross-tier gates, notifications, and accepted-odds anomaly detection. |
| `test_betting_bridge.py` | 18 | Real-WebSocket protocol tests: bad-token close, hello/ack + connected state, logged-in status updates, place/result correlation round-trip, timeout → PlacementTimeout, disconnect-mid-placement → PlacementTimeout (unknown semantics), no-client → PlacementError, supersede: second client kicks first with 4409 and old close never clears the new client, result step-timings logged (`result_timings`), login lost/restored notifier state machine (status-only "lost", debounced against flapping with cancel-on-restore, startup watchdog for sessions dead at boot, paired restore, hello-False never alerts, state survives reconnects, notifier exceptions contained), app-level keepalive pings |
| `test_extension_selectors.py` | 20 | Source/fixture contract for extension 0.8.3: placement mutex, login confirmation, exact offer identity, fail-closed empty/singleton slip proof, stale-rect refusal, stake read-back before confirm, bounded waits, and version lockstep.; `loggedIn()` keys only on the session timer (no avatar) and the placement's login gate on the pinned tab precedes navigation; the session-refresh keepalive is hourly and migrates the persisted alarm period; every trusted click target (odd, stake, remove, submit) is scrolled on screen and viewport-checked before its rect is measured, both `offer_gone` exits carry per-opp diagnostics, and the actuator Chrome launches with a full-size window |
| `test_extension_runtime.py` | 13 | Real headless Chromium executes `selectors.js` against the captured live slip (`tests/fixtures/tipsport_offer_slip.html`), hermetic — `set_content` + `add_script_tag`, no network — and SKIPS (never errors) when the browser binary is absent, so a fresh clone still passes: `findOddButton` compares the opp-id field exactly (a value that occurs only in the match-id field returns `null`, the substring trap), `slipLegCount` counts DISTINCT legs (a cloned duplicate still counts 1; a genuinely new leg counts 2), and `stakeAmount` fails closed — Czech decimals parse (`1 234,5` → 1234.5, `10,50` → 10.5) while `"10 Kč"` / empty / whitespace / garbage read `null`, never `0`, with `stakeRaw()` returning the field verbatim (and `null` when the input is gone) so a `stake_mismatch` refusal names the unreadable form; the login probe ignores a feed of 20 `profile-avatar` nodes and keys on the session timer alone (2026-09-09 incident); a stubbed worker verifies that hourly refreshes deferred by probes or placements retry on the next free keepalive tick; `clickPoint()` scrolls a control 3000px below the fold into a 600px viewport and returns on-screen coordinates (null for hidden/missing), and `oddDiagnostics()` reports node count, rects, viewport and document height |
| `test_excluded_rows.py` | 10 | `excluded_reason` annotation (2026-06 basketball matchView remediation): `Store.mark_rows_excluded` stamps only matching sport+window (dry-run; skips already-excluded), reporting surfaces (dashboard aggregates/recent lists/activity feed, per-book audit, `get_live_edge_candidates`) omit stamped rows, `oddsiq mark-excluded` CLI stamp / dry-run / inverted-window rejection |
| `test_retry.py` | 6 | Shared backoff helper: returns on success without sleeping, retries-then-reraises with full jitter (injected rng/sleep), honors `Retry-After`, non-retryable propagates immediately, `parse_retry_after` (delta-seconds only) |
| `test_portability.py` | 7 | Cross-platform portability: UTF-8 raw-response + log-handler writes, repo-relative `BACKUP_DIR` default, platform-guarded signal handlers (POSIX `add_signal_handler` vs Windows `signal.signal`), SQLite `busy_timeout`, Chromium `--no-sandbox` launch args |
| `test_resilience.py` | 3 | 24/7 resilience: `refresh` prunes handled-live to slate, guarded startup recovery (swallows errors), and `_maybe_alert_budget` — exhaustion + max-throttle each WARN exactly once and never re-log (they were Telegram alerts until the 2026-08-10 rebrand; the phone now carries real-money events only). The live-scout dedup-prune and Telegram health-ping cases went away with the live-edge removal and the rebrand |
| `test_scraper_backoff.py` | 5 | Backoff integration (pytest-httpx): Pinnacle 429-then-success + persistent-5xx → `[]`; schedule 429-then-success, persistent-429 → `ScheduleFetchError`, hard 4xx fatal (no retry) |
| `test_scraper_registry.py` | 2 | Soft-book registry: the default flags build exactly `[TipsportScraper]`, and disabling every Czech book is safe (empty scraper list + a WARNING, no crash) |

## Integration Tests

`test_integration_lifecycle.py` drives a full game lifecycle against a real Store (tmp SQLite via `migrations.run`) and the real `Scheduler` — only the scraper HTTP layer is mocked, so `find_edges`, `mark_closing_snapshots`, `remove_vig`/`calculate_clv`, and `resolve_bet_result` all run for real. It covers: a hot-tier poll detecting an edge → closing-snapshot mark → CLV + win/loss resolution on the final game; total-market over/under/push resolution end-to-end; and scraper-error isolation (one scraper raising never aborts the cycle, the healthy scraper's data still persists). This closes the integration gap previously flagged here.

## Components Tested

- [[Scheduler]] -- `test_scheduler.py`
- [[CLI Entry Point]] -- `test_cli.py`
- [[PinnacleScraper]] -- `test_pinnacle.py`
- [[TipsportScraper]] -- `test_tipsport.py`
- Browser preflight -- `test_preflight.py`
- [[CloudflareSession]] -- `test_cloudflare.py`
- [[CLV Calculation]] -- `test_clv.py`
- [[Edge Detection]] -- `test_edge.py`
- [[Team Name Normalization]] -- `test_normalize.py`
- [[SQLite Store]] -- `test_store.py`
- [[Migrations]] -- `test_migrations.py`, `test_migration_005.py`, `test_migration_007.py`, `test_migration_009.py`, `test_migration_010.py`, `test_migration_012.py`, `test_migration_013.py`
- [[Vig Removal]] -- `test_vig.py`
- Schedule scraper -- `test_odds_api_schedule.py`
- [[Telegram Notifications]] -- `test_telegram.py`
- [[Data Model]] -- `test_models.py`
- [[Backup]] -- `test_backup.py`
- [[BaseScraper]] -- `test_base_scraper.py`
- [[Dashboard]] -- `test_dashboard_queries.py`, `test_dashboard_server.py`, `test_dashboard_state.py`, `test_log_tail.py`
- Config -- `test_config.py`
- End-to-end lifecycle -- `test_integration_lifecycle.py`
- Poll coordinator -- `test_poll_coordinator.py`
- Schedule service -- `test_schedule_service.py`
- Settlement service -- `test_settlement_service.py`
- Budget governor -- `test_budget_governor.py`
- Coverage monitor -- `test_coverage_monitor.py`
- Edge service -- `test_edge_service.py`
- Polling tiers -- `test_tiers.py`
- Sports profiles -- `test_sports.py`, `test_sports_profiles.py`

## Related

- [[Phase Overview]] -- test suite was part of P0 delivery
