from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import math
import re
import signal
import sys
from pathlib import Path

from oddsiq import backup, config
from oddsiq.db import migrations
from oddsiq.db.store import Store
from oddsiq.notifications import telegram
from oddsiq.pipeline import Scheduler
from oddsiq.scrapers import base as scraper_base
from oddsiq.scrapers.pinnacle import PinnacleScraper

_MISSED_GAMEDAY_AUDIT_META_KEY = "missed_gameday_audit_known_external_ids"


# Noisy HTTP-client loggers that include URLs (with query params and path
# tokens) in their INFO output. Silencing them at WARNING avoids leaking the
# Odds API ``?apiKey=...`` and Telegram ``/bot<token>/`` path into terminal
# logs — important because operators routinely share log dumps when debugging.
_NOISY_HTTP_LOGGERS: tuple[str, ...] = ("httpx", "httpcore", "urllib3")


class _SecretsRedactingFilter(logging.Filter):
    """Redact common secret patterns from log records.

    Defense-in-depth alongside ``_NOISY_HTTP_LOGGERS``: if a future library
    (or one we forgot to silence) emits a URL containing a secret, this
    catches the leak before it reaches stdout/stderr/files.

    Mutates ``record.msg`` and clears ``record.args`` so downstream handlers
    render the redacted version. Always returns ``True`` (never drops a
    record — we want operators to see *that* the request happened, just
    not the secret).
    """

    # Patterns are deliberately tight to avoid false positives in prose logs.
    _PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
        # Odds API query param: ``?apiKey=<key>``
        (re.compile(r"apiKey=[A-Za-z0-9]+"), "apiKey=***"),
        # Generic ``api_key=`` variants (case-insensitive)
        (re.compile(r"api_key=[A-Za-z0-9]+", re.IGNORECASE), "api_key=***"),
        # Telegram bot URL path: ``/bot<id>:<secret>``
        (re.compile(r"/bot\d+:[A-Za-z0-9_-]+"), "/bot***"),
        # ``Authorization: Bearer <token>``
        (re.compile(r"Bearer [A-Za-z0-9_.\-]+"), "Bearer ***"),
    )

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 — matches logging.Filter
        msg = record.getMessage()
        redacted = msg
        for pattern, replacement in self._PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = None
        return True


def _install_secrets_filter() -> None:
    """Redact secrets on the root logger AND every console StreamHandler.

    Python's logging propagation does NOT consult ancestor *logger* filters
    when a child logger (``oddsiq.*``) emits a record — only *handler*-level
    filters run. So a filter on the root logger alone leaves the console
    (stderr / ``docker logs``) unredacted for child-originated records.
    Attaching the filter to the basicConfig StreamHandler closes that gap;
    the rotating file handler is filtered separately in its own installer.
    Idempotent on both the logger and each handler."""
    root = logging.getLogger()
    if not any(isinstance(f, _SecretsRedactingFilter) for f in root.filters):
        root.addFilter(_SecretsRedactingFilter())
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            if not any(isinstance(f, _SecretsRedactingFilter) for f in handler.filters):
                handler.addFilter(_SecretsRedactingFilter())


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %z",
    )
    for noisy in _NOISY_HTTP_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _install_secrets_filter()
    _install_rotating_file_handler()


def _install_rotating_file_handler() -> None:
    """Attach a RotatingFileHandler to root for ``logs/oddsiq.log``.

    Idempotent: if a RotatingFileHandler with the same baseFilename is
    already attached (because another CLI subcommand or test already ran
    setup_logging), skip. Otherwise the dashboard process would see
    duplicated lines on every poll.

    Secret redaction: ``_SecretsRedactingFilter`` is attached directly to
    this handler. Python's logging propagation does NOT consult ancestor
    logger filters when a child logger emits a record — only handler-level
    filters run. Attaching to the handler is required for redaction to apply
    to records from any child logger (``oddsiq.pipeline.scheduler``,
    ``oddsiq.dashboard``, etc.).
    """
    if not config.LOG_FILE_ENABLED:
        return
    log_path = config.LOG_FILE.resolve()
    root = logging.getLogger()
    for h in root.handlers:
        if (
            isinstance(h, logging.handlers.RotatingFileHandler)
            and Path(h.baseFilename).resolve() == log_path
        ):
            return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=config.LOG_FILE_MAX_BYTES,
        backupCount=config.LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S %z",
        )
    )
    handler.setLevel(logging.INFO)
    handler.addFilter(_SecretsRedactingFilter())
    root.addHandler(handler)


async def check_budget() -> int:
    """Probe the Odds API quota and print used/remaining/total. Returns exit code."""
    if not config.ODDS_API_KEY:
        print(
            "Odds API key not set (set ODDS_API_KEY in .env)",
            file=sys.stderr,
        )
        return 1
    scraper = PinnacleScraper()
    try:
        result = await scraper.probe_budget()
        if result is None:
            print("Odds API budget probe failed", file=sys.stderr)
            return 1
        total = scraper.budget_total
        print(
            f"Odds API budget: {result['used']} used / "
            f"{result['remaining']} remaining ({total} total)"
        )
        return 0
    finally:
        await scraper.close()


async def cleanup_closing_flags(dry_run: bool = False) -> int:
    """Reset is_closing=1 flags on existing data and re-mark under the
    current freshness gate. Useful when CLOSING_FRESHNESS_MINUTES has
    changed or to repair rows where the only available pre-tipoff snapshot
    is too stale to legitimately count as a closing line.

    Per-game work is two statements: clear_closing_flags (resets all rows)
    then mark_closing_snapshots (re-marks only rows fresh enough to pass).
    These are separate transactions; on mid-game failure the game ends up
    with is_closing=0 across all rows, which is a SAFE state under the
    freshness gate (no fake closings) and recoverable by re-running cleanup.

    --dry-run: compute what would change without writing.
    """
    setup_logging()
    logger = logging.getLogger("oddsiq")
    if dry_run:
        logger.info("[DRY-RUN] cleanup-closing-flags: no writes will be performed")

    await asyncio.to_thread(migrations.run, config.DATABASE_PATH)
    store = Store(config.DATABASE_PATH)
    await store.initialize()
    try:
        final_games = await store.get_games_by_status("final")
        total_before = 0
        total_after = 0
        games_with_no_fresh_closing = 0
        prefix = "[DRY-RUN] " if dry_run else ""

        for game in final_games:
            before = await store.count_closing_flags(game.id)
            # Mirror mark_closing_snapshots exactly (shared windowed SQL) so the
            # dry-run preview matches the real re-mark — including when a
            # selection's latest snapshot is post-tipoff.
            would_mark = await store.count_markable_closing_snapshots(
                game.id, config.CLOSING_FRESHNESS_MINUTES
            )

            if not dry_run:
                await store.clear_closing_flags(game.id)
                await store.mark_closing_snapshots(
                    game.id, config.CLOSING_FRESHNESS_MINUTES
                )
                after = await store.count_closing_flags(game.id)
            else:
                after = would_mark

            total_before += before
            total_after += after
            if before > 0 and after == 0:
                games_with_no_fresh_closing += 1

            if before != after or before > 0:
                logger.info(
                    "%sgame_id=%d (%s @ %s, %s): is_closing rows %d -> %d",
                    prefix,
                    game.id,
                    game.away_team,
                    game.home_team,
                    game.scheduled_at.date().isoformat(),
                    before,
                    after,
                )

        logger.info(
            "%sTotals across %d final game(s): is_closing=1 rows %d -> %d "
            "(games left with no fresh closing: %d)",
            prefix,
            len(final_games),
            total_before,
            total_after,
            games_with_no_fresh_closing,
        )
    finally:
        await store.close()
    return 0


async def mark_excluded(
    *,
    sports: str,
    from_iso: str,
    until_iso: str,
    reason: str,
    dry_run: bool = False,
) -> int:
    """One-shot: stamp `excluded_reason` on value_bets
    rows for the given sports inside a [from, until) UTC window, retiring
    known-bad rows from every reporting surface (dashboard, audit,
    reporting) without deleting them. Built for the 2026-06
    basketball matchView incident; the bounds are explicit required
    arguments because the contaminated window depends on the DEPLOY times
    of the bug and the fix on the host, not the commit dates.

    --dry-run: report per-table counts without writing.
    """
    setup_logging()
    logger = logging.getLogger("oddsiq")

    from datetime import datetime, timezone

    # Bounds are stringified and lexicographically compared to flagged_at /
    # observed_at (always stored +00:00) in SQLite, so they MUST be normalized
    # to UTC: a naive value is assumed UTC, an explicit offset (e.g. a CET
    # deploy time) is converted to its UTC instant.
    def _normalize(label: str, value: str) -> str | None:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            print(
                f"mark-excluded: invalid {label} {value!r} — use ISO-8601",
                file=sys.stderr,
            )
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    sport_list = [s.strip() for s in sports.split(",") if s.strip()]
    if not sport_list:
        print("mark-excluded: --sports must name at least one sport", file=sys.stderr)
        return 1
    if not reason.strip():
        print("mark-excluded: --reason must be non-empty", file=sys.stderr)
        return 1
    from_utc = _normalize("--from", from_iso)
    until_utc = _normalize("--until", until_iso)
    if from_utc is None or until_utc is None:
        return 1
    if until_utc <= from_utc:
        print("mark-excluded: --until must be after --from", file=sys.stderr)
        return 1

    if dry_run:
        logger.info("[DRY-RUN] mark-excluded: no writes will be performed")
    await asyncio.to_thread(migrations.run, config.DATABASE_PATH)
    store = Store(config.DATABASE_PATH)
    await store.initialize()
    try:
        counts = await store.mark_rows_excluded(
            sports=sport_list,
            from_iso=from_utc,
            until_iso=until_utc,
            reason=reason,
            dry_run=dry_run,
        )
        prefix = "[DRY-RUN] " if dry_run else ""
        logger.info(
            "%smark-excluded: sports=%s window=[%s, %s) reason=%s -> value_bets=%d",
            prefix,
            ",".join(sport_list),
            from_utc,
            until_utc,
            reason,
            counts["value_bets"],
        )
    finally:
        await store.close()
    return 0


def _find_zombie_live_games(games, now, grace_days: int):
    """Games permanently stuck ``status='live'``: tipoff + the sport's live
    backstop + ``grace_days`` has passed, so the game is long past pollable AND
    past the Odds API ``/scores?daysFrom=3`` window — its final can never
    arrive (it finalized during a multi-day outage, or a suspended game was
    dropped from the feed). Non-live games are never selected.
    """
    from datetime import timedelta
    from oddsiq.sports.profiles import get_profile

    zombies = []
    for g in games:
        if g.status != "live":
            continue
        tipoff = g.commence_time or g.scheduled_at
        backstop = get_profile(g.sport).live_backstop_hours
        if tipoff + timedelta(hours=backstop, days=grace_days) < now:
            zombies.append(g)
    return zombies


def _find_zombie_scheduled_games(games, now, grace_days: int):
    """Games permanently stuck ``status='scheduled'``: the tipoff + the
    sport's live backstop + ``grace_days`` has passed while the game never
    went live/final. The Odds API exposes no ``postponed`` status —
    postponed/cancelled fixtures simply drop from the feed and linger as
    ``scheduled`` until they age out of the ``daysFrom=3`` window, tripping
    the startup stranded-game warning at every boot. Same age rule as the
    live finder, so a game merely hours past tipoff (finals window still
    open) is never selected. Non-scheduled games are never selected.
    """
    from datetime import timedelta
    from oddsiq.sports.profiles import get_profile

    zombies = []
    for g in games:
        if g.status != "scheduled":
            continue
        tipoff = g.commence_time or g.scheduled_at
        backstop = get_profile(g.sport).live_backstop_hours
        if tipoff + timedelta(hours=backstop, days=grace_days) < now:
            zombies.append(g)
    return zombies


async def delete_zombie_games(
    *,
    grace_days: int = 3,
    dry_run: bool = False,
    include_scheduled: bool = False,
) -> int:
    """One-shot: DELETE games permanently stuck ``status='live'`` (see
    ``_find_zombie_live_games``) together with every dependent row —
    ``odds_snapshots``, ``value_bets``, ``placed_bets`` — children
    first in a single transaction. Their bets/candidates can never settle and
    would pollute reporting denominators forever; the operator decision
    (2026-07-10) is to remove them outright rather than annotate them.

    --include-scheduled additionally removes dropped/postponed fixtures stuck
    ``status='scheduled'`` past the same age rule (2026-08-09; see
    ``_find_zombie_scheduled_games``).

    --dry-run: report the would-be deletions without writing.
    """
    setup_logging()
    logger = logging.getLogger("oddsiq")

    from datetime import datetime, timezone

    if grace_days < 0:
        print("delete-zombie-games: --grace-days must be >= 0", file=sys.stderr)
        return 1
    if dry_run:
        logger.info("[DRY-RUN] delete-zombie-games: no writes will be performed")
    await asyncio.to_thread(migrations.run, config.DATABASE_PATH)
    store = Store(config.DATABASE_PATH)
    await store.initialize()
    try:
        now = datetime.now(timezone.utc)
        live_games = await store.get_games_by_status("live")
        zombies = _find_zombie_live_games(live_games, now, grace_days)
        n_live = len(zombies)
        n_scheduled = 0
        if include_scheduled:
            scheduled_games = await store.get_games_by_status("scheduled")
            scheduled_zombies = _find_zombie_scheduled_games(
                scheduled_games, now, grace_days
            )
            n_scheduled = len(scheduled_zombies)
            zombies = zombies + scheduled_zombies
        prefix = "[DRY-RUN] " if dry_run else ""
        for g in zombies:
            tipoff = g.commence_time or g.scheduled_at
            logger.info(
                "%szombie game_id=%d external_id=%s status=%s sport=%s %s @ %s tipoff=%s",
                prefix,
                g.id,
                g.external_id,
                g.status,
                g.sport,
                g.away_team,
                g.home_team,
                tipoff.isoformat(),
            )
        counts = await store.delete_games_with_children(
            [g.id for g in zombies], dry_run=dry_run
        )
        logger.info(
            "%sdelete-zombie-games: grace_days=%d -> games=%d (live=%d scheduled=%d) "
            "odds_snapshots=%d value_bets=%d placed_bets=%d",
            prefix,
            grace_days,
            counts["games"],
            n_live,
            n_scheduled,
            counts["odds_snapshots"],
            counts["value_bets"],
            counts["placed_bets"],
        )
    finally:
        await store.close()
    return 0


def _effective_money_knob(
    raw: str | None,
    default: float,
    maximum: float,
    *,
    minimum: float = 0.0,
    as_int: bool = False,
) -> str:
    """Render one money knob exactly as the ENGINE reads it.

    Mirrors ``BettingService._bounded_meta_{float,int}``: parse the meta value
    (garbage / non-finite falls back to the default), then clamp into
    ``[minimum, env ceiling]``. Printing the raw meta instead would lie — a
    hand-edited ``betting_max_bets_per_day=999`` shows 999 while the engine
    still enforces ``BETTING_MAX_BETS_PER_DAY``. When the two differ, the raw
    value is shown alongside so the stale edit is obvious.
    """
    caster = int if as_int else float
    ceiling = maximum if (as_int or math.isfinite(maximum)) else default
    ceiling = max(minimum, ceiling)

    parsed: float | None
    if raw is None:
        parsed = None
        value = default
    else:
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            parsed, value = None, default
        else:
            if not as_int and not math.isfinite(value):
                parsed, value = None, default
            else:
                parsed = value
    effective = max(minimum, min(value, ceiling))
    shown = str(int(effective)) if as_int else f"{effective:g}"

    if raw is None:
        return f"{shown} (default)"
    if parsed is not None and parsed == effective:
        return shown
    return f"{shown} (meta {raw}, {'clamped' if parsed is not None else 'invalid'})"


async def betting_control(
    *,
    arm: bool = False,
    disarm: bool = False,
    reconcile_id: int | None = None,
    reconcile_as: str | None = None,
    accepted_odds: float | None = None,
    receipt_id: str | None = None,
    reason: str | None = None,
) -> int:
    """Arm/disarm the auto-betting engine, or print its status (default).

    The armed flag lives in the meta table so it survives restarts and is
    shared with the dashboard (Phase 3). Arming while BETTING_ENABLED is off
    is harmless — the engine isn't constructed, so nothing can fire — but the
    status output makes that state explicit.
    """
    setup_logging()
    logger = logging.getLogger("oddsiq")

    from datetime import datetime, timezone

    await asyncio.to_thread(migrations.run, config.DATABASE_PATH)
    store = Store(config.DATABASE_PATH)
    await store.initialize()
    try:
        if reconcile_id is None and any(
            value is not None
            for value in (reconcile_as, accepted_odds, receipt_id, reason)
        ):
            print(
                "Reconciliation options require --reconcile PLACED_BET_ID.",
                file=sys.stderr,
            )
            return 2
        if reconcile_id is not None:
            if reconcile_as is None:
                print(
                    "Reconciliation requires --as-placed or --as-rejected.",
                    file=sys.stderr,
                )
                return 2
            if reconcile_as == "placed" and (
                accepted_odds is None or not receipt_id or not receipt_id.strip()
            ):
                print(
                    "Placed reconciliation requires --accepted-odds and --receipt-id.",
                    file=sys.stderr,
                )
                return 2
            if reconcile_as == "rejected" and (not reason or not reason.strip()):
                print("Rejected reconciliation requires --reason.", file=sys.stderr)
                return 2
            try:
                changed = await store.reconcile_placed_bet(
                    reconcile_id,
                    status=reconcile_as,
                    accepted_odds=accepted_odds,
                    receipt_id=receipt_id,
                    reason=reason,
                )
            except ValueError as exc:
                print(f"Reconciliation refused: {exc}", file=sys.stderr)
                return 2
            if not changed:
                current = await store.get_placed_bet(reconcile_id)
                detail = (
                    "not found"
                    if current is None
                    else f"current status={current.status}"
                )
                print(
                    f"Reconciliation refused for placed_bet id={reconcile_id}: {detail}.",
                    file=sys.stderr,
                )
                return 2
            was_armed = (await store.get_meta("betting_armed")) == "1"
            await store.set_meta("betting_armed", "0")
            await store.set_meta(
                "betting_disarm_reason",
                f"operator_reconciliation_{reconcile_id}",
            )
            logger.warning(
                "bet_reconciled: placed_bet_id=%d status=%s receipt=%s reason=%s",
                reconcile_id,
                reconcile_as,
                receipt_id,
                reason,
            )
            print(
                f"Reconciled placed_bet id={reconcile_id} as {reconcile_as}. "
                + (
                    "Engine disarmed (was armed)"
                    if was_armed
                    else "Engine remains disarmed"
                )
                + " — re-arm explicitly when ready."
            )
        elif arm:
            await store.set_meta("betting_armed", "1")
            logger.info("betting_armed: by operator CLI")
            print("Auto-betting ARMED.")
        elif disarm:
            await store.set_meta("betting_armed", "0")
            logger.info("betting_disarmed: reason=operator_cli")
            print("Auto-betting DISARMED.")

        from oddsiq.pipeline.betting_service import parse_betting_sports

        armed = (await store.get_meta("betting_armed")) == "1"
        # Print the EFFECTIVE parsed gate, not the raw meta — what the engine
        # enforces is what the operator must see (matches the dashboard chip).
        raw_sports = await store.get_meta("betting_sports")
        betting_sports = ",".join(sorted(parse_betting_sports(raw_sports)))
        if raw_sports is None:
            betting_sports += " (default)"
        # Print the EFFECTIVE (clamped) knobs for the same reason as the gate
        # above: the engine enforces the env ceiling, so a meta edited past it
        # must not read back as if it were in force.
        stake = _effective_money_knob(
            await store.get_meta("betting_flat_stake_czk"),
            20.0,
            config.BETTING_MAX_STAKE_CZK,
        )
        max_day = _effective_money_knob(
            await store.get_meta("betting_max_bets_per_day"),
            10,
            config.BETTING_MAX_BETS_PER_DAY,
            as_int=True,
        )
        max_open = _effective_money_knob(
            await store.get_meta("betting_max_open_stakes_czk"),
            500.0,
            config.BETTING_MAX_OPEN_STAKES_CZK,
        )
        loss_limit = _effective_money_knob(
            await store.get_meta("betting_daily_loss_limit_czk"),
            300.0,
            config.BETTING_MAX_DAILY_LOSS_CZK,
        )
        disarm_reason = await store.get_meta("betting_disarm_reason")
        today = datetime.now(timezone.utc).date().isoformat()
        stats = await store.betting_day_stats(today)

        print("Auto-betting status")
        print("=" * 40)
        print(
            f"BETTING_ENABLED (env):  {'1' if config.BETTING_ENABLED else '0 (engine not constructed)'}"
        )
        print(f"Armed:                  {'YES' if armed else 'no'}")
        print(f"Betting sports:         {betting_sports}")
        if disarm_reason:
            print(f"Last disarm:            {disarm_reason}")
        print(f"Flat stake (Kč):        {stake}")
        print(f"Max bets/day:           {max_day}")
        print(f"Max open stakes (Kč):   {max_open}")
        print(f"Daily loss limit (Kč):  {loss_limit}")
        print(f"Hard stake ceiling (Kč): {config.BETTING_MAX_STAKE_CZK:.0f} (env)")
        print(f"Hard bets/day ceiling:   {config.BETTING_MAX_BETS_PER_DAY} (env)")
        print(
            f"Hard open ceiling (Kč):  {config.BETTING_MAX_OPEN_STAKES_CZK:.0f} (env)"
        )
        print(f"Hard loss ceiling (Kč):  {config.BETTING_MAX_DAILY_LOSS_CZK:.0f} (env)")
        print(
            f"Today ({today}): commanded={stats['bets_commanded']} "
            f"open={stats['open_stakes_czk']:.0f} Kč "
            f"realized={stats['realized_pl_czk']:+.0f} Kč"
        )
        return 0
    finally:
        await store.close()


async def audit_bookmakers(
    *,
    bookmaker: str | None = None,
    since: str | None = None,
    as_json: bool = False,
) -> int:
    """Run the per-bookmaker audit and print the report. Returns exit code."""
    setup_logging()

    # Validate + normalize --since up front: it's compared lexicographically
    # against flagged_at/captured_at (always stored +00:00) in SQLite, so it MUST
    # be normalized to UTC — a naive value is assumed UTC, an explicit offset is
    # converted to its UTC instant (otherwise a +02:00 string would mis-compare
    # and silently drop in-window rows).
    if since is not None:
        from datetime import datetime as _dt, timezone as _tz

        try:
            since_dt = _dt.fromisoformat(since)
        except ValueError:
            print("--since must be an ISO date/time, e.g. 2026-06-01", file=sys.stderr)
            return 1
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=_tz.utc)
        else:
            since_dt = since_dt.astimezone(_tz.utc)
        since = since_dt.isoformat()

    # Apply pending migrations so a fresh DB is queryable.
    await asyncio.to_thread(migrations.run, config.DATABASE_PATH)

    from oddsiq.audit import bookmakers as audit_module

    store = Store(config.DATABASE_PATH)
    await store.initialize()
    try:
        audit = await audit_module.run_audit(store, bookmaker=bookmaker, since=since)
        text = (
            audit_module.format_json(audit)
            if as_json
            else audit_module.format_text(audit)
        )
        print(text)
        return 0
    finally:
        await store.close()


# Tipsport moneyline smoke: a wide odds band + generous elapsed ceiling (a cold
# Cloudflare solve on the first run can be slow).
_TIPSPORT_SMOKE_MIN_ODDS: float = 1.01
_TIPSPORT_SMOKE_MAX_ODDS: float = 100.0
_TIPSPORT_SMOKE_MAX_ELAPSED_S: float = 60.0


async def tipsport_smoke(sport: str = "nba") -> int:
    """Run TipsportScraper.fetch_odds once outside the scheduler. Prints what
    it would emit, never writes to the DB. Exits non-zero on transport/parser
    errors, no snapshots, no moneyline pairs, invalid odds, or excessive
    elapsed time.
    """
    from datetime import date, datetime, timezone
    from oddsiq.scrapers.tipsport import TipsportScraper
    from oddsiq.sports.profiles import get_profile, SPORTS

    setup_logging()
    logger = logging.getLogger("oddsiq")
    try:
        profile = get_profile(sport)
    except KeyError:
        print(
            f"Unknown sport '{sport}'. Valid: {', '.join(sorted(SPORTS))}",
            file=sys.stderr,
        )
        return 1

    scraper = TipsportScraper()
    started = datetime.now(timezone.utc)
    try:
        snapshots = await scraper.fetch_odds(date.today(), profile)
    except Exception as e:
        logger.exception("Tipsport smoke fetch failed")
        print(f"Tipsport smoke FAILED: {e}", file=sys.stderr)
        await scraper.close()
        return 1

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    internal_errors = getattr(scraper, "last_fetch_error_count", 0)
    market_counts: dict[str, int] = {}
    for snap in snapshots:
        market_counts[snap.market_type] = market_counts.get(snap.market_type, 0) + 1

    print(f"Tipsport smoke test — {started.isoformat()}")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Snapshots returned: {len(snapshots)}")
    print(f"Fetch errors: {internal_errors}")
    print(f"Market counts: {market_counts}")
    print("")

    events: dict[tuple[str, str], dict[str, float]] = {}
    for snap in snapshots:
        if snap.market_type != "moneyline":
            continue
        key = (snap._home_team or "?", snap._away_team or "?")
        events.setdefault(key, {})[snap.selection] = snap.odds

    if not events:
        print(
            "(no moneyline events parsed — Tipsport may have returned an empty listing)"
        )
    else:
        print(f"First {min(3, len(events))} moneyline events:")
        for i, ((home, away), odds) in enumerate(list(events.items())[:3], start=1):
            home_odds = odds.get("home", "?")
            away_odds = odds.get("away", "?")
            print(f"  {i}. {home} vs {away} — home {home_odds} / away {away_odds}")
        # Two-way overround (implied-sum) — the hold measurement read before
        # arming a new sport: it bounds the max co-temporal edge (the WNBA
        # lesson). 1.00-odds placeholder legs are excluded (suspended offers).
        sums = sorted(
            sum(1.0 / o for o in ev.values())
            for ev in events.values()
            if len(ev) == 2 and all(o and o > 1.0 for o in ev.values())
        )
        if sums:
            avg = sum(sums) / len(sums)
            print(
                f"Two-way overround: avg {avg:.3f} across {len(sums)} event(s) "
                f"(min {sums[0]:.3f}, max {sums[-1]:.3f})"
            )

    await scraper.close()

    failures: list[str] = []
    moneyline_count = market_counts.get("moneyline", 0)
    if internal_errors > 0:
        failures.append(f"scraper reported {internal_errors} internal fetch error(s)")
    if len(snapshots) == 0:
        failures.append("zero snapshots returned (likely a parser or fetch failure)")
    if moneyline_count == 0:
        failures.append("zero moneyline snapshots returned")
    if moneyline_count > 0 and moneyline_count % 2 != 0:
        failures.append(
            f"odd moneyline snapshot count {moneyline_count}; expected home + away pairs"
        )
    out_of_range = sorted(
        s.odds
        for s in snapshots
        if s.odds < _TIPSPORT_SMOKE_MIN_ODDS or s.odds > _TIPSPORT_SMOKE_MAX_ODDS
    )
    if out_of_range:
        failures.append(
            f"{len(out_of_range)} snapshot(s) outside [{_TIPSPORT_SMOKE_MIN_ODDS}, "
            f"{_TIPSPORT_SMOKE_MAX_ODDS}]: {out_of_range[:3]}"
        )
    if elapsed > _TIPSPORT_SMOKE_MAX_ELAPSED_S:
        failures.append(
            f"elapsed {elapsed:.1f}s exceeds {_TIPSPORT_SMOKE_MAX_ELAPSED_S}s"
        )

    if failures:
        print(
            "\nTipsport smoke FAILED — acceptance criteria not met:",
            file=sys.stderr,
        )
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    return 0


async def audit_missed_gamedays(store: Store) -> None:
    """Surface final games that have zero odds_snapshots rows.

    Pipeline downtime leaves no closing line, no CLV, no value bets from
    that night. Without this audit the user has no visibility into missed
    gamedays at restart. Pure visibility — no retry, no requeue.

    The audit is stateful: historical misses are recorded in the meta table
    so Telegram only fires for newly discovered missed games, not the same
    stale list on every restart.
    """
    missed = await store.get_final_games_without_snapshots()
    known_raw = await store.get_meta(_MISSED_GAMEDAY_AUDIT_META_KEY)
    if not missed:
        if known_raw != "[]":
            await store.set_meta(_MISSED_GAMEDAY_AUDIT_META_KEY, "[]")
        return
    logger = logging.getLogger("oddsiq")

    known_external_ids = _decode_known_missed_gameday_ids(known_raw)
    missed_external_ids = {g.external_id for g in missed}
    summary = _format_missed_games(missed)

    if known_raw is None:
        logger.warning(
            "Pipeline has %d historical final game(s) with zero snapshots: %s "
            "(recorded as known; Telegram suppressed)",
            len(missed),
            summary,
        )
        await store.set_meta(
            _MISSED_GAMEDAY_AUDIT_META_KEY,
            _encode_missed_gameday_ids(missed_external_ids),
        )
        return

    newly_missed = [
        game for game in missed if game.external_id not in known_external_ids
    ]
    if not newly_missed:
        logger.info(
            "Missed-gameday audit: %d known final game(s) still have zero "
            "snapshots; Telegram suppressed.",
            len(missed),
        )
        await store.set_meta(
            _MISSED_GAMEDAY_AUDIT_META_KEY,
            _encode_missed_gameday_ids(missed_external_ids),
        )
        return

    new_summary = _format_missed_games(newly_missed)
    logger.warning(
        "Pipeline missed %d new final game(s) with zero snapshots: %s",
        len(newly_missed),
        new_summary,
    )
    # Logged only; no phone alert (2026-08-10 rebrand — only placed/won/lost).
    await store.set_meta(
        _MISSED_GAMEDAY_AUDIT_META_KEY,
        _encode_missed_gameday_ids(missed_external_ids),
    )


def _format_missed_games(games: list[object]) -> str:
    # Feed-sourced names are interpolated into an HTML-parse-mode Telegram
    # message; escape &, <, > so a stray special char can't get the alert
    # rejected. html.escape is an identity transform for normal names, so the
    # reused plain-text log line is unaffected in practice.
    from html import escape

    return ", ".join(
        f"{escape(str(g.external_id))} ({escape(g.away_team)} @ {escape(g.home_team)}, "
        f"{g.scheduled_at.date().isoformat()})"
        for g in games
    )


def _decode_known_missed_gameday_ids(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(decoded, list):
        return set()
    return {str(value) for value in decoded}


def _encode_missed_gameday_ids(external_ids: set[str]) -> str:
    return json.dumps(sorted(external_ids), separators=(",", ":"))


def _install_signal_handlers(loop, on_signal) -> None:
    """Wire SIGINT/SIGTERM to ``on_signal`` cross-platform.

    POSIX loops support ``loop.add_signal_handler``. The Windows
    ProactorEventLoop raises ``NotImplementedError`` for it, so on Windows we
    fall back to ``signal.signal`` and marshal the callback onto the loop with
    ``call_soon_threadsafe`` (safe from a C-level signal handler). Linux/Docker
    — the real runtime — keeps the existing behavior; the win32 branch only
    enables the native-Windows fallback.
    """
    if sys.platform == "win32":

        def _win_handler(signum, frame):  # pragma: no cover - invoked by the OS
            loop.call_soon_threadsafe(on_signal)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, _win_handler)
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)


async def async_main() -> None:
    setup_logging()
    logger = logging.getLogger("oddsiq")

    if not config.ODDS_API_KEY:
        logger.error(
            "Odds API key not set. Copy .env.example to .env and set ODDS_API_KEY."
        )
        sys.exit(1)

    config.RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Backup BEFORE opening any connection — captures the prior session's final
    # state intact, including any crashed-session WAL data (the online backup API
    # handles WAL safely; see oddsiq/backup.py).
    if config.BACKUP_ENABLED:
        db_existed = config.DATABASE_PATH.exists()
        backup_path = await asyncio.to_thread(
            backup.backup_database,
            config.DATABASE_PATH,
            config.BACKUP_DIR,
            "startup",
            keep_daily=config.BACKUP_KEEP_DAILY,
            keep_monthly=config.BACKUP_KEEP_MONTHLY,
        )
        if backup_path is None and db_existed:
            logger.warning("Startup backup failed; see logs.")

    if config.RAW_RESPONSE_RETENTION_ENABLED:
        try:
            await asyncio.to_thread(
                scraper_base.prune_raw_responses,
                config.RAW_DATA_DIR,
                config.RAW_RESPONSE_KEEP_DAYS,
            )
        except Exception as e:
            logger.warning("Raw response pruning failed: %s", e)

    # Apply pending schema migrations before opening the aiosqlite store.
    # Blocking stdlib sqlite3 wrapped in to_thread, same pattern as backup.
    await asyncio.to_thread(migrations.run, config.DATABASE_PATH)

    store = Store(config.DATABASE_PATH)
    await store.initialize()

    await audit_missed_gamedays(store)

    scheduler = Scheduler(store)

    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        logger.info("Interrupt received, stopping...")
        scheduler.stop()

    _install_signal_handlers(loop, handle_signal)

    try:
        await scheduler.run()
    finally:
        await store.close()
        if config.BACKUP_ENABLED:
            db_existed = config.DATABASE_PATH.exists()
            backup_path = await asyncio.to_thread(
                backup.backup_database,
                config.DATABASE_PATH,
                config.BACKUP_DIR,
                "shutdown",
                keep_daily=config.BACKUP_KEEP_DAILY,
                keep_monthly=config.BACKUP_KEEP_MONTHLY,
            )
            if backup_path is None and db_existed:
                logger.warning("Shutdown backup failed; see logs.")
        if config.RAW_RESPONSE_RETENTION_ENABLED:
            try:
                await asyncio.to_thread(
                    scraper_base.prune_raw_responses,
                    config.RAW_DATA_DIR,
                    config.RAW_RESPONSE_KEEP_DAYS,
                )
            except Exception as e:
                logger.warning("Raw response pruning failed: %s", e)
        # Close the telegram httpx client again in case anything recreated it
        # after scheduler._shutdown closed it (idempotent — no-op if closed).
        await telegram.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="oddsiq")
    parser.add_argument(
        "--check-budget",
        action="store_true",
        help="Probe the Odds API quota and exit without starting the pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command")
    cleanup = subparsers.add_parser(
        "cleanup-closing-flags",
        help=(
            "One-shot: reset is_closing=1 flags on existing data and re-mark "
            "under the current freshness gate. Use --dry-run to preview."
        ),
    )
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    mark = subparsers.add_parser(
        "mark-excluded",
        help=(
            "One-shot: stamp excluded_reason on value_bets "
            "for the given sports in a [from, until) UTC window, hiding "
            "known-bad rows from reporting without deleting them. "
            "Use --dry-run to preview."
        ),
    )
    mark.add_argument(
        "--sports",
        required=True,
        help="Comma-separated sport keys (e.g. 'nba,wnba')",
    )
    mark.add_argument(
        "--from",
        dest="from_iso",
        required=True,
        help="Window start, ISO-8601, inclusive (naive = UTC)",
    )
    mark.add_argument(
        "--until",
        dest="until_iso",
        required=True,
        help="Window end, ISO-8601, exclusive (naive = UTC)",
    )
    mark.add_argument(
        "--reason",
        required=True,
        help="Slug stored in excluded_reason (e.g. 'basketball_3way_matchview_2026_06')",
    )
    mark.add_argument(
        "--dry-run",
        action="store_true",
        help="Show per-table counts without writing.",
    )
    zombies = subparsers.add_parser(
        "delete-zombie-games",
        help=(
            "One-shot: DELETE games permanently stuck status='live' past "
            "tipoff + lifecycle backstop + grace days (their finals can never "
            "arrive), together with their placed_bets / value_bets / "
            "odds_snapshots. Use --dry-run to preview."
        ),
    )
    zombies.add_argument(
        "--grace-days",
        type=int,
        default=3,
        help=(
            "Days past tipoff+backstop before a live game counts as a zombie "
            "(default 3 — the Odds API scores window)"
        ),
    )
    zombies.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without writing.",
    )
    zombies.add_argument(
        "--include-scheduled",
        action="store_true",
        help=(
            "Also delete dropped/postponed fixtures stuck status='scheduled' "
            "past the same tipoff + backstop + grace-days age rule (the Odds "
            "API has no 'postponed' status; such fixtures linger forever and "
            "trip the startup stranded-game warning)."
        ),
    )
    betting = subparsers.add_parser(
        "betting",
        help=(
            "Auto-betting engine control: --arm / --disarm flip the persistent "
            "armed flag (meta table); bare invocation prints status + today's "
            "counts. The engine only exists when BETTING_ENABLED=1."
        ),
    )
    betting_mode = betting.add_mutually_exclusive_group()
    betting_mode.add_argument(
        "--arm", action="store_true", help="Arm the engine (allow real placements)."
    )
    betting_mode.add_argument(
        "--disarm",
        action="store_true",
        help="Disarm the engine (no further placements).",
    )
    betting_mode.add_argument(
        "--reconcile",
        dest="reconcile_id",
        type=int,
        metavar="PLACED_BET_ID",
        help="Resolve one pending/unknown decision after checking Tipsport history.",
    )
    reconcile_outcome = betting.add_mutually_exclusive_group()
    reconcile_outcome.add_argument(
        "--as-placed",
        dest="reconcile_as",
        action="store_const",
        const="placed",
        help="Confirm the wager exists (requires accepted odds and receipt id).",
    )
    reconcile_outcome.add_argument(
        "--as-rejected",
        dest="reconcile_as",
        action="store_const",
        const="rejected",
        help="Confirm no wager exists (requires --reason).",
    )
    betting.add_argument("--accepted-odds", type=float)
    betting.add_argument("--receipt-id")
    betting.add_argument(
        "--reason",
        help="Operator reconciliation evidence/note: REQUIRED with --as-rejected, "
        "optional with --as-placed.",
    )
    audit = subparsers.add_parser(
        "audit-bookmakers",
        help="Per-bookmaker audit report: snapshot volumes, value bets, CLV stats, closing quality, coverage-gap proxy.",
    )
    audit.add_argument(
        "--bookmaker", default=None, help="Restrict to one bookmaker (e.g., 'tipsport')"
    )
    audit.add_argument(
        "--since", default=None, help="YYYY-MM-DD; restrict the audit window"
    )
    audit.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit machine-readable JSON"
    )
    smoke = subparsers.add_parser(
        "tipsport-smoke",
        help="Pre-flight: run TipsportScraper once outside the scheduler and print results. Does not write to the DB.",
    )
    smoke.add_argument(
        "--sport", default="nba", help="Sport key (nba|nhl|mlb|wnba|nfl)"
    )
    dash = subparsers.add_parser(
        "dashboard",
        help=(
            "Start the read-only FastAPI dashboard (localhost:8080 by default). "
            "Reads oddsiq.db (WAL — concurrent readers OK) and tails logs/oddsiq.log."
        ),
    )
    dash.add_argument(
        "--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)"
    )
    dash.add_argument("--port", type=int, default=8080, help="Bind port (default 8080)")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    if args.command == "cleanup-closing-flags":
        sys.exit(asyncio.run(cleanup_closing_flags(dry_run=args.dry_run)))
    if args.command == "mark-excluded":
        sys.exit(
            asyncio.run(
                mark_excluded(
                    sports=args.sports,
                    from_iso=args.from_iso,
                    until_iso=args.until_iso,
                    reason=args.reason,
                    dry_run=args.dry_run,
                )
            )
        )
    if args.command == "delete-zombie-games":
        sys.exit(
            asyncio.run(
                delete_zombie_games(
                    grace_days=args.grace_days,
                    dry_run=args.dry_run,
                    include_scheduled=args.include_scheduled,
                )
            )
        )
    if args.command == "betting":
        sys.exit(
            asyncio.run(
                betting_control(
                    arm=args.arm,
                    disarm=args.disarm,
                    reconcile_id=args.reconcile_id,
                    reconcile_as=args.reconcile_as,
                    accepted_odds=args.accepted_odds,
                    receipt_id=args.receipt_id,
                    reason=args.reason,
                )
            )
        )
    if args.command == "audit-bookmakers":
        sys.exit(
            asyncio.run(
                audit_bookmakers(
                    bookmaker=args.bookmaker,
                    since=args.since,
                    as_json=args.as_json,
                )
            )
        )
    if args.command == "tipsport-smoke":
        sys.exit(asyncio.run(tipsport_smoke(sport=args.sport)))
    if args.command == "dashboard":
        from oddsiq.dashboard.server import run_dashboard

        setup_logging()
        if args.host != "127.0.0.1":
            logging.getLogger("oddsiq").warning(
                "Dashboard binding to non-loopback host %s — no auth, only do this "
                "if you know what you're doing.",
                args.host,
            )
        sys.exit(run_dashboard(host=args.host, port=args.port))
    if args.check_budget:
        sys.exit(asyncio.run(check_budget()))
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
