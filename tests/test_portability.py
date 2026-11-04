from __future__ import annotations

import logging
from datetime import date

import pytest

from oddsiq import config
from oddsiq.scrapers.pinnacle import PinnacleScraper


def test_save_raw_response_writes_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DATA_DIR", tmp_path)
    scraper = PinnacleScraper(api_key="k")
    # Non-ASCII payload (Czech) must round-trip regardless of OS locale.
    path = scraper._save_raw_response({"team": "Sparta Praha č"}, date(2026, 6, 9))
    assert path is not None
    from pathlib import Path
    raw = Path(path).read_text(encoding="utf-8")
    assert "Sparta" in raw


def test_log_handler_uses_utf8(monkeypatch, tmp_path):
    import oddsiq.cli as cli  # also imports logging.handlers
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "oddsiq.log")
    root = logging.getLogger()
    pre = list(root.handlers)
    cli._install_rotating_file_handler()
    added = [
        h for h in root.handlers
        if h not in pre and isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    try:
        assert added, "expected a new RotatingFileHandler"
        assert (added[0].encoding or "").lower() == "utf-8"
    finally:
        for h in added:  # don't leak a handler pointing at the temp dir
            root.removeHandler(h)
            h.close()


def test_backup_dir_default_is_repo_relative(monkeypatch):
    import importlib
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    import oddsiq.config as cfg
    importlib.reload(cfg)
    try:
        assert "Desktop" not in str(cfg.BACKUP_DIR)
        assert cfg.BACKUP_DIR.name == "backups"
    finally:
        importlib.reload(cfg)  # restore module state for other tests


import signal

import oddsiq.cli as cli_mod


def test_install_signal_handlers_posix(monkeypatch):
    monkeypatch.setattr(cli_mod.sys, "platform", "linux")
    added = []

    class FakeLoop:
        def add_signal_handler(self, sig, cb):
            added.append(sig)

        def call_soon_threadsafe(self, cb):
            raise AssertionError("call_soon_threadsafe must not be used on POSIX")

    cli_mod._install_signal_handlers(FakeLoop(), lambda: None)
    assert signal.SIGINT in added and signal.SIGTERM in added


def test_install_signal_handlers_windows(monkeypatch):
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    registered = []
    monkeypatch.setattr(cli_mod.signal, "signal", lambda sig, handler: registered.append(sig))

    class FakeLoop:
        def add_signal_handler(self, *a, **k):
            raise AssertionError("add_signal_handler must not be called on win32")

        def call_soon_threadsafe(self, cb):
            cb()

    cli_mod._install_signal_handlers(FakeLoop(), lambda: None)
    assert signal.SIGINT in registered and signal.SIGTERM in registered


@pytest.mark.asyncio
async def test_store_sets_busy_timeout(tmp_path):
    from oddsiq.db.store import Store
    store = Store(tmp_path / "t.db")
    await store.initialize()
    try:
        cur = await store._db.execute("PRAGMA busy_timeout")
        row = await cur.fetchone()
        assert row[0] == 5000
    finally:
        await store.close()


def test_chromium_launch_args_include_no_sandbox():
    from oddsiq.scrapers import cloudflare
    assert "--no-sandbox" in cloudflare._CHROMIUM_LAUNCH_ARGS
    assert "--disable-blink-features=AutomationControlled" in cloudflare._CHROMIUM_LAUNCH_ARGS
