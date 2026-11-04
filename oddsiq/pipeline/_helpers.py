"""Small shared helpers used across pipeline services."""
from __future__ import annotations

import json

# Opposite side of a two-way market selection. Used by SettlementService
# (CLV pairing).
OPPOSITE_SELECTION = {"home": "away", "away": "home", "over": "under", "under": "over"}


def opposite_selection(selection: str) -> str:
    return OPPOSITE_SELECTION[selection]


def log_value(value: str) -> str:
    """JSON-quote a free-form field so embedded spaces survive the dashboard log
    parser (e.g. ``away="Los Angeles Lakers"``)."""
    return json.dumps(value)
