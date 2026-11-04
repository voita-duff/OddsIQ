# Zombie-Game Deletion (`oddsiq delete-zombie-games`) — Design (2026-07-10)

> **Scope note:** the first iteration of this design (same date) proposed an
> automatic "abandonment sweep" — a terminal `status='abandoned'` plus
> `excluded_reason` stamping, run at startup and hourly. Review changed
> the approach: **delete the zombie games outright**, as a manual one-shot CLI
> command the operator runs on the VM after deploying. The abandonment
> implementation was reverted (it exists only in git history) and this spec
> records the shipped deletion design.

## Problem

Games that finalize (or get suspended and dropped from the feed) while the
pipeline is down longer than The Odds API's `/scores?daysFrom=3` window can
never be auto-settled: the finals never arrive, so the game sits at
`status='live'` forever. Observed on the 2026-07-07 DB snapshot: 25
permanently stuck games — 20 clustered on 2026-06-19..21 (the ~10-day VM
outage), the rest rain-suspended MLB games that dropped from the feed while
the pipeline was running.

Consequences:

- `value_bets` rows stay `result IS NULL` forever (e.g. id 865, game 77966,
  SF Giants @ Atlanta Braves 2026-06-16) — reporting counts them as pending.
- `live_edge_candidates` rows stay `markout_status='pending'` / `result IS
  NULL` forever — they pollute the markout breakdown and settled denominators.
- The dashboard `/live` status strip counts the stuck games in `games_live`
  indefinitely.
- `SettlementService.recover_historical_bets` only replays `status='final'`
  games, so none of this can self-heal.

## Decision

**Delete, don't annotate.** These games' bets and candidates can never settle;
the operator prefers removing them (and their odds history) outright over
keeping excluded-but-present rows. Delivery is a **manual one-shot CLI
subcommand with `--dry-run`**, matching the `mark-excluded` /
`cleanup-closing-flags` pattern, so the run happens deliberately on the VM
after a deploy — never automatically.

Detection stays automatic and unchanged: the startup stranded-game WARNING
(`Store.get_stranded_games`, >96h past tipoff, non-terminal status) flags
stuck games at every boot.

## Shipped design

- **`oddsiq delete-zombie-games [--grace-days N] [--dry-run]`** (`oddsiq/cli.py`)
  - Selection (`_find_zombie_live_games`): games with `status='live'` whose
    `tipoff + SportProfile.live_backstop_hours + grace_days` has passed.
    Default `--grace-days 3` = the Odds API scores window; past it the final
    can never arrive. Only `live` games are ever selected — old `scheduled`
    strays are left to the stranded WARNING.
  - Logs one `zombie game_id=… external_id=… sport=… away @ home tipoff=…`
    line per selected game plus per-table totals; `[DRY-RUN]` prefix and no
    writes under `--dry-run`.
- **`Store.delete_games_with_children(game_ids, dry_run=False)`**
  (`oddsiq/db/store.py`) — deletes `live_edge_candidates`, `value_bets`,
  `odds_snapshots`, then `games` (children before parent for the FK), all in
  one transaction with a single commit, so a mid-way failure can't strand
  orphans or half-deleted games. Returns per-table counts; `dry_run` computes
  the same counts via `SELECT COUNT(*)` without writing.

## Verification (2026-07-10, against a copy of the production snapshot)

- `--dry-run` listed exactly the 25 stuck-`live` games (June 19–21 outage
  cluster + earlier suspended MLB games) with totals `games=25
  odds_snapshots=1758 value_bets=1 live_edge_candidates=53`, and wrote
  nothing.
- Real run deleted those same counts — including value_bet id 865 (game
  77966, SF Giants @ Atlanta Braves 2026-06-16) — leaving 0 `live` games and
  all `final`/`scheduled` games (with their rows) untouched; re-run deleted 0.
- Post-checks: zero orphaned rows in all three child tables; `PRAGMA
  integrity_check` ok.

## Testing

- `test_store.py`: FK-ordered cascade + per-game scoping; dry-run counts with
  zero writes; empty-list no-op.
- `test_cli.py`: `parse_args` for the subcommand; selection respects per-sport
  backstop, grace window, and `live`-only status; end-to-end command run
  deletes the zombie and spares fresh-live/final games; `--dry-run` writes
  nothing.
