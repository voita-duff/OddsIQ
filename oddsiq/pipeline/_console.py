"""Console-print helpers shared by the pipeline services (value-bet + result lines)."""

from __future__ import annotations

from datetime import datetime


def console_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def game_id_label(game) -> str:
    return str(game.id) if game.id is not None else "unknown"
