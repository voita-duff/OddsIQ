import importlib

from oddsiq import config


def test_schedule_refresh_cadence_defaults():
    # Active-window cadence is 900s (was 300 until 2026-07: /scores at 5 min
    # cost ~2 credits per fetched sport and made up roughly half the monthly
    # Odds API burn — both the 2026-06 and 2026-07 quotas exhausted early).
    # The full-sweep/idle cadence is 10800s and the finals-check window 900s
    # (both raised 2026-08-08: /scores overhead was still ~55-60% of burn and
    # the August quota was projected to exhaust ~Aug 20).
    assert config.SCHEDULE_REFRESH_INTERVAL == 10800
    assert config.SCHEDULE_GAME_WINDOW_REFRESH == 900
    assert config.SCHEDULE_COMPLETION_CHECK_INTERVAL == 900


def test_schedule_game_window_refresh_env_override(monkeypatch):
    monkeypatch.setenv("SCHEDULE_GAME_WINDOW_REFRESH", "120")
    importlib.reload(config)
    try:
        assert config.SCHEDULE_GAME_WINDOW_REFRESH == 120
    finally:
        importlib.reload(config)  # restore module-level defaults for other tests


def test_schedule_sweep_and_completion_check_env_override(monkeypatch):
    # Non-default values so the override is actually proven.
    monkeypatch.setenv("SCHEDULE_REFRESH_INTERVAL", "7200")
    monkeypatch.setenv("SCHEDULE_COMPLETION_CHECK_INTERVAL", "300")
    importlib.reload(config)
    try:
        assert config.SCHEDULE_REFRESH_INTERVAL == 7200
        assert config.SCHEDULE_COMPLETION_CHECK_INTERVAL == 300
    finally:
        importlib.reload(config)  # restore module-level defaults for other tests


def test_env_flag_accepts_common_truthy_tokens(monkeypatch):
    """The documented convention is '1'/'0', but an operator who types
    'true'/'yes'/'on' must not silently get the feature disabled."""
    for truthy in ("1", "true", "True", "TRUE", " yes ", "on"):
        monkeypatch.setenv("ODDSIQ_TEST_FLAG", truthy)
        assert config._env_flag("ODDSIQ_TEST_FLAG", "0") is True, truthy
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("ODDSIQ_TEST_FLAG", falsy)
        assert config._env_flag("ODDSIQ_TEST_FLAG", "1") is False, falsy
    monkeypatch.delenv("ODDSIQ_TEST_FLAG", raising=False)
    assert config._env_flag("ODDSIQ_TEST_FLAG", "1") is True
    assert config._env_flag("ODDSIQ_TEST_FLAG", "0") is False


def test_placement_timeout_exceeds_extension_enforced_budgets():
    """Cross-language contract: the bridge's placement timeout must exceed the
    extension's enforced success-path caps, or a slow real placement is
    classified unknown and auto-disarms the engine on a bet that landed.
    All five caps are READ OUT of background.js — the three named constants
    (ODD_WAIT_BUDGET_MS moved 12s -> 20s in 0.6.0) plus the two inline
    literals in runPlacement (the slip wait, anchored on slipHasOpp so a
    future second waitFor() call site can't be picked up instead, and the
    ticket-page wait). Retyping them here as Python literals, as this test
    once did, let the JS drift past a stale copy without failing."""
    import re
    from pathlib import Path
    js = Path(__file__).resolve().parents[1].joinpath("extension", "background.js").read_text()
    nav_ms = int(re.search(r"^const NAV_COMPLETE_CAP_MS = (\d+);", js, re.M).group(1))
    odd_ms = int(re.search(r"^const ODD_WAIT_BUDGET_MS = (\d+);", js, re.M).group(1))
    stake_ms = int(re.search(r"^const STAKE_VERIFY_BUDGET_MS = (\d+);", js, re.M).group(1))
    slip_ms = int(re.search(r"waitFor\(tabId,[^;]*slipHasOpp[^;]*?,\s*(\d+)\)", js).group(1))
    ticket_ms = int(re.search(r"waitForTicketPage\(tabId,\s*(\d+)\)", js).group(1))
    enforced_s = (nav_ms + odd_ms + slip_ms + stake_ms + ticket_ms) / 1000
    default_s = float(re.search(
        r'"BETTING_PLACEMENT_TIMEOUT_SECONDS", "([\d.]+)"',
        Path(__file__).resolve().parents[1].joinpath("oddsiq", "config.py").read_text(),
    ).group(1))
    assert enforced_s == 44.0                     # the documented cap sum
    assert default_s > enforced_s + 10            # >=10s headroom for the uncapped CDP tail
