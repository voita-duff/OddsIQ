from __future__ import annotations


class SessionStats:
    def __init__(self) -> None:
        # Aggregated counters for the shutdown console summary (the Telegram
        # session summary was removed in the 2026-08-10 rebrand — the console
        # print in Scheduler is the only remaining consumer).
        self.value_bets_flagged: int = 0
        self.total_edge: float = 0.0
        self.total_clv: float = 0.0
        self.clv_count: int = 0
        # Per-book breakdowns (originally for the multi-book era; retained for
        # the single-book path) so the console summary can show which book
        # contributed which bets/CLV.
        self.value_bets_per_book: dict[str, int] = {}
        self.total_edge_per_book: dict[str, float] = {}
        self.total_clv_per_book: dict[str, float] = {}
        self.clv_count_per_book: dict[str, int] = {}
        self.scraper_polls: dict[str, int] = {}
        self.scraper_zero_odds: dict[str, int] = {}
        self.scraper_errors: dict[str, int] = {}
        self.scraper_duration_ms_total: dict[str, int] = {}
        self.scraper_duration_ms_max: dict[str, int] = {}

    def record_scraper_poll(
        self,
        scraper_name: str,
        *,
        duration_ms: int,
        zero_odds: bool,
        error_count: int,
    ) -> None:
        self.scraper_polls[scraper_name] = self.scraper_polls.get(scraper_name, 0) + 1
        if zero_odds:
            self.scraper_zero_odds[scraper_name] = (
                self.scraper_zero_odds.get(scraper_name, 0) + 1
            )
        if error_count:
            self.scraper_errors[scraper_name] = (
                self.scraper_errors.get(scraper_name, 0) + error_count
            )
        self.scraper_duration_ms_total[scraper_name] = (
            self.scraper_duration_ms_total.get(scraper_name, 0) + duration_ms
        )
        self.scraper_duration_ms_max[scraper_name] = max(
            self.scraper_duration_ms_max.get(scraper_name, 0),
            duration_ms,
        )
