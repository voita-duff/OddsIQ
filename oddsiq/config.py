from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_flag(name: str, default: str) -> bool:
    # Convention is "1"/"0", but accept common truthy words case-insensitively
    # so a human-friendly "true"/"yes"/"on" enables a flag instead of silently
    # disabling it. Anything else ("0"/"false"/"no"/"off"/"" /junk) is False.
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


# API key — a single Odds API credential, used for BOTH Pinnacle odds and the
# schedule/scores source. (Multi-key pool rotation was removed: one strong key
# under the 20k/month budget needs no failover.)
ODDS_API_KEY: str = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE_URL: str = "https://api.the-odds-api.com/v4"

# Telegram notifications. A second, fully independent recipient (account 2) can
# be configured; notifications are mirrored to every fully-configured recipient.
# Either, both, or neither account may be set — an account counts only when both
# its token and chat id are present.
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_BOT_TOKEN_2: str = os.environ.get("TELEGRAM_BOT_TOKEN_2", "")
TELEGRAM_CHAT_ID_2: str = os.environ.get("TELEGRAM_CHAT_ID_2", "")

# Database
DATABASE_PATH: Path = Path(os.environ.get("DATABASE_PATH", "./oddsiq.db"))

# Edge detection
EDGE_THRESHOLD: float = float(os.environ.get("EDGE_THRESHOLD", "0.03"))

# Pregame plausibility cap (FRACTION). A pregame edge at/above this is a
# stale-Tipsport price, not real value: calibrated 2026-07-10 on settled MLB
# data, edges >= 8% went 2W-9L (-63% flat ROI) while showing INFLATED CLV
# (+5 to +26) — CLV compares the same stale entry price to the close, so it
# cannot catch the mirage. Capped bets are still recorded (and settle, so the
# cap stays validatable) but are stamped excluded_reason=
# 'implausible_edge_pregame': hidden from reporting, never alerted. <=0 disables.
PREGAME_MAX_PLAUSIBLE_EDGE: float = float(
    os.environ.get("PREGAME_MAX_PLAUSIBLE_EDGE", "0.08")
)
# Soft-book coherence floor: if the soft book's OWN two-way implied sum
# (1/home + 1/away) drops below this, the book is arbing against itself —
# impossible on a fresh coherent book (Tipsport pregame overround is ~1.04-1.07),
# so one side is stale. Every edge from that market is stamped
# 'incoherent_book_pregame' (recorded, not alerted), even sides individually
# below the plausibility cap. All 3 historical both-sides-+EV games (incl. a
# 37% "edge" that lost) would have been caught. <=0 disables.
PREGAME_MIN_BOOK_OVERROUND: float = float(
    os.environ.get("PREGAME_MIN_BOOK_OVERROUND", "1.0")
)
# Maximum age of the Pinnacle market timestamp carried by The Odds API at
# observation time. This is a different failure mode from betting queue age:
# the HTTP response can arrive now while containing a feed snapshot that has
# not refreshed for minutes. The 300s default sits far beyond the measured
# 14-day p99 (~54s) while rejecting the observed 10-15 minute feed stalls.
# Stale edges are recorded as excluded evidence and never bet. <=0 disables.
PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS: float = float(
    os.environ.get("PREGAME_MAX_SHARP_SOURCE_AGE_SECONDS", "300")
)
# Completeness guard: a Tipsport listing that carries an execution id must
# also carry BOTH sides of its two-way market, else the edge is stamped
# 'incomplete_book_market'. It rides the same block as the coherence floor but
# is independently switchable — PREGAME_MIN_BOOK_OVERROUND<=0 disables only the
# floor. Default on; set 0 to stop stamping 'incomplete_book_market' without a
# rebuild.
PREGAME_REQUIRE_COMPLETE_BOOK_MARKET: bool = _env_flag(
    "PREGAME_REQUIRE_COMPLETE_BOOK_MARKET", "1"
)

# CLV closing-snapshot freshness — refuse to mark a snapshot as is_closing=1
# if its captured_at is older than this many minutes before tipoff. Default
# matches the hot-tier boundary so any tipped-off game whose hot-tier polling
# completed cleanly will have a fresh closing snapshot.
CLOSING_FRESHNESS_MINUTES: int = int(os.environ.get("CLOSING_FRESHNESS_MINUTES", "30"))

# Raw data storage
RAW_DATA_DIR: Path = Path(os.environ.get("RAW_DATA_DIR", "./data/raw"))

# Czech bookmaker selection. Tipsport is the single enabled soft book.
BOOKMAKER_TIPSPORT_ENABLED: bool = _env_flag("BOOKMAKER_TIPSPORT_ENABLED", "1")

# Tipsport API. Per-sport competition id / superSportId / moneyline matchView
# now live on each SportProfile (oddsiq/sports/profiles.py), not here.
TIPSPORT_API_URL: str = "https://www.tipsport.cz/rest/offer/v2/offer"

# Scraper settings
MAX_CONSECUTIVE_FAILURES: int = 3
CF_SOLVE_TIMEOUT: float = float(os.environ.get("CF_SOLVE_TIMEOUT", "30.0"))

# Odds API budget — upgraded to 20,000 requests/month.
ODDS_API_MONTHLY_BUDGET: int = 20000

# Budget governor (spec §3.5). Throttle Pinnacle cadence to fit the monthly quota.
BUDGET_RESERVE_FLOOR: int = int(os.environ.get("BUDGET_RESERVE_FLOOR", "1500"))
BUDGET_THROTTLE_SLACK: float = float(os.environ.get("BUDGET_THROTTLE_SLACK", "1.10"))
# Day-of-month the Odds API quota resets. The API exposes NO reset-date header
# (only x-requests-{used,remaining,last}), so the reset day is configured here.
# Default 1 = calendar-month reset; set to your billing/signup anniversary day
# if the quota resets on a non-calendar cycle. Drives frac_elapsed in the burn
# projection — wrong here only mis-times the soft early-warning throttle
# (levels 1-2); the reserve floor uses the authoritative `remaining`.
ODDS_API_QUOTA_RESET_DAY: int = int(os.environ.get("ODDS_API_QUOTA_RESET_DAY", "1"))

# Pinnacle markets scope (quota cost = regions × markets per request).
# Default "h2h" (moneyline only) keeps the budget comfortable.
# Widen to "h2h,spreads,totals" if you need spread/total baselines — see
# docs/Decisions/Markets.md for rationale and triggers to revisit. NOTE:
# Tipsport is moneyline-only now, so widening this alone yields Pinnacle-only
# spread/total snapshots with no Czech counterpart (zero edges) until the
# Tipsport scraper is also re-extended to those markets.
PINNACLE_MARKETS: str = os.environ.get("PINNACLE_MARKETS", "h2h")

# Polling tiers (seconds) — data-driven cadence (spec §3.5). 89% of historical
# value bets land in the last hour pregame, peaking 15-30m before tipoff.
POLL_TIER_FAR: int = (
    1800  # >2h: Tipsport only, no Pinnacle (lines too soft; 1 bet ever)
)
POLL_TIER_WARM: int = 300  # 1-2h: every 5 min (rare early edge; cheap baseline)
POLL_TIER_HOT: int = 60  # <60m: every 60s (89% of bets land here; tightened
# 90->60 on 2026-08-27, funded by the live-tier removal
# freeing ~150-225 credits/day — earlier detection +
# fresher entry prices in the betting window)
# (POLL_TIER_LIVE and the whole live-scout knob block were removed 2026-08-27
# with the live-edge subsystem — post-tipoff games are no longer polled.)

# Schedule refresh
# Full-sweep / idle cadence: the all-sports /scores sweep that discovers new
# games in sports with nothing currently pollable (and the refresh interval
# when no game is active at all). Raised 3600 -> 10800 on 2026-08-08: slates
# post days ahead, so 3h discovery latency is harmless, and each sweep costs
# ~2 credits per enabled sport.
SCHEDULE_REFRESH_INTERVAL: int = int(
    os.environ.get("SCHEDULE_REFRESH_INTERVAL", "10800")
)
# Active-window /scores cadence. Each refresh costs ~2 Odds API credits per
# fetched sport; at the old 300s default schedule calls were roughly half the
# monthly burn and both the 2026-06 and 2026-07 quotas exhausted ~10 days
# early. 900s keeps slate discovery timely (game-final detection has its own
# path in check_completed_games) at about a third of the quota cost.
SCHEDULE_GAME_WINDOW_REFRESH: int = int(
    os.environ.get("SCHEDULE_GAME_WINDOW_REFRESH", "900")
)
# Game-final detection cadence (check_completed_games), per sport with a live
# game, 2 credits per check. Was a hardcoded 300s; measured 2026-08 it was the
# largest single Odds API burn (MLB is live 10-14h/day -> ~250-350 credits/day)
# while /scores overhead overall was ~55-60% of spend. 900s only delays
# settlement + result Telegrams by ~10 min; CLV is unaffected because closing
# snapshots are marked at tipoff, not at final.
SCHEDULE_COMPLETION_CHECK_INTERVAL: int = int(
    os.environ.get("SCHEDULE_COMPLETION_CHECK_INTERVAL", "900")
)

# Auto-betting engine (P1). BETTING_ENABLED gates construction entirely —
# default off, the pipeline behaves exactly as before. Even enabled, the
# engine is DISARMED until the operator arms it (meta `betting_armed`,
# via `oddsiq betting --arm` or the dashboard) and the Chrome-extension
# executor is connected. Runtime money knobs (flat stake, caps) live in the
# meta table, not env — see docs/Design/2026-08-09-p1-auto-betting-design.md.
BETTING_ENABLED: bool = _env_flag("BETTING_ENABLED", "0")
# Dashboard write controls (arm/disarm, stake, caps on /betting). Default OFF.
# When enabled, mutating endpoints additionally require these app-level Basic
# auth credentials and a same-origin Origin/Referer. Empty credentials fail
# closed; reverse-proxy auth remains useful defense in depth.
DASHBOARD_CONTROLS_ENABLED: bool = _env_flag("DASHBOARD_CONTROLS_ENABLED", "0")
DASHBOARD_CONTROLS_USERNAME: str = os.environ.get("DASHBOARD_CONTROLS_USERNAME", "")
DASHBOARD_CONTROLS_PASSWORD: str = os.environ.get("DASHBOARD_CONTROLS_PASSWORD", "")
BETTING_BRIDGE_HOST: str = os.environ.get("BETTING_BRIDGE_HOST", "127.0.0.1")
BETTING_BRIDGE_PORT: int = int(os.environ.get("BETTING_BRIDGE_PORT", "8765"))
# Shared secret the extension must present in its hello; REQUIRED when
# BETTING_ENABLED (the scheduler refuses to start without it).
BETTING_BRIDGE_TOKEN: str = os.environ.get("BETTING_BRIDGE_TOKEN", "")
# Display-only mirror of the money gate for the dashboard safety chain. The
# AUTHORITATIVE value is baked into the Chrome container's config.js at ITS
# start (docker/chrome/entrypoint.sh reads the same .env), so this can drift
# from the running extension if one container restarted against an edited
# .env — the dashboard labels it "(env)" accordingly.
BETTING_CONFIRM_ENABLED: bool = _env_flag("BETTING_CONFIRM_ENABLED", "0")
# Hard per-bet stake ceiling (CZK) — an env-level bound the runtime
# meta stake can never exceed (clamped, not skipped).
BETTING_MAX_STAKE_CZK: float = float(os.environ.get("BETTING_MAX_STAKE_CZK", "500"))
# Immutable env-level ceilings for every dashboard-adjustable aggregate cap.
# The betting engine clamps meta values again, so direct DB edits cannot widen
# exposure beyond these bounds.
BETTING_MAX_BETS_PER_DAY: int = int(os.environ.get("BETTING_MAX_BETS_PER_DAY", "10"))
BETTING_MAX_OPEN_STAKES_CZK: float = float(
    os.environ.get("BETTING_MAX_OPEN_STAKES_CZK", "500")
)
BETTING_MAX_DAILY_LOSS_CZK: float = float(
    os.environ.get("BETTING_MAX_DAILY_LOSS_CZK", "300")
)
# No result from the extension within this window -> the placement is
# 'unknown' and the engine AUTO-DISARMS (never blind-retry an unconfirmed bet).
# INVARIANT: must exceed the extension's worst-case SUCCESSFUL path, or a slow
# real placement is classified unknown and the engine stands down on a bet
# that landed. extension/background.js enforced caps: nav 5s + odd 20s + slip
# 5s + stake read-back 2s + ticket 12s = 44s, plus an unbounded tail of CDP round trips (the
# receipt read is NOT capped — evalPage's third arg is an awaitPromise flag,
# not a timeout), so this is also the only backstop on a hung extension.
# Pinned by tests/test_config.py against the named JS budget constants.
# Was 10 (config) / 45 (VM .env).
BETTING_PLACEMENT_TIMEOUT_SECONDS: float = float(
    os.environ.get("BETTING_PLACEMENT_TIMEOUT_SECONDS", "60")
)
# Never bet against a sharp reference older than this (the blind-betting
# interlock — quota exhaustion or a Pinnacle latch-off must stop betting).
BETTING_FAIR_MAX_AGE_SECONDS: float = float(
    os.environ.get("BETTING_FAIR_MAX_AGE_SECONDS", "120")
)
# Never START a placement this close to (or after) first pitch: Tipsport
# flips the offer to in-play around the scheduled start, so the prematch odd
# button vanishes mid-placement (2026-08-26/27 forensics: 4 of 7 rejections
# were tipoff-boundary offer_gone within +-30s of the pitch). <=0 disables
# the buffer (post-tipoff is still blocked by the game_started rule).
BETTING_TIPOFF_BUFFER_SECONDS: float = float(
    os.environ.get("BETTING_TIPOFF_BUFFER_SECONDS", "120")
)

# Dead-man ping (healthchecks.io-style): when set, the scheduler GETs this
# URL on every heartbeat (~60s). The monitoring service pages the operator
# when pings STOP arriving — true dead-process detection, unlike the opt-in
# Telegram heartbeat which requires noticing silence. Empty = disabled.
HEARTBEAT_PING_URL: str = os.environ.get("HEARTBEAT_PING_URL", "")

# Backup
BACKUP_ENABLED: bool = _env_flag("BACKUP_ENABLED", "1")
# Periodic in-process backup cadence (hours; <=0 disables). Startup/shutdown
# backups alone left a 17-day hole when the container ran Jul 22→Aug 8
# without a restart — this tick closes that. Uses the same WAL-safe,
# integrity-checked, retention-pruned routine as the startup backup.
BACKUP_INTERVAL_HOURS: float = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
# Default to a repo-relative ./backups (consistent with DATABASE_PATH / LOG_FILE).
# In Docker this is overridden to a mounted volume path (see docker-compose.yml).
# The old ~/Desktop default did not exist on a headless VM / in a container.
BACKUP_DIR: Path = Path(os.environ.get("BACKUP_DIR", "./backups")).expanduser()
BACKUP_KEEP_DAILY: int = int(os.environ.get("BACKUP_KEEP_DAILY", "7"))
BACKUP_KEEP_MONTHLY: int = int(os.environ.get("BACKUP_KEEP_MONTHLY", "6"))

# Raw response retention — prune dated subdirectories under RAW_DATA_DIR older
# than RAW_RESPONSE_KEEP_DAYS at startup/shutdown. Keeps the data/raw tree from
# silently growing during 24/7 operation.
RAW_RESPONSE_RETENTION_ENABLED: bool = _env_flag("RAW_RESPONSE_RETENTION_ENABLED", "1")
RAW_RESPONSE_KEEP_DAYS: int = int(os.environ.get("RAW_RESPONSE_KEEP_DAYS", "14"))

# Rotating file logging — written by setup_logging() in cli.py. The
# dashboard process tails this file for the live activity feed and pipeline
# liveness signal. Idempotent setup so all CLI subcommands can call
# setup_logging() without multiplying handlers.
LOG_FILE: Path = Path(os.environ.get("LOG_FILE", "./logs/oddsiq.log"))
LOG_FILE_MAX_BYTES: int = int(os.environ.get("LOG_FILE_MAX_BYTES", "10485760"))
LOG_FILE_BACKUP_COUNT: int = int(os.environ.get("LOG_FILE_BACKUP_COUNT", "5"))
LOG_FILE_ENABLED: bool = _env_flag("LOG_FILE_ENABLED", "1")

# Scheduler heartbeat cadence. Emits one log line per interval so the
# dashboard's "is the pipeline alive?" check works even when no games
# are tracked. See docs/Components/Dashboard.md.
SCHEDULER_HEARTBEAT_SECONDS: int = int(
    os.environ.get("SCHEDULER_HEARTBEAT_SECONDS", "60")
)

# (The opt-in Telegram health ping was removed in the 2026-08-10 notification
# rebrand — the phone carries only real-money placed/won/lost. Liveness is the
# dashboard heartbeat line + the HEARTBEAT_PING_URL dead-man ping.)
