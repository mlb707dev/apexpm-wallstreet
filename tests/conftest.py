import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from apexpm.config import Config  # noqa: E402


@pytest.fixture
def cfg(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("ALPACA_", "MAX_", "KELLY", "PAPER", "DRY_RUN")):
            monkeypatch.delenv(k, raising=False)
    return Config()
