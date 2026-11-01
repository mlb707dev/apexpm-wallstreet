"""Scan loop. In DRY_RUN it computes everything and journals, but never orders.

Order lifecycle (PAPER/LIVE):
  intent row (status=pending) -> submit limit -> poll each scan ->
  filled: promote to open with the real fill price/qty
  unfilled after ENTRY_GOOD_FOR_BARS bars or at cutoff: cancel + mark cancelled
Exits close whatever the broker actually holds (close_position) and record the
broker's fill price. Every step writes the journal BEFORE talking to the broker.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from . import risk as riskmod
from .broker import Broker
from .config import Config
from .data import DataFeed
from .db import Journal, et_date
from .exits import ExitPlan, update as exit_update
from .levels import LevelMap, levels_from_daily
from .log import get_logger, safe
from . import calendar as cal
from .profiles import EffectiveConfig, entries_paused, get_profile
from .registry import Registry
from .ribbon import ribbon_from_closes
from .sizing import size_trade
from .stream import FastLayer
from .strategies import ExampleBreakout
from .strategies.base import MarketSnapshot, Signal
from .webhook import WebhookServer

ET = ZoneInfo("America/New_York")


class Bot:
    def __init__(self, cfg: Config, journal: Optional[Journal] = None,
                 broker: Optional[Broker] = None, feed: Optional[DataFeed] = None):
        self.cfg = cfg
        self.log = get_logger(cfg.log_path)
        self.journal = journal or Journal(cfg.db_path)
        self.broker = broker or Broker(cfg)
        self.feed = feed or DataFeed(cfg)
        self.registry = Registry(self.journal, cfg)
        self.registry.register(ExampleBreakout(), "paper")
        self.webhook: Optional[WebhookServer] = None
        self.state: Optional[riskmod.RiskState] = None
        self._levels: Dict[str, LevelMap] = {}
        self._reached: Dict[str, Dict[str, float]] = {}
        self._today = ""
        self._day_ctx: Optional[cal.DayContext] = None
        self._momentum: Dict[str, bool] = {}
        self._exit_lock = threading.RLock()
        self.fast: Optional[FastLayer] = None
        cal.ensure_table(self.journal)

    # ---- startup ----------------------------------------------------------
    def startup(self, now_utc: Optional[datetime] = None) -> None:
        now_utc = now_utc or datetime.now(timezone.utc)
        for p in self.cfg.validate():
            self.log.warning("config: %s", p)
        self.log.info("mode=%s symbols=%s broker=%s feed=%s", self.cfg.mode, self.cfg.symbols,
                      "connected" if self.broker.connected else "offline",
                      "connected" if self.feed.connected else "offline")
        if self.cfg.mode == "LIVE":
            self.log.warning("LIVE MODE. Aborting in %ds unless killed.", self.cfg.live_abort_delay_sec)
            time.sleep(self.cfg.live_abort_delay_sec)

        acct = self.broker.account()
        today = et_date(now_utc)
        # Kill-switch anchor is per session date and survives restarts.
        anchor_key = f"starting_equity:{today}"
        anchor = self.journal.get_state(anchor_key)
        if anchor is None:
            anchor = str(acct.equity)
            self.journal.set_state(anchor_key, anchor)
        self.journal.set_state("starting_equity", anchor)
        halted = self.journal.get_state(f"halted:{today}") == "1"

        # Reconcile: journal open trades vs broker positions.
        broker_pos = self.broker.position_map()
        journal_open = {t["symbol"] for t in self.journal.open_trades()}
        for sym in journal_open - set(broker_pos):
            if self.broker.connected and not self.cfg.dry_run:
                self.log.error("RECONCILE: journal has open %s but broker has no position", sym)
        for sym in set(broker_pos) - journal_open:
            self.log.error("RECONCILE: broker holds %s (%s x%.0f) with no journal trade; not managed by the bot",
                           sym, broker_pos[sym].side, broker_pos[sym].qty)
        open_syms = journal_open | set(broker_pos)

        self.state = riskmod.RiskState(starting_equity=float(anchor), equity=acct.equity,
                                       open_symbols=open_syms, halted=halted,
                                       halt_reason="persisted" if halted else None)
        self._roll_day(today)
        self.log.info("equity=%.2f anchor=%.2f open=%s halted=%s", acct.equity, float(anchor), sorted(open_syms), halted)
        for n in self.registry.names():
            self.log.info("strategy %-18s status=%-10s mode=%s", n, self.registry.status(n), self.registry.mode_for(n))
        if self.cfg.webhook_enabled and self.webhook is None:
            self.webhook = WebhookServer(self.cfg, self.journal)
            self.webhook.start()

    # ---- per-day ----------------------------------------------------------
    def _roll_day(self, today: str) -> None:
        if today == self._today:
            return
        self._today = today
        self._levels.clear()
        self._reached.clear()
        from datetime import date as _date
        self._day_ctx = cal.day_context(_date.fromisoformat(today), self.cfg.symbols, self.journal)
        safe(self.journal.set_state, "day_context", json.dumps(self._day_ctx.as_dict()))
        if self._day_ctx.events:
            self.log.info("calendar %s: %s (size x%.2f, windows %s)", today,
                          ", ".join(e["label"] for e in self._day_ctx.events), self._day_ctx.size_mult, self._day_ctx.no_entry)
        if self.state:
            self.state.trades_today = self.journal.trades_today(today)
            self.state.realized_pnl_today = self.journal.realized_pnl_today(today)
            self.state.forced_dry_run = False

    def _levels_for(self, symbol: str, today: str) -> Optional[LevelMap]:
        if symbol in self._levels:
            return self._levels[symbol]
        if not self.feed.connected:
            return None
        daily = self.feed.daily(symbol)
        if len(daily) < self.cfg.atr_period + 1:
            self.log.warning("%s: not enough daily bars (%d)", symbol, len(daily))
            return None
        try:
            lv = levels_from_daily(daily, self.cfg.atr_period)
        except ValueError as exc:
            self.log.warning("%s: levels unavailable: %s", symbol, exc)
            return None
        self._levels[symbol] = lv
        try:
            v = daily["volume"]
            safe(self.journal.set_state, f"vol:{symbol}", json.dumps({
                "avg5": float(v.tail(5).mean()), "avg30": float(v.tail(30).mean()), "prev": float(v.iloc[-1])}))
        except Exception:  # noqa: BLE001
            pass
        self.journal.save_plan(date=today, symbol=symbol, anchor=lv.anchor, atr=lv.atr,
                               levels_json=json.dumps(lv.as_dict()))
        self.log.info("%s levels anchor=%.2f atr=%.2f bands=%s", symbol, lv.anchor, lv.atr, sorted(lv.up))
        return lv

    # ---- one scan ---------------------------------------------------------
    def snapshot(self, symbol: str, today: str, now_utc: datetime) -> Optional[MarketSnapshot]:
        lv = self._levels_for(symbol, today)
        if lv is None:
            return None
        ltf = self.feed.intraday(symbol, self.cfg.bar_minutes)
        htf = self.feed.intraday(symbol, self.cfg.htf_bar_minutes)
        if len(ltf) < self.cfg.ema_slow or len(htf) < self.cfg.ema_slow:
            self.log.warning("%s: not enough intraday bars", symbol)
            return None
        price = (self.fast.prices.get(symbol, 5) if self.fast else None) \
            or self.feed.last_price(symbol, self.cfg.max_price_age_sec) or float(ltf["close"].iloc[-1])
        try:
            if "volume" in ltf and hasattr(ltf.index, "tz_convert"):
                et_idx = ltf.index.tz_convert(ET)
                today_mask = et_idx.date == now_utc.astimezone(ET).date()
                safe(self.journal.set_state, f"vol_today:{symbol}", str(float(ltf["volume"][today_mask].sum())))
        except Exception:  # noqa: BLE001
            pass
        rl = ribbon_from_closes(ltf["close"].values, self.cfg.ema_fast, self.cfg.ema_mid, self.cfg.ema_slow)
        rh = ribbon_from_closes(htf["close"].values, self.cfg.ema_fast, self.cfg.ema_mid, self.cfg.ema_slow)
        prev_rl = None
        if len(ltf) > self.cfg.ema_slow + 1:
            prev_rl = ribbon_from_closes(ltf["close"].values[:-1], self.cfg.ema_fast, self.cfg.ema_mid, self.cfg.ema_slow)

        reached = self._reached.setdefault(symbol, {"long": 0.0, "short": 0.0})
        prev_reached = dict(reached)
        pos = lv.position_in_range(price)
        if pos > 0:
            reached["long"] = max(reached["long"], pos)
        else:
            reached["short"] = max(reached["short"], -pos)

        now_et = now_utc.astimezone(ET)
        mins = (now_et.hour - 9) * 60 + now_et.minute - 30
        regime = "trend_up" if rh.state == "bull" else "trend_down" if rh.state == "bear" else "chop"
        self._momentum[symbol] = {"long": rl.fast > rl.mid, "short": rl.fast < rl.mid}
        return MarketSnapshot(symbol=symbol, price=price, levels=lv, ribbon_ltf=rl, ribbon_htf=rh,
                              ltf_closes=list(ltf["close"].values), regime=regime,
                              reached_today=prev_reached, bar_index_today=max(0, mins) // self.cfg.bar_minutes,
                              minutes_since_open=mins, prev_ribbon_ltf=prev_rl)

    def scan_once(self, now_utc: Optional[datetime] = None) -> List[Signal]:
        now_utc = now_utc or datetime.now(timezone.utc)
        now_et = now_utc.astimezone(ET)
        today = now_et.date().isoformat()
        if self.state is None:
            self.startup(now_utc)
        assert self.state is not None
        self._roll_day(today)

        acct = self.broker.account()
        self.state.equity = acct.equity
        # Account reset detection: equity far from the anchor with no trades closed today
        # is a paper reset (or a deposit/withdrawal), not a drawdown. Re-anchor instead of halting.
        anchor = self.state.starting_equity
        if anchor > 0 and abs(acct.equity - anchor) / anchor > 0.5 and self.journal.realized_pnl_today(today) == 0.0 \
                and not self.journal.open_trades():
            self.log.warning("account reset detected: equity %.2f vs anchor %.2f; re-anchoring", acct.equity, anchor)
            self.state.starting_equity = acct.equity
            self.journal.set_state(f"starting_equity:{today}", str(acct.equity))
            self.journal.set_state("starting_equity", str(acct.equity))
        ks = riskmod.check_kill_switch(self.state, self.cfg.kill_dd_pct)
        if ks:
            self.journal.set_state(f"halted:{today}", "1")
            self.log.error("HALT: %s", ks)
            return []
        if self.state.halted:
            return []
        cap = self.cfg.max_daily_loss
        if self.cfg.max_daily_loss_pct > 0:
            cap = min(cap, self.state.starting_equity * self.cfg.max_daily_loss_pct)
        dl = riskmod.check_daily_loss(self.state, cap)
        if dl:
            self.log.warning("forced DRY_RUN for today: %s", dl)

        self._poll_pending(now_utc)

        taken: List[Signal] = []
        for symbol in self.cfg.symbols:
            try:
                taken += self._scan_symbol(symbol, today, now_utc, now_et)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("%s scan failed: %s", symbol, exc)
        return taken

    def _scan_symbol(self, symbol: str, today: str, now_utc: datetime, now_et: datetime) -> List[Signal]:
        assert self.state is not None
        taken: List[Signal] = []
        ctx = self._day_ctx or cal.day_context(now_et.date(), self.cfg.symbols, self.journal)
        ecfg = EffectiveConfig(self.cfg, get_profile(self.journal), ctx.size_mult)
        snap = self.snapshot(symbol, today, now_utc)
        if snap is None:
            return taken
        ts = now_utc.isoformat(timespec="seconds")
        safe(self.journal.set_state, "heartbeat", ts)
        safe(self.journal.set_state, f"last_price:{symbol}", str(snap.price))
        safe(self.journal.set_state, f"regime:{symbol}", snap.regime)
        safe(self.journal.scan, ts=ts, symbol=symbol, price=snap.price, atr=snap.levels.atr,
             anchor=snap.levels.anchor, ribbon_ltf=snap.ribbon_ltf.state,
             ribbon_htf=snap.ribbon_htf.state, regime=snap.regime, signal=None)
        reject = lambda *a, **k: safe(self.journal.reject, *a, now=ts, **k)  # noqa: E731
        if snap.minutes_since_open < 0:
            reject(symbol, "session", "premarket")
            return taken
        if riskmod.past_cutoff(now_et, self.cfg.no_entry_after):
            reject(symbol, "session", "past_no_entry_cutoff")
            return taken
        if today in self.cfg.blackout_dates:
            reject(symbol, "session", "blackout_date")
            return taken
        if entries_paused(self.journal):
            reject(symbol, "session", "entries_paused")
            return taken
        if symbol in ctx.skip_symbols:
            reject(symbol, "session", "earnings_skip")
            return taken
        blocked = ctx.entries_blocked_at(now_et.strftime("%H:%M"))
        if blocked:
            reject(symbol, "session", blocked)
            return taken

        active = self.registry.active(symbol)
        observing = self.registry.observing(symbol)
        for strat in active + observing:
            observe_only = strat in observing
            sig, why = strat.evaluate(snap, self.cfg)
            if sig is None:
                if why not in ("not_at_entry_level",):
                    reject(symbol, strat.name, why)
                continue
            if observe_only:
                reject(symbol, strat.name, "research_observed", sig.direction,
                       json.dumps({"entry": sig.entry, "stop": sig.stop, "target": sig.target}))
                continue
            gates = riskmod.entry_gates(self.state, symbol=symbol, direction=sig.direction, now=now_utc,
                                        last_stop_iso=self.journal.last_stop_time(symbol, sig.direction), cfg=ecfg)
            if any(t["symbol"] == symbol for t in self.journal.pending_trades()):
                gates.append("pending_order")
            if gates:
                reject(symbol, strat.name, ",".join(gates), sig.direction)
                continue
            pnls = [t["pnl"] for t in self.journal.closed_trades(strat.name) if t["pnl"] is not None]
            sz = size_trade(equity=self.state.equity, price=snap.price, entry=sig.entry, stop=sig.stop,
                            target=sig.target, atr=snap.levels.atr, closed_pnls=pnls, cfg=ecfg)
            if sz.qty <= 0:
                reject(symbol, strat.name, f"size:{sz.reason}", sig.direction)
                continue
            cm = ecfg.confluence_mult(int(sig.context.get("confluence", 1)))
            if cm != 1.0:
                cap = min(self.cfg.max_pos_dollars, self.state.equity * self.cfg.max_notional_pct) / snap.price
                qty = min(sz.qty * cm, cap)
                qty = round(qty, 3) if self.cfg.fractional_shares else float(int(qty))
                sz = sz.__class__(qty, sz.dollar_risk * cm, sz.kelly_full, sz.kelly_used, sz.vol_scale, qty * snap.price)
                sig.context["confluence_mult"] = cm
            chased = abs(snap.price - sig.entry) > self.cfg.level_tolerance_atr * snap.levels.atr * 2
            if chased and not self.cfg.allow_gap_fills and not sig.context.get("fill_at_market"):
                reject(symbol, strat.name, "gap_fill_skipped", sig.direction)
                continue
            hook = self.webhook.consume(symbol, sig.direction) if self.webhook else None
            agreed = None
            if hook is not None:
                ref = sig.level_targeted
                agreed = abs(float(hook["level"]) - ref) <= self.cfg.level_tolerance_atr
                self.log.info("webhook %s %s level=%s agreed=%s", hook["event"], hook["direction"], hook["level"], agreed)
            self._enter(sig, sz, snap, now_utc, webhook_agreed=agreed)
            taken.append(sig)
        return taken

    # ---- entries ----------------------------------------------------------
    def _enter(self, sig: Signal, sz, snap: MarketSnapshot, now_utc: datetime, webhook_agreed=None) -> None:
        assert self.state is not None
        side = "buy" if sig.direction == "long" else "sell"
        mode = self.registry.mode_for(sig.setup)
        if self.state.forced_dry_run:
            mode = "DRY_RUN"
        dry = mode == "DRY_RUN"
        # 1. intent row first
        qty = sz.qty
        tid = self.journal.open_trade(
            opened_at=now_utc.isoformat(timespec="seconds"),
            symbol=sig.symbol, setup=sig.setup, direction=sig.direction, regime=snap.regime,
            qty=sz.qty, entry=sig.entry, stop=sig.stop, target=sig.target,
            max_hold_min=sig.max_hold_min, level_targeted=sig.level_targeted,
            rr_planned=sig.rr, vol_scale=sz.vol_scale, kelly_used=sz.kelly_used,
            mode=mode, status="open" if dry else "pending", notes=json.dumps(sig.context),
            webhook_agreed=None if webhook_agreed is None else int(webhook_agreed))
        self.state.trades_today += 1
        self.state.open_symbols.add(sig.symbol)
        if dry:
            self.log.info("DRY_RUN trade #%d would %s %s x%s @ %.2f stop=%.2f target=%.2f rr=%.2f",
                          tid, side, sig.symbol, qty, sig.entry, sig.stop, sig.target, sig.rr)
            return
        # 2. then the order
        try:
            if sig.context.get("fill_at_market"):
                oid = self.broker.submit_market(sig.symbol, sz.qty, side)
            else:
                # Marketable limit: fill now at the current price, capped a small
                # tolerance beyond the level (the backtester's "next_open" model).
                tol = self.cfg.level_tolerance_atr * snap.levels.atr * 2
                cap = sig.entry + tol if sig.direction == "long" else sig.entry - tol
                limit = min(snap.price + 0.01, cap) if sig.direction == "long" else max(snap.price - 0.01, cap)
                oid = self.broker.submit_limit(sig.symbol, sz.qty, side, limit)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("order submit failed for trade #%d: %s", tid, exc)
            self.journal.cancel_trade(tid, f"submit_failed:{type(exc).__name__}", now_utc.isoformat(timespec="seconds"))
            self.state.open_symbols.discard(sig.symbol)
            return
        self.journal.set_order_id(tid, oid)
        self.log.info("trade #%d pending (%s) order=%s", tid, mode, oid)

    def _poll_pending(self, now_utc: datetime) -> None:
        """Promote filled orders; cancel stale ones."""
        assert self.state is not None
        now_et = now_utc.astimezone(ET)
        now_iso = now_utc.isoformat(timespec="seconds")
        for t in self.journal.pending_trades():
            try:
                st = self.broker.order_state(t["broker_order_id"]) if t["broker_order_id"] else None
                if st is None:
                    # no broker: the order can never fill; treat as cancelled so DRY_RUN restarts stay clean
                    if not self.broker.connected or self.cfg.dry_run:
                        self.journal.cancel_trade(t["id"], "no_broker", now_iso)
                        self.state.open_symbols.discard(t["symbol"])
                    continue
                if st.status == "filled" or (st.done and st.filled_qty > 0):
                    self.journal.mark_filled(t["id"], st.filled_avg_price or t["entry"], st.filled_qty, st.id)
                    self.log.info("trade #%d filled %.0f @ %.2f", t["id"], st.filled_qty, st.filled_avg_price or t["entry"])
                    if st.status != "filled":
                        self.log.warning("trade #%d partial fill %.0f/%.0f", t["id"], st.filled_qty, t["qty"])
                    continue
                if st.done:  # canceled/expired/rejected with nothing filled
                    self.journal.cancel_trade(t["id"], f"broker_{st.status}", now_iso)
                    self.state.open_symbols.discard(t["symbol"])
                    continue
                age_bars = (now_utc - datetime.fromisoformat(t["opened_at"])).total_seconds() / 60 / self.cfg.bar_minutes
                if age_bars >= self.cfg.entry_good_for_bars or riskmod.past_cutoff(now_et, self.cfg.no_entry_after):
                    self.broker.cancel_order(t["broker_order_id"])
                    self.journal.cancel_trade(t["id"], "unfilled", now_iso)
                    self.state.open_symbols.discard(t["symbol"])
                    self.log.info("trade #%d cancelled unfilled after %.1f bars", t["id"], age_bars)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("pending poll failed for trade #%d: %s", t["id"], exc)

    # ---- exits ------------------------------------------------------------
    def check_exits(self, now_utc: Optional[datetime] = None) -> None:
        """Compare price to the stored exit plan; never recompute the plan.
        Time-based exits (max_hold, flat_by) fire even without a price."""
        now_utc = now_utc or datetime.now(timezone.utc)
        now_et = now_utc.astimezone(ET)
        today = now_et.date().isoformat()
        for t in self.journal.open_trades():
            try:
                self._check_exit(t, now_utc, now_et, today)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("exit check failed for trade #%d: %s", t["id"], exc)

    # ---- fast layer callbacks (stream thread) --------------------------------
    def _on_tick(self, symbol: str, price: float) -> None:
        """Runs on every trade print: exit engine at tick speed for that symbol."""
        if self.state is None:
            return
        now_utc = datetime.now(timezone.utc)
        now_et = now_utc.astimezone(ET)
        today = now_et.date().isoformat()
        if self.fast and self.fast.ticks % 50 == 0:
            safe(self.journal.set_state, "stream_ticks", str(self.fast.ticks))
            safe(self.journal.set_state, "stream_last_tick", now_utc.isoformat(timespec="seconds"))
        for t in self.journal.open_trades():
            if t["symbol"] != symbol:
                continue
            try:
                self._check_exit(t, now_utc, now_et, today, price=price)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("tick exit failed for trade #%d: %s", t["id"], exc)

    def _on_order(self, ev: dict) -> None:
        """Runs on every order update: promote fills immediately."""
        try:
            if ev["event"] not in ("fill", "partial_fill", "canceled", "expired", "rejected"):
                return
            with self._exit_lock:
                for t in self.journal.pending_trades():
                    if t["broker_order_id"] != ev["order_id"]:
                        continue
                    if ev["filled_qty"] > 0:
                        self.journal.mark_filled(t["id"], ev["filled_avg_price"] or t["entry"], ev["filled_qty"], ev["order_id"])
                        self.log.info("trade #%d filled via stream %.0f @ %s", t["id"], ev["filled_qty"], ev["filled_avg_price"])
                    elif ev["event"] in ("canceled", "expired", "rejected"):
                        self.journal.cancel_trade(t["id"], f"broker_{ev['event']}")
                        if self.state:
                            self.state.open_symbols.discard(t["symbol"])
        except Exception as exc:  # noqa: BLE001
            self.log.exception("order update failed: %s", exc)

    def _check_exit(self, t, now_utc: datetime, now_et: datetime, today: str, price: Optional[float] = None) -> None:
      with self._exit_lock:
        sign = 1 if t["direction"] == "long" else -1
        opened = datetime.fromisoformat(t["opened_at"])
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        held_min = (now_utc - opened).total_seconds() / 60
        if price is None and self.fast:
            price = self.fast.prices.get(t["symbol"], 5)
        if price is None:
            try:
                price = self.feed.last_price(t["symbol"], self.cfg.max_price_age_sec) if self.feed.connected else None
            except Exception as exc:  # noqa: BLE001
                self.log.warning("price fetch failed for %s: %s", t["symbol"], exc)
        reason = None
        if held_min >= t["max_hold_min"]:
            reason = "max_hold"
        elif riskmod.past_cutoff(now_et, self.cfg.flat_by):
            reason = "flat_by"
        elif price is not None:
            lv = self._levels.get(t["symbol"])
            atr = lv.atr if lv else abs(t["entry"] - (t["initial_stop"] or t["stop"])) * 5
            lv_dir = (sorted(lv.up.values()) if t["direction"] == "long" else sorted(lv.down.values(), reverse=True)) if lv else []
            plan = ExitPlan(t["direction"], t["entry"], t["initial_stop"] or t["stop"], t["stop"], t["target"], atr, lv_dir,
                            runner=bool(t["runner"]), be_done=bool(t["be_done"]))
            m = self._momentum.get(t["symbol"])
            momentum = bool(m and m["long" if sign == 1 else "short"])
            reason, _px, changed = exit_update(plan, price, price, momentum, self.cfg)
            if changed or plan.stop != t["stop"] or plan.target != t["target"]:
                self.journal.update_exit_plan(t["id"], plan.stop, plan.target, plan.runner, plan.be_done)
                self.log.info("trade #%d plan: stop %.2f target %.2f runner=%s", t["id"], plan.stop, plan.target, plan.runner)
        if reason is None:
            return
        exit_price = price
        exit_oid = None
        if t["mode"] != "DRY_RUN":
            exit_oid = self.broker.close_position(t["symbol"])
            # best-effort real fill price
            try:
                st = self.broker.order_state(exit_oid) if exit_oid else None
                if st and st.filled_avg_price:
                    exit_price = st.filled_avg_price
            except Exception:  # noqa: BLE001
                pass
        if exit_price is None:
            exit_price = t["target"] if reason in ("target", "runner_target") else t["stop"] if reason.endswith("stop") else t["entry"]
            self.log.warning("trade #%d closed %s without a live price; recorded %.2f", t["id"], reason, exit_price)
        lv = self._levels.get(t["symbol"])
        reached = lv.position_in_range(exit_price) * sign if lv else None
        if self.journal.close_trade(t["id"], exit_price, reason, reached, exit_oid,
                                    now=now_utc.isoformat(timespec="seconds")):
            if self.state:
                self.state.open_symbols.discard(t["symbol"])
                self.state.realized_pnl_today = self.journal.realized_pnl_today(today)
            self.log.info("trade #%d closed %s @ %.2f", t["id"], reason, exit_price)

    def _maybe_daily_review(self) -> None:
        """Once per session date, after the close, run the review (auto-suspension + report)."""
        now_et = datetime.now(timezone.utc).astimezone(ET)
        today = now_et.date().isoformat()
        if now_et.weekday() >= 5 or now_et.hour < 16 or self.journal.get_state("reviewed") == today:
            return
        if not self.journal.q1("SELECT 1 FROM scans WHERE session_date=? LIMIT 1", (today,)):
            return  # no session data today (holiday); nothing to review
        try:
            from .review import daily_review
            report = daily_review(self, today)
            self.journal.set_state("reviewed", today)
            self.log.info("daily review done:\n%s", report)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("daily review failed: %s", exc)

    # ---- run --------------------------------------------------------------
    def run_forever(self) -> None:
        if self.cfg.run_dashboard:
            from .dashboard import serve
            serve(self.cfg, block=False, journal=self.journal)
            self.log.info("dashboard serving on %s:%d", self.cfg.dashboard_bind, self.cfg.dashboard_port)
        self.startup()
        if self.cfg.stream_enabled and self.broker.connected and not self.cfg.dry_run:
            self.fast = FastLayer(self.cfg, self.cfg.symbols, self._on_tick, self._on_order)
            self.fast.start()
        self.log.info("loop every %ds; fast layer %s", self.cfg.scan_interval_sec, "on" if self.fast else "off")
        while True:
            try:
                if self.broker.clock_is_open():
                    self.check_exits()
                    self.scan_once()
                else:
                    self.log.info("market closed")
                    safe(self.journal.set_state, "heartbeat", datetime.now(timezone.utc).isoformat(timespec="seconds"))
                    safe(self.journal.set_state, "market", "closed")
                    self._maybe_daily_review()
                if self.broker.clock_is_open():
                    safe(self.journal.set_state, "market", "open")
            except KeyboardInterrupt:
                self.log.info("stopped by user")
                return
            except Exception as exc:  # noqa: BLE001
                self.log.exception("loop error: %s", exc)
            try:
                time.sleep(self.cfg.scan_interval_sec)
            except KeyboardInterrupt:
                self.log.info("stopped by user")
                return
