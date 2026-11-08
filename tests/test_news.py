from datetime import datetime, timezone

from apexpm.db import Journal
from apexpm.news import NewsFeed, ensure_table


def test_latest_and_dedup(cfg):
    j = Journal(":memory:")
    cfg.alpaca_api_key = ""   # offline
    nf = NewsFeed(cfg, j)
    ensure_table(j)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with j.tx() as c:
        c.execute("INSERT OR IGNORE INTO news VALUES ('a', ?, 'x', 'Fed holds rates', '[\"SPY\"]', 'u', ?)", (now, now))
        c.execute("INSERT OR IGNORE INTO news VALUES ('a', ?, 'x', 'Fed holds rates', '[\"SPY\"]', 'u', ?)", (now, now))
    assert len(nf.latest()) == 1
    assert nf.fetch(datetime.now(timezone.utc)) == 0      # no client offline → 0, no crash
    assert "SPY" in nf.symbols and "NVDA" in nf.symbols
