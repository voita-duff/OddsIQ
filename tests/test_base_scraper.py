from datetime import date, datetime, timezone

from oddsiq.db.models import OddsSnapshot
from oddsiq.scrapers import base as base_mod
from oddsiq.scrapers.base import BaseScraper, prune_raw_responses


class DummyScraper(BaseScraper):
    @property
    def name(self) -> str:
        return "dummy"

    async def fetch_odds(self, game_date: date, profile=None) -> list[OddsSnapshot]:
        return []


def test_save_raw_response_uses_microseconds_to_avoid_same_second_overwrite(
    tmp_path, monkeypatch
):
    times = [
        datetime(2026, 4, 10, 12, 30, 15, 123456, tzinfo=timezone.utc),
        datetime(2026, 4, 10, 12, 30, 15, 654321, tzinfo=timezone.utc),
    ]

    class FakeDatetime:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            return times.pop(0)

    monkeypatch.setattr("oddsiq.config.RAW_DATA_DIR", tmp_path)
    monkeypatch.setattr(base_mod, "datetime", FakeDatetime)

    scraper = DummyScraper()
    first = scraper._save_raw_response({"n": 1}, date(2026, 4, 10))
    second = scraper._save_raw_response({"n": 2}, date(2026, 4, 10))

    assert first is not None
    assert second is not None
    assert first != second
    assert first.endswith("123015123456.json")
    assert second.endswith("123015654321.json")


class _FixedDatetime:
    @staticmethod
    def now(tz):
        assert tz is timezone.utc
        return datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


def _seed_dir(root, scraper, day) -> None:
    d = root / scraper / day
    d.mkdir(parents=True)
    (d / "120000000000.json").write_text("{}")


def test_prune_raw_responses_deletes_old_keeps_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "datetime", _FixedDatetime)
    _seed_dir(tmp_path, "tipsport", "2026-04-01")  # 37 days old → delete
    _seed_dir(tmp_path, "tipsport", "2026-05-07")  # 1 day old → keep
    _seed_dir(tmp_path, "pinnacle", "2026-04-15")  # 23 days old → delete
    _seed_dir(tmp_path, "pinnacle", "2026-05-08")  # today → keep

    prune_raw_responses(tmp_path, keep_days=14)

    assert not (tmp_path / "tipsport" / "2026-04-01").exists()
    assert (tmp_path / "tipsport" / "2026-05-07").exists()
    assert not (tmp_path / "pinnacle" / "2026-04-15").exists()
    assert (tmp_path / "pinnacle" / "2026-05-08").exists()


def test_prune_raw_responses_ignores_nonmatching_directory_names(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "datetime", _FixedDatetime)
    (tmp_path / "tipsport" / "latest").mkdir(parents=True)
    (tmp_path / "tipsport" / "latest" / "x.json").write_text("{}")
    (tmp_path / "tipsport" / "not-a-date").mkdir()
    _seed_dir(tmp_path, "tipsport", "2026-04-01")  # old, should be pruned
    (tmp_path / "stray-file.txt").write_text("ignored")

    prune_raw_responses(tmp_path, keep_days=14)

    assert (tmp_path / "tipsport" / "latest").exists()
    assert (tmp_path / "tipsport" / "not-a-date").exists()
    assert (tmp_path / "stray-file.txt").exists()
    assert not (tmp_path / "tipsport" / "2026-04-01").exists()


def test_prune_raw_responses_no_root_is_noop(tmp_path):
    missing = tmp_path / "does-not-exist"
    prune_raw_responses(missing, keep_days=14)
    assert not missing.exists()


def test_prune_raw_responses_keep_days_zero_only_keeps_today(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "datetime", _FixedDatetime)
    _seed_dir(tmp_path, "tipsport", "2026-05-07")  # yesterday → delete
    _seed_dir(tmp_path, "tipsport", "2026-05-08")  # today → keep

    prune_raw_responses(tmp_path, keep_days=0)

    assert not (tmp_path / "tipsport" / "2026-05-07").exists()
    assert (tmp_path / "tipsport" / "2026-05-08").exists()
