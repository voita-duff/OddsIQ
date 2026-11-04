from __future__ import annotations

import json
import logging
import re
import shutil
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from oddsiq import config
from oddsiq.db.models import OddsSnapshot

logger = logging.getLogger(__name__)

_RAW_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def prune_raw_responses(root: Path, keep_days: int) -> None:
    """Delete dated subdirectories under ``root/{scraper}/{YYYY-MM-DD}`` older
    than ``keep_days`` days from today (UTC).

    Directories whose name does not match ``YYYY-MM-DD`` are ignored — never
    counted, never deleted. Empty scraper-name parents are left in place to
    avoid racing with concurrent saves on long sessions. A non-existent
    ``root`` is a no-op, not an error.
    """
    if not root.exists():
        return
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=keep_days)
    for scraper_dir in root.iterdir():
        if not scraper_dir.is_dir():
            continue
        for date_dir in scraper_dir.iterdir():
            if not date_dir.is_dir():
                continue
            if not _RAW_DATE_DIR_RE.match(date_dir.name):
                continue
            try:
                dir_date = date.fromisoformat(date_dir.name)
            except ValueError:
                continue
            if dir_date >= cutoff:
                continue
            try:
                shutil.rmtree(date_dir)
            except OSError as e:
                logger.warning("Could not prune raw response dir %s: %s", date_dir, e)


class BaseScraper(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def fetch_odds(self, game_date: date, profile) -> list[OddsSnapshot]: ...

    async def close(self) -> None:
        pass

    def _save_raw_response(self, data: object, game_date: date) -> str | None:
        try:
            now = datetime.now(timezone.utc)
            dir_path = config.RAW_DATA_DIR / self.name / game_date.isoformat()
            dir_path.mkdir(parents=True, exist_ok=True)
            file_path = dir_path / f"{now.strftime('%H%M%S%f')}.json"
            # Compact (no indent) — these are machine-read archives; pretty-print
            # roughly doubled the serialize cost on every poll. Callers offload
            # this whole method via asyncio.to_thread to keep it off the loop.
            file_path.write_text(json.dumps(data), encoding="utf-8")
            return str(file_path)
        except OSError as e:
            logger.error("Failed to save raw response: %s", e)
            return None
