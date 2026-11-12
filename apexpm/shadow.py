"""Shadow book: lab strategies trade on paper-free simulation so they earn forward evidence (owner, 2026-11-10).

Before this, a lab strategy that fired only journaled "research_observed" and was never followed to an exit,
so the shadow book was empty and nothing but backtests could argue for a promotion.

Isolation is the point. Shadow trades live in their OWN table (shadow_trades). Nothing that sizes, gates or
reconciles the paper book reads it: trades today, day P&L, the stop cooldown, open positions, the broker
reconciliation, the auditor, the earned-size ladder and the live readiness bar all keep reading `trades`
only. The Trades tab, the Strategies analytics and the readiness rule below are the only readers.

Rules, mirroring what the strategy would do if promoted:
  entry     a lab strategy's signal that passes the same session gates (the scan's early returns) and its own
            conviction minimum opens a simulated trade at the current price plus slippage (BT_SLIPPAGE per
            share for stocks, 2 bps for crypto); one open trade per strategy and ticker; 20 per strategy per
            day; 30 minutes after a shadow stop on that strategy and ticker
  size      full hyper size: the paper account's equity times the profile's max position share (fractional)
  exits     the shared exit engine (stop, breakeven, runner, target) on each scan's bar high/low, plus max hold
            and the flat-by cutoff for stocks; exits pay slippage too
  skipped   option signals (no option prices in the simulation yet)

The 24/5 desk's lab strategies are simulated too (open_desk / manage_desk), by the desk's own rules: the
entry is a LIMIT at the signal price that only fills when the price trades at or through it, the target is a
resting limit, the stop is a watched price that exits through the quote by OVERNIGHT_EXIT_LIMIT_BPS, and the
trade is out by the strategy's exit deadline. Sized like the desk (OVERNIGHT_RISK_PCT of equity, capped at
OVERNIGHT_MAX_NOTIONAL_PCT), in fractional shares so a small account still gets evidence on every symbol.
Desk rows carry desk=1 and are never touched by the regular-hours exit pass.

Readiness: once a day after the close, a lab strategy whose shadow record clears the bar gets ONE pending
owner decision to promote it to paper. Promotion stays the owner's typed decision; nothing here promotes.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .exits import ExitPlan, update as exit_update

MODE = "SHADOW"
MAX_PER_DAY = 20
COOLDOWN_MIN = 30
CRYPTO_SLIP_BPS = 2.0
# readiness bar for proposing a promotion to paper (forward evidence on top of the five-year backtest)
READY = {"min_trades": 20, "min_days": 5, "min_avg_r": 0.10, "min_pf": 1.3, "max_top_share": 0.5}
PROPOSE_TITLE = "Promote lab strategy {name} to paper"

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset TEXT NOT NULL DEFAULT 'stock',
    direction TEXT NOT NULL,
    qty REAL NOT NULL,
    entry REAL NOT NULL,
    initial_stop REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    max_hold_min INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    session_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl REAL,
    rr_realized REAL,
    runner INTEGER NOT NULL DEFAULT 0,
    be_done INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_shadow_open ON shadow_trades(status, symbol);
CREATE INDEX IF NOT EXISTS ix_shadow_strategy ON shadow_trades(strategy, status);
"""


def ensure(journal) -> None:
    if getattr(journal, "_shadow_ready", False):
        return
    with journal.tx() as c:
        c.executescript(SCHEMA)
    cols = {r["name"] for r in journal.q("PRAGMA table_info(shadow_trades)")}
    if "desk" not in cols:          # added 2026-11-10 for the 24/5 desk's lab
        with journal.tx() as c:
            c.execute("ALTER TABLE shadow_trades ADD COLUMN desk INTEGER NOT NULL DEFAULT 0")
    journal._shadow_ready = True


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _slip(symbol: str, price: float, cfg) -> float:
    if "/" in symbol:
        return price * CRYPTO_SLIP_BPS / 1e4
    return float(getattr(cfg, "bt_slippage", 0.03) or 0.0)


def open_trade(journal, sig, snap, cfg, *, equity: float, max_pos_pct: float, conviction: Optional[float],
               now_utc: datetime, session_date: str) -> Optional[int]:
    """Open a simulated trade for a lab signal. Returns the shadow id, or None when a rule says no."""
    ensure(journal)
    if (sig.context or {}).get("asset") == "option":
        return None
    name, sym = sig.setup, sig.symbol
    if journal.q1("SELECT 1 FROM shadow_trades WHERE strategy=? AND symbol=? AND status='open'", (name, sym)):
        return None
    n_today = journal.q1("SELECT COUNT(*) AS n FROM shadow_trades WHERE strategy=? AND session_date=?", (name, session_date))
    if n_today and n_today["n"] >= MAX_PER_DAY:
        return None
    last_stop = journal.q1("SELECT closed_at FROM shadow_trades WHERE strategy=? AND symbol=? AND exit_reason='stop' "
                           "ORDER BY closed_at DESC LIMIT 1", (name, sym))
    if last_stop and last_stop["closed_at"]:
        if now_utc - datetime.fromisoformat(last_stop["closed_at"]) < timedelta(minutes=COOLDOWN_MIN):
            return None
    sign = 1 if sig.direction == "long" else -1
    price = float(snap.price)
    fill = price + sign * _slip(sym, price, cfg)
    # the market has to still be between the stop and the target for the trade to make sense
    if (fill - sig.stop) * sign <= 0 or (sig.target - fill) * sign <= 0:
        return None
    if equity <= 0 or max_pos_pct <= 0 or fill <= 0:
        return None
    qty = round(equity * max_pos_pct / fill, 4)
    if qty <= 0:
        return None
    notes = {"conviction": conviction, "session": (sig.context or {}).get("session"), "signal_entry": sig.entry,
             "atr": float(getattr(getattr(snap, "levels", None), "atr", 0.0) or 0.0)}
    if (sig.context or {}).get("scanner"):
        notes["scanner"] = sig.context["scanner"]       # sourced by the market scanner, outside the strategy's own tickers
    with journal.tx() as c:
        cur = c.execute(
            "INSERT INTO shadow_trades (strategy, symbol, asset, direction, qty, entry, initial_stop, stop, target, max_hold_min, "
            "opened_at, session_date, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, sym, "crypto" if "/" in sym else "stock", sig.direction, qty, round(fill, 4), sig.stop, sig.stop, sig.target,
             int(sig.max_hold_min), _iso(now_utc), session_date, json.dumps(notes)))
        return int(cur.lastrowid)


def _close(journal, t, price: float, reason: str, now_utc: datetime, cfg) -> None:
    sign = 1 if t["direction"] == "long" else -1
    fill = price - sign * _slip(t["symbol"], price, cfg)
    pnl = (fill - t["entry"]) * sign * t["qty"]
    risk = abs(t["entry"] - t["initial_stop"])
    rr = ((fill - t["entry"]) * sign / risk) if risk else None
    with journal.tx() as c:
        c.execute("UPDATE shadow_trades SET status='closed', closed_at=?, exit_price=?, exit_reason=?, pnl=?, rr_realized=? WHERE id=?",
                  (_iso(now_utc), round(fill, 4), reason, round(pnl, 2), round(rr, 4) if rr is not None else None, t["id"]))


def manage(journal, symbol: str, snap, cfg, *, now_utc: datetime, flat_by_passed: bool, momentum: Dict[str, bool]) -> List[int]:
    """Run the exit engine on every open shadow trade for this ticker against the scan's latest bar. Returns closed ids."""
    ensure(journal)
    closed: List[int] = []
    rows = journal.q("SELECT * FROM shadow_trades WHERE status='open' AND desk=0 AND symbol=?", (symbol,))
    if not rows:
        return closed
    price = float(snap.price)
    hi = max(price, float(getattr(snap, "bar_high", 0.0) or price))
    lo = min(price, float(getattr(snap, "bar_low", 0.0) or price))
    lv = getattr(snap, "levels", None)
    for t in rows:
        opened = datetime.fromisoformat(t["opened_at"])
        held = (now_utc - opened).total_seconds() / 60
        if held >= t["max_hold_min"]:
            _close(journal, t, price, "max_hold", now_utc, cfg); closed.append(t["id"]); continue
        if flat_by_passed and t["asset"] == "stock":
            _close(journal, t, price, "flat_by", now_utc, cfg); closed.append(t["id"]); continue
        if held < 0.5:
            continue          # the bar that produced the signal cannot also exit it
        atr = float(getattr(lv, "atr", 0.0) or 0.0) or abs(t["entry"] - t["initial_stop"]) * 5
        lv_dir = []
        if lv is not None:
            lv_dir = sorted(lv.up.values()) if t["direction"] == "long" else sorted(lv.down.values(), reverse=True)
        plan = ExitPlan(t["direction"], t["entry"], t["initial_stop"], t["stop"], t["target"], atr, lv_dir,
                        runner=bool(t["runner"]), be_done=bool(t["be_done"]))
        reason, px, changed = exit_update(plan, hi, lo, bool(momentum.get(t["direction"])), cfg)
        if reason:
            _close(journal, t, float(px if px is not None else price), reason, now_utc, cfg); closed.append(t["id"])
        elif changed or plan.stop != t["stop"] or plan.target != t["target"]:
            with journal.tx() as c:
                c.execute("UPDATE shadow_trades SET stop=?, target=?, runner=?, be_done=? WHERE id=?",
                          (plan.stop, plan.target, int(plan.runner), int(plan.be_done), t["id"]))
    return closed


# ---- the 24/5 desk's lab ------------------------------------------------------------------------------------
def open_desk(journal, sig, cfg, *, equity: float, session: str, now_utc: datetime, session_date: str) -> Optional[int]:
    """A desk lab signal becomes a working LIMIT at the signal's entry (status 'pending'); manage_desk fills it."""
    ensure(journal)
    name, sym = sig.setup, sig.symbol
    if journal.q1("SELECT 1 FROM shadow_trades WHERE strategy=? AND symbol=? AND status IN ('open','pending')", (name, sym)):
        return None
    sign = 1 if sig.direction == "long" else -1
    per_share = abs(sig.entry - sig.stop)
    if equity <= 0 or sig.entry <= 0 or per_share <= 0 or (sig.target - sig.entry) * sign <= 0:
        return None
    qty = round(min(equity * float(cfg.overnight_risk_pct) / per_share, equity * float(cfg.overnight_max_notional_pct) / sig.entry), 4)
    if qty <= 0:
        return None
    notes = {"ext": True, "session": session, "tags": ["24/5"], "exit_by": (sig.context or {}).get("exit_by"),
             "exit_rule": (sig.context or {}).get("exit_rule"), "limit": sig.entry}
    with journal.tx() as c:
        cur = c.execute(
            "INSERT INTO shadow_trades (strategy, symbol, asset, direction, qty, entry, initial_stop, stop, target, max_hold_min, "
            "opened_at, session_date, status, notes, desk) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (name, sym, "stock", sig.direction, qty, sig.entry, sig.stop, sig.stop, sig.target, int(sig.max_hold_min),
             _iso(now_utc), session_date, "pending", json.dumps(notes)))
        return int(cur.lastrowid)


def manage_desk(journal, cfg, price_of, *, now_utc: datetime, tradable=lambda symbol: True) -> Dict[str, List[int]]:
    """One pass over the desk's shadow orders and positions. price_of(symbol) -> last price or None.

    pending: fills at the limit once the price is at or through it; cancelled at the exit deadline.
    open:    target (resting limit, fills at the target), stop or deadline (exit through the quote by the
             desk's exit-limit bps). Nothing fills or exits while the symbol cannot trade."""
    ensure(journal)
    out: Dict[str, List[int]] = {"filled": [], "cancelled": [], "closed": []}
    bps = float(getattr(cfg, "overnight_exit_limit_bps", 30.0))
    for t in journal.q("SELECT * FROM shadow_trades WHERE desk=1 AND status IN ('open','pending')"):
        notes = json.loads(t["notes"] or "{}")
        sign = 1 if t["direction"] == "long" else -1
        exit_by = notes.get("exit_by")
        due = bool(exit_by) and now_utc >= datetime.fromisoformat(exit_by).astimezone(timezone.utc)
        last = price_of(t["symbol"]) if tradable(t["symbol"]) else None
        if t["status"] == "pending":
            if due:
                with journal.tx() as c:
                    c.execute("UPDATE shadow_trades SET status='cancelled', closed_at=?, exit_reason='not_filled' WHERE id=?", (_iso(now_utc), t["id"]))
                out["cancelled"].append(t["id"])
            elif last is not None and (last - t["entry"]) * sign <= 0:
                with journal.tx() as c:   # the hold clock starts at the fill
                    c.execute("UPDATE shadow_trades SET status='open', opened_at=? WHERE id=?", (_iso(now_utc), t["id"]))
                out["filled"].append(t["id"])
            continue
        held = (now_utc - datetime.fromisoformat(t["opened_at"])).total_seconds() / 60
        reason, px = None, None
        if last is not None and (last - t["target"]) * sign >= 0:
            reason, px = "target", float(t["target"])
        elif last is not None and (last - t["stop"]) * sign <= 0:
            reason, px = "stop", last * (1 - sign * bps / 1e4)
        elif (due or held >= t["max_hold_min"]) and last is not None:
            reason, px = "time", last * (1 - sign * bps / 1e4)
        if reason is None:
            continue
        pnl = (px - t["entry"]) * sign * t["qty"]
        risk = abs(t["entry"] - t["initial_stop"])
        with journal.tx() as c:
            c.execute("UPDATE shadow_trades SET status='closed', closed_at=?, exit_price=?, exit_reason=?, pnl=?, rr_realized=? WHERE id=?",
                      (_iso(now_utc), round(px, 4), reason, round(pnl, 2), round((px - t["entry"]) * sign / risk, 4) if risk else None, t["id"]))
        out["closed"].append(t["id"])
    return out


# ---- readers: trade-shaped rows for the analytics (ids negative so they never collide with real trades) ----
SCANNER_TAG = '%"scanner"%'      # notes of a trade the market scanner sourced


def rows(journal, *, name: Optional[str] = None, status: Optional[str] = None, limit: int = 20000,
         scanner: Optional[bool] = None) -> List[Dict[str, Any]]:
    """scanner: None = every trade, False = only the strategy's own tickers, True = only scanner-sourced tickers."""
    ensure(journal)
    q, args = "SELECT * FROM shadow_trades WHERE status != 'cancelled'", []
    if scanner is not None:
        q += " AND COALESCE(notes, '') LIKE ?" if scanner else " AND COALESCE(notes, '') NOT LIKE ?"; args.append(SCANNER_TAG)
    if name:
        q += " AND strategy=?"; args.append(name)
    if status:
        q += " AND status=?"; args.append(status)
    q += " ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?"; args.append(int(limit))
    out = []
    for r in journal.q(q, tuple(args)):
        d = dict(r)
        out.append({"id": -int(d["id"]), "setup": d["strategy"], "symbol": d["symbol"], "asset": "stock" if d["asset"] != "option" else "option",
                    "direction": d["direction"], "qty": d["qty"], "entry": d["entry"], "stop": d["stop"], "initial_stop": d["initial_stop"],
                    "target": d["target"], "max_hold_min": d["max_hold_min"], "opened_at": d["opened_at"], "closed_at": d["closed_at"],
                    "session_date": d["session_date"], "status": d["status"], "exit_price": d["exit_price"], "exit_reason": d["exit_reason"],
                    "pnl": d["pnl"], "rr_realized": d["rr_realized"], "mode": MODE, "notes": d["notes"], "contract": None,
                    "level_reached": None, "level_targeted": None, "regime": None})
    return out


def _sum(rs: List[dict]) -> Dict[str, Any]:
    rr = [r["rr_realized"] for r in rs if r["rr_realized"] is not None]
    pnl = [r["pnl"] or 0.0 for r in rs]
    gw = sum(p for p in pnl if p > 0); gl = -sum(p for p in pnl if p < 0)
    return {"n": len(rs), "days": len({r["session_date"] for r in rs}), "avg_r": (sum(rr) / len(rr)) if rr else None,
            "pf": (gw / gl) if gl > 0 else (math.inf if gw > 0 else None), "net": round(sum(pnl), 2),
            "win": (sum(1 for p in pnl if p > 0) / len(pnl)) if pnl else None, "top_share": (max(pnl) / gw) if gw > 0 else None}


def readiness(journal, registry) -> List[Dict[str, Any]]:
    """Every lab strategy's forward shadow record against the bar."""
    ensure(journal)
    out = []
    for name in registry.names():
        if registry.status(name) != "research":
            continue
        # the bar is judged on the tickers the strategy would actually trade; scanner-sourced trades are a separate record
        rs = [dict(r) for r in journal.q("SELECT * FROM shadow_trades WHERE strategy=? AND status='closed' AND pnl IS NOT NULL "
                                         "AND COALESCE(notes, '') NOT LIKE ?", (name, SCANNER_TAG))]
        if not rs:
            continue
        s = _sum(rs)
        b = READY
        checks = {"trades": s["n"] >= b["min_trades"], "days": s["days"] >= b["min_days"],
                  "avg_r": s["avg_r"] is not None and s["avg_r"] >= b["min_avg_r"],
                  "pf": s["pf"] is not None and s["pf"] >= b["min_pf"],
                  "not_one_trade": s["top_share"] is not None and s["top_share"] <= b["max_top_share"]}
        out.append({"name": name, **s, "checks": checks, "ready": all(checks.values())})
    return out


def propose_ready(journal, registry) -> List[int]:
    """One pending owner decision per lab strategy that cleared the bar (the registry deduplicates). Never promotes."""
    ids = []
    for r in readiness(journal, registry):
        if not r["ready"]:
            continue
        pf = "no losses" if r["pf"] == math.inf else f"{r['pf']:.2f}"
        detail = (f"Shadow book (simulated, no orders): {r['n']} closed trades over {r['days']} session days, won {r['win'] * 100:.0f}%, "
                  f"average {r['avg_r']:+.2f}x risk, profit factor {pf}, net ${r['net']:,.2f}; the best trade is "
                  f"{r['top_share'] * 100:.0f}% of the gains. Bar: {READY['min_trades']}+ trades on {READY['min_days']}+ days, "
                  f"average >= +{READY['min_avg_r']:.2f}x, profit factor >= {READY['min_pf']}, no single trade over "
                  f"{READY['max_top_share'] * 100:.0f}% of the gains.")
        ids.append(registry.propose("shadow", r["name"], PROPOSE_TITLE.format(name=r["name"]), detail,
                                    "promote to paper from the Strategies tab (type 'promote' and a reason)"))
    return ids


def lines(journal, session_date: str) -> List[str]:
    """Daily review lines: what the shadow book did today."""
    ensure(journal)
    rs = [dict(r) for r in journal.q("SELECT * FROM shadow_trades WHERE session_date=? AND status='closed'", (session_date,))]
    open_n = journal.q1("SELECT COUNT(*) AS n FROM shadow_trades WHERE status='open'")["n"]
    if not rs and not open_n:
        return ["- shadow book: no simulated trades today"]
    s = _sum(rs)
    return [f"- shadow book: {s['n']} simulated trades closed today, net ${s['net']:,.2f}, {open_n} still open"]
