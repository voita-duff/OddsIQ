---
tags:
  - type/roadmap
  - status/in-progress
  - phase/p4
---
# P4 -- Resilience and Deployment

## Goal

Run the pipeline unattended 24/7. Currently requires manual start on game nights and manual monitoring. P4 automates operations and hardens the pipeline against all failure modes without human intervention.

## Design

### Deployment
- **Systemd service** or **Docker container** for long-lived process management
- Auto-start on boot, auto-restart on crash
- Environment variable injection via systemd unit file or Docker env

### Heartbeat Monitoring (Proposed → superseded; see Status)

The original proposal, kept for the record: a periodic [[Telegram Notifications]] heartbeat that fires during active polling so the operator (or future supervisor) can detect a frozen / crashed pipeline without watching the terminal.

**Signal contents:** active scraper status, time since last matched coverage per `(game_id, scraper)`, fetch-level diagnostics per scraper, and current polling tier.

**Cadence and silence interpretation:** intervals (e.g. every 30 minutes) and the "two consecutive misses = real problem" heuristic are operator-tunable; the design point is that *one missed heartbeat is noise, repeated silence is signal*.

**Implementation hooks:** `Scheduler` is the natural owner -- it already tracks per-scraper fetch timestamps plus per-game matched-coverage timestamps. The proposal put delivery on the existing [[Telegram Notifications]] channel; that half is superseded (see Status) — liveness now leaves the process as an outbound dead-man ping instead.

**Status:** A *log-line* heartbeat ships — `Scheduler._maybe_emit_heartbeat()` emits a `heartbeat:` log line on `SCHEDULER_HEARTBEAT_SECONDS` cadence (default 60s) for dashboard liveness, and `Scheduler` tracks `_last_heartbeat`. The *Telegram* health ping shipped 2026-06-09 (opt-in via `TELEGRAM_HEARTBEAT_ENABLED`) but was **deleted 2026-08-10** in the bettor-facing-only rebrand — the phone carries real-money events only, and a self-reported "I'm alive" ping cannot detect the failure it exists for: a dead process sends nothing. It is superseded by `HEARTBEAT_PING_URL` (`oddsiq/config.py`), a healthchecks.io-style **dead-man ping** the scheduler GETs on every heartbeat; the external monitor pages the operator when the pings STOP. That inverts the signal correctly and closes the July 2026 silent-outage gap. Together with the [[Dashboard|P3 dashboard]] and the log heartbeat, unattended monitoring is covered.

### Error Recovery
- **Exponential backoff with jitter** on all scraper failures (not just Tipsport)
- Currently only [[CloudflareSession]] has retry logic; [[PinnacleScraper]] and the schedule scraper need the same treatment
- Automatic recovery from all [[Failure Modes]] without human intervention

### Log Management
- **Log rotation** -- prevent disk space exhaustion on long-running deployments
- Structured logging for machine parsing
- Disk space monitoring and alerting

### Season Boundaries
- **Graceful handling** when no NBA games are scheduled for months (off-season)
- Reduced polling, no unnecessary API budget consumption
- Automatic resumption when games return

## Success Criteria

- [x] Docker container — `Dockerfile` + `docker-compose.yml` + `DEPLOY.md` (shipped 2026-06-09; Linux container, `restart: unless-stopped`)
- [x] Unattended liveness monitoring — the opt-in Telegram health ping shipped 2026-06-09 and was removed 2026-08-10 (rebrand); superseded by the `HEARTBEAT_PING_URL` dead-man ping, alongside the log-line heartbeat + P3 dashboard
- [x] Exponential backoff with jitter on the Odds API callers — `PinnacleScraper` + schedule source (shipped 2026-06-09, honors `Retry-After`). Tipsport keeps its Cloudflare/consecutive-failure logic; unifying it is deferred.
- [ ] Automatic recovery from all failure modes without human intervention
- [ ] Log rotation + disk-space management — log rotation & raw/backup retention pruning ship; disk-space *monitoring/alerting* still deferred
- [ ] Graceful handling of season boundaries — deferred (staggered NBA/NHL/MLB/WNBA rarely leave a true no-games gap; the Budget Governor already caps spend)

## Components Affected

- [[Scheduler]] -- backoff logic, season boundary handling
- [[PinnacleScraper]] -- exponential backoff
- Schedule scraper -- exponential backoff
- [[CloudflareSession]] -- already has retry, extend to full backoff pattern
- [[Telegram Notifications]] -- the proposed heartbeat's original delivery channel; superseded 2026-08-10 (see Status), so nothing on this page is messaged today

## Dependencies

Hardens the current single-book multi-sport pipeline (Tipsport across NBA/NHL/MLB/WNBA) and [[P3 - Dashboard and Reporting|P3]] dashboard before [[P5 - Auto-Betting|P5 Auto-Betting]] automates real money flow without human supervision. (The multi-book era — P2 Betano/Allwyn/Fortuna — was archived 2026-06-02; the foundation being hardened here is the post-refactor pipeline.)

## Related

- [[Phase Overview]]
- [[Health Monitoring]] -- operational health checks for the running pipeline
- [[Failure Modes]] -- what can go wrong and current mitigations
- [[Backup and Retention]] -- data durability for long-running deployments
- [[Runbook]] -- current manual operations that P4 automates
