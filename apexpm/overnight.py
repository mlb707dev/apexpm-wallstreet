"""Overnight desk: the loop's execution path for pre-market, after-hours and Alpaca's 20:00-04:00 session.

Feature-flagged (OVERNIGHT_ENABLED, default off). Runs only strategies that carry `ext_sessions`
(private recipes); the regular-hours scan never runs them and never manages their trades.

What is different from the regular-hours path, and why (research/overnight/thesis.md):
  * limit orders only, extended_hours=True, TIF day, whole shares: Alpaca rejects market, stop, stop-limit,
    trailing and bracket orders outside 09:30-16:00
  * no stop orders exist after hours, so the stop is a price this desk watches: when the price reaches it
    (or the hold deadline passes) it cancels the resting target and sends a marketable LIMIT priced through
    the bid by OVERNIGHT_EXIT_LIMIT_BPS, re-pricing every OVERNIGHT_REPRICE_SEC until it fills
  * the target rests as a sell limit as soon as the entry fills
  * entries are skipped when the quoted spread is wider than OVERNIGHT_MAX_SPREAD_BPS, and in the overnight
    session only for symbols Alpaca marks overnight_tradable (and not overnight_halted)
  * one desk position at a time by default, sized from OVERNIGHT_RISK_PCT of equity, capped at
    OVERNIGHT_MAX_NOTIONAL_PCT of equity, and never in a symbol the regular-hours book holds
Lab (research) strategies are evaluated and journaled as observations ("research_observed"), exactly like
the regular-hours lab; only a strategy the owner promotes to paper can order. Nothing here promotes.
Every step writes the journal before it talks to the broker.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import ext_sessions as xs
from .log import safe
from .profiles import entries_paused
from .strategies.ext_base import ExtStrategy

ET = xs.ET
TAG = "24/5"
ACTIVATE_TITLE = "Activate 24/5 strategy {name} (overnight desk)"


def tags_of(registry, name: str) -> List[str]:
    e = getattr(registry, "_entries", {}).get(name)
    return list(getattr(e.strategy, "tags", ()) or ()) if e else []


def _latest_evolve_overnight(journal) -> dict:
    """The nightly pass's per-base overnight assessment (scripts/evolve.py posts it with its harness report)."""
    for r in journal.q("SELECT detail FROM strategy_events WHERE name='evolve' AND event='ran' ORDER BY id DESC LIMIT 5"):
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        if isinstance(d.get("overnight"), dict):
            return d["overnight"]
    return {}


def readiness(journal, registry) -> List[dict]:
    """24/5 strategies that have passed their time-split rule and are still in the lab: the registered recipe
    when the nightly pass reports it passing, and every tuned overnight variant (the bot only accepts a variant
    whose evidence passed). Never changes a status."""
    from . import variants as variantsmod
    out = []
    names = [n for n, e in getattr(registry, "_entries", {}).items() if is_desk_strategy(e.strategy)]
    ev = _latest_evolve_overnight(journal)
    vrows = {}
    try:
        vrows = {v["name"]: v for v in variantsmod.rows(journal, active_only=False)}
    except Exception:  # noqa: BLE001
        pass
    for n in names:
        if registry.status(n) != "research":
            continue
        if n in vrows:
            v = vrows[n]
            pooled = (v.get("evidence") or {}).get("pooled") or {}
            out.append({"name": n, "base": v["base"], "kind": "variant", "symbols": v.get("symbols") or [], "pooled": pooled,
                        "rule": (v.get("evidence") or {}).get("rule", "")})
        elif isinstance(ev.get(n), dict) and ev[n].get("passes"):
            out.append({"name": n, "base": n, "kind": "recipe", "symbols": ev[n].get("symbols") or [], "pooled": ev[n].get("pooled") or {},
                        "rule": ev[n].get("rule", "")})
    return out


def _fmt_split(p: dict) -> str:
    f = lambda x: f"{(x or {}).get('exp', 0):+.2f}R n{(x or {}).get('n', 0)}"  # noqa: E731
    return f"train {f(p.get('train'))}, validate {f(p.get('val'))}, test {f(p.get('test'))} PF {(p.get('test') or {}).get('pf', 0)}"


def propose_ready(journal, registry) -> List[int]:
    """File one pending owner decision per ready 24/5 strategy (deduplicated by the registry). Never promotes."""
    ids = []
    for r in readiness(journal, registry):
        detail = (f"[24/5] {r['name']} passed the overnight time-split rule on {', '.join(r['symbols']) or 'its symbols'}: {_fmt_split(r['pooled'])}. "
                  "It trades pre-market, after-hours and the 20:00-04:00 session with limit orders only; stops are watched, "
                  "not resting, so they can fill past the level.")
        proposal = ("Promote to paper with a typed confirmation, and set OVERNIGHT_ENABLED=true so the overnight desk runs. "
                    "Nothing changes until you do.")
        ids.append(registry.propose("tuner", r["name"], ACTIVATE_TITLE.format(name=r["name"]), detail, proposal))
    return ids


def is_desk_strategy(strategy) -> bool:
    return isinstance(strategy, ExtStrategy) or bool(getattr(strategy, "ext_sessions", None))


def trade_is_desk(trade, registry=None) -> bool:
    """A journal trade the desk owns: its notes say ext, or its setup is a registered desk strategy."""
    try:
        notes = json.loads(trade["notes"] or "{}")
    except Exception:  # noqa: BLE001
        notes = {}
    if isinstance(notes, dict) and notes.get("ext"):
        return True
    if registry is not None:
        e = getattr(registry, "_entries", {}).get(trade["setup"])
        return bool(e and is_desk_strategy(e.strategy))
    return False


def size_shares(equity: float, price: float, entry: float, stop: float, risk_pct: float, max_notional_pct: float) -> int:
    """Whole shares: risk budget / per-share risk, capped by the notional cap. 0 when nothing fits."""
    per_share = abs(entry - stop)
    if equity <= 0 or price <= 0 or per_share <= 0:
        return 0
    by_risk = math.floor(equity * risk_pct / per_share)
    by_cash = math.floor(equity * max_notional_pct / price)
    return max(0, min(by_risk, by_cash))


def exit_limit(direction: str, bid: Optional[float], ask: Optional[float], last: float, bps: float) -> float:
    """Marketable exit limit: through the bid (selling a long) or the ask (covering a short) by `bps`."""
    if direction == "long":
        ref = bid if bid else last
        return round(ref * (1 - bps / 1e4), 2)
    ref = ask if ask else last
    return round(ref * (1 + bps / 1e4), 2)


EXIT_MAX_STEPS = 3          # re-price escalation cap: 30, 60, 90, 120 bps through the quote, then hold at 120


def window_key(snap) -> str:
    """One window = one regular close to the next regular open; keyed by that next open's date."""
    d = snap.next_open_et.date() if snap.next_open_et else xs.next_weekday(snap.now_et.date())
    return d.isoformat()


class OvernightDesk:
    def __init__(self, bot):
        self.bot = bot
        self.cfg = bot.cfg
        self.log = bot.log
        self._last_bar: Dict[str, object] = {}
        self._attrs: Dict[str, tuple] = {}            # symbol -> (date, attributes)

    # ---- helpers ---------------------------------------------------------------------
    def strategies(self) -> List[ExtStrategy]:
        reg = self.bot.registry
        return [e.strategy for e in reg._entries.values() if is_desk_strategy(e.strategy)]

    def desk_trades(self, status: str = "open") -> list:
        j = self.bot.journal
        rows = j.open_trades() if status == "open" else j.pending_trades()
        return [t for t in rows if trade_is_desk(t, self.bot.registry)]

    def overnight_ok(self, symbol: str, today: str) -> bool:
        cached = self._attrs.get(symbol)
        if not cached or cached[0] != today:
            try:
                attrs = self.bot.broker.asset_attributes(symbol)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("desk: asset lookup failed for %s: %s", symbol, exc)
                attrs = []
            self._attrs[symbol] = cached = (today, attrs)
        return xs.eligible_sessions(cached[1])["overnight"]

    def can_trade(self, symbol: str, session: str, today: str) -> bool:
        """Orders only go out while a session is open for this symbol (never on weekends or holidays' dead hours)."""
        if session == "closed":
            return False
        if session == "overnight":
            return self.overnight_ok(symbol, today)
        return True

    def _held(self, symbol: str) -> Optional[float]:
        """Shares the broker actually holds (None when the broker cannot be asked)."""
        try:
            pm = self.bot.broker.position_map()
        except Exception:  # noqa: BLE001
            return None
        p = pm.get(symbol)
        return abs(float(p.qty)) if p is not None else 0.0

    def _snapshot(self, symbol: str, now_utc: datetime):
        """ExtSnapshot at the last COMPLETED bar, built by the backtester's own code (parity). Bars are
        split-adjusted like the history, with ~90 days so the Wilder ATR has settled."""
        from .overnight_backtest import make_snapshot, prepare
        bars = self.bot.feed.ext_bars(symbol, self.cfg.bar_minutes, days=90, feed=self.cfg.overnight_feed)
        if bars is None or len(bars) == 0:
            return None, None
        bar_min = self.cfg.bar_minutes
        done = bars[bars.index + timedelta(minutes=bar_min) <= now_utc]
        if len(done) == 0:
            return None, None
        p = prepare(done, getattr(self.cfg, "atr_period", 14))
        if p is None:
            return None, None
        i = len(done) - 1
        return make_snapshot(p, i, symbol, self.cfg, None, bar_min), done.index[-1]

    # ---- the tick ----------------------------------------------------------------------
    def tick(self, now_utc: Optional[datetime] = None) -> None:
        if not self.cfg.overnight_enabled:
            return
        now_utc = now_utc or datetime.now(timezone.utc)
        now_et = now_utc.astimezone(ET)
        session = xs.session_at(now_et)
        safe(self.bot.journal.set_state, "overnight_session", session)
        safe(self.bot.journal.set_state, "overnight_tick", now_utc.isoformat(timespec="seconds"))
        self.poll_pending(now_utc, session)
        self.manage_exits(now_utc, session)
        if session in xs.EXT_SESSIONS:
            self.scan(now_utc, now_et, session)

    # ---- entries -----------------------------------------------------------------------
    def scan(self, now_utc: datetime, now_et: datetime, session: str) -> None:
        bot = self.bot
        today = now_et.date().isoformat()
        strategies = [s for s in self.strategies()
                      if session in (s.knob(self.cfg, "ext_sessions_override", None) or s.ext_sessions)]
        if not strategies:
            return
        ts = now_utc.isoformat(timespec="seconds")
        bar_min = self.cfg.bar_minutes
        # the last bar that can have completed: skip the (heavy) fetch until a new one has
        epoch = int(now_utc.timestamp()) // (bar_min * 60) * (bar_min * 60) - bar_min * 60
        expect = datetime.fromtimestamp(epoch, timezone.utc)
        for symbol in self.cfg.overnight_symbols:
            if self._last_bar.get(symbol + ":due") == expect:
                continue
            self._last_bar[symbol + ":due"] = expect
            try:
                snap, bar_ts = self._snapshot(symbol, now_utc)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("desk: %s snapshot failed: %s", symbol, exc)
                continue
            if snap is None or self._last_bar.get(symbol) == bar_ts:
                continue                                  # decide once per completed bar
            self._last_bar[symbol] = bar_ts
            snap.shortable = bool(getattr(bot, "_shorting", False))
            for strat in strategies:
                self._consider(strat, snap, symbol, session, today, now_utc, ts)

    def _consider(self, strat, snap, symbol, session, today, now_utc, ts) -> None:
        j, reg = self.bot.journal, self.bot.registry
        reject = lambda reason, d=None, detail=None: safe(j.reject, symbol, strat.name, reason, d, detail, now=ts)  # noqa: E731
        why = strat.session_ok(snap, self.cfg)
        if why:
            return
        entry = reg._entries.get(strat.name)
        if entry is not None and symbol not in entry.symbols:
            return
        # one decision per strategy, symbol and window, as in the backtest (an unfilled order uses it up)
        wkey = f"desk_window:{strat.name}:{symbol}:{window_key(snap)}"
        if j.get_state(wkey):
            return
        sig, why = strat.evaluate(snap, self.cfg)
        if sig is None:
            if why:
                reject(f"ext:{why}")
            return
        status = reg.status(strat.name)
        detail = json.dumps({"entry": sig.entry, "stop": sig.stop, "target": sig.target, "exit_by": sig.context.get("exit_by"),
                             "session": session, "tags": list(getattr(strat, "tags", ()))})
        if status == "research":
            safe(j.set_state, wkey, ts)
            reject("research_observed", sig.direction, detail)
            return
        if status not in ("paper", "live"):
            return
        gates = self.gates(sig, symbol, session, today)
        if gates:
            reject(",".join(gates), sig.direction, detail)
            return
        safe(j.set_state, wkey, ts)
        self.enter(sig, snap, session, now_utc)

    def gates(self, sig, symbol: str, session: str, today: str) -> List[str]:
        """Desk gates. Desk trades share the account with the regular-hours book on purpose: they count toward
        the day's trade cap, open positions and realized P&L, and a halt or forced DRY_RUN stops them too."""
        bot = self.bot
        out = []
        if entries_paused(bot.journal):
            out.append("entries_paused")
        if bot.state is not None and bot.state.halted:
            out.append("halted")
        if bot.state is not None and getattr(bot.state, "forced_dry_run", False):
            out.append("forced_dry_run")
        if sig.direction == "short" and not getattr(bot, "_shorting", False):
            out.append("no_shorting")
        if session in ("pre", "ah") and self.cfg.overnight_feed == "iex" and not self.cfg.overnight_allow_iex:
            out.append("thin_feed_iex")               # backtests are on SIP; IEX barely prints outside 09:30-16:00
        desk_open = self.desk_trades("open") + self.desk_trades("pending")
        if len(desk_open) >= self.cfg.overnight_max_positions:
            out.append("desk_max_positions")
        held = {t["symbol"] for t in bot.journal.open_trades()} | {t["symbol"] for t in bot.journal.pending_trades()}
        try:
            held |= set(bot.broker.position_map())
        except Exception:  # noqa: BLE001
            pass
        if symbol in held:
            out.append("symbol_open")
        if not self.can_trade(symbol, session, today):
            out.append("not_overnight_tradable" if session == "overnight" else "session_closed")
        bid, ask = bot.feed.ext_quote(symbol, session, self.cfg.overnight_feed) if bot.feed.connected else (None, None)
        if bid and ask:
            spread_bps = (ask - bid) / ((ask + bid) / 2) * 1e4
            if spread_bps > self.cfg.overnight_max_spread_bps:
                out.append(f"spread_{spread_bps:.0f}bps")
        elif bot.feed.connected:
            out.append("no_quote")
        return out

    def enter(self, sig, snap, session: str, now_utc: datetime) -> Optional[int]:
        bot, j = self.bot, self.bot.journal
        try:
            equity = float(bot.broker.account().equity)       # the regular loop does not refresh equity after hours
        except Exception:  # noqa: BLE001
            equity = bot.state.equity if bot.state else 0.0
        qty = size_shares(equity, snap.price, sig.entry, sig.stop, self.cfg.overnight_risk_pct, self.cfg.overnight_max_notional_pct)
        if qty < 1:
            safe(j.reject, sig.symbol, sig.setup, "ext:size_zero", sig.direction, None, now=now_utc.isoformat(timespec="seconds"))
            return None
        mode = bot.registry.mode_for(sig.setup)
        ctx = dict(sig.context)
        ctx.update({"ext": True, "session": session, "tags": [TAG]})
        tid = j.open_trade(opened_at=now_utc.isoformat(timespec="seconds"), asset="stock", symbol=sig.symbol, setup=sig.setup,
                           direction=sig.direction, regime=session, qty=qty, entry=sig.entry, stop=sig.stop, target=sig.target,
                           max_hold_min=sig.max_hold_min, level_targeted=0.0, rr_planned=sig.rr, mode=mode,
                           status="open" if mode == "DRY_RUN" else "pending", notes=json.dumps(ctx))
        if bot.state is not None:
            bot.state.open_symbols.add(sig.symbol)
            bot.state.trades_today += 1
        if mode == "DRY_RUN":
            self.log.info("desk DRY_RUN #%d %s %s x%d @ %.2f stop %.2f target %.2f exit_by %s", tid, sig.direction, sig.symbol,
                          qty, sig.entry, sig.stop, sig.target, ctx.get("exit_by"))
            return tid
        side = "buy" if sig.direction == "long" else "sell"
        try:
            oid = bot.broker.submit_limit_ext(sig.symbol, qty, side, sig.entry)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("desk: entry submit failed for #%d: %s", tid, exc)
            j.cancel_trade(tid, f"submit_failed:{type(exc).__name__}", now_utc.isoformat(timespec="seconds"))
            if bot.state is not None:
                bot.state.open_symbols.discard(sig.symbol)
            return None
        j.set_order_id(tid, oid)
        self.log.info("desk #%d pending ext limit %s %s x%d @ %.2f (%s)", tid, side, sig.symbol, qty, sig.entry, session)
        return tid

    # ---- pending entries -------------------------------------------------------------------
    def _filled(self, t, qty: float, price: Optional[float], oid: str) -> None:
        j = self.bot.journal
        j.mark_filled(t["id"], price or t["entry"], qty, oid)
        self.log.info("desk #%d filled %.0f @ %.2f", t["id"], qty, price or t["entry"])
        self.place_target(j.get_trade(t["id"]))

    def poll_pending(self, now_utc: datetime, session: str) -> None:
        """The desk owns its fills (the loop's order stream skips desk trades), so a fill always gets its target."""
        bot, j = self.bot, self.bot.journal
        now_iso = now_utc.isoformat(timespec="seconds")
        for t in self.desk_trades("pending"):
            try:
                st = bot.broker.order_state(t["broker_order_id"]) if t["broker_order_id"] else None
                if st is None:
                    if not bot.broker.connected or self.cfg.dry_run:
                        j.cancel_trade(t["id"], "no_broker", now_iso)
                        if bot.state is not None:
                            bot.state.open_symbols.discard(t["symbol"])
                    continue
                if st.status == "filled" or (st.done and st.filled_qty > 0):
                    self._filled(t, st.filled_qty, st.filled_avg_price, st.id)
                    continue
                if st.done:
                    j.cancel_trade(t["id"], f"broker_{st.status}", now_iso)
                    if bot.state is not None:
                        bot.state.open_symbols.discard(t["symbol"])
                    continue
                notes = json.loads(t["notes"] or "{}")
                opened = datetime.fromisoformat(t["opened_at"])
                opened = opened if opened.tzinfo else opened.replace(tzinfo=timezone.utc)
                age = (now_utc - opened).total_seconds() / 60
                if age >= self.cfg.overnight_entry_good_for_min or session != notes.get("session"):
                    self._cancel(t["broker_order_id"])
                    st2 = bot.broker.order_state(t["broker_order_id"]) or st
                    if st2.filled_qty > 0:          # partly filled before the cancel: keep what we hold, manage it
                        self._filled(t, st2.filled_qty, st2.filled_avg_price, st2.id)
                        continue
                    j.cancel_trade(t["id"], "unfilled", now_iso)
                    if bot.state is not None:
                        bot.state.open_symbols.discard(t["symbol"])
                    self.log.info("desk #%d cancelled unfilled after %.0f min", t["id"], age)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("desk: pending poll failed for #%d: %s", t["id"], exc)

    def _cancel(self, oid: Optional[str]) -> None:
        """Cancel, tolerating an order that already finished (Alpaca answers 422 for those)."""
        if not oid:
            return
        try:
            self.bot.broker.cancel_order(oid)
        except Exception as exc:  # noqa: BLE001
            self.log.info("desk: cancel %s: %s", oid, exc)

    def place_target(self, t, qty: Optional[float] = None) -> Optional[str]:
        if t is None or t["mode"] == "DRY_RUN":
            return None
        side = "sell" if t["direction"] == "long" else "buy"
        q = int(qty if qty is not None else t["qty"])
        if q < 1:
            return None
        try:
            oid = self.bot.broker.submit_limit_ext(t["symbol"], q, side, float(t["target"]))
        except Exception as exc:  # noqa: BLE001
            self.log.exception("desk: target order failed for #%d: %s", t["id"], exc)
            return None
        if oid:
            self.bot.journal.patch_notes(t["id"], {"target_order_id": oid})
        return oid

    # ---- exits -------------------------------------------------------------------------------
    def manage_exits(self, now_utc: datetime, session: str) -> None:
        for t in self.desk_trades("open"):
            try:
                self.check_exit(t, now_utc, session)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("desk: exit check failed for #%d: %s", t["id"], exc)

    def _price(self, symbol: str, session: str):
        """(last, bid, ask). Overnight the quote mid from Alpaca's overnight feed is the price (the IEX last trade
        would be a stale regular-hours print); otherwise the last trade, falling back to the mid."""
        feed = self.bot.feed
        if not feed.connected:
            return None, None, None
        bid, ask = feed.ext_quote(symbol, session, self.cfg.overnight_feed)
        mid = (bid + ask) / 2 if (bid and ask) else None
        if session == "overnight":
            return mid, bid, ask
        last = None
        try:
            last = feed.last_price(symbol, self.cfg.max_price_age_sec * 5)
        except Exception:  # noqa: BLE001
            last = None
        return (last if last is not None else mid), bid, ask

    def _close(self, t, price: float, reason: str, oid: Optional[str], now_utc: datetime) -> str:
        self.bot.journal.close_trade(t["id"], price, reason, None, oid, now=now_utc.isoformat(timespec="seconds"))
        self._done(t)
        return reason

    def check_exit(self, t, now_utc: datetime, session: str, price: Optional[float] = None) -> Optional[str]:
        bot, j = self.bot, self.bot.journal
        notes = json.loads(t["notes"] or "{}")
        today = now_utc.astimezone(ET).date().isoformat()
        long_ = t["direction"] == "long"
        live = t["mode"] != "DRY_RUN"
        tgt_oid = notes.get("target_order_id")
        tgt_st = bot.broker.order_state(tgt_oid) if (live and tgt_oid) else None
        ex = notes.get("exit_order")
        ex_st = bot.broker.order_state(ex["oid"]) if (live and ex and ex.get("oid")) else None
        held = self._held(t["symbol"]) if live else None
        # 1. the position is gone at the broker: whatever filled closed it (target or our exit)
        if live and held == 0.0:
            if ex_st is not None and ex_st.filled_qty > 0 and ex_st.filled_avg_price:
                return self._close(t, ex_st.filled_avg_price, ex["reason"], ex["oid"], now_utc)
            if tgt_st is not None and tgt_st.filled_qty > 0 and tgt_st.filled_avg_price:
                return self._close(t, tgt_st.filled_avg_price, "target", tgt_oid, now_utc)
            return self._close(t, float(t["target"] if tgt_st and tgt_st.filled_qty else t["entry"]), "broker_no_position", None, now_utc)
        # 2. an exit already working: wait, then re-price what is still held (capped escalation)
        if live and ex:
            if ex_st is not None and not ex_st.done and (now_utc - datetime.fromisoformat(ex["at"])).total_seconds() < self.cfg.overnight_reprice_sec:
                return None
            if not self.can_trade(t["symbol"], session, today):
                return None                     # nothing trades now; the next session's first tick re-prices
            self._cancel(ex.get("oid"))
            return self._send_exit(t, now_utc, session, ex["reason"], min(int(ex.get("n", 0)) + 1, EXIT_MAX_STEPS), held)
        # 3. a target that expired (DAY orders end at 20:00) or was cancelled: put it back when the symbol can trade
        if live and tgt_oid and tgt_st is not None and tgt_st.done and tgt_st.status != "filled" and self.can_trade(t["symbol"], session, today):
            self.place_target(t, held if held is not None else None)
        # 4. decide
        last, bid, ask = (price, None, None) if price is not None else self._price(t["symbol"], session)
        exit_by = notes.get("exit_by")
        opened = datetime.fromisoformat(t["opened_at"])
        opened = opened if opened.tzinfo else opened.replace(tzinfo=timezone.utc)
        reason = None
        if exit_by and now_utc >= datetime.fromisoformat(exit_by).astimezone(timezone.utc):
            reason = "time"
        elif (now_utc - opened).total_seconds() / 60 >= t["max_hold_min"]:
            reason = "time"
        elif last is not None and ((last <= t["stop"]) if long_ else (last >= t["stop"])):
            reason = "stop"
        elif not live and last is not None and ((last > t["target"]) if long_ else (last < t["target"])):
            reason = "target"
        if reason is None:
            return None
        if not live:
            px = t["target"] if reason == "target" else (last if last is not None else t["entry"])
            return self._close(t, px, reason, None, now_utc)
        if not self.can_trade(t["symbol"], session, today):
            return None                         # weekend / dead hours: the exit goes out when a session opens
        if tgt_oid and (tgt_st is None or not tgt_st.done):
            self._cancel(tgt_oid)
        return self._send_exit(t, now_utc, session, reason, 0, held, last, bid, ask)

    def _send_exit(self, t, now_utc, session, reason, n, held=None, last=None, bid=None, ask=None) -> Optional[str]:
        qty = int(held) if held is not None else int(t["qty"])
        if qty < 1:
            return None
        if last is None:
            last, bid, ask = self._price(t["symbol"], session)
        if last is None:
            self.log.warning("desk #%d: %s exit wanted but no price; retrying next tick", t["id"], reason)
            return None
        bps = self.cfg.overnight_exit_limit_bps * (1 + n)       # each re-price reaches further, up to the cap
        px = exit_limit(t["direction"], bid, ask, last, bps)
        side = "sell" if t["direction"] == "long" else "buy"
        oid = self.bot.broker.submit_limit_ext(t["symbol"], qty, side, px)
        # persisted, so a restart picks up the working exit instead of sending a second one
        self.bot.journal.patch_notes(t["id"], {"exit_order": {"oid": oid, "at": now_utc.isoformat(timespec="seconds"),
                                                              "reason": reason, "n": n}})
        self.log.info("desk #%d %s exit: limit %s x%d @ %.2f (step %d)", t["id"], reason, side, qty, px, n + 1)
        return None

    def _done(self, t) -> None:
        st = self.bot.state
        if st is not None:
            st.open_symbols.discard(t["symbol"])
            st.realized_pnl_today = self.bot.journal.realized_pnl_today(datetime.now(ET).date().isoformat())
