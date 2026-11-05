"""Strategy registry: status, feature flags, rolling stats and auto-suspension.

Status lives in the journal's `strategy_status` table so it survives restarts
and so a human can override it. Code declares the default status; the table
wins once a row exists. Nothing un-suspends itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from .db import Journal, utcnow
from .strategies.base import Strategy

STATUSES = ("research", "paper", "live", "suspended", "retired")
ORDERING = {"research": False, "paper": True, "live": True, "suspended": False, "retired": False}
# The three books. research and suspended both trade the shadow book (journaled, never ordered);
# suspended is shown as "quarantine": it got there by rule, not by choice.
BOOK = {"live": "live", "paper": "paper", "research": "shadow", "suspended": "shadow", "retired": "retired"}
LABEL = {"research": "lab", "paper": "paper", "live": "live", "suspended": "quarantine", "retired": "retired"}


@dataclass
class StrategyStats:
    name: str
    trades: int
    win_rate: float
    expectancy_r: float
    profit_factor: float
    max_dd_r: float
    level_discipline: float   # share of exits at target/stop/max_hold that did not overhold past target
    loss_streak: int = 0      # current consecutive losses (most recent trades)
    pnl: float = 0.0          # realized $ over ALL closed trades of this strategy
    pnl_window: float = 0.0   # realized $ over the review window


@dataclass
class Entry:
    strategy: Strategy
    default_status: str
    symbols: List[str]


class Registry:
    def __init__(self, journal: Journal, cfg):
        self.journal = journal
        self.cfg = cfg
        self._entries: Dict[str, Entry] = {}
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self.journal.tx() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS strategy_status (
                name TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL, note TEXT);
            CREATE TABLE IF NOT EXISTS strategy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, name TEXT NOT NULL,
                event TEXT NOT NULL, detail TEXT);
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, source TEXT NOT NULL,
                subject TEXT, title TEXT NOT NULL, detail TEXT, proposal TEXT,
                status TEXT NOT NULL DEFAULT 'pending', resolved_at TEXT, note TEXT);
            """)

    # ---- registration -----------------------------------------------------
    def register(self, strategy: Strategy, default_status: str = "research",
                 symbols: Optional[List[str]] = None) -> None:
        assert default_status in STATUSES
        self._entries[strategy.name] = Entry(strategy, default_status, symbols or (list(self.cfg.symbols) + list(getattr(self.cfg, "crypto_symbols", []))))
        row = self.journal.q1("SELECT status FROM strategy_status WHERE name=?", (strategy.name,))
        if row is None:
            self.set_status(strategy.name, default_status, "registered with default status")

    def names(self) -> List[str]:
        return list(self._entries)

    def status(self, name: str) -> str:
        row = self.journal.q1("SELECT status FROM strategy_status WHERE name=?", (name,))
        return row[0] if row else self._entries[name].default_status

    def set_status(self, name: str, status: str, note: str = "") -> None:
        assert status in STATUSES
        with self.journal.tx() as c:
            c.execute("INSERT OR REPLACE INTO strategy_status (name, status, updated_at, note) VALUES (?,?,?,?)",
                      (name, status, utcnow(), note))
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (?,?,?,?)",
                      (utcnow(), name, f"status:{status}", note))

    def active(self, symbol: Optional[str] = None) -> List[Strategy]:
        """Strategies allowed to produce signals that may become orders."""
        out = []
        for name, e in self._entries.items():
            if not ORDERING[self.status(name)]:
                continue
            if symbol and symbol not in e.symbols:
                continue
            out.append(e.strategy)
        return out

    def observing(self, symbol: Optional[str] = None) -> List[Strategy]:
        """Research strategies: evaluated and journaled (DRY_RUN rows) but never ordered."""
        out = []
        for name, e in self._entries.items():
            if self.status(name) != "research":
                continue
            if symbol and symbol not in e.symbols:
                continue
            out.append(e.strategy)
        return out

    def mode_for(self, name: str) -> str:
        """Effective order mode for a strategy given global config."""
        st = self.status(name)
        if st == "live" and not self.cfg.paper and not self.cfg.dry_run:
            return "LIVE"
        if st in ("paper", "live") and not self.cfg.dry_run:
            return "PAPER"
        return "DRY_RUN"

    # ---- stats and the learning loop ------------------------------------
    def stats(self, name: str, window: Optional[int] = None) -> Optional[StrategyStats]:
        window = window or self.cfg.review_window_trades
        rows = self.journal.closed_trades(name)[-window:]
        rows = [r for r in rows if r["pnl"] is not None and r["rr_realized"] is not None]
        if not rows:
            return None
        rs = np.array([r["rr_realized"] for r in rows], dtype=float)
        wins, losses = rs[rs > 0], rs[rs <= 0]
        gw, gl = wins.sum() if len(wins) else 0.0, -losses.sum() if len(losses) else 0.0
        cum = np.cumsum(rs)
        dd = float((np.maximum.accumulate(cum) - cum).max())
        streak = 0
        for r in reversed(rows):
            if (r["pnl"] or 0) <= 0:
                streak += 1
            else:
                break
        overholds = sum(1 for r in rows if r["level_reached"] is not None and r["level_targeted"]
                        and r["level_reached"] > r["level_targeted"] + 0.1 and r["exit_reason"] not in ("target", "runner_target", "trail_stop"))
        disc = 1.0 - overholds / len(rows)
        all_rows = [r for r in self.journal.closed_trades(name) if r["pnl"] is not None]
        return StrategyStats(name=name, trades=len(rows), win_rate=float((rs > 0).mean()),
                             expectancy_r=float(rs.mean()),
                             profit_factor=float(gw / gl) if gl else float("inf"),
                             max_dd_r=dd, level_discipline=disc, loss_streak=streak,
                             pnl=float(sum(r["pnl"] for r in all_rows)), pnl_window=float(sum(r["pnl"] for r in rows)))

    def live_readiness(self, name: str) -> dict:
        """Owner's live bar (2026-11-04): a strategy may go LIVE only when, over ALL its closed PAPER
        trades, it has >= cfg.ready_min_trades, positive expectancy, and a drawdown within
        cfg.ready_max_dd_pct of the ladder start. No override."""
        rows = [r for r in self.journal.closed_trades(name) if r["mode"] == "PAPER" and r["rr_realized"] is not None]
        n = len(rows)
        exp = (sum(r["rr_realized"] for r in rows) / n) if n else None
        start = float(self.journal.get_state("ladder_start") or self.journal.get_state("starting_equity") or 0) or 0.0
        peak = eq = start
        dd = 0.0
        for r in rows:
            eq += r["pnl"] or 0.0
            peak = max(peak, eq)
            if peak > 0:
                dd = max(dd, (peak - eq) / peak)
        min_trades = getattr(self.cfg, "ready_min_trades", 30)
        max_dd = getattr(self.cfg, "ready_max_dd_pct", 0.30)
        checks = [{"name": f">= {min_trades} closed paper trades", "ok": n >= min_trades, "value": n},
                  {"name": "positive expectancy (R)", "ok": bool(exp is not None and exp > 0), "value": round(exp, 3) if exp is not None else None},
                  {"name": f"max drawdown <= {max_dd:.0%}", "ok": dd <= max_dd, "value": round(dd, 3)}]
        return {"ready": all(c["ok"] for c in checks), "checks": checks}

    def book(self, name: str) -> str:
        return BOOK[self.status(name)]

    def label(self, name: str) -> str:
        return LABEL[self.status(name)]

    # ---- decisions queue: judgment calls that are NOT obvious go to the owner ----
    def propose(self, source: str, subject: Optional[str], title: str, detail: str = "", proposal: str = "") -> int:
        dup = self.journal.q1("SELECT id FROM decisions WHERE status='pending' AND subject IS ? AND title=?", (subject, title))
        if dup:
            return int(dup["id"])
        with self.journal.tx() as c:
            cur = c.execute("INSERT INTO decisions (ts, source, subject, title, detail, proposal) VALUES (?,?,?,?,?,?)",
                            (utcnow(), source, subject, title, detail, proposal))
            return int(cur.lastrowid)

    def decisions(self, status: str = "pending"):
        return self.journal.q("SELECT * FROM decisions WHERE status=? ORDER BY id DESC", (status,))

    def resolve(self, decision_id: int, status: str, note: str = "") -> None:
        assert status in ("approved", "dismissed")
        with self.journal.tx() as c:
            c.execute("UPDATE decisions SET status=?, resolved_at=?, note=? WHERE id=? AND status='pending'",
                      (status, utcnow(), note, decision_id))
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (?,?,?,?)",
                      (utcnow(), "owner", f"decision_{status}", f"#{decision_id} {note}"))

    def review(self) -> List[str]:
        """Apply the lifecycle rules. Obvious cases act automatically (quarantine);
        borderline cases become decisions for the owner. Returns actions taken."""
        actions = []
        for name in self._entries:
            st = self.status(name)
            if st not in ("paper", "live"):
                continue
            s = self.stats(name)
            if s is None:
                continue
            # losing streak is checked before the minimum-trades gate: it is the fast rule
            if s.loss_streak >= self.cfg.suspend_loss_streak:
                self.set_status(name, "suspended", f"auto: {s.loss_streak} losses in a row")
                actions.append(f"QUARANTINED {name}: {s.loss_streak} losses in a row")
                continue
            if s.trades < self.cfg.review_min_trades:
                continue
            why = None
            if s.expectancy_r < self.cfg.suspend_expectancy_r:
                why = f"expectancy {s.expectancy_r:.2f}R < {self.cfg.suspend_expectancy_r}"
            elif s.profit_factor < self.cfg.suspend_profit_factor:
                why = f"profit factor {s.profit_factor:.2f} < {self.cfg.suspend_profit_factor}"
            elif s.max_dd_r > self.cfg.suspend_dd_r:
                why = f"drawdown {s.max_dd_r:.1f}R > {self.cfg.suspend_dd_r}"
            elif s.level_discipline < self.cfg.suspend_discipline:
                why = f"level discipline {s.level_discipline:.0%} < {self.cfg.suspend_discipline:.0%}"
            if why:
                self.set_status(name, "suspended", f"auto: {why} over last {s.trades} trades")
                actions.append(f"QUARANTINED {name}: {why}")
                continue
            # borderline: edge present but thin, or drawdown climbing → owner decides
            if 0 <= s.expectancy_r < self.cfg.suspend_expectancy_r + 0.05 or s.max_dd_r > self.cfg.suspend_dd_r * 0.75:
                self.propose("review", name, f"{name}: edge thin or drawdown rising",
                             f"last {s.trades}: exp {s.expectancy_r:+.2f}R, pf {s.profit_factor:.2f}, dd {s.max_dd_r:.1f}R, "
                             f"streak {s.loss_streak}", "Reduce size, or move to shadow for a tuning pass")
        return actions

    def events(self, limit: int = 50):
        return self.journal.q("SELECT * FROM strategy_events ORDER BY id DESC LIMIT ?", (limit,))
