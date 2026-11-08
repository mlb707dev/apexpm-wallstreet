"""Headlines: Alpaca's news API (Benzinga) for the traded symbols and the watch list.

Every headline is journaled once (dedup on the provider id) with its symbols
and timestamp, so trades can later be read against what the tape knew. Live:
refreshed every NEWS_REFRESH_MIN minutes by the loop. History: scripts/fetch_news_history.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .calendar import MARKET_MOVERS
from .log import get_logger

log = get_logger()


def ensure_table(journal) -> None:
    with journal.tx() as c:
        c.executescript("""CREATE TABLE IF NOT EXISTS news (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT, headline TEXT NOT NULL,
            symbols TEXT, url TEXT, fetched_at TEXT);
            CREATE INDEX IF NOT EXISTS news_created ON news(created_at);""")


class NewsFeed:
    def __init__(self, cfg, journal):
        self.cfg = cfg
        self.j = journal
        self._client = None
        self._last: Optional[datetime] = None
        ensure_table(journal)
        if cfg.alpaca_api_key and cfg.alpaca_secret_key:
            try:
                from alpaca.data.historical.news import NewsClient
                self._client = NewsClient(cfg.alpaca_api_key, cfg.alpaca_secret_key)
            except Exception as exc:  # noqa: BLE001
                log.warning("news client unavailable: %s", exc)

    @property
    def symbols(self) -> List[str]:
        wl = getattr(self.cfg, "news_watchlist", None) or MARKET_MOVERS
        return sorted(set([s for s in self.cfg.symbols if "/" not in s] + list(wl)))

    def fetch(self, start: datetime, end: Optional[datetime] = None, limit: int = 50) -> int:
        if not self._client:
            return 0
        from alpaca.data.requests import NewsRequest
        req = NewsRequest(symbols=",".join(self.symbols), start=start, end=end, limit=limit, sort="desc")
        res = self._client.get_news(req)
        items = res.data.get("news", []) if hasattr(res, "data") else list(res)
        n = 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.j.tx() as c:
            for it in items:
                try:
                    cur = c.execute("INSERT OR IGNORE INTO news (id, created_at, source, headline, symbols, url, fetched_at) VALUES (?,?,?,?,?,?,?)",
                                    (str(it.id), it.created_at.isoformat(), getattr(it, "source", None), it.headline,
                                     json.dumps(list(getattr(it, "symbols", []) or [])), getattr(it, "url", None), now))
                    n += cur.rowcount
                except Exception as exc:  # noqa: BLE001
                    log.warning("news row skipped: %s", exc)
        return n

    def refresh(self, now_utc: datetime, every_min: int = 10) -> None:
        if self._last and (now_utc - self._last).total_seconds() < every_min * 60:
            return
        self._last = now_utc
        try:
            n = self.fetch(now_utc - timedelta(hours=6))
            if n:
                log.info("news: %d new headlines", n)
        except Exception as exc:  # noqa: BLE001
            log.warning("news refresh failed: %s", exc)

    def latest(self, limit: int = 12, hours: int = 24):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        return [dict(r) for r in self.j.q("SELECT * FROM news WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?", (since, limit))]
