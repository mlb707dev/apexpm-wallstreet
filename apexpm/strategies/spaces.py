"""Search spaces for the per-strategy tuner.

Each base strategy declares the knobs the tuner may explore; the incumbent is the
variant currently registered. Knobs are read through Strategy.knob / session_ok /
conviction_min so a variant behaves the same in the backtester and the live loop.
The public mirror ships one example space; the private recipes are not published.
"""
from __future__ import annotations

from typing import Dict, List

from .example_breakout import ExampleBreakout

BASES = {"example_breakout": ExampleBreakout}

INCUMBENTS: Dict[str, Dict] = {
    "example_breakout": {"name": "example_breakout", "overrides": {"entry_before_min": 240, "conviction_min": 0}},
}

SPACES: Dict[str, Dict[str, List]] = {
    "example_breakout": {
        "entry_before_min": [120, 180, 240],
        "conviction_min": [0, 2, 3],
    },
}
