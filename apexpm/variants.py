"""Tuned strategy variants: journal-backed challengers that enter the shadow book.

A variant is a base strategy plus a dict of overrides and a symbol allowlist. The
tuner proposes them with time-split evidence; they register as `research`
(shadow book: journaled, never ordered) and earn live shadow evidence like any
other strategy. Promotion to paper or live goes through the same gate as every
strategy (typed confirmation, readiness bar). One unpromoted challenger per base:
a new proposal retires the previous one unless the owner already promoted it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .db import Journal, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS variants (
    name TEXT PRIMARY KEY,
    base TEXT NOT NULL,
    overrides TEXT NOT NULL,
    symbols TEXT NOT NULL,
    evidence TEXT,
    created_at TEXT NOT NULL,
    replaced_by TEXT,
    note TEXT);
"""


def ensure(journal: Journal) -> None:
    with journal.tx() as c:
        c.executescript(SCHEMA)


def rows(journal: Journal, active_only: bool = True) -> List[dict]:
    ensure(journal)
    sql = "SELECT * FROM variants" + (" WHERE replaced_by IS NULL" if active_only else "") + " ORDER BY created_at"
    out = []
    for r in journal.q(sql):
        d = dict(r)
        d["overrides"] = json.loads(d["overrides"] or "{}")
        d["symbols"] = json.loads(d["symbols"] or "[]")
        d["evidence"] = json.loads(d["evidence"] or "{}")
        out.append(d)
    return out


def strategy_for(row: dict):
    from .strategies.spaces import BASES
    cls = BASES[row["base"]]
    ov = dict(row["overrides"])
    for k, v in list(ov.items()):
        if isinstance(v, list):
            ov[k] = tuple(v)
    ev = row.get("evidence") or {}
    pooled = ev.get("pooled", {})
    t = pooled.get("test", {})
    desc = (f"tuned variant of {row['base']}: " + ", ".join(f"{k}={v}" for k, v in row["overrides"].items())
            + (f" | test {t.get('exp', 0):+.3f}R n={t.get('n', 0)}" if t else ""))
    return cls(name=row["name"], overrides=ov, description=desc)


def register_all(registry, journal: Journal) -> List[str]:
    """Register every active variant not yet known to this registry. Idempotent and cheap;
    the loop calls it each iteration so a variant submitted during the day is live
    (in the shadow book) by the next scan."""
    added = []
    for r in rows(journal):
        if r["name"] in registry._entries:
            continue
        registry.register(strategy_for(r), "research", list(r["symbols"]))
        added.append(r["name"])
    return added


def _unique_name(journal: Journal, base: str, when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    stem = f"{base}_t{when.strftime('%y%m%d')}"
    taken = {r["name"] for r in journal.q("SELECT name FROM variants WHERE name LIKE ?", (stem + "%",))}
    for letter in "abcdefghijklmnopqrstuvwxyz":
        if stem + letter not in taken:
            return stem + letter
    raise RuntimeError("too many variants today for " + base)


def submit(registry, journal: Journal, specs: List[dict], source: str = "tuner") -> Dict[str, List[str]]:
    """specs: [{base, overrides, symbols, evidence, note}]. Returns names added and retired."""
    from .strategies.spaces import BASES
    ensure(journal)
    added, retired, skipped = [], [], []
    for spec in specs:
        base = spec.get("base")
        if base not in BASES:
            skipped.append(f"{base}: unknown base")
            continue
        ov = spec.get("overrides") or {}
        same = [r for r in rows(journal) if r["base"] == base and r["overrides"] == ov]
        if same:
            skipped.append(f"{base}: identical variant {same[0]['name']} already active")
            continue
        # retire the previous unpromoted challenger of this base
        for r in rows(journal):
            if r["base"] != base:
                continue
            st = registry.status(r["name"]) if r["name"] in registry._entries else "research"
            if st in ("paper", "live"):
                continue            # the owner promoted it: it stays, on its own evidence
            name = _unique_name(journal, base)
            with journal.tx() as c:
                c.execute("UPDATE variants SET replaced_by=? WHERE name=?", (name, r["name"]))
            if r["name"] in registry._entries:
                registry.set_status(r["name"], "retired", f"{source}: replaced by {name}")
            retired.append(r["name"])
        name = _unique_name(journal, base)
        with journal.tx() as c:
            c.execute("INSERT INTO variants (name, base, overrides, symbols, evidence, created_at, note) VALUES (?,?,?,?,?,?,?)",
                      (name, base, json.dumps(ov), json.dumps(list(spec.get("symbols") or [])),
                       json.dumps(spec.get("evidence") or {}), utcnow(), spec.get("note") or source))
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (?,?,?,?)",
                      (utcnow(), name, "variant_proposed", f"{source}: {base} " + json.dumps(ov)))
        row = [r for r in rows(journal) if r["name"] == name][0]
        registry.register(strategy_for(row), "research", list(row["symbols"]))
        added.append(name)
    return {"added": added, "retired": retired, "skipped": skipped}
