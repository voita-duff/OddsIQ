from __future__ import annotations

import logging

from oddsiq import config
from oddsiq.scrapers.base import BaseScraper
from oddsiq.scrapers.tipsport import TipsportScraper

logger = logging.getLogger(__name__)


def build_czech_scrapers() -> list[BaseScraper]:
    """Build enabled Czech/soft-book scrapers.

    Pinnacle is intentionally not part of this registry. It is the sharp
    reference book and stays budget-gated in the scheduler.
    """
    scrapers: list[BaseScraper] = []

    if config.BOOKMAKER_TIPSPORT_ENABLED:
        scrapers.append(TipsportScraper())

    if not scrapers:
        logger.warning(
            "No Czech bookmaker scrapers enabled; scheduler will continue without edge detection"
        )
    return scrapers


def configured_czech_bookmakers() -> dict[str, bool]:
    """Expose current bookmaker flags for focused registry/config tests."""
    return {
        "tipsport": config.BOOKMAKER_TIPSPORT_ENABLED,
    }
