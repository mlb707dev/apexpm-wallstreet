"""Intraday backtester that runs the same Strategy objects as the live loop.

Inputs are plain DataFrames (daily, ltf, htf) so any data source works.

Timing is honest: a signal is decided at the CLOSE of bar t and can only fill
on bars after t.
  fill model "limit":     resting limit at the signal's entry level, live for
                          ENTRY_GOOD_FOR_BARS bars; fills at the level when a
                          later bar's range touches it, or at that bar's open
                          if it opens through the level favourably.
  fill model "next_open": market at the next bar's open, plus slippage.
Costs: BT_COMMISSION per share per side always; BT_SLIPPAGE per share on
marketable sides (market entries, stop and time exits). Target exits are
limit fills at the level (no slippage).
Exits: stop and target checked on bar high/low; if both touch in one bar the
stop is assumed. The fill bar itself is checked for the stop. No re-entry on
the bar a trade exited.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .levels import LevelMap, build_levels, wilder_atr_series
from .ribbon import Ribbon
from .strategies.base import MarketSnapshot, Signal, Strategy

ET = ZoneInfo("America/New_York")


@dataclass
class BtTrade:
    symbol: str
    setup: str
    direction: str
    entry_ts: datetime
    exit_ts: Optional[datetime]
    entry: float
    stop: float
    target: float
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    max_hold_min: int = 90
    level_targeted: float = 0.0
    gap_entry: bool = False
    signal_ts: Optional[datetime] = None
    costs: float = 0.0          # $/share total (commissions + slippage)
    context: Dict[str, object] = field(default_factory=dict)

    @property
    def r(self) -> float:
        """Result in R multiples (risk units), net of costs."""
        if self.exit_price is None:
            return 0.0
        sign = 1 if self.direction == "long" else -1
        risk = abs(self.entry - self.stop)
        return (((self.exit_price - self.entry) * sign - self.costs) / risk) if risk else 0.0

    @property
    def hold_min(self) -> float:
        if not self.exit_ts:
            return 0.0
        return (self.exit_ts - self.entry_ts).total_seconds() / 60


@dataclass
class BtResult:
    trades: List[BtTrade]
    rejections: Dict[str, int]
    sessions: int

    def summary(self) -> Dict[str, float]:
        rs = np.array([t.r for t in self.trades if t.exit_price is not None])
        n = len(rs)
        if n == 0:
            return {"trades": 0, "sessions": self.sessions}
        wins = rs[rs > 0]
        losses = rs[rs <= 0]
        gross_win = wins.sum() if len(wins) else 0.0
        gross_loss = -losses.sum() if len(losses) else 0.0
        cum = np.cumsum(rs)
        peak = np.maximum.accumulate(cum)
        dd = (peak - cum).max() if n else 0.0
        reasons: Dict[str, int] = {}
        for t in self.trades:
            reasons[t.exit_reason or "open"] = reasons.get(t.exit_reason or "open", 0) + 1
        return {
            "trades": n,
            "sessions": self.sessions,
            "win_rate": float((rs > 0).mean()),
            "avg_r": float(rs.mean()),
            "expectancy_r": float(rs.mean()),
            "profit_factor": float(gross_win / gross_loss) if gross_loss else float("inf"),
            "total_r": float(cum[-1]),
            "max_dd_r": float(dd),
            "avg_win_r": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss_r": float(losses.mean()) if len(losses) else 0.0,
            "avg_hold_min": float(np.mean([t.hold_min for t in self.trades])),
            "exit_reasons": reasons,
            "gap_entries": int(sum(t.gap_entry for t in self.trades)),
            "avg_cost_per_share": float(np.mean([t.costs for t in self.trades])),
            "sessions_with_trades": len({t.entry_ts.date() for t in self.trades}),
        }

    def trades_frame(self) -> pd.DataFrame:
        rows = []
        for t in self.trades:
            rows.append({"entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "dir": t.direction,
                         "entry": t.entry, "stop": t.stop, "target": t.target,
                         "exit": t.exit_price, "reason": t.exit_reason, "r": round(t.r, 3),
                         "hold_min": round(t.hold_min, 1), "gap": t.gap_entry, **t.context})
        return pd.DataFrame(rows)


def _ema_frame(df: pd.DataFrame, fast: int, mid: int, slow: int) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for name, span in (("fast", fast), ("mid", mid), ("slow", slow)):
        out[name] = df["close"].ewm(span=span, adjust=False).mean()
    return out


def _past(ts_et: datetime, hhmm: str) -> bool:
    h, m = (int(x) for x in hhmm.split(":"))
    return (ts_et.hour, ts_et.minute) >= (h, m)


class Backtester:
    def __init__(self, cfg, strategy: Strategy, symbol: str = "SPY"):
        """One strategy per run; the registry runs several and merges results."""
        self.cfg = cfg
        self.strategy = strategy
        self.symbol = symbol

    def run(self, daily: pd.DataFrame, ltf: pd.DataFrame, htf: pd.DataFrame) -> BtResult:
        cfg = self.cfg
        daily = daily.sort_index()
        ltf = ltf.sort_index()
        htf = htf.sort_index()
        if ltf.index.tz is None:
            ltf.index = ltf.index.tz_localize("UTC")
        if htf.index.tz is None:
            htf.index = htf.index.tz_localize("UTC")

        atr_series = pd.Series(
            wilder_atr_series(daily["high"].values, daily["low"].values, daily["close"].values, cfg.atr_period),
            index=daily.index)
        daily_dates = [d.date() for d in daily.index]

        ltf_ema = _ema_frame(ltf, cfg.ema_fast, cfg.ema_mid, cfg.ema_slow)
        htf_ema = _ema_frame(htf, cfg.ema_fast, cfg.ema_mid, cfg.ema_slow)
        htf_minutes = cfg.htf_bar_minutes

        ltf_et = ltf.index.tz_convert(ET)
        session_of = pd.Series([t.date() for t in ltf_et], index=ltf.index)

        trades: List[BtTrade] = []
        rejections: Dict[str, int] = {}
        sessions = 0
        open_trade: Optional[BtTrade] = None
        pending: Optional[dict] = None       # {"sig": Signal, "placed_ts": ts, "bars": n}
        last_exit_ts: Optional[datetime] = None
        comm, slip = cfg.bt_commission, cfg.bt_slippage

        def rej(k: str) -> None:
            rejections[k] = rejections.get(k, 0) + 1

        for day, idx in session_of.groupby(session_of).groups.items():
            prior = [i for i, d in enumerate(daily_dates) if d < day]
            if not prior:
                continue
            pi = prior[-1]
            atr = atr_series.iloc[pi]
            if np.isnan(atr):
                continue
            levels: LevelMap = build_levels(float(daily["close"].iloc[pi]), float(atr))
            sessions += 1
            reached = {"long": 0.0, "short": 0.0}
            trades_today = 0
            last_stop: Dict[str, Optional[datetime]] = {"long": None, "short": None}
            bars = ltf.loc[idx]
            pending = None

            for pos_i, (ts, bar) in enumerate(bars.iterrows()):
                ts_et = ts.tz_convert(ET)
                o, hi, lo, close = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])

                # ---- 1. try to fill a pending order on THIS bar ---------------
                if pending is not None and open_trade is None:
                    sig = pending["sig"]
                    sign = 1 if sig.direction == "long" else -1
                    fill = None
                    marketable = False
                    if cfg.bt_fill_model == "next_open" or sig.context.get("fill_at_market"):
                        fill, marketable = o, True
                    else:
                        # resting limit: favourable gap-through fills at the open, else at the level
                        if (o - sig.entry) * sign <= 0:
                            fill = o
                        elif lo <= sig.entry <= hi:
                            fill = sig.entry
                    if fill is not None:
                        costs = comm + (slip if marketable else 0.0)
                        open_trade = BtTrade(symbol=self.symbol, setup=sig.setup, direction=sig.direction,
                                             entry_ts=ts, exit_ts=None, entry=fill, stop=sig.stop,
                                             target=sig.target, max_hold_min=sig.max_hold_min,
                                             level_targeted=sig.level_targeted, gap_entry=(fill != sig.entry),
                                             signal_ts=pending["placed_ts"], costs=costs, context=dict(sig.context))
                        trades.append(open_trade)
                        trades_today += 1
                        pending = None
                        # entry-bar stop check (conservative: stop before target on the fill bar)
                        hit_stop = (lo <= sig.stop) if sign == 1 else (hi >= sig.stop)
                        if hit_stop:
                            open_trade.exit_ts, open_trade.exit_price, open_trade.exit_reason = ts, sig.stop, "stop"
                            open_trade.costs += comm + slip
                            last_stop[sig.direction] = ts
                            last_exit_ts = ts
                            open_trade = None
                    else:
                        pending["bars"] += 1
                        if pending["bars"] >= cfg.entry_good_for_bars or _past(ts_et, cfg.no_entry_after):
                            rej("unfilled")
                            pending = None

                # ---- 2. manage an open trade on this bar ---------------------
                elif open_trade is not None:
                    t = open_trade
                    sign = 1 if t.direction == "long" else -1
                    hit_stop = (lo <= t.stop) if sign == 1 else (hi >= t.stop)
                    hit_tgt = (hi >= t.target) if sign == 1 else (lo <= t.target)
                    reason = px = None
                    if hit_stop:
                        reason, px = "stop", t.stop
                    elif hit_tgt:
                        reason, px = "target", t.target
                    elif (ts - t.entry_ts) >= timedelta(minutes=t.max_hold_min):
                        reason, px = "max_hold", close
                    elif _past(ts_et, cfg.flat_by):
                        reason, px = "flat_by", close
                    if reason:
                        t.exit_ts, t.exit_price, t.exit_reason = ts, px, reason
                        t.costs += comm + (0.0 if reason == "target" else slip)
                        if reason == "stop":
                            last_stop[t.direction] = ts
                        last_exit_ts = ts
                        open_trade = None

                # ---- 3. reached levels: snapshot BEFORE this bar, then update ----
                prev_reached = dict(reached)
                reached["long"] = max(reached["long"], levels.position_in_range(hi))
                reached["short"] = max(reached["short"], -levels.position_in_range(lo))

                if open_trade is not None or pending is not None:
                    continue
                if last_exit_ts is not None and ts <= last_exit_ts:
                    continue                      # no re-entry on the exit bar
                if _past(ts_et, cfg.no_entry_after):
                    continue
                if trades_today >= cfg.max_trades_per_day:
                    continue

                # ---- 4. decide at the close of this bar ---------------------
                e = ltf_ema.loc[ts]
                rl = Ribbon(float(e["fast"]), float(e["mid"]), float(e["slow"]))
                cutoff = ts - timedelta(minutes=htf_minutes)
                h_idx = htf_ema.index[htf_ema.index <= cutoff]
                if len(h_idx) < cfg.ema_slow:
                    continue
                he = htf_ema.loc[h_idx[-1]]
                rh = Ribbon(float(he["fast"]), float(he["mid"]), float(he["slow"]))
                prev_rl = None
                if pos_i > 0:
                    pe = ltf_ema.loc[bars.index[pos_i - 1]]
                    prev_rl = Ribbon(float(pe["fast"]), float(pe["mid"]), float(pe["slow"]))
                snap = MarketSnapshot(symbol=self.symbol, price=close, levels=levels,
                                      ribbon_ltf=rl, ribbon_htf=rh, ltf_closes=[],
                                      regime="trend_up" if rh.state == "bull" else "trend_down" if rh.state == "bear" else "chop",
                                      reached_today=prev_reached, bar_index_today=pos_i,
                                      minutes_since_open=int((ts_et.hour - 9) * 60 + ts_et.minute - 30),
                                      prev_ribbon_ltf=prev_rl)
                sig, why = self.strategy.evaluate(snap, cfg)
                if sig is None:
                    if why and why != "not_at_entry_level":
                        rej(why)
                    continue
                ls = last_stop[sig.direction]
                if ls is not None and (ts - ls) < timedelta(minutes=cfg.stop_cooldown_min):
                    rej("stop_cooldown")
                    continue
                if (sig.direction == "long" and hi >= sig.target) or (sig.direction == "short" and lo <= sig.target):
                    rej("bar_spans_target")
                    continue
                pending = {"sig": sig, "placed_ts": ts, "bars": 0}

            # end of session: force flat, drop pending
            pending = None
            if open_trade is not None:
                last_ts = bars.index[-1]
                open_trade.exit_ts = last_ts
                open_trade.exit_price = float(bars["close"].iloc[-1])
                open_trade.exit_reason = "session_end"
                open_trade.costs += comm + slip
                open_trade = None

        return BtResult(trades=trades, rejections=rejections, sessions=sessions)
