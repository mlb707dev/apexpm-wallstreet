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

import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from . import risk as riskmod
from .broker import Broker, OptionsBroker
from .config import Config
from .data import DataFeed
from .db import Journal, et_date
from .exits import ExitPlan, update as exit_update
from .levels import LevelMap, levels_from_daily
from .log import get_logger, safe
from . import calendar as cal
from .conviction import score as conviction_score
from .profiles import positions_exception, EffectiveConfig, entries_paused, get_profile
from .registry import Registry
from .ribbon import ribbon_from_closes
from .sizing import size_trade
try:
    from .weekly import maybe_weekly
except ImportError:  # the weekly letter is part of the private build
    def maybe_weekly(*_args, **_kwargs):
        return None
from .stream import FastLayer
from .news import NewsFeed
from .options_exec import OptionsWrapper, size_contracts
from .strategies import ExampleBreakout
from .overnight import OvernightDesk, is_desk_strategy, trade_is_desk
from . import variants
from .strategies.base import MarketSnapshot, Signal
from .webhook import WebhookServer

ET = ZoneInfo("America/New_York")


def snapshot_bar_fields(ltf) -> dict:
    """Current-bar and prior-bar fields for a MarketSnapshot, from an intraday frame (oldest → newest).
    The backtester fills these; the live loop must fill the same ones or strategies that look one or
    two bars back (and the volume confirmations) can never fire. Found as a live fault 2026-11-04."""
    n = len(ltf)
    last = ltf.iloc[-1]
    c = float(last["close"])
    out = {"bar_open": float(last["open"]) if "open" in ltf else c, "bar_high": float(last["high"]) if "high" in ltf else c,
           "bar_low": float(last["low"]) if "low" in ltf else c,
           "prev_close": float(ltf["close"].iloc[-2]) if n >= 2 else 0.0,
           "prev2_close": float(ltf["close"].iloc[-3]) if n >= 3 else 0.0,
           "bar_volume": float(last["volume"]) if "volume" in ltf else 0.0,
           "vol_avg20": float(ltf["volume"].iloc[-21:-1].mean()) if "volume" in ltf and n > 21 else 0.0}
    return out



def vix_last_and_change(h):
    """(last, change vs the PREVIOUS SESSION'S CLOSE) from intraday ^VIX bars.

    The old code used the first bar of the 2-day window, i.e. early yesterday, as "previous close":
    on 2026-11-10 that showed 19.83 (-5.00) when the real change vs Nov 9's 19.92 close (Cboe) was -0.09."""
    if h is None or len(h) == 0:
        return None, None
    closes = h["Close"] if "Close" in h else h["close"]
    last = float(closes.iloc[-1])
    days = [ts.date() for ts in closes.index]
    today = days[-1]
    prev = [float(c) for c, d in zip(closes, days) if d < today]
    return last, (last - prev[-1]) if prev else None

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
        variants.register_all(self.registry, self.journal)   # tuned challengers (shadow book)
        self.obroker = OptionsBroker(cfg, self.broker)
        if cfg.options_enabled:
            chain = (lambda u, a, b: self.obroker.chain(u, a, b)) if self.obroker.connected else (lambda u, a, b: [])
            self.registry.register(OptionsWrapper(ExampleBreakout(), chain, min_dte=cfg.opt_min_dte, max_dte=cfg.opt_max_dte), "research")
        # extended-hours strategies (ext_sessions) register here and run on the overnight desk only
        self.webhook: Optional[WebhookServer] = None
        self.news = NewsFeed(cfg, self.journal) if cfg.news_enabled else None
        self.state: Optional[riskmod.RiskState] = None
        self._levels: Dict[str, LevelMap] = {}
        self._pd_range: Dict[str, tuple] = {}      # symbol -> (prior-day high, prior-day low)
        self._reached: Dict[str, Dict[str, float]] = {}
        self._today = ""
        self._day_ctx: Optional[cal.DayContext] = None
        self._momentum: Dict[str, bool] = {}
        self._exit_lock = threading.RLock()
        self.fast: Optional[FastLayer] = None
        self.overnight = OvernightDesk(self)
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
        reanchored = False
        if anchor is None:
            anchor = str(acct.equity)
            self.journal.set_state(anchor_key, anchor)
        elif float(anchor) > 0 and abs(acct.equity - float(anchor)) / float(anchor) > 0.5 \
                and self.journal.realized_pnl_today(today) == 0.0 and not self.journal.open_trades():
            # Stored anchor is from a different account state (paper reset, new key, deposit): re-anchor at startup too.
            self.log.warning("startup: equity %.2f far from stored anchor %s with no trades today; re-anchoring", acct.equity, anchor)
            anchor = str(acct.equity)
            self.journal.set_state(anchor_key, anchor)
            reanchored = True
        self.journal.set_state("starting_equity", anchor)
        if self.journal.get_state("ladder_start") is None:
            self.journal.set_state("ladder_start", anchor)   # where the capital ladder began (shown as "Started with")
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
        # A paper reset seen at startup (new key, reset account) starts a new ladder, as it does mid-session;
        # a dashboard request made while the bot was down is honoured here too.
        if reanchored and self.cfg.paper:
            self._ladder_reset(acct.equity, today, "paper account reset detected at startup")
        if self.journal.get_state("ladder_reset_requested") == "1":
            self.journal.set_state("ladder_reset_requested", "0")
            self._ladder_reset(acct.equity, today, "owner request (dashboard)")
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
        daily = self.feed.daily(symbol, lookback_days=600)
        if len(daily) < self.cfg.atr_period + 1:
            self.log.warning("%s: not enough daily bars (%d)", symbol, len(daily))
            return None
        try:
            lv = levels_from_daily(daily, self.cfg.atr_period)
        except ValueError as exc:
            self.log.warning("%s: levels unavailable: %s", symbol, exc)
            return None
        self._levels[symbol] = lv
        try:   # same row the anchor comes from (levels_from_daily uses the last daily bar)
            self._pd_range[symbol] = (float(daily["high"].iloc[-1]), float(daily["low"].iloc[-1]))
        except Exception:  # noqa: BLE001
            self._pd_range[symbol] = (0.0, 0.0)
        extra = {"day": lv.as_dict()}
        try:
            weekly = daily.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
            monthly = daily.resample("ME").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
            now_et = datetime.now(timezone.utc).astimezone(ET)
            # completed periods only
            weekly = weekly[weekly.index.date <= now_et.date() - timedelta(days=now_et.weekday() + 1)]
            monthly = monthly[monthly.index.to_period("M") < pd.Period(now_et.date(), "M")]
            for name, frame in (("week", weekly), ("month", monthly)):
                if len(frame) >= self.cfg.atr_period + 1:
                    extra[name] = levels_from_daily(frame, self.cfg.atr_period).as_dict()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("%s: multi-timeframe levels unavailable: %s", symbol, exc)
        try:
            v = daily["volume"]
            safe(self.journal.set_state, f"vol:{symbol}", json.dumps({
                "avg5": float(v.tail(5).mean()), "avg30": float(v.tail(30).mean()), "prev": float(v.iloc[-1])}))
        except Exception:  # noqa: BLE001
            pass
        self.journal.save_plan(date=today, symbol=symbol, anchor=lv.anchor, atr=lv.atr,
                               levels_json=json.dumps(extra))
        self.log.info("%s levels anchor=%.2f atr=%.2f bands=%s", symbol, lv.anchor, lv.atr, sorted(lv.up))
        return lv

    # ---- one scan ---------------------------------------------------------
    def snapshot(self, symbol: str, today: str, now_utc: datetime) -> Optional[MarketSnapshot]:
        lv = self._levels_for(symbol, today)
        if lv is None:
            return None
        # Indicators run on regular-hours bars, exactly like the backtest history; the extended-hours
        # bars are kept only for the pre-market levels. Crypto trades around the clock: no filter.
        from . import sessions as sess
        crypto = "/" in symbol
        ltf_all = self.feed.intraday(symbol, self.cfg.bar_minutes, lookback_bars=300)
        ltf = ltf_all if crypto else sess.rth(ltf_all).tail(120)
        htf_raw = self.feed.intraday(symbol, self.cfg.htf_bar_minutes, lookback_bars=200)
        htf = htf_raw if crypto else sess.rth(htf_raw).tail(120)
        if len(ltf) < self.cfg.ema_slow or len(htf) < self.cfg.ema_slow:
            self.log.warning("%s: not enough intraday bars", symbol)
            return None
        price = (self.fast.prices.get(symbol, 5) if self.fast else None) \
            or self.feed.last_price(symbol, self.cfg.max_price_age_sec) or float(ltf["close"].iloc[-1])
        day_open = 0.0
        try:
            if hasattr(ltf.index, "tz_convert"):
                et_idx = ltf.index.tz_convert(ET)
                today_mask = et_idx.date == now_utc.astimezone(ET).date()
                tb = ltf[today_mask]
                if len(tb):
                    day_open = float(tb["open"].iloc[0]) if "open" in tb else float(tb["close"].iloc[0])
                    if "volume" in tb:
                        safe(self.journal.set_state, f"vol_today:{symbol}", str(float(tb["volume"].sum())))
                    safe(self.journal.set_state, f"range:{symbol}", json.dumps({
                        "open": float(tb["open"].iloc[0]) if "open" in tb else float(tb["close"].iloc[0]),
                        "high": float(tb["high"].max()) if "high" in tb else float(tb["close"].max()),
                        "low": float(tb["low"].min()) if "low" in tb else float(tb["close"].min()),
                        "last": price}))
        except Exception:  # noqa: BLE001
            pass
        rl = ribbon_from_closes(ltf["close"].values, self.cfg.ema_fast, self.cfg.ema_mid, self.cfg.ema_slow)
        rh = ribbon_from_closes(htf["close"].values, self.cfg.ema_fast, self.cfg.ema_mid, self.cfg.ema_slow)
        prev_rl = prev2_rl = None
        if len(ltf) > self.cfg.ema_slow + 2:
            prev_rl = ribbon_from_closes(ltf["close"].values[:-1], self.cfg.ema_fast, self.cfg.ema_mid, self.cfg.ema_slow)
            prev2_rl = ribbon_from_closes(ltf["close"].values[:-2], self.cfg.ema_fast, self.cfg.ema_mid, self.cfg.ema_slow)

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
        bars = snapshot_bar_fields(ltf)
        cols = ["open", "high", "low", "close"]
        ltf_ohlc = ltf[cols].values[-121:].astype(float) if all(c in ltf for c in cols) else None
        htf_ohlc = htf[cols].values[-80:].astype(float) if all(c in htf for c in cols) else None
        pd_hi, pd_lo = self._pd_range.get(symbol, (0.0, 0.0))
        # session structure (pre-market range, opening range) and optional private overlays
        try:
            sl = sess.levels_for_day(ltf_all, now_et.date(), now_et)
        except Exception:  # noqa: BLE001
            sl = {"pm_high": 0.0, "pm_low": 0.0, "or_high": 0.0, "or_low": 0.0}
        if any(sl.values()):
            safe(self.journal.set_state, f"session_levels:{symbol}", json.dumps(sl))
        g = None
        if self.cfg.gex_enabled and "/" not in symbol:
            try:
                from . import gex as gexmod
                g = gexmod.refresh(self.journal, self.broker, getattr(self, "obroker", None), symbol, price)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("gex unavailable for %s: %s", symbol, exc)
        return MarketSnapshot(symbol=symbol, price=price, levels=lv, ribbon_ltf=rl, ribbon_htf=rh,
                              ltf_closes=list(ltf["close"].values), regime=regime,
                              reached_today=prev_reached, bar_index_today=max(0, mins) // self.cfg.bar_minutes,
                              minutes_since_open=mins, prev_ribbon_ltf=prev_rl, prev2_ribbon_ltf=prev2_rl,
                              ltf_ohlc=ltf_ohlc, htf_ohlc=htf_ohlc, pd_high=pd_hi, pd_low=pd_lo, day_open=day_open,
                              pm_high=sl["pm_high"], pm_low=sl["pm_low"], or_high=sl["or_high"], or_low=sl["or_low"],
                              session=sess.session_name(now_et), gex=g if (g and g.get("ok")) else None, **bars)

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
        self._buying_power = float(getattr(acct, "buying_power", 0.0) or 0.0)
        # Account reset detection: equity far from the anchor with no trades closed today
        # is a paper reset (or a deposit/withdrawal), not a drawdown. Re-anchor instead of halting.
        anchor = self.state.starting_equity
        if anchor > 0 and abs(acct.equity - anchor) / anchor > 0.5 and self.journal.realized_pnl_today(today) == 0.0 \
                and not self.journal.open_trades():
            self.log.warning("account reset detected: equity %.2f vs anchor %.2f; re-anchoring", acct.equity, anchor)
            self.state.starting_equity = acct.equity
            self.journal.set_state(f"starting_equity:{today}", str(acct.equity))
            self.journal.set_state("starting_equity", str(acct.equity))
            if self.cfg.paper:
                self._ladder_reset(acct.equity, today, "paper account reset detected")
        if self.journal.get_state("ladder_reset_requested") == "1":
            self.journal.set_state("ladder_reset_requested", "0")
            self._ladder_reset(acct.equity, today, "owner request (dashboard)")
        self._shorting = bool(getattr(acct, "shorting_enabled", True))
        band = EffectiveConfig(self.cfg, get_profile(self.journal, self.state.equity), 1.0, self.state.equity)
        self._band = band
        ks = riskmod.check_kill_switch(self.state, band.kill_dd_pct)
        if ks:
            self.journal.set_state(f"halted:{today}", "1")
            self.log.error("HALT: %s", ks)
            return []
        if self.state.halted:
            return []
        self._banger = self._banger_check(today)
        if self._banger.halt:
            self.journal.set_state(f"halted:{today}", "1")
            self.state.halted, self.state.halt_reason = True, "banger_floor"
            self.log.error("HALT: banger floor touched (day gain %.2f <= floor %.2f, peak %.2f)",
                           self.state.realized_pnl_today, self._banger.floor, self._banger.peak_gain)
            with self.journal.tx() as c:
                c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'portfolio', 'banger_halt', ?)",
                          (f"gain {self.state.realized_pnl_today:.2f} floor {self._banger.floor:.2f} peak {self._banger.peak_gain:.2f}",))
            return []
        if band.max_daily_loss_pct > 0:
            cap = min(self.cfg.max_daily_loss, self.state.starting_equity * band.max_daily_loss_pct)
            dl = riskmod.check_daily_loss(self.state, cap)
        else:
            dl = None   # hyper band: in-trade stops are the protection; no daily cap
        if dl:
            self.log.warning("forced DRY_RUN for today: %s", dl)

        self._poll_pending(now_utc)
        self._track_vix(now_utc)
        if self.news:
            self.news.refresh(now_utc)

        taken: List[Signal] = []
        for symbol in self.cfg.symbols:
            try:
                taken += self._scan_symbol(symbol, today, now_utc, now_et)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("%s scan failed: %s", symbol, exc)
        # crypto: shadow book only (never ordered), scanned in the same session window so the
        # journal can compare like with like; the weekly review flags it if it is working
        for symbol in self.cfg.crypto_symbols:
            try:
                self._scan_symbol(symbol, today, now_utc, now_et, shadow_only=True)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("%s shadow scan failed: %s", symbol, exc)
        return taken

    _vix_at: Optional[datetime] = None

    def _track_vix(self, now_utc: datetime) -> None:
        """VIX for direction/regime context (not tradable on Alpaca). Refreshed every 5 minutes via yfinance;
        failures are logged once and never block the scan."""
        if not self.cfg.track_vix:
            return
        if self._vix_at and (now_utc - self._vix_at).total_seconds() < 300:
            return
        self._vix_at = now_utc
        try:
            import yfinance as yf
            h = yf.Ticker("^VIX").history(period="2d", interval="5m")
            if h is None or h.empty:
                return
            last, chg = vix_last_and_change(h)
            if last is None:
                return
            safe(self.journal.set_state, "vix", json.dumps({"last": round(last, 2), "chg": round(chg, 2) if chg is not None else None,
                                                           "ts": now_utc.isoformat(timespec="seconds")}))
        except Exception as exc:  # noqa: BLE001
            self.log.warning("vix fetch failed: %s", exc)

    def _ladder_reset(self, equity: float, today: str, why: str) -> None:
        """An account reset (or an unexplained equity jump) is a CASH MOVEMENT, not a new history
        (owner, 2026-11-07): the difference between the new equity and the old baseline goes into the
        cash ledger, which shifts the baselines (ladder_start, starting equity). Every trade stays, every
        statistic keeps counting it. The day's profit-lock peak restarts because the day's gain did."""
        from . import ledger
        old = float(self.journal.get_state("ladder_start") or self.journal.get_state("starting_equity") or equity)
        diff = round(equity - old, 2)
        if diff != 0:
            ledger.record(self.journal, diff, "reset", note=why, source="reset" if "owner" in why else "detected", today=today)
        # the ledger shifted ladder_start/starting_equity by diff; pin today's anchor to the live equity exactly
        self.journal.set_state("ladder_start", str(equity))
        self.journal.set_state(f"starting_equity:{today}", str(equity))
        self.journal.set_state("starting_equity", str(equity))
        self.journal.set_state(f"banger_peak:{today}", "0")
        self.journal.set_state(f"banger_active:{today}", "")
        if self.state is not None:
            self.state.starting_equity = equity
        with self.journal.tx() as c:
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'portfolio', 'ladder_reset', ?)",
                      (f"{why}: baseline moves to {equity:.2f} (cash movement {diff:+.2f} recorded; trade history untouched)",))
        self.log.warning("BASELINE MOVED to %.2f (%s; cash movement %+.2f)", equity, why, diff)

    def _maybe_ledger_sync(self) -> None:
        """Hourly: pull deposits/withdrawals from the broker into the cash ledger (baselines shift, trades stay)."""
        from . import ledger
        last = self.journal.get_state("ledger_synced_at")
        try:
            if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 3600:
                return
        except ValueError:
            pass
        r = ledger.sync_broker(self.journal, self.broker, today=et_date(datetime.now(timezone.utc)))
        if r.get("added"):
            self.log.warning("cash ledger: %d new movement(s) from the broker; baselines shifted", r["added"])
            if self.state is not None:
                try:
                    self.state.starting_equity = float(self.journal.get_state(f"starting_equity:{et_date(datetime.now(timezone.utc))}") or self.state.starting_equity)
                except ValueError:
                    pass

    def _banger_check(self, today: str) -> riskmod.BangerDecision:
        """Day gain = realized + unrealized today; peak and active flag persist per session date."""
        unreal = 0.0
        try:
            for t in self.journal.open_trades():
                px = self.fast.prices.get(t["symbol"], 30) if self.fast else None
                if px:
                    sign = 1 if t["direction"] == "long" else -1
                    mult = 100 if (t["asset"] or "stock") == "option" else 1
                    unreal += (px - t["entry"]) * (1 if mult == 100 else sign) * t["qty"] * mult
        except Exception:  # noqa: BLE001
            unreal = 0.0
        gain = self.state.realized_pnl_today + unreal
        prev_peak = float(self.journal.get_state(f"banger_peak:{today}", "0") or 0)
        was_active = self.journal.get_state(f"banger_active:{today}") == "1"
        band = getattr(self, "_band", None)
        trigger = band.banger_trigger_pct if band is not None else self.cfg.banger_trigger_pct
        d = riskmod.banger_update(gain, self.state.starting_equity, prev_peak, was_active,
                                  trigger_pct=trigger, floor_pct=self.cfg.banger_floor_pct,
                                  size_mult=self.cfg.banger_size_mult, conviction_min=self.cfg.banger_conviction_min)
        if d.peak_gain != prev_peak:
            safe(self.journal.set_state, f"banger_peak:{today}", str(d.peak_gain))
        if d.active and not was_active:
            safe(self.journal.set_state, f"banger_active:{today}", "1")
            self.log.warning("BANGER MODE: day gain %.2f >= %.0f%% of %.2f; floor %.2f, size x%.2f, conviction >= %d",
                             gain, self.cfg.banger_trigger_pct * 100, self.state.starting_equity, d.floor, d.size_mult, d.conviction_min)
            with self.journal.tx() as c:
                c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'portfolio', 'banger_mode_on', ?)",
                          (f"gain {gain:.2f} of {self.state.starting_equity:.2f}",))
        return d

    def _scan_symbol(self, symbol: str, today: str, now_utc: datetime, now_et: datetime, shadow_only: bool = False) -> List[Signal]:
        assert self.state is not None
        taken: List[Signal] = []
        ctx = self._day_ctx or cal.day_context(now_et.date(), self.cfg.symbols, self.journal)
        banger = getattr(self, "_banger", None)
        size_mult = ctx.size_mult * (banger.size_mult if banger and banger.active else 1.0)
        ecfg = EffectiveConfig(self.cfg, get_profile(self.journal, self.state.equity), size_mult, self.state.equity,
                               positions_exception_on=positions_exception(self.journal))
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
        if riskmod.past_cutoff(now_et, self.cfg.no_entry_after) or (now_et.hour * 60 + now_et.minute) >= self._close_minutes(now_et) - 30:
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

        active = [] if shadow_only else self.registry.active(symbol)
        observing = self.registry.observing(symbol) if not shadow_only else \
            [e.strategy for e in self.registry._entries.values() if not getattr(e.strategy, "inner", None)]
        for strat in active + observing:
            if is_desk_strategy(strat):
                continue                      # extended-hours recipes run on the overnight desk only
            observe_only = strat in observing
            why = strat.session_ok(snap, self.cfg)
            if why:
                reject(symbol, strat.name, why)
                continue
            sig, why = strat.evaluate(snap, self.cfg)
            if sig is None:
                if why not in ("not_at_entry_level",):
                    reject(symbol, strat.name, why)
                continue
            # conviction gate: several independent confirmations or no trade (journaled either way)
            conv = conviction_score(sig.direction, snap, snap.ltf_closes)
            # (private live-only overlays may adjust the read here; not part of the public build)
            sig.context["session"] = snap.session
            sig.context["conviction"] = conv.score
            sig.context["confirmations"] = conv.fired
            if observe_only:
                reject(symbol, strat.name, "research_observed", sig.direction,
                       json.dumps({"entry": sig.entry, "stop": sig.stop, "target": sig.target, "conviction": conv.score}))
                continue
            conv_min = max(strat.conviction_min(self.cfg), banger.conviction_min if banger and banger.active else 0)
            if conv.score < conv_min:
                reject(symbol, strat.name, f"low_conviction:{conv.score}" + (":banger" if banger and banger.active else ""),
                       sig.direction, json.dumps(conv.factors))
                continue
            gates = riskmod.entry_gates(self.state, symbol=symbol, direction=sig.direction, now=now_utc,
                                        last_stop_iso=self.journal.last_stop_time(symbol, sig.direction), cfg=ecfg)
            if any(t["symbol"] == symbol for t in self.journal.pending_trades()):
                gates.append("pending_order")
            if gates:
                reject(symbol, strat.name, ",".join(gates), sig.direction)
                continue
            if sig.context.get("asset") == "option":
                n, why_sz = size_contracts(self.state.equity, sig.entry, self.cfg.opt_premium_stop,
                                           ecfg.max_pos_pct, self.cfg.opt_max_premium_pct,
                                           at_least_one=(ecfg.profile == "hyper"))
                if n < 1:
                    reject(symbol, strat.name, f"size:{why_sz}", sig.direction)
                    continue
                from .sizing import SizeResult
                sz = SizeResult(float(n), sig.entry * 100 * self.cfg.opt_premium_stop * n, 0.0, ecfg.max_pos_pct, 1.0, sig.entry * 100 * n)
                self._enter(sig, sz, snap, now_utc, webhook_agreed=None)
                taken.append(sig)
                continue
            pnls = [t["pnl"] for t in self.journal.closed_trades(strat.name) if t["pnl"] is not None]
            sz = size_trade(equity=self.state.equity, price=snap.price, entry=sig.entry, stop=sig.stop,
                            target=sig.target, atr=snap.levels.atr, closed_pnls=pnls, cfg=ecfg)
            if sz.qty <= 0:
                reject(symbol, strat.name, f"size:{sz.reason}", sig.direction)
                continue
            cm = ecfg.confluence_mult(max(int(sig.context.get("confluence", 1)), int(sig.context.get("conviction", 1))))
            if cm != 1.0:
                cap = min(self.cfg.max_pos_dollars, self.state.equity * self.cfg.max_notional_pct) / snap.price
                qty = min(sz.qty * cm, cap)
                qty = round(qty, 3) if self.cfg.fractional_shares else float(int(qty))
                sz = sz.__class__(qty, sz.dollar_risk * cm, sz.kelly_full, sz.kelly_used, sz.vol_scale, qty * snap.price)
                sig.context["confluence_mult"] = cm
            # earned size: more only after the strategy has proven itself on filled trades (apexpm/earned.py)
            try:
                from . import earned as earnedmod
                el = earnedmod.mult_for(self.journal, strat.name, self.cfg)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("earned size unavailable for %s: %s", strat.name, exc)
                el = {"mult": 1.0, "tier": 0, "name": "proving", "guard": None}
            sig.context["earned_tier"] = el["tier"]
            sig.context["earned_mult"] = el["mult"]
            if el["mult"] != 1.0 and sz.qty > 0:
                cap = min(self.cfg.max_pos_dollars, self.state.equity * self.cfg.max_notional_pct) / snap.price
                qty = min(sz.qty * el["mult"], cap)
                qty = round(qty, 3) if self.cfg.fractional_shares else float(int(qty))
                if qty > 0:
                    sz = sz.__class__(qty, sz.dollar_risk * qty / sz.qty, sz.kelly_full, sz.kelly_used, sz.vol_scale, qty * snap.price)
            # buying power: a second position in a cash account can only use what the first left.
            # Size down to it rather than let the broker reject the order (a wasted signal).
            bp = float(getattr(self, "_buying_power", 0.0) or 0.0)
            if bp > 0 and sz.qty * snap.price > bp * 0.98:
                qty = bp * 0.98 / snap.price
                qty = round(qty, 3) if self.cfg.fractional_shares else float(int(qty))
                if qty * snap.price < self.cfg.min_notional or qty <= 0:
                    reject(symbol, strat.name, "size:buying_power", sig.direction, json.dumps({"buying_power": bp, "wanted": sz.notional}))
                    continue
                self.log.info("%s sized down to buying power: %.3f -> %.3f shares (bp %.2f)", symbol, sz.qty, qty, bp)
                sz = sz.__class__(qty, sz.dollar_risk * qty / sz.qty, sz.kelly_full, sz.kelly_used, sz.vol_scale, qty * snap.price)
                sig.context["sized_to_buying_power"] = True
            if sig.direction == "short" and not getattr(self, "_shorting", True):
                reject(symbol, strat.name, "short:not_enabled", sig.direction)   # cash account: broker would refuse
                continue
            if sig.direction == "short" and self.cfg.fractional_shares and float(sz.qty) != int(sz.qty):
                reject(symbol, strat.name, "fractional_short_not_allowed", sig.direction)
                continue
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
        is_opt = sig.context.get("asset") == "option"
        tid = self.journal.open_trade(
            opened_at=now_utc.isoformat(timespec="seconds"),
            asset="option" if is_opt else "stock", contract=sig.context.get("contract") if is_opt else None,
            symbol=sig.symbol, setup=sig.setup, direction=sig.direction, regime=snap.regime,
            qty=sz.qty, entry=sig.entry, stop=sig.stop, target=sig.target,
            max_hold_min=sig.max_hold_min, level_targeted=sig.level_targeted,
            rr_planned=sig.rr, vol_scale=sz.vol_scale, kelly_used=sz.kelly_used,
            mode=mode, status="open" if dry else "pending", notes=json.dumps(sig.context),
            webhook_agreed=None if webhook_agreed is None else int(webhook_agreed))
        self.state.open_symbols.add(sig.symbol)
        if dry:
            self.state.trades_today += 1
            self.log.info("DRY_RUN trade #%d would %s %s x%s @ %.2f stop=%.2f target=%.2f rr=%.2f",
                          tid, side, sig.symbol, qty, sig.entry, sig.stop, sig.target, sig.rr)
            return
        # 2. then the order
        try:
            if is_opt:
                oid = self.obroker.buy_to_open(sig.context["contract"], int(sz.qty))   # long calls/puts only, for now
                self._snap_greeks(tid, sig.context["contract"], "entry")
            elif sig.context.get("fill_at_market"):
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
        self.state.trades_today += 1
        self.journal.set_order_id(tid, oid)
        self.log.info("trade #%d pending (%s) order=%s", tid, mode, oid)

    def _poll_pending(self, now_utc: datetime) -> None:
        """Promote filled orders; cancel stale ones."""
        assert self.state is not None
        now_et = now_utc.astimezone(ET)
        now_iso = now_utc.isoformat(timespec="seconds")
        for t in self.journal.pending_trades():
            if trade_is_desk(t, self.registry):
                continue                      # the overnight desk manages its own orders
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
            if trade_is_desk(t, self.registry):
                continue                      # limit-only exits on the overnight desk
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
            if t["symbol"] != symbol or (t["asset"] or "stock") != "stock" or trade_is_desk(t, self.registry):
                continue   # option positions are marked on the slow loop (REST), never per tick
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
                    if t["broker_order_id"] != ev["order_id"] or trade_is_desk(t, self.registry):
                        continue          # the overnight desk owns its fills (it places the target on fill)
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
        t = self.journal.get_trade(t["id"])          # re-read under the lock: another thread may have closed it
        if t is None or t["status"] != "open":
            return
        if t["asset"] == "option":
            return self._check_option_exit(t, now_utc, now_et, today)
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
            try:
                exit_oid = self.broker.close_position(t["symbol"])
            except Exception as exc:  # noqa: BLE001
                if "position" in str(exc).lower() or "404" in str(exc):
                    self.log.warning("trade #%d: broker holds no %s position; closing journal row (%s)", t["id"], t["symbol"], exc)
                    reason = "broker_no_position"
                else:
                    raise
            exit_price = self._fill_price(exit_oid, exit_price)
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

    def _maybe_warden(self) -> None:
        """Every 30 minutes: the warden checks every harness role and repairs what it can."""
        from . import warden
        if not warden.due(self.journal):
            return
        try:
            w = warden.patrol(self.journal, self.registry, self.cfg, bot=self)
            failed = [c["duty"] for c in w["checks"] if not c["ok"]]
            repaired = [c["duty"] for c in w["checks"] if c["repaired"]]
            self.log.info("warden: %s%s%s", "all duties done" if w["ok"] else "FAILING " + ", ".join(failed),
                          f"; repaired {', '.join(repaired)}" if repaired else "", "")
        except Exception as exc:  # noqa: BLE001
            self.log.exception("warden failed: %s", exc)

    def _close_minutes(self, now_et: datetime) -> int:
        """Minutes after midnight ET of today's close (13:00 on early-close days)."""
        ctx = self._day_ctx
        if ctx and any(e["type"] == "half_day" for e in ctx.events):
            return 13 * 60
        return 16 * 60

    def _fill_price(self, order_id: Optional[str], fallback: Optional[float]) -> Optional[float]:
        """Best-effort real fill price: poll the order a few times before falling back."""
        if not order_id:
            return fallback
        for _ in range(4):
            try:
                st = self.broker.order_state(order_id)
                if st and st.filled_avg_price:
                    return st.filled_avg_price
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.5)
        return fallback

    def _snap_greeks(self, tid: int, occ: str, when: str) -> None:
        """Record delta/gamma/theta/vega/IV and VIX on the journal row at purchase and at sale,
        so the reports can say what the greeks looked like when we bought and when we sold."""
        try:
            g = self.obroker.greeks(occ) if self.obroker.connected else None
            vix = json.loads(self.journal.get_state("vix") or "{}")
            patch = {f"greeks_{when}": g, f"vix_{when}": vix.get("last")}
            self.journal.patch_notes(tid, patch)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("greeks snapshot (%s) failed for trade #%d: %s", when, tid, exc)

    def _check_option_exit(self, t, now_utc: datetime, now_et: datetime, today: str) -> None:
        """Option position: premium stop/target with the runner engine on premium,
        plus the underlying's stop/target, max hold, flat-by, and expiry-day cutoff."""
        opened = datetime.fromisoformat(t["opened_at"])
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        held_min = (now_utc - opened).total_seconds() / 60
        ctx = json.loads(t["notes"] or "{}")
        expiry = ctx.get("expiry")
        reason = None
        mark = self.obroker.mark(t["contract"]) if self.obroker.connected else None
        if held_min >= t["max_hold_min"]:
            reason = "max_hold"
        elif riskmod.past_cutoff(now_et, self.cfg.flat_by):
            reason = "flat_by"
        elif expiry == today and (now_et.hour * 60 + now_et.minute) >= self._close_minutes(now_et) - self.cfg.opt_flat_before_close_min:
            reason = "expiry_cutoff"
        else:
            under = (self.fast.prices.get(t["symbol"], 5) if self.fast else None)
            sign = 1 if t["direction"] == "long" else -1
            if under is not None and ctx.get("under_stop") and (under - ctx["under_stop"]) * sign <= 0:
                reason = "under_stop"
            elif mark is not None:
                # premium plan: stop/target stored on the trade; runner trails on premium (levels = +50% steps)
                steps = [round(t["entry"] * (1 + 0.5 * k), 2) for k in range(2, 9)]
                plan = ExitPlan("long", t["entry"], t["initial_stop"] or t["stop"], t["stop"], t["target"], t["entry"] * 0.5,
                                steps, runner=bool(t["runner"]), be_done=bool(t["be_done"]))
                m = self._momentum.get(t["symbol"])
                momentum = bool(m and m["long" if sign == 1 else "short"])
                reason, _px, changed = exit_update(plan, mark, mark, momentum, self.cfg)
                if changed or plan.stop != t["stop"] or plan.target != t["target"]:
                    self.journal.update_exit_plan(t["id"], plan.stop, plan.target, plan.runner, plan.be_done)
        if reason is None:
            return
        exit_oid = None
        exit_price = mark
        if t["mode"] != "DRY_RUN":
            qty = int(t["qty"])
            try:   # (5) sell whatever the broker actually holds
                pos = self.broker.position_map().get(t["contract"])
                if pos is not None:
                    qty = int(pos.qty)
            except Exception:  # noqa: BLE001
                pass
            self._snap_greeks(t["id"], t["contract"], "exit")
            try:
                exit_oid = self.obroker.sell_to_close(t["contract"], qty)
            except Exception as exc:  # noqa: BLE001
                if "position" in str(exc).lower() or "404" in str(exc) or "insufficient" in str(exc).lower():
                    self.log.warning("option trade #%d: broker holds no %s; closing journal row (%s)", t["id"], t["contract"], exc)
                    reason = "broker_no_position"
                else:
                    raise
            exit_price = self._fill_price(exit_oid, exit_price)
        if exit_price is None:
            exit_price = t["stop"] if reason.endswith("stop") else t["entry"]
        if self.journal.close_trade(t["id"], exit_price, reason, None, exit_oid, now=now_utc.isoformat(timespec="seconds")):
            if self.state:
                self.state.open_symbols.discard(t["symbol"])
                self.state.realized_pnl_today = self.journal.realized_pnl_today(today)
            self.log.info("option trade #%d closed %s @ %.2f", t["id"], reason, exit_price)

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
                added = safe(variants.register_all, self.registry, self.journal) or []
                for name in added:
                    self.log.info("variant registered in the shadow book: %s", name)
                if self.broker.clock_is_open():
                    self.check_exits()
                    self.scan_once()
                else:
                    self.log.info("market closed")
                    safe(self.journal.set_state, "heartbeat", datetime.now(timezone.utc).isoformat(timespec="seconds"))
                    safe(self.journal.set_state, "market", "closed")
                    self._maybe_daily_review()
                    maybe_weekly(self)
                if self.broker.clock_is_open():
                    safe(self.journal.set_state, "market", "open")
                self._maybe_warden()
                safe(self._maybe_ledger_sync)
                if self.cfg.overnight_enabled:
                    safe(self.overnight.tick)     # extended + overnight sessions (and its trades' exits in any session)
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
