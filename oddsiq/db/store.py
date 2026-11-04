from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from oddsiq.db.models import Game, OddsSnapshot, PlacedBet, ValueBet

logger = logging.getLogger(__name__)

# Shared windowed subquery selecting the snapshot ids that should be marked
# closing: the latest row per (bookmaker, market_type, selection, line) WITHIN
# the pre-tipoff freshness window. The freshness/pre-tipoff filter lives INSIDE
# the window input so a post-tipoff row can't win ROW_NUMBER()=1 and orphan a
# valid pre-tipoff candidate. mark_closing_snapshots (the UPDATE) and
# count_markable_closing_snapshots (the dry-run COUNT) both build on this, so the
# real re-mark and its preview can never diverge.
_MARKABLE_CLOSING_IDS_SQL = """
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY bookmaker, market_type, selection, IFNULL(line, -999.0)
            ORDER BY captured_at DESC
        ) as rn
        FROM odds_snapshots
        WHERE game_id = ?
          AND captured_at >= ?
          AND captured_at <= ?
    ) WHERE rn = 1
"""

# Explicit write-lock wait (ms). aiosqlite inherits sqlite3's default connect
# timeout (5s), which already maps to busy_timeout — but setting it here makes
# the lock-wait intent explicit and independent of that default. The dashboard
# is a concurrent WAL reader, so a brief writer-exclusive checkpoint shouldn't
# surface as an immediate SQLITE_BUSY.
_BUSY_TIMEOUT_MS = 5000


def _valid_decimal_odds(value: object) -> float | None:
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    return odds if math.isfinite(odds) and odds > 1.0 else None


class Store:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        # Schema creation lives in oddsiq/db/migrations/000_baseline.py and
        # is applied by the migration runner (oddsiq.db.migrations.run) on
        # CLI startup before Store is instantiated. This method only sets
        # per-connection PRAGMAs.
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def upsert_game(self, game: Game) -> int:
        cursor = await self._db.execute(
            """INSERT INTO games
               (external_id, home_team, away_team, scheduled_at, commence_time, status, home_score, away_score, sport)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(external_id) DO UPDATE SET
                   scheduled_at=excluded.scheduled_at,
                   commence_time=COALESCE(excluded.commence_time, games.commence_time),
                   -- Once final, freeze status+scores: a later feed glitch that
                   -- re-reports the event as live with null scores must not
                   -- clobber a finalized row. Otherwise update status and only
                   -- overwrite scores when new ones are present (COALESCE), so a
                   -- status-only refresh can't wipe scores.
                   status=CASE WHEN games.status='final' THEN 'final' ELSE excluded.status END,
                   home_score=CASE WHEN games.status='final' THEN games.home_score
                                   ELSE COALESCE(excluded.home_score, games.home_score) END,
                   away_score=CASE WHEN games.status='final' THEN games.away_score
                                   ELSE COALESCE(excluded.away_score, games.away_score) END
               RETURNING id, commence_time""",
            (game.external_id, game.home_team, game.away_team,
             game.scheduled_at.isoformat(),
             game.commence_time.isoformat() if game.commence_time else None,
             game.status, game.home_score, game.away_score, game.sport),
        )
        row = await cursor.fetchone()
        await self._db.commit()
        if row["commence_time"] is not None:
            game.commence_time = datetime.fromisoformat(row["commence_time"])
        return row["id"]

    async def get_games_by_date(self, date: datetime) -> list[Game]:
        """Games whose stored ``scheduled_at`` falls on ``date`` (matched in the
        stored value's own tz — UTC). Test/tooling convenience only: no
        production caller. A local-tz caller near a UTC day boundary (overnight
        CET tipoffs) could mismatch — prefer querying by status/id in real code."""
        date_str = date.strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT * FROM games WHERE date(scheduled_at) = ?", (date_str,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_game(row) for row in rows]

    async def get_games_by_status(self, status: str) -> list[Game]:
        cursor = await self._db.execute(
            "SELECT * FROM games WHERE status = ?", (status,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_game(row) for row in rows]

    async def update_game_status(self, game_id: int, status: str, home_score: int | None = None, away_score: int | None = None) -> None:
        # COALESCE keeps existing scores when callers omit them (e.g. the
        # live-transition path passes status only) — passing None must not
        # wipe scores already recorded at finalization.
        await self._db.execute(
            "UPDATE games SET status=?, "
            "home_score=COALESCE(?, home_score), "
            "away_score=COALESCE(?, away_score) WHERE id=?",
            (status, home_score, away_score, game_id),
        )
        await self._db.commit()

    async def update_game_commence_time(self, game_id: int, commence_time: datetime) -> None:
        await self._db.execute(
            "UPDATE games SET commence_time=? WHERE id=?",
            (commence_time.isoformat(), game_id),
        )
        await self._db.commit()

    async def insert_odds_snapshot(self, snap: OddsSnapshot) -> int:
        """Insert one snapshot, return its id. Convenience for tests and one-off
        tooling; the production poll path uses ``insert_odds_snapshots_batch``
        (which dedups). Not on the hot path."""
        cursor = await self._db.execute(
            """INSERT INTO odds_snapshots
               (game_id, bookmaker, market_type, selection, line, odds, is_closing,
                raw_response_path, source_updated_at, captured_at, sport)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (snap.game_id, snap.bookmaker, snap.market_type, snap.selection,
             snap.line, snap.odds, snap.is_closing, snap.raw_response_path,
             snap.source_updated_at.isoformat() if snap.source_updated_at else None,
             snap.captured_at.isoformat(),
             snap.sport),
        )
        row = await cursor.fetchone()
        await self._db.commit()
        return row[0]

    async def insert_odds_snapshots_batch(self, snapshots: list[OddsSnapshot]) -> None:
        """Insert snapshots, skipping rows identical in (odds, line) to the most
        recent snapshot for the same (game_id, bookmaker, market_type, selection).

        Rationale: polls typically return unchanged odds. Writing a new row each
        time bloats the DB and dilutes CLV reads without adding information —
        the most recent row is the effective current odds regardless.
        """
        if not snapshots:
            return
        to_insert: list[OddsSnapshot] = []
        # A deduped row means "last seen". Keep its timestamp, raw payload and
        # upstream freshness evidence aligned instead of pairing a fresh
        # captured_at with the first observation's old raw file. The raw path
        # is COALESCEd in SQL below: _save_raw_response returns None when the
        # write failed, and a failed archive write must not erase the path of
        # a payload that IS on disk. source_updated_at deliberately overwrites
        # even with NULL — losing upstream freshness evidence is the
        # fail-closed direction (downstream treats NULL as "unknown").
        to_bump: list[tuple[str, str | None, str | None, int]] = []
        for snap in snapshots:
            latest = await self.get_latest_odds(
                snap.game_id, snap.bookmaker, snap.market_type, snap.selection, snap.line
            )
            if (latest is not None and latest.odds == snap.odds
                    and latest.line == snap.line and not latest.is_closing):
                # Dedup hit on a non-closing row — bump captured_at ("last seen").
                # Without this, captured_at drifts when odds are stable and CLV
                # reads a stale closing timestamp.
                to_bump.append((
                    snap.captured_at.isoformat(),
                    snap.raw_response_path,
                    snap.source_updated_at.isoformat() if snap.source_updated_at else None,
                    latest.id,
                ))
                continue
            # Otherwise insert. Never bump an is_closing=1 row: moving its
            # captured_at past tipoff would break the "is_closing => pre-tipoff"
            # invariant that mark_closing_snapshots / get_closing_odds rely on
            # (live polls are the only path that writes after the closing mark).
            to_insert.append(snap)
        if to_bump:
            await self._db.executemany(
                """UPDATE odds_snapshots
                   SET captured_at=?,
                       raw_response_path=COALESCE(?, raw_response_path),
                       source_updated_at=?
                   WHERE id=?""",
                to_bump,
            )
        if to_insert:
            await self._db.executemany(
                """INSERT INTO odds_snapshots
                   (game_id, bookmaker, market_type, selection, line, odds, is_closing,
                    raw_response_path, source_updated_at, captured_at, sport)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(s.game_id, s.bookmaker, s.market_type, s.selection, s.line, s.odds,
                  s.is_closing, s.raw_response_path,
                  s.source_updated_at.isoformat() if s.source_updated_at else None,
                  s.captured_at.isoformat(), s.sport) for s in to_insert],
            )
        if to_bump or to_insert:
            await self._db.commit()

    async def get_latest_odds(
        self,
        game_id: int,
        bookmaker: str,
        market_type: str,
        selection: str,
        line: float | None = None,
    ) -> OddsSnapshot | None:
        """Most-recent snapshot for the key, scoped to ``line`` (line-aware via
        the IFNULL(-999.0) idiom so NULL moneyline lines group correctly).
        Without the line filter the snapshot dedup compares against the latest
        row across ALL lines and would re-insert a stable spread/total price
        every poll once more than one line coexists for the same selection."""
        cursor = await self._db.execute(
            """SELECT * FROM odds_snapshots
               WHERE game_id=? AND bookmaker=? AND market_type=? AND selection=?
                 AND IFNULL(line, -999.0) = IFNULL(?, -999.0)
               ORDER BY captured_at DESC LIMIT 1""",
            (game_id, bookmaker, market_type, selection, line),
        )
        row = await cursor.fetchone()
        return self._row_to_snapshot(row) if row else None

    async def mark_closing_snapshots(self, game_id: int, freshness_minutes: int) -> None:
        """Mark the latest snapshot per (bookmaker, market_type, selection, line)
        as closing — but only if its captured_at is within `freshness_minutes`
        before the game's effective tipoff time. Stale or post-tipoff snapshots are
        left at is_closing=0; downstream CLV calculation skips games without
        a closing snapshot.

        Partitioning includes `line` (via IFNULL to keep NULL-NULL grouping)
        so spread/total selections at different lines (e.g. -4.5 vs -3.5)
        each get their own closing marker. Without that, the latest row across
        all lines would win and CLV could be computed against a different line
        than the bet was flagged at. Moneyline (line IS NULL) is unaffected.

        The freshness + pre-tipoff filter is applied INSIDE the window function
        so post-tipoff rows can't shadow valid pre-tipoff candidates. A poll
        whose captured_at landed just after tipoff (or a dedup bump that pushed
        a previously fresh snapshot past tipoff via "last seen" semantics)
        would otherwise win ROW_NUMBER()=1 and then be excluded by the outer
        filter — leaving the genuine pre-tipoff snapshot unmarked.
        """
        bounds = await self._closing_window_bounds(game_id, freshness_minutes)
        if bounds is None:
            return
        cutoff_iso, tipoff_iso = bounds
        await self._db.execute(
            f"UPDATE odds_snapshots SET is_closing = 1 WHERE id IN ({_MARKABLE_CLOSING_IDS_SQL})",
            (game_id, cutoff_iso, tipoff_iso),
        )
        await self._db.commit()

    async def _closing_window_bounds(
        self, game_id: int, freshness_minutes: int
    ) -> tuple[str, str] | None:
        """Return ``(cutoff_iso, effective_tipoff_iso)`` for the closing-mark
        window, or None if the game is missing. Shared by mark_closing_snapshots
        and count_markable_closing_snapshots so both use identical bounds."""
        cursor = await self._db.execute(
            "SELECT scheduled_at, commence_time FROM games WHERE id=?", (game_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        scheduled_at = datetime.fromisoformat(row["scheduled_at"])
        effective_tipoff = (
            datetime.fromisoformat(row["commence_time"])
            if row["commence_time"] is not None
            else scheduled_at
        )
        cutoff = effective_tipoff - timedelta(minutes=freshness_minutes)
        return cutoff.isoformat(), effective_tipoff.isoformat()

    async def count_markable_closing_snapshots(
        self, game_id: int, freshness_minutes: int
    ) -> int:
        """Count the rows mark_closing_snapshots WOULD mark, without writing.

        Uses the same windowed SQL as the real re-mark so the
        ``cleanup-closing-flags --dry-run`` preview matches it exactly — including
        when a selection's latest snapshot is post-tipoff."""
        bounds = await self._closing_window_bounds(game_id, freshness_minutes)
        if bounds is None:
            return 0
        cutoff_iso, tipoff_iso = bounds
        cursor = await self._db.execute(
            f"SELECT COUNT(*) FROM ({_MARKABLE_CLOSING_IDS_SQL})",
            (game_id, cutoff_iso, tipoff_iso),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_closing_flags(self, game_id: int) -> int:
        """Count is_closing=1 snapshots for a game (cleanup before/after counts)."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM odds_snapshots WHERE game_id=? AND is_closing=1",
            (game_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_closing_odds(
        self,
        game_id: int,
        bookmaker: str,
        market_type: str,
        selection: str,
        line: float | None = None,
    ) -> OddsSnapshot | None:
        """Return the closing snapshot for the given key, line-aware.

        Pass `line` to match spread/total bets at a specific line; moneyline
        callers pass None (the default) and match rows where line IS NULL.
        The IFNULL(...) = IFNULL(?, -999.0) idiom mirrors mark_closing_snapshots
        and the value_bets unique index so all three agree on what "same bet"
        means.
        """
        # mark_closing_snapshots marks exactly one row per key, but nothing
        # structurally forbids a second; ORDER BY makes the LIMIT 1 deterministic
        # (newest wins) instead of relying on physical row order.
        cursor = await self._db.execute(
            """SELECT * FROM odds_snapshots
               WHERE game_id=? AND bookmaker=? AND market_type=? AND selection=?
                 AND IFNULL(line, -999.0) = IFNULL(?, -999.0)
                 AND is_closing=1
               ORDER BY captured_at DESC, id DESC
               LIMIT 1""",
            (game_id, bookmaker, market_type, selection, line),
        )
        row = await cursor.fetchone()
        return self._row_to_snapshot(row) if row else None

    async def insert_value_bet(self, vb: ValueBet) -> int | None:
        # ON CONFLICT target must syntactically match migration 013's
        # expression index (the current definition of
        # idx_value_bets_unique_edge) — keeping `IFNULL(line, -999.0)` and
        # `IFNULL(excluded_reason, '')` identical in both places lets SQLite
        # map the conflict to that index.
        # Including line in the dedup key prevents two genuinely different
        # spread bets (e.g., -4.5 vs -3.5 after a line move) from collapsing
        # onto the same row; including `tier` keeps warm-tier and
        # hot-tier observations of the same selection as independent rows
        # (historical rows may carry 'medium' from before the cadence rename;
        # see migration 003 for the rationale).
        cursor = await self._db.execute(
            """INSERT INTO value_bets
               (game_id, bookmaker, market_type, selection, line, entry_odds, fair_odds, edge_pct, flagged_at, tier, sport, excluded_reason,
                tipsport_match_id, tipsport_opp_id, tipsport_url,
                sharp_source_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, bookmaker, market_type, selection,
                           IFNULL(line, -999.0), tier,
                           IFNULL(excluded_reason, '')) DO NOTHING
               RETURNING id""",
            (vb.game_id, vb.bookmaker, vb.market_type, vb.selection, vb.line,
             vb.entry_odds, vb.fair_odds, vb.edge_pct, vb.flagged_at.isoformat(), vb.tier, vb.sport,
             vb.excluded_reason,
             vb.tipsport_match_id, vb.tipsport_opp_id, vb.tipsport_url,
             vb.sharp_source_updated_at.isoformat()
             if vb.sharp_source_updated_at else None),
        )
        row = await cursor.fetchone()
        await self._db.commit()
        return row[0] if row else None

    async def update_value_bet_closing(self, bet_id: int, closing_odds: float, pinnacle_closing: float, clv_pct: float) -> None:
        await self._db.execute(
            "UPDATE value_bets SET closing_odds=?, pinnacle_closing=?, clv_pct=? WHERE id=?",
            (closing_odds, pinnacle_closing, clv_pct, bet_id),
        )
        await self._db.commit()

    async def update_value_bet_result(self, bet_id: int, result: str) -> None:
        await self._db.execute(
            "UPDATE value_bets SET result=? WHERE id=?", (result, bet_id)
        )
        await self._db.commit()

    async def clear_closing_flags(self, game_id: int) -> int:
        """Reset is_closing=0 for every snapshot of `game_id`. Returns the
        number of rows changed. Used by the one-shot cleanup pass to wipe
        pre-Fix-3 stale closings before re-running mark_closing_snapshots
        with the freshness gate."""
        cursor = await self._db.execute(
            "UPDATE odds_snapshots SET is_closing=0 WHERE game_id=? AND is_closing=1",
            (game_id,),
        )
        rows = cursor.rowcount
        await self._db.commit()
        return rows

    async def mark_rows_excluded(
        self, *, sports: list[str], from_iso: str, until_iso: str,
        reason: str, dry_run: bool = False,
    ) -> dict[str, int]:
        """Stamp `excluded_reason` on value_bets (by flagged_at) for the
        given sports inside the [from_iso, until_iso) window. Rows already
        excluded are left untouched, so re-runs are no-ops. Returns per-table
        affected counts; with dry_run the counts are computed without writing.

        One-shot operator action behind `oddsiq mark-excluded` — retires
        known-bad rows (e.g. the 2026-06 basketball matchView incident)
        from reporting aggregates without deleting them.
        """
        placeholders = ", ".join("?" for _ in sports)
        counts: dict[str, int] = {}
        for table, ts_col in (
            ("value_bets", "flagged_at"),
        ):
            predicate = (
                f"sport IN ({placeholders}) AND excluded_reason IS NULL "
                f"AND {ts_col} >= ? AND {ts_col} < ?"
            )
            params = [*sports, from_iso, until_iso]
            if dry_run:
                cursor = await self._db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {predicate}", params
                )
                counts[table] = (await cursor.fetchone())[0]
            else:
                cursor = await self._db.execute(
                    f"UPDATE {table} SET excluded_reason = ? WHERE {predicate}",
                    [reason, *params],
                )
                counts[table] = cursor.rowcount
        if not dry_run:
            await self._db.commit()
        return counts

    async def get_final_games_with_incomplete_bets(self) -> list[Game]:
        """Return final games whose value_bets are missing CLV or result.

        Used at startup to backfill bets stranded outside the scheduler's
        3-day refresh window — once a game ages past yesterday/today/tomorrow,
        the normal `_recover_finalized_game` path can no longer see it, so
        any bet missing CLV (P1 closing-snapshot bug) or result (pipeline
        downtime during the final transition) stays stuck.
        """
        cursor = await self._db.execute(
            """SELECT DISTINCT g.* FROM games g
               JOIN value_bets vb ON vb.game_id = g.id
               WHERE g.status='final'
                 AND (vb.clv_pct IS NULL OR vb.result IS NULL)
               ORDER BY g.scheduled_at"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_game(row) for row in rows]

    async def get_final_games_without_snapshots(self) -> list[Game]:
        """Return final games that have zero rows in odds_snapshots.

        Used by the startup audit to surface days the pipeline was offline —
        otherwise a missed gameday is invisible until the user manually
        queries the DB.
        """
        cursor = await self._db.execute(
            """SELECT g.* FROM games g
               WHERE g.status='final'
                 AND NOT EXISTS (
                     SELECT 1 FROM odds_snapshots s WHERE s.game_id = g.id
                 )
               ORDER BY g.scheduled_at"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_game(row) for row in rows]

    async def get_handled_live_game_ids(self) -> set[int]:
        """Return game IDs that already have closing snapshots marked.

        Used on startup to rebuild _handled_live without a separate table.
        A game with is_closing=1 snapshots has already been through
        _handle_game_live() — no need to re-run.
        """
        cursor = await self._db.execute(
            "SELECT DISTINCT game_id FROM odds_snapshots WHERE is_closing = 1"
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def get_pending_value_bets(self, game_id: int) -> list[ValueBet]:
        cursor = await self._db.execute(
            "SELECT * FROM value_bets WHERE game_id=? AND result IS NULL", (game_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_value_bet(row) for row in rows]

    async def get_value_bets_without_clv(self, game_id: int) -> list[ValueBet]:
        """Return value_bets for a game whose CLV has not been computed,
        regardless of whether `result` is set.

        Used by recovery passes to backfill CLV on bets where the closing
        snapshot was missing at initial resolution time. `get_pending_value_bets`
        filters `result IS NULL` and so cannot revisit a bet whose result was
        set without CLV — that path is for resolution, not CLV repair.
        """
        cursor = await self._db.execute(
            "SELECT * FROM value_bets WHERE game_id=? AND clv_pct IS NULL", (game_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_value_bet(row) for row in rows]

    async def get_stranded_games(
        self, now: datetime, *, min_age_hours: float
    ) -> list[Game]:
        """Games still non-final/non-postponed more than ``min_age_hours`` past
        tipoff — i.e. stuck. The Odds API /scores window is only 3 days, so a game
        that finalized during a longer outage can never be auto-settled and would
        otherwise sit 'live'/'scheduled' forever. Surfaced at startup as a warning
        (read-only; this does not change status). ``min_age_hours`` should exceed
        every sport's live backstop (max 4h) so genuinely live games aren't flagged.
        """
        cutoff = (now - timedelta(hours=min_age_hours)).isoformat()
        cursor = await self._db.execute(
            """SELECT * FROM games
               WHERE status NOT IN ('final', 'postponed')
                 AND COALESCE(commence_time, scheduled_at) < ?
               ORDER BY COALESCE(commence_time, scheduled_at)""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_game(row) for row in rows]

    async def delete_games_with_children(
        self, game_ids: list[int], *, dry_run: bool = False
    ) -> dict[str, int]:
        """Delete games and every row referencing them — children first
        (value_bets, odds_snapshots reference games via FK), then the games
        themselves, all in one transaction so a mid-way failure can't strand
        orphan rows or half-deleted games. Returns per-table row counts; with
        ``dry_run`` the counts are computed and nothing is written. One-shot
        operator action behind ``oddsiq delete-zombie-games``.
        """
        counts = {
            "games": 0, "odds_snapshots": 0, "value_bets": 0,
            "placed_bets": 0,
        }
        if not game_ids:
            return counts
        placeholders = ", ".join("?" for _ in game_ids)
        # Children before parent: FK order for the real delete, and a stable
        # report order either way. placed_bets references BOTH value_bets and
        # games, so it must go before value_bets.
        for table, col in (
            ("placed_bets", "game_id"),
            ("value_bets", "game_id"),
            ("odds_snapshots", "game_id"),
            ("games", "id"),
        ):
            if dry_run:
                cursor = await self._db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({placeholders})",
                    game_ids,
                )
                counts[table] = (await cursor.fetchone())[0]
            else:
                cursor = await self._db.execute(
                    f"DELETE FROM {table} WHERE {col} IN ({placeholders})",
                    game_ids,
                )
                counts[table] = cursor.rowcount
        if not dry_run:
            await self._db.commit()
        return counts

    async def insert_placed_bet(self, pb: "PlacedBet") -> int | None:
        """Insert a betting decision. Returns the row id, or None when a
        decision for this value bet already exists (schema UNIQUE — the
        double-bet guard; Phase 1 has no retries)."""
        cursor = await self._db.execute(
            """INSERT INTO placed_bets
               (value_bet_id, game_id, sport, status, skip_reason, stake_czk,
                requested_odds, min_odds, accepted_odds, receipt_id, error,
                balance_after_czk, commanded_at, resolved_at,
                latency_detect_ms, latency_place_ms, settled_pl_czk, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(value_bet_id) DO NOTHING
               RETURNING id""",
            (pb.value_bet_id, pb.game_id, pb.sport, pb.status, pb.skip_reason,
             pb.stake_czk, pb.requested_odds, pb.min_odds, pb.accepted_odds,
             pb.receipt_id, pb.error, pb.balance_after_czk,
             pb.commanded_at.isoformat() if pb.commanded_at else None,
             pb.resolved_at.isoformat() if pb.resolved_at else None,
             pb.latency_detect_ms, pb.latency_place_ms, pb.settled_pl_czk,
             pb.created_at.isoformat()),
        )
        row = await cursor.fetchone()
        await self._db.commit()
        return row[0] if row else None

    async def update_placed_bet_result(
        self, pb_id: int, *, status: str,
        accepted_odds: float | None = None, receipt_id: str | None = None,
        balance_after_czk: float | None = None, error: str | None = None,
        latency_place_ms: int | None = None,
    ) -> None:
        """Record the executor's outcome for a pending decision; stamps
        resolved_at with the transition time."""
        await self._db.execute(
            """UPDATE placed_bets SET status=?, accepted_odds=?, receipt_id=?,
               balance_after_czk=?, error=?, latency_place_ms=?, resolved_at=?
               WHERE id=?""",
            (status, accepted_odds, receipt_id, balance_after_czk, error,
             latency_place_ms, datetime.now(timezone.utc).isoformat(), pb_id),
        )
        await self._db.commit()

    async def get_placed_bet_for_value_bet(self, value_bet_id: int) -> "PlacedBet | None":
        cursor = await self._db.execute(
            "SELECT * FROM placed_bets WHERE value_bet_id=?", (value_bet_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_placed_bet(row) if row else None

    async def get_placed_bet(self, pb_id: int) -> "PlacedBet | None":
        cursor = await self._db.execute(
            "SELECT * FROM placed_bets WHERE id=?", (pb_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_placed_bet(row) if row else None

    async def reconcile_placed_bet(
        self,
        pb_id: int,
        *,
        status: str,
        accepted_odds: float | None = None,
        receipt_id: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Resolve a crash/timeout ambiguity after checking Tipsport history.

        Only ``pending``/``unknown`` rows may be reconciled. Confirming a
        placement requires both receipt id and valid accepted odds; this tool
        never guesses and never retries the wager.
        """
        if status not in {"placed", "rejected"}:
            raise ValueError("reconciliation status must be placed or rejected")
        if status == "placed":
            if (
                accepted_odds is None
                or not math.isfinite(accepted_odds)
                or accepted_odds <= 1.0
            ):
                raise ValueError("placed reconciliation requires accepted_odds > 1")
            if not receipt_id or not receipt_id.strip():
                raise ValueError("placed reconciliation requires a receipt id")
        elif not reason or not reason.strip():
            raise ValueError("rejected reconciliation requires a reason")
        lookup = await self._db.execute(
            "SELECT status, value_bet_id FROM placed_bets WHERE id=?",
            (pb_id,),
        )
        existing = await lookup.fetchone()
        if existing is None or existing["status"] not in {"pending", "unknown"}:
            return False
        # APPEND the operator's finding to `error`, never overwrite it: a
        # pending/unknown row's error holds the original placement evidence
        # (the timeout/crash text) and that is half the money audit trail.
        # A blank/whitespace reason must not blank the audit note (the placed
        # path accepts an optional reason) — fall back to the default marker.
        note = (reason or "").strip() or f"operator_reconciled_{status}"
        cursor = await self._db.execute(
            """UPDATE placed_bets
               SET status=?, accepted_odds=?, receipt_id=?,
                   error=CASE WHEN error IS NULL OR error='' THEN ?
                              ELSE error || '; ' || ? END,
                   resolved_at=?
               WHERE id=? AND status IN ('pending','unknown')""",
            (
                status,
                accepted_odds if status == "placed" else None,
                receipt_id.strip() if status == "placed" and receipt_id else None,
                note,
                note,
                datetime.now(timezone.utc).isoformat(),
                pb_id,
            ),
        )
        await self._db.commit()
        changed = cursor.rowcount == 1
        if changed and status == "placed":
            # The game may have finalized while the decision was ambiguous.
            # Settle immediately; normal recovery only revisits unresolved
            # value bets and would otherwise strand this newly confirmed bet.
            result_cursor = await self._db.execute(
                "SELECT result FROM value_bets WHERE id=?",
                (existing["value_bet_id"],),
            )
            current = await result_cursor.fetchone()
            if current is not None and current["result"] is not None:
                await self.settle_placed_bet(
                    existing["value_bet_id"], current["result"],
                )
        return changed

    async def has_open_decision_for_selection(self, game_id: int, selection: str) -> bool:
        """True when a decision that may carry money (pending/placed/unknown/
        settled) already exists for this game+selection — the CROSS-TIER
        double-bet guard (2026-08-30, shipped with warm-tier betting): warm and
        hot detections of one selection are separate value_bets rows (tier is
        in the dedup key), so the schema's value_bet_id UNIQUE cannot see the
        pair. rejected/failed/skipped do NOT block — a fresh detection is a new
        opportunity, not a blind retry of the same command."""
        cursor = await self._db.execute(
            "SELECT 1 FROM placed_bets pb JOIN value_bets vb ON vb.id = pb.value_bet_id "
            "WHERE pb.game_id = ? AND vb.selection = ? "
            "AND pb.status IN ('pending','placed','unknown','settled') LIMIT 1",
            (game_id, selection),
        )
        return await cursor.fetchone() is not None

    async def settle_placed_bet(self, value_bet_id: int, result: str) -> None:
        """Settle the `placed` decision for a resolved value bet: win pays
        stake*(accepted_odds-1), loss costs the stake, push/draw returns it.
        Only `placed` rows transition; every other status is terminal.
        resolved_at is re-stamped so the daily-loss cap can day-scope
        realized P&L by settlement time."""
        cursor = await self._db.execute(
            "SELECT id, stake_czk, requested_odds, accepted_odds FROM placed_bets "
            "WHERE value_bet_id=? AND status='placed'", (value_bet_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        stake = float(row["stake_czk"] or 0.0)
        accepted = _valid_decimal_odds(row["accepted_odds"])
        requested = _valid_decimal_odds(row["requested_odds"])
        settlement_odds = accepted if accepted is not None else requested
        used_requested = result == "win" and accepted is None
        if result == "win":
            if settlement_odds is None:
                logger.error(
                    "placed_bet_settlement_blocked: value_bet_id=%d reason=no_valid_odds",
                    value_bet_id,
                )
                return
            pl = stake * (settlement_odds - 1.0)
        elif result == "loss":
            pl = -stake
        else:  # push / draw returns the stake
            pl = 0.0
        await self._db.execute(
            """UPDATE placed_bets SET status='settled', settled_pl_czk=?, resolved_at=?,
               error=CASE WHEN ? THEN
                   CASE WHEN error IS NULL OR error='' THEN 'settled_using_requested_odds'
                        ELSE error || '; settled_using_requested_odds' END
                   ELSE error END
               WHERE id=?""",
            (pl, datetime.now(timezone.utc).isoformat(), used_requested, row["id"]),
        )
        await self._db.commit()

    async def cumulative_realized_pl_czk(self) -> float:
        """All-time realized P&L (Kč) across settled placed bets — the running
        total shown on the win/loss Telegram."""
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(settled_pl_czk), 0.0) AS total "
            "FROM placed_bets WHERE status='settled'",
        )
        r = await cursor.fetchone()
        return float(r["total"] or 0.0)

    async def betting_day_stats(self, day_iso: str) -> dict[str, float]:
        """Aggregates the betting caps consume: `bets_commanded` (non-skipped
        decisions created on `day_iso`), `open_stakes_czk` (money currently at
        risk — pending/placed/unknown, ALL-TIME not day-scoped), and
        `realized_pl_czk` (settled P&L whose settlement fell on `day_iso`)."""
        cursor = await self._db.execute(
            "SELECT "
            "SUM(CASE WHEN status != 'skipped' AND date(created_at) = ? THEN 1 ELSE 0 END) AS commanded, "
            "SUM(CASE WHEN status IN ('pending','placed','unknown') THEN stake_czk ELSE 0 END) AS open_stakes, "
            "SUM(CASE WHEN status = 'settled' AND date(resolved_at) = ? THEN settled_pl_czk ELSE 0 END) AS realized "
            "FROM placed_bets",
            (day_iso, day_iso),
        )
        r = await cursor.fetchone()
        return {
            "bets_commanded": int(r["commanded"] or 0),
            "open_stakes_czk": float(r["open_stakes"] or 0.0),
            "realized_pl_czk": float(r["realized"] or 0.0),
        }

    @staticmethod
    def _row_to_placed_bet(row: aiosqlite.Row) -> "PlacedBet":
        return PlacedBet(
            id=row["id"], value_bet_id=row["value_bet_id"], game_id=row["game_id"],
            sport=row["sport"], status=row["status"], skip_reason=row["skip_reason"],
            stake_czk=row["stake_czk"], requested_odds=row["requested_odds"],
            min_odds=row["min_odds"], accepted_odds=row["accepted_odds"],
            receipt_id=row["receipt_id"], error=row["error"],
            balance_after_czk=row["balance_after_czk"],
            commanded_at=datetime.fromisoformat(row["commanded_at"]) if row["commanded_at"] else None,
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            latency_detect_ms=row["latency_detect_ms"],
            latency_place_ms=row["latency_place_ms"],
            settled_pl_czk=row["settled_pl_czk"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def get_meta(self, key: str) -> str | None:
        cursor = await self._db.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._db.execute(
            """INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        await self._db.commit()

    @staticmethod
    def _row_to_game(row: aiosqlite.Row) -> Game:
        return Game(
            id=row["id"], external_id=row["external_id"],
            home_team=row["home_team"], away_team=row["away_team"],
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            commence_time=(
                datetime.fromisoformat(row["commence_time"])
                if row["commence_time"] is not None
                else None
            ),
            status=row["status"], home_score=row["home_score"], away_score=row["away_score"],
            sport=row["sport"],
        )

    @staticmethod
    def _row_to_snapshot(row: aiosqlite.Row) -> OddsSnapshot:
        return OddsSnapshot(
            id=row["id"], game_id=row["game_id"], bookmaker=row["bookmaker"],
            market_type=row["market_type"], selection=row["selection"],
            line=row["line"], odds=row["odds"], is_closing=bool(row["is_closing"]),
            raw_response_path=row["raw_response_path"],
            source_updated_at=(
                datetime.fromisoformat(row["source_updated_at"])
                if row["source_updated_at"] else None
            ),
            captured_at=datetime.fromisoformat(row["captured_at"]),
            sport=row["sport"],
        )

    @staticmethod
    def _row_to_value_bet(row: aiosqlite.Row) -> ValueBet:
        return ValueBet(
            id=row["id"], game_id=row["game_id"], bookmaker=row["bookmaker"],
            market_type=row["market_type"], selection=row["selection"],
            line=row["line"], entry_odds=row["entry_odds"], fair_odds=row["fair_odds"],
            edge_pct=row["edge_pct"], closing_odds=row["closing_odds"],
            pinnacle_closing=row["pinnacle_closing"], clv_pct=row["clv_pct"],
            result=row["result"], stake=row["stake"],
            tier=row["tier"],
            sport=row["sport"],
            excluded_reason=row["excluded_reason"],
            tipsport_match_id=row["tipsport_match_id"],
            tipsport_opp_id=row["tipsport_opp_id"],
            tipsport_url=row["tipsport_url"],
            sharp_source_updated_at=(
                datetime.fromisoformat(row["sharp_source_updated_at"])
                if row["sharp_source_updated_at"] else None
            ),
            flagged_at=datetime.fromisoformat(row["flagged_at"]),
        )
