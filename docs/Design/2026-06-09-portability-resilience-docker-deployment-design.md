---
tags: [type/spec, phase/p4]
related:
  - "[[Scheduler]]"
  - "[[Pipeline Services]]"
  - "[[PinnacleScraper]]"
  - "[[CloudflareSession]]"
  - "[[Telegram Notifications]]"
  - "[[Backup and Retention]]"
---

# Portability + Resilience + Docker Deployment — Design

> Status: design, approved 2026-06-09. Builds out most of **P4 (Resilience and Deployment)** so the pipeline runs unattended 24/7 in a **Linux Docker container on a Windows VM**. Three sequenced, independently-shippable phases: (1) cross-platform portability, (2) P4-core in-app resilience, (3) Docker-Linux deployment. Stays in evaluation mode: NO auto-betting, NO new books, NO new sports.

## Problem / Context

OddsIQ was built and runs on macOS. The owner has a Windows VM and wants the pipeline running unattended 24/7. An audit (2026-06-09) found three gaps:

1. **Native Windows would crash on startup** — `loop.add_signal_handler()` raises `NotImplementedError` on the Windows ProactorEventLoop ([cli.py:692-699](../../../oddsiq/cli.py)), and `ZoneInfo("Europe/Prague")` raises `ZoneInfoNotFoundError` because Windows ships no system tz DB and `tzdata` is not a dependency ([dashboard/server.py:30](../../../oddsiq/dashboard/server.py), [queries.py:44](../../../oddsiq/dashboard/queries.py)).
2. **The app logic is robust for 24/7, but operational resilience has gaps** — no auto-restart, silent degradation when the API budget is exhausted, no retry/backoff on the two Odds API callers (a transient 429 silently drops a poll), no proactive liveness signal, and two small unbounded in-memory accumulators.
3. **No deployment packaging exists** — Docker is referenced only as an unbuilt P4 idea.

## Decision: run as Docker-Linux, native Windows is a nice-to-have

The real 24/7 runtime is a **Linux container** on the Windows VM (Docker Desktop / WSL2 backend). In a Linux container the two hard "Windows blockers" never fire (Linux has signal handlers + a system tz DB). Native Windows is a **nice-to-have fallback** the owner can stand up directly on the VM if Docker setup stalls.

Consequence: the portability fixes are no longer "unblock Windows" but **cross-platform hygiene that the Docker image also needs** (`tzdata`, UTF-8 encoding, a container-sane backup path) plus one cheap native-Windows enabler (the signal guard).

## Goals

1. App runs correctly on Linux/Docker **and** native Windows (Phase 1).
2. Robust retries, visible degradation, and a liveness push for unattended operation (Phase 2 — P4 core).
3. A supervised, restart-on-crash Docker deployment with persistent state and a deploy runbook for the Windows VM (Phase 3).

## Non-Goals (project-mode guardrails)

- No auto-betting (P5), no new bookmakers, **no new sports** (the registry stays NBA/NHL/MLB; the owner explicitly deferred sports this round).
- Native Windows is not a first-class, perpetually-verified runtime — it is a best-effort fallback. We do not add a Windows CI matrix or a Windows service installer.
- Season-boundary handling and disk-space monitoring (the remaining two P4 success criteria) are **out of scope this round** — staggered NBA/NHL/MLB rarely leave a true no-games gap and the Budget Governor already caps spend. Tracked for later.
- External uptime-monitor account setup (e.g. healthchecks.io) is documented as a recommendation, not built; an optional one-line app hook to feed one is offered but off by default.

## Locked decisions

| Decision | Choice | Rationale |
|---|---|---|
| Runtime target | Docker-Linux primary; native Windows nice-to-have | Linux container sidesteps both hard Windows blockers; native fallback still wanted |
| Q3 scope | P4 core (lean hardening + backoff/jitter + opt-in Telegram health ping) | Covers the important unattended gaps without the larger season-boundary design |
| Chromium sandbox | Add `--no-sandbox` to both launch sites, run container as root | Benign on local/macOS; tests mock Playwright; avoids WSL2 volume-permission friction |
| Container topology | One `docker-compose.yml`, two services: `pipeline` (always on) + `dashboard` (optional compose profile) | Dashboard available for a clean HTTP healthcheck but not mandatory; shared `/data` volume |

---

## Phase 1 — Cross-platform portability

Four small, self-contained changes. After this phase the app runs on Linux/Docker and native Windows.

1. **Add `tzdata` to `pyproject.toml` `dependencies`.** `zoneinfo` automatically falls back to the PyPI `tzdata` package when no system DB exists. Resolves the dashboard import-time crash on any tz-DB-less host.
2. **Add `encoding="utf-8"`** to the two writers missing it:
   - Raw-response write at [scrapers/base.py:74](../../../oddsiq/scrapers/base.py) (`write_text(...)`).
   - `RotatingFileHandler` construction at [cli.py:111](../../../oddsiq/cli.py).
   - Removes the latent cp1252 `UnicodeEncodeError` on Czech Tipsport content. (Reads in `dashboard/log_tail.py` are already explicit UTF-8 — no change.)
3. **Change `BACKUP_DIR` default** at [config.py:100-102](../../../oddsiq/config.py) from `~/Desktop/oddsiq-backup` to a repo-relative `./backups` (consistent with `DATABASE_PATH=./oddsiq.db` and `LOG_FILE=./logs/...`). Phase 3 overrides it to a volume path. The old default is headless/container-hostile.
4. **Platform-guard the signal handler** at [cli.py:692-699](../../../oddsiq/cli.py): keep `loop.add_signal_handler(SIGINT/SIGTERM)` on POSIX; on Windows (`sys.platform == "win32"`) skip it and rely on a `signal.signal(...)`-set flag / `KeyboardInterrupt` path that still calls `scheduler.stop()`. No-op for Linux/Docker; makes native Windows actually run and shut down cleanly.

**Acceptance:** existing suite green; dashboard imports on a host with no system tz DB; native Windows starts and stops via Ctrl-C without `NotImplementedError`; Linux behavior unchanged.

---

## Phase 2 — P4-core in-app resilience

Platform-agnostic hardening. Docker's restart policy (Phase 3) provides crash-restart, so this phase is purely in-process resilience + visibility.

### Lean hardening
- **`PRAGMA busy_timeout`** in `Store.initialize` ([db/store.py:43](../../../oddsiq/db/store.py)) — a module constant (5000 ms). Absorbs transient `SQLITE_BUSY` from the concurrent dashboard reader (WAL allows concurrent readers; brief writer-exclusive moments can otherwise surface as immediate errors).
- **Budget-exhaustion / throttle alert** — send a Telegram alert (once per trip, in-memory dedup) when `should_skip_request()` first returns true ([pinnacle.py:126-128](../../../oddsiq/scrapers/pinnacle.py)) or `BudgetGovernor.throttle_level` first reaches its max ([budget_governor.py:143-171](../../../oddsiq/pipeline/budget_governor.py)). Makes silent zero-output degradation visible. Skipped if Telegram unconfigured (existing best-effort semantics).
- **Guard startup recovery** — wrap the pre-loop startup sequence in [scheduler.py:60-70](../../../oddsiq/pipeline/scheduler.py) (`load_handled_live()`, `recover_historical_bets()`) so a transient DB error degrades to "start polling, recovery retries on next refresh" instead of crashing before the main loop's try/except. Prevents a boot-time blip from becoming a restart crash-loop under Docker.
- **Prune the two unbounded in-memory accumulators** to the current slate each cycle: `_handled_live` ([schedule_service.py:32](../../../oddsiq/pipeline/schedule_service.py)) and `_alerted_bucket` ([live_scout_service.py:61](../../../oddsiq/pipeline/live_scout_service.py)), mirroring the existing precedent in [coverage_monitor.py:51-54](../../../oddsiq/pipeline/coverage_monitor.py).

### P4 core
- **Exponential backoff + jitter on the two Odds API callers.** A small shared retry helper (module constants: max 3 attempts, base 1.0s, cap 30s, full jitter) applied to:
  - `PinnacleScraper.fetch_odds` ([pinnacle.py:149-151](../../../oddsiq/scrapers/pinnacle.py)) — currently any `httpx.HTTPError` (incl. 429) just logs and returns `[]`.
  - `OddsApiScheduleSource._fetch_endpoint` ([odds_api_schedule.py:98-141](../../../oddsiq/scrapers/odds_api_schedule.py)) — currently retries 5xx/transport once, treats all 4xx (incl. 429) as fatal.
  - Both **honor `Retry-After`** on 429. The hard-stop budget guard (`should_skip_request`) still short-circuits before any call, so backoff only applies to genuine transient errors. Tipsport keeps its existing Cloudflare/consecutive-failure logic (out of scope; P4's "extend Tipsport to full backoff" is deferred).
  - Jitter uses an injectable RNG so tests are deterministic; sleep is injectable/awaitable so tests don't wall-clock.
- **Opt-in proactive Telegram health ping** — new `Scheduler._maybe_send_telegram_heartbeat()` that, while actively polling, sends a Telegram health summary (active scraper status, time since last matched coverage, current tier, budget throttle) every `TELEGRAM_HEARTBEAT_INTERVAL_MINUTES`. Gated behind `TELEGRAM_HEARTBEAT_ENABLED` (default off). Reuses the existing `_last_heartbeat` tracking and the best-effort Telegram channel.
  - **Honest limitation (documented):** this covers *frozen-but-alive*; a fully dead container/VM cannot push. True dead-process paging needs an **external uptime monitor**. Phase 3's deploy doc explains wiring one (e.g. healthchecks.io); an optional "ping external URL each cycle" hook is offered (off by default).

**New config:** `TELEGRAM_HEARTBEAT_ENABLED` (default `0`), `TELEGRAM_HEARTBEAT_INTERVAL_MINUTES` (default `30`). Backoff params and `busy_timeout` are module constants (tunable later). Changed default: `BACKUP_DIR`.

**Acceptance:** new unit tests for backoff (deterministic jitter, injected sleep, `Retry-After` honored, gives up after max attempts), health-ping gating (on/off, cadence, only-while-polling), budget-alert dedup, and the startup-recovery guard. Full suite green; `docs-audit.py` config-coverage passes for the new flags.

---

## Phase 3 — Docker-Linux deployment

New artifacts at repo root: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `DEPLOY.md`.

### Image
- **Base:** `mcr.microsoft.com/playwright/python:v1.60.0-jammy`. The tag is locked to the `playwright~=1.60.0` pin (bundled Chromium rev 1223 / 148.x); a future Playwright bump must move this tag in lockstep. Ships Chromium + OS libs + tzdata.
- **Install:** `pip install -e .` (no `[dev]` extra). Python floor 3.11.
- **Chromium sandbox:** add `--no-sandbox` to the two launch arg lists ([cloudflare.py:62-65](../../../oddsiq/scrapers/cloudflare.py), [preflight.py:27](../../../oddsiq/scrapers/preflight.py)); run container as root. (Benign locally; tests mock Playwright.)
- **`.dockerignore`** mirrors `.gitignore` (`*.db`, `logs/`, `data/raw/`, `.venv/`, `.env`) plus `.git`, `tests/`, `docs/`, `.obsidian/`, `*.egg-info/`.

### State / volumes
One `/data` volume; env overrides point all runtime paths into it:
| Env | In-container value | Notes |
|---|---|---|
| `DATABASE_PATH` | `/data/db/oddsiq.db` | Mount the **directory** — WAL `-wal`/`-shm` sidecars live beside it. Local disk / named volume only (WAL breaks on SMB/network mounts). |
| `BACKUP_DIR` | `/data/backups` | Overrides the Phase-1 `./backups` default |
| `LOG_FILE` | `/data/logs/oddsiq.log` | Shared with the dashboard service |
| `RAW_DATA_DIR` | `/data/raw` | Self-pruning |

### Compose (two services, one file)
- `pipeline`: `command: ["oddsiq"]`, `restart: unless-stopped` (the free auto-restart supervisor), `stop_grace_period: 30s` (shutdown does a full backup + integrity check), `init: true` (reap Chromium children), `shm_size: 1g` (headless Chromium needs >64 MB `/dev/shm`), `env_file`.
- `dashboard` (optional, behind a compose `profiles:` tag): `command: ["oddsiq","dashboard","--host","0.0.0.0","--port","8080"]`, `ports: ["8080:8080"]`, shares the `/data` volume, `HEALTHCHECK` = `curl -f http://localhost:8080/`. ⚠️ The dashboard has **no auth** ([cli.py:816-820](../../../oddsiq/cli.py)) — publish the port only to a trusted network.
- Exactly **one** pipeline instance (shared SQLite + doubled API spend otherwise).

### Env / secrets
`ODDS_API_KEY` required (CLI exits without it). Optional: `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, `TZ=Europe/Prague` (aligns local-time log lines), the new heartbeat flags, plus existing threshold/budget knobs. Passed via `--env-file`; `python-dotenv` is a no-op when no `.env` is baked in. Logs already redact the API key.

### `DEPLOY.md` (Windows VM runbook)
Docker Desktop/WSL2 prerequisites; create `/data` volume + `.env`; `docker compose up -d` (pipeline) and `--profile dashboard up -d` (optional UI); how to view logs (`docker compose logs -f`), update (rebuild on Playwright bump), back up `/data`, and the external-uptime-monitor recommendation.

**Acceptance:** image builds; in-container `oddsiq --check-budget` reaches the API; the startup browser preflight launches Chromium headless with `--no-sandbox`; `docker compose up` starts the pipeline, persists DB/logs/backups to the volume across a `down`/`up`, and `docker stop` shuts down gracefully within the grace period; dashboard profile serves and its healthcheck passes.

---

## Verification strategy

- **Each phase keeps `ruff` + the full pytest suite + `docs-audit.py` green** (CI gates every push, Python 3.11/3.12, Playwright mocked).
- **New tests:** signal-guard platform branch (Phase 1); backoff/jitter, health-ping gating, budget-alert dedup, startup-recovery guard (Phase 2). Phase 3 is verified by building the image and exercising preflight + `--check-budget` + a compose up/down cycle (manual / documented, not in CI — CI does not install browsers).
- **References sweep on every config change** (`tzdata`, `BACKUP_DIR` default, two heartbeat flags): update `.env.example`, `oddsiq/config.py`, the project notes (Environment section + test count), the relevant `docs/` pages, and the `scripts/docs-audit.py` config-coverage expectations. (Repo rule: check all references when adding/removing code.)
- Update the project notes' test count and add a short "Deployment (Docker)" note; add/refresh `docs/Roadmap/P4 - Resilience and Deployment.md` status to reflect what shipped vs what's still deferred (season boundaries, disk monitoring, Tipsport full-backoff).

## Sequencing & checkpoints

Phase 1 → review → Phase 2 → review → Phase 3 → review. Each phase is a coherent commit set on `p4-deployment-hardening`; the branch becomes one PR (or three stacked, owner's call at finish). No history rewrites across the work.
