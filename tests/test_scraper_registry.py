import logging

from oddsiq.scrapers import registry
from oddsiq.scrapers.tipsport import TipsportScraper


def _set_flags(
    monkeypatch,
    *,
    tipsport: bool = True,
) -> None:
    monkeypatch.setattr(registry.config, "BOOKMAKER_TIPSPORT_ENABLED", tipsport)


def test_default_flags_enable_only_tipsport(monkeypatch):
    _set_flags(monkeypatch)

    flags = registry.configured_czech_bookmakers()
    scrapers = registry.build_czech_scrapers()

    assert flags == {
        "tipsport": True,
    }
    assert [type(scraper) for scraper in scrapers] == [TipsportScraper]


def test_disabling_all_czech_books_is_safe(monkeypatch, caplog):
    _set_flags(monkeypatch, tipsport=False)

    with caplog.at_level(logging.WARNING, logger="oddsiq.scrapers.registry"):
        scrapers = registry.build_czech_scrapers()

    assert scrapers == []
    assert "No Czech bookmaker scrapers enabled" in caplog.text
