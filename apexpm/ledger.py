"""Cash ledger: deposits, withdrawals and account resets move the equity baseline; they never touch
the trade history (owner, 2026-11-07: "ledger balance changes, but all trade history remains").

Every cash movement is one row in cash_flows with a signed amount (+ in, - out). Recording one shifts
the baselines the risk and progress math use (ladder_start, starting_equity, today's starting equity)
by the same amount, so a withdrawal is not a drawdown and a deposit is not a winning day.

Sources
  broker     Alpaca's account activities (CSD deposits, CSW withdrawals), synced by the loop; each has
             the broker's activity id so it is recorded once
  dashboard  the owner typed it in (Settings -> Cash movements)
  reset      the owner reset the account at the broker: the difference between the new equity and the
             old baseline is recorded as one movement
  detected   the loop saw equity jump at startup by more than any trade could explain
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS cash_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT,
    source TEXT NOT NULL,
    external_id TEXT UNIQUE
);
"""


def ensure(journal) -> None:
    with journal.tx() as c:
        c.executescript(SCHEMA)


def _shift(journal, key: str, amount: float) -> None:
    v = journal.get_state(key)
    if v is None:
        return
    try:
        journal.set_state(key, str(round(float(v) + amount, 2)))
    except ValueError:
        pass


def record(journal, amount: float, kind: str, note: str = "", source: str = "dashboard",
           external_id: Optional[str] = None, when: Optional[str] = None, today: Optional[str] = None) -> Dict[str, Any]:
    """Record one movement and shift the baselines. Returns {"ok", "id", "duplicate"}."""
    ensure(journal)
    amount = float(amount)
    if amount == 0:
        return {"ok": False, "error": "amount is zero"}
    if kind not in ("deposit", "withdrawal", "reset", "adjust"):
        return {"ok": False, "error": "kind must be deposit, withdrawal, reset or adjust"}
    if external_id and journal.q1("SELECT 1 FROM cash_flows WHERE external_id=?", (external_id,)):
        return {"ok": True, "duplicate": True}
    ts = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with journal.tx() as c:
        cur = c.execute("INSERT INTO cash_flows (ts, kind, amount, note, source, external_id) VALUES (?,?,?,?,?,?)",
                        (ts, kind, amount, note, source, external_id))
        fid = cur.lastrowid
    for key in ("ladder_start", "starting_equity"):
        _shift(journal, key, amount)
    if today:
        _shift(journal, f"starting_equity:{today}", amount)
    try:
        with journal.tx() as c:
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (?, 'ledger', ?, ?)",
                      (ts, kind, json.dumps({"amount": amount, "note": note, "source": source})))
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "id": fid, "duplicate": False}


def flows(journal, limit: int = 100) -> List[dict]:
    ensure(journal)
    return [dict(r) for r in journal.q("SELECT * FROM cash_flows ORDER BY id DESC LIMIT ?", (limit,))]


def net(journal, since: Optional[str] = None) -> float:
    ensure(journal)
    if since:
        r = journal.q1("SELECT COALESCE(SUM(amount), 0) AS s FROM cash_flows WHERE ts >= ?", (since,))
    else:
        r = journal.q1("SELECT COALESCE(SUM(amount), 0) AS s FROM cash_flows")
    return float(r["s"] if r else 0.0)


def sync_broker(journal, broker, today: Optional[str] = None) -> Dict[str, Any]:
    """Pull deposits and withdrawals from the broker's account activities; record new ones."""
    if broker is None or not getattr(broker, "connected", False):
        return {"ok": False, "reason": "broker not connected"}
    try:
        acts = broker.cash_activities()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    added = []
    for a in acts:
        kind = "deposit" if a["amount"] > 0 else "withdrawal"
        r = record(journal, a["amount"], kind, note=a.get("note", "broker activity"), source="broker",
                   external_id=a["id"], when=a.get("ts"), today=today)
        if r.get("ok") and not r.get("duplicate"):
            added.append(a["id"])
    journal.set_state("ledger_synced_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return {"ok": True, "added": len(added), "seen": len(acts)}


def lines(journal, limit: int = 10) -> List[str]:
    out = ["## Cash movements (baseline shifts; trades untouched)", ""]
    fl = flows(journal, limit)
    if not fl:
        return out + ["- none recorded"]
    for f in fl:
        out.append(f"- {f['ts'][:10]} {f['kind']} {f['amount']:+.2f} ({f['source']}{': ' + f['note'] if f['note'] else ''})")
    return out
