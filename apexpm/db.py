"""SQLite journal. Every trade carries its exit plan; every rejection its reason."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    symbol TEXT NOT NULL,
    setup TEXT NOT NULL,
    direction TEXT NOT NULL,
    regime TEXT,
    qty REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    max_hold_min INTEGER NOT NULL,
    level_targeted REAL,
    level_reached REAL,
    exit_price REAL,
    exit_reason TEXT,
    pnl REAL,
    rr_planned REAL,
    rr_realized REAL,
    vol_scale REAL,
    kelly_used REAL,
    webhook_agreed INTEGER,
    mode TEXT NOT NULL,
    broker_order_id TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    setup TEXT NOT NULL,
    direction TEXT,
    reason TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL,
    atr REAL,
    anchor REAL,
    ribbon_ltf TEXT,
    ribbon_htf TEXT,
    regime TEXT,
    signal TEXT
);
CREATE TABLE IF NOT EXISTS premarket_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    anchor REAL NOT NULL,
    atr REAL NOT NULL,
    levels_json TEXT NOT NULL,
    vix REAL,
    upside_thesis TEXT,
    downside_thesis TEXT,
    invalidation TEXT,
    UNIQUE(date, symbol)
);
CREATE TABLE IF NOT EXISTS daily_reports (
    date TEXT PRIMARY KEY,
    starting_equity REAL,
    ending_equity REAL,
    realized_pnl REAL,
    trades INTEGER,
    wins INTEGER,
    overholds INTEGER,
    rejections INTEGER,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def et_date(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).date().isoformat()


# Columns added after the first schema; applied idempotently at startup.
MIGRATIONS = [
    ("trades", "status", "TEXT NOT NULL DEFAULT 'open'"),
    ("trades", "session_date", "TEXT"),
    ("trades", "fill_qty", "REAL"),
    ("trades", "exit_order_id", "TEXT"),
    ("trades", "initial_stop", "REAL"),
    ("trades", "runner", "INTEGER NOT NULL DEFAULT 0"),
    ("trades", "be_done", "INTEGER NOT NULL DEFAULT 0"),
    ("rejections", "session_date", "TEXT"),
    ("scans", "session_date", "TEXT"),
]


class Journal:
    def __init__(self, path: str = "data/apexpm.db"):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        for table, col, decl in MIGRATIONS:
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized transaction. One connection is shared by the loop, the
        webhook thread and the dashboard, so every access takes the lock."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def q(self, sql: str, params=()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def q1(self, sql: str, params=()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ---- trades -----------------------------------------------------------
    def open_trade(self, **row: Any) -> int:
        """Insert a trade row. Status defaults to 'open'; the live loop inserts
        'pending' BEFORE sending the order (intent first), then promotes it."""
        row.setdefault("opened_at", utcnow())
        row.setdefault("session_date", et_date(datetime.fromisoformat(row["opened_at"])))
        row.setdefault("initial_stop", row.get("stop"))
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO trades ({cols}) VALUES ({marks})", list(row.values()))
            return int(cur.lastrowid)

    def mark_filled(self, trade_id: int, fill_price: float, fill_qty: float, order_id: Optional[str]) -> None:
        with self.tx() as c:
            c.execute("UPDATE trades SET status='open', entry=?, qty=?, fill_qty=?, broker_order_id=COALESCE(?, broker_order_id) "
                      "WHERE id=? AND status='pending'", (fill_price, fill_qty, fill_qty, order_id, trade_id))

    def update_exit_plan(self, trade_id: int, stop: float, target: float, runner: bool, be_done: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE trades SET stop=?, target=?, runner=?, be_done=? WHERE id=? AND closed_at IS NULL",
                      (stop, target, int(runner), int(be_done), trade_id))

    def set_order_id(self, trade_id: int, order_id: Optional[str]) -> None:
        with self.tx() as c:
            c.execute("UPDATE trades SET broker_order_id=? WHERE id=?", (order_id, trade_id))

    def cancel_trade(self, trade_id: int, reason: str, now: Optional[str] = None) -> None:
        """A pending trade whose order never filled. Not a loss; no pnl."""
        with self.tx() as c:
            c.execute("UPDATE trades SET status='cancelled', closed_at=?, exit_reason=? "
                      "WHERE id=? AND status='pending'", (now or utcnow(), reason, trade_id))

    def close_trade(self, trade_id: int, exit_price: float, exit_reason: str,
                    level_reached: Optional[float] = None, exit_order_id: Optional[str] = None,
                    now: Optional[str] = None) -> bool:
        """Idempotent: returns False if the trade was already closed."""
        t = self.get_trade(trade_id)
        if t is None or t["closed_at"] is not None:
            return False
        sign = 1 if t["direction"] == "long" else -1
        pnl = (exit_price - t["entry"]) * sign * t["qty"]
        risk = abs(t["entry"] - (t["initial_stop"] or t["stop"]))
        rr_real = ((exit_price - t["entry"]) * sign / risk) if risk else None
        with self.tx() as c:
            cur = c.execute(
                "UPDATE trades SET closed_at=?, exit_price=?, exit_reason=?, pnl=?, "
                "rr_realized=?, level_reached=?, status='closed', exit_order_id=? WHERE id=? AND closed_at IS NULL",
                (now or utcnow(), exit_price, exit_reason, pnl, rr_real, level_reached, exit_order_id, trade_id),
            )
            return cur.rowcount == 1

    def get_trade(self, trade_id: int) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM trades WHERE id=?", (trade_id,))

    def open_trades(self) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM trades WHERE closed_at IS NULL AND status='open'")

    def pending_trades(self) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM trades WHERE closed_at IS NULL AND status='pending'")

    def closed_trades(self, setup: Optional[str] = None) -> List[sqlite3.Row]:
        if setup:
            return self.q("SELECT * FROM trades WHERE status='closed' AND setup=? ORDER BY closed_at", (setup,))
        return self.q("SELECT * FROM trades WHERE status='closed' ORDER BY closed_at")

    def trades_today(self, date_iso: str) -> int:
        return int(self.q1("SELECT COUNT(*) FROM trades WHERE session_date=? AND status!='cancelled'", (date_iso,))[0])

    def realized_pnl_today(self, date_iso: str) -> float:
        v = self.q1("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE session_date=? AND status='closed'", (date_iso,))[0]
        return float(v or 0.0)

    def last_stop_time(self, symbol: str, direction: str) -> Optional[str]:
        r = self.q1("SELECT closed_at FROM trades WHERE symbol=? AND direction=? AND exit_reason='stop' "
                    "ORDER BY closed_at DESC LIMIT 1", (symbol, direction))
        return r[0] if r else None

    # ---- rejections / scans ----------------------------------------------
    def reject(self, symbol: str, setup: str, reason: str, direction: Optional[str] = None,
               detail: Optional[str] = None, now: Optional[str] = None) -> None:
        now = now or utcnow()
        with self.tx() as c:
            c.execute("INSERT INTO rejections (ts, symbol, setup, direction, reason, detail, session_date) "
                      "VALUES (?,?,?,?,?,?,?)", (now, symbol, setup, direction, reason, detail,
                                                 et_date(datetime.fromisoformat(now))))

    def scan(self, **row: Any) -> None:
        row.setdefault("ts", utcnow())
        row.setdefault("session_date", et_date(datetime.fromisoformat(row["ts"])))
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.tx() as c:
            c.execute(f"INSERT INTO scans ({cols}) VALUES ({marks})", list(row.values()))

    def rejection_histogram(self, date_iso: str) -> Dict[str, int]:
        rows = self.q("SELECT reason, COUNT(*) FROM rejections WHERE session_date=? GROUP BY reason", (date_iso,))
        return {r[0]: int(r[1]) for r in rows}

    # ---- plans / state ----------------------------------------------------
    def save_plan(self, **row: Any) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.tx() as c:
            c.execute(f"INSERT OR REPLACE INTO premarket_plans ({cols}) VALUES ({marks})",
                      list(row.values()))

    def get_plan(self, date_iso: str, symbol: str) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM premarket_plans WHERE date=? AND symbol=?", (date_iso, symbol))

    def set_state(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (key, value))

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        r = self.q1("SELECT value FROM state WHERE key=?", (key,))
        return r[0] if r else default

    def close(self) -> None:
        self._conn.close()
