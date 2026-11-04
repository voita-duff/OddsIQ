#!/usr/bin/env python3
"""Mechanical drift checks for the OddsIQ vault.

Run from repo root:

    make audit                       # uses the venv interpreter automatically
    .venv/bin/python scripts/docs-audit.py

The test-count check shells out to `pytest --collect-only`, so it must run
under an interpreter that has pytest installed (the project venv). A bare
system `python3` without pytest will fail check #2. `make audit` handles
this by preferring `.venv/bin/python`.

Exits non-zero if any check finds issues. Designed to be run manually
between work sessions and (eventually) wired into CI.

Checks:
  1. Wikilink resolution — every `[[X]]` link points to an existing .md
     file in docs/ (excluding code blocks). Section anchors and pipe
     aliases are normalized away before the lookup.
  2. Test counts — docs/Testing/Test Suite Overview.md
     match `pytest --collect-only -q`, including parametrized tests.
  3. Config coverage — every constant in oddsiq/config.py is mentioned
     by name in docs/Production/Config Constants.md.
  4. Migration list — docs/Components/Migrations.md
     mention every migration file present in oddsiq/db/migrations/.
  5. Banned phrases — patterns that have surfaced as drift in past
     audits (stale test counts, deleted-file references, dead wikilinks,
     pre-migration bootstrap claims, etc.).
  6. Deleted identifiers — names deleted from oddsiq/ that keep resurfacing
     in prose (DELETED_IDENTIFIERS, matched word-bounded against the RAW
     text so backticked mentions are caught), scanned across the active
     docs plus the root deploy runbook. A narrow
     per-file/per-identifier exemption list covers retirement records, and
     every exemption is itself checked for staleness: once the file stops
     naming the identifier — or the carve-out points at a file the sweep
     never reads, or at a name no longer in DELETED_IDENTIFIERS — it fails
     the audit until deleted.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIG_PY = ROOT / "oddsiq" / "config.py"
MIGRATIONS_DIR = ROOT / "oddsiq" / "db" / "migrations"
TESTS_DIR = ROOT / "tests"

# Folders inside docs/ that are historical/frozen or contain placeholder
# scaffolding, and are not subject to drift checks.
SKIP_DIRS = {"Design"}


def collect_active_md(root: Path) -> list[Path]:
    out = []
    for f in root.rglob("*.md"):
        if any(p in SKIP_DIRS for p in f.parts):
            continue
        out.append(f)
    return out


def strip_code(text: str) -> str:
    """Remove fenced code blocks and inline `code` so wikilink-syntax examples
    inside code don't trigger false positives."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", "", text)
    return text


# --- 1. Wikilinks -----------------------------------------------------------

# Match Obsidian wikilinks. Pipe alias accepts both `|` (Obsidian default)
# and `\|` (escaped form used inside markdown tables, where `|` is the
# column delimiter). Section anchors (`#X`) are normalized away.
WIKILINK_RE = re.compile(
    r"\[\[(?P<target>[^\]|#\\]+)(?:#[^\]|\\]+)?(?:\\?\|[^\]]+)?\]\]"
)


def check_wikilinks(active: list[Path]) -> list[str]:
    issues: list[str] = []
    # Lookup pool is every .md file under docs/ (including Templates and
    # Design/), so wikilinks to historical specs still resolve.
    valid = {f.stem for f in DOCS.rglob("*.md")}
    for f in active:
        text = strip_code(f.read_text())
        for m in WIKILINK_RE.finditer(text):
            target = m.group("target").strip()
            if not target:
                continue
            if target in valid:
                continue
            line_no = text[: m.start()].count("\n") + 1
            issues.append(f"{f.relative_to(ROOT)}:{line_no}: dangling [[{target}]]")
    return issues


# --- 2. Test counts ---------------------------------------------------------


def count_tests() -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in sorted(TESTS_DIR.glob("test_*.py")):
        counts[f.name] = 0

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"pytest collection failed during docs audit:\n{details}")

    for line in result.stdout.splitlines():
        m = re.match(r"^tests/(test_[^:]+\.py)::", line)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def check_test_counts(_active: list[Path]) -> list[str]:
    issues: list[str] = []
    actual = count_tests()
    total = sum(actual.values())
    file_count = len(actual)

    overview_path = DOCS / "Testing" / "Test Suite Overview.md"
    if overview_path.exists():
        overview = overview_path.read_text()
        m = re.search(r"(\d+) tests across (\d+) test files", overview)
        if m:
            if int(m.group(1)) != total:
                issues.append(
                    f"Test Suite Overview.md: claims {m.group(1)} tests, actual is {total}"
                )
            if int(m.group(2)) != file_count:
                issues.append(
                    f"Test Suite Overview.md: claims {m.group(2)} test files, actual is {file_count}"
                )
        for filename, n in actual.items():
            row = re.compile(rf"\| `{re.escape(filename)}` \| (\d+) \|")
            mr = row.search(overview)
            if mr and int(mr.group(1)) != n:
                issues.append(
                    f"Test Suite Overview.md: {filename} listed as {mr.group(1)}, actual is {n}"
                )
    return issues


# --- 3. Config coverage -----------------------------------------------------


def collect_config_constants() -> list[str]:
    out: list[str] = []
    for line in CONFIG_PY.read_text().splitlines():
        m = re.match(r"^([A-Z_][A-Z_0-9]*)\s*:\s*", line)
        if m:
            out.append(m.group(1))
    return out


def check_config_coverage(_active: list[Path]) -> list[str]:
    issues: list[str] = []
    cc_path = DOCS / "Production" / "Config Constants.md"
    if not cc_path.exists():
        return [f"{cc_path.relative_to(ROOT)} is missing"]
    cc = cc_path.read_text()
    for const in collect_config_constants():
        if f"`{const}`" not in cc:
            issues.append(
                f"Config Constants.md: missing entry for `{const}` (defined in oddsiq/config.py)"
            )
    return issues


# --- 4. Migration list ------------------------------------------------------


def collect_migrations() -> list[str]:
    return sorted(f.name for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.py"))


def check_migration_list(_active: list[Path]) -> list[str]:
    issues: list[str] = []
    actual = collect_migrations()
    targets = [
        ("Migrations.md", DOCS / "Components" / "Migrations.md"),
    ]
    for label, path in targets:
        if not path.exists():
            continue
        text = path.read_text()
        for mig in actual:
            if mig not in text:
                issues.append(f"{label}: missing migration `{mig}` in description")
    return issues


# --- 5. Banned phrases ------------------------------------------------------

# Each entry: (compiled regex, human explanation). Regexes match within
# the post-strip-code text so wikilink-syntax examples in code blocks
# don't trigger.
BANNED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Stale test counts
    (re.compile(r"~166 tests"), "stale test count (~166)"),
    (re.compile(r"\b138 tests\b"), "stale test count (138)"),
    (
        re.compile(r"\b14 test files\b"),
        "stale file count (14; run pytest --collect-only -q for the current number)",
    ),
    # Tipsport per-game wording (actually per-cycle)
    (re.compile(r"3 POSTs? per game"), "Tipsport is per-cycle, not per-game"),
    (re.compile(r"POST requests? per game"), "Tipsport is per-cycle, not per-game"),
    # Dead wikilinks (notes that were never written)
    (
        re.compile(r"\[\[Decision Framework\]\]"),
        "Decision Framework note doesn't exist; use HANDBOOK § CLV gating",
    ),
    (
        re.compile(r"\[\[Bankroll Management\]\]"),
        "Bankroll Management note doesn't exist",
    ),
    (
        re.compile(r"\[\[Open Questions(?:#[^\]]*)?\]\]"),
        "Open Questions note doesn't exist",
    ),
    # Deleted top-level docs
    (re.compile(r"docs/ROADMAP\.md"), "docs/ROADMAP.md was deleted"),
    (re.compile(r"docs/VALIDATION\.md"), "docs/VALIDATION.md was deleted"),
    # ForFuture rename
    (
        re.compile(r"docs/ForFuture - 1\. Markets\.md"),
        "renamed to docs/Decisions/Markets.md",
    ),
    # Pre-migration bootstrap claims
    (
        re.compile(r"CREATE TABLE IF NOT EXISTS for all tables"),
        "tables come from migrations now",
    ),
    # Basketball matchView (corrected 2026-06-12): only MLB uses WHOLE_MATCH
    (
        re.compile(r"NBA and MLB use `?WINNER_WHOLE_MATCH"),
        "NBA/WNBA use WINNER_FINAL_DECISION since 2026-06-12; only MLB rides WHOLE_MATCH",
    ),
    # Old shutdown terminology
    (re.compile(r"_running\s*=\s*False"), "shutdown uses scheduler.stop() now"),
    # Legacy 200-bet manual gate (specific framing)
    (
        re.compile(r"200 manually placed bets with proven positive CLV"),
        "legacy 200-bet gate is dead under evaluation mode",
    ),
    # Tier 2/3 hidden internals — `_opposite()` wrapper that doesn't exist
    (
        re.compile(r"\b_opposite\(\)"),
        "function is `_opposite_selection()` not `_opposite()`",
    ),
    # Multi-book + post-Phase-1 drift (added by the 2026-05-31 vault refresh)
    (re.compile(r"[Tt]hree migrations"), "seven migrations have shipped (000-006)"),
    (
        re.compile(r"only runs during the hot tier"),
        "edge detection runs in warm AND hot tiers (<=2h)",
    ),
    (
        re.compile(r"\bthree tables\b"),
        "DB has 6 tables (games, odds_snapshots, value_bets, placed_bets, meta, schema_version)",
    ),
    # ("eight subcommand" ban removed 2026-07-10: delete-zombie-games made
    # eight named subcommands true again)
    (
        re.compile(r"\b(six|seven) (named )?subcommands\b"),
        "CLI has 8 named subcommands since delete-zombie-games (2026-07-10)",
    ),
    (
        re.compile(r"Once, on entry"),
        "warm-tier Pinnacle is polled every cycle, not once-on-entry",
    ),
    (re.compile(r"\./raw_responses"), "RAW_DATA_DIR default is ./data/raw"),
    (re.compile(r"Desktop/OddsIQ"), "repo root is ~/dev/OddsIQ, not ~/Desktop/OddsIQ"),
]


def check_banned_phrases(active: list[Path]) -> list[str]:
    issues: list[str] = []
    for f in active:
        text = f.read_text()
        stripped = strip_code(text)
        for pattern, msg in BANNED_PATTERNS:
            for m in pattern.finditer(stripped):
                # Re-locate in the original text to get an accurate line number.
                # `stripped` removes content; finding the same match in the
                # original is best-effort but sufficient.
                m_orig = pattern.search(text)
                if m_orig is None:
                    continue
                line_no = text[: m_orig.start()].count("\n") + 1
                snippet = m_orig.group()
                issues.append(f"{f.relative_to(ROOT)}:{line_no}: '{snippet}' — {msg}")
                break  # one report per pattern per file
    return issues


# --- 6. Deleted identifiers -------------------------------------------------

# Identifiers deleted from oddsiq/ that keep resurfacing in prose. Unlike the
# banned patterns above these are matched against the RAW text: the vault writes
# identifiers in backticks, which strip_code() would hide — and a backticked
# mention is exactly the drift worth catching. The match is word-bounded, so a
# longer name that merely starts with a dead one does NOT fire (e.g.
# `TELEGRAM_HEARTBEAT_ENABLED_X` and `format_clv_update` are not flagged by
# `TELEGRAM_HEARTBEAT_ENABLED` / `format_clv`) — add such a name explicitly if
# it is also deleted.
DELETED_IDENTIFIERS: list[tuple[str, str]] = [
    # 2026-08-10 Telegram rebrand: the phone carries real-money events only.
    ("format_value_bet", "deleted 2026-08-10; detections are log-only (`value_bet:`)"),
    ("format_clv", "deleted 2026-08-10; CLV is log-only (`clv:`) + dashboard"),
    (
        "format_session_summary",
        "deleted 2026-08-10; the summary is console-only via Scheduler.print_summary()",
    ),
    ("format_coverage_gap", "deleted 2026-08-10; coverage gaps are a WARNING log"),
    (
        "format_health_ping",
        "deleted 2026-08-10; liveness is the HEARTBEAT_PING_URL dead-man ping",
    ),
    # Deleted 2026-08-10 with the rebrand (e5270bd), ahead of the 2026-08-27
    # removal of the live-edge subsystem it reported on.
    (
        "format_live_edge",
        "deleted 2026-08-10; the live-edge subsystem it alerted for was removed 2026-08-27",
    ),
    (
        "TELEGRAM_HEARTBEAT_ENABLED",
        "removed 2026-08-10; superseded by the HEARTBEAT_PING_URL dead-man ping",
    ),
    (
        "TELEGRAM_HEARTBEAT_INTERVAL_MINUTES",
        "removed 2026-08-10 with the Telegram health ping; the dead-man ping rides SCHEDULER_HEARTBEAT_SECONDS",
    ),
    # 2026-08-27 live-edge removal: migration 011 dropped live_edge_candidates.
    (
        "resolve_live_candidate_results",
        "deleted 2026-08-27 with the live-edge subsystem; only resolve_bet_result() survives",
    ),
]

# Root-level files scanned for deleted identifiers on top of the active docs
# set. DEPLOY.md (VM runbook) names config keys and store functions often
# enough to drift, and is not covered by the docs/ sweep.
DELETED_IDENTIFIER_EXTRA_FILES: tuple[str, ...] = ("DEPLOY.md",)

# Files allowed to name a deleted identifier because they document its
# retirement. Repo-relative path -> the identifiers that file may mention.
# Keep these narrow: an exemption is per-file AND per-identifier, so a
# retirement record can't quietly re-authorize the whole set. Every entry is
# self-liquidating — see check_stale_exemptions().
DELETED_IDENTIFIER_EXEMPTIONS: dict[str, set[str]] = {
    # The canonical page's authoritative "any reference to X is historical" clause.
    "docs/Components/Telegram Notifications.md": {
        "format_value_bet",
        "format_clv",
        "format_session_summary",
        "format_coverage_gap",
    },
    # The roadmap records the flag's ship-then-delete history by name.
    "docs/Roadmap/P4 - Resilience and Deployment.md": {"TELEGRAM_HEARTBEAT_ENABLED"},
    # Historical post-mortem: the fix record for the moneyline-tie bug names the
    # store function that carried the draw branch. The subsystem died 2026-08-27
    # but the post-mortem is a dated record of what was fixed, not a live claim —
    # post-mortems are never rewritten.
    "docs/Known Issues/Fixed - Moneyline Tie Not Handled.md": {
        "resolve_live_candidate_results"
    },
}


def _mentions(text: str, name: str) -> re.Match[str] | None:
    """Word-bounded search in RAW text (so backticked mentions are caught)."""
    return re.search(rf"\b{re.escape(name)}\b", text)


def deleted_identifier_scan_set(active: list[Path]) -> list[Path]:
    scan = list(active)
    seen = {p.resolve() for p in scan}
    for name in DELETED_IDENTIFIER_EXTRA_FILES:
        p = ROOT / name
        if p.exists() and p.resolve() not in seen:
            scan.append(p)
            seen.add(p.resolve())
    return scan


def check_stale_exemptions(active: list[Path]) -> list[str]:
    """Fail once a carve-out's debt is paid — or was never owed.

    An exemption is a hole in the guard, so it must not outlive the mention it
    was opened for: when the file stops naming the identifier (or the file is
    gone), the entry is reported until someone deletes it. Two further shapes
    are dead on arrival and reported the same way:

      * a target outside the deleted-identifier scan set (anything under
        docs/Design/, say) — the sweep never reads that
        file, so the carve-out protects nothing and can only mislead;
      * a name that is no longer in DELETED_IDENTIFIERS — there is nothing left
        to be exempt from.

    Reported against scripts/docs-audit.py, not the doc: this file is the one
    the reader has to edit.
    """
    issues: list[str] = []
    scanned = {
        p.relative_to(ROOT).as_posix() for p in deleted_identifier_scan_set(active)
    }
    known = {name for name, _ in DELETED_IDENTIFIERS}
    for rel, names in sorted(DELETED_IDENTIFIER_EXEMPTIONS.items()):
        path = ROOT / rel
        if not path.exists():
            issues.append(
                f"scripts/docs-audit.py: exemption target {rel} no longer exists — "
                f"delete its DELETED_IDENTIFIER_EXEMPTIONS entry"
            )
            continue
        if rel not in scanned:
            issues.append(
                f"scripts/docs-audit.py: exemption target {rel} is not in the scan set "
                f"— delete its entry"
            )
            continue
        text = path.read_text()
        for name in sorted(names):
            if name not in known:
                issues.append(
                    f"scripts/docs-audit.py: exemption for {name} in {rel} names an "
                    f"identifier that is not in DELETED_IDENTIFIERS — delete it"
                )
                continue
            if _mentions(text, name) is None:
                issues.append(
                    f"scripts/docs-audit.py: exemption for {name} in {rel} is no longer "
                    f"needed — delete it"
                )
    return issues


def check_deleted_identifiers(active: list[Path]) -> list[str]:
    issues: list[str] = []
    for f in deleted_identifier_scan_set(active):
        rel = f.relative_to(ROOT).as_posix()
        exempt = DELETED_IDENTIFIER_EXEMPTIONS.get(rel, frozenset())
        text = f.read_text()
        for name, msg in DELETED_IDENTIFIERS:
            if name in exempt:
                continue
            m = _mentions(text, name)
            if m is None:
                continue
            line_no = text[: m.start()].count("\n") + 1
            issues.append(
                f"{rel}:{line_no}: '{name}' no longer exists in oddsiq/ — {msg}"
            )
    issues.extend(check_stale_exemptions(active))
    return issues


# --- Driver -----------------------------------------------------------------


def main() -> int:
    active = collect_active_md(DOCS)
    for top in ("README.md", "HANDBOOK.md"):
        p = ROOT / top
        if p.exists():
            active.append(p)

    # The deleted-identifier sweep reads a couple of root files on top of the
    # active docs, so the banner would otherwise undercount what was scanned.
    extra = len(deleted_identifier_scan_set(active)) - len(active)
    extra_note = (
        f" (+ {extra} root file{'s' if extra != 1 else ''} for deleted identifiers)"
        if extra
        else ""
    )
    print(
        f"Auditing {len(active)} active docs (excluding {sorted(SKIP_DIRS)}){extra_note}...\n"
    )

    sections: list[tuple[str, Callable[[list[Path]], list[str]]]] = [
        ("Wikilink resolution", check_wikilinks),
        ("Test counts", check_test_counts),
        ("Config coverage", check_config_coverage),
        ("Migration list", check_migration_list),
        ("Banned phrases", check_banned_phrases),
        ("Deleted identifiers", check_deleted_identifiers),
    ]

    total_issues = 0
    for label, func in sections:
        issues = func(active)
        print(f"### {label}")
        if not issues:
            print("  clean")
        else:
            for i in issues:
                print(f"  {i}")
        print()
        total_issues += len(issues)

    if total_issues:
        print(f"Found {total_issues} issue(s).")
        return 1
    print("All checks clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
