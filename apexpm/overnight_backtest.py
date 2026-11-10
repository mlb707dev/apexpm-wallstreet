"""Backtester for the overnight desk: extended-hours bars, limit-only fills, synthetic stops, wide costs.

Input: one symbol's extended file (scripts/fetch_ext_history.py: SIP pre/rth/ah + BOATS overnight, a
`session` column per bar). The regular-hours files the other backtests use are never read.

Rules (the same ones the live desk follows, apexpm/overnight.py):
  decision    at the CLOSE of bar t, only in a session the strategy lists; one position per symbol per
              strategy, at most EXT_MAX_TRADES_PER_WINDOW entries per window (close -> next open)
  entry       a resting buy (sell) limit at signal.entry, live for EXT_ENTRY_GOOD_FOR_MIN minutes and
              never past the end of the session it was placed in. It fills only when a LATER bar trades
              THROUGH the limit (low < limit for a buy, high > limit for a sell): touching is not a fill,
              and a minute with no trades has no bar and so no fill. Fill price = the limit, or the bar
              open when the bar opens through it.
  target      resting limit at signal.target; fills only when a bar trades through it, at the target
  stop        Alpaca has no stop orders outside regular hours, so the stop is a price the desk watches:
              when a bar's low reaches it, a marketable limit goes out and fills at min(stop, bar open)
              minus the session's slippage. Stop is checked before target in the same bar (conservative),
              and on the fill bar itself.
  time        at signal.context["exit_by"] (and max_hold_min) a marketable limit fills at the first bar's
              open at or after the deadline, minus slippage. No bar = no exit until one prints.
  costs       commission per share per side (cfg.bt_commission) + EXT_SLIP_BPS of price on every
              marketable exit (stop / time). Entries and targets are passive (no slippage) but must trade
              through, which already charges the adverse-selection cost of a resting order.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import ext_sessions as xs
from .backtest import BtResult, BtTrade
from .strategies.ext_base import ExtSnapshot, ExtStrategy

ET = xs.ET
# Marketable-exit slippage in basis points of price. A marketable limit pays roughly the whole quoted spread
# in a thin session, so the per-symbol table is 1.5x the 90th-percentile quoted spread measured by
# scripts/ext_spread_study.py (research/overnight/spreads.csv: SIP quotes pre/rth/ah, BOATS overnight,
# 8 weekdays 2025-03..2026-10), floored at 3 bps. Spreads in 2021-2023 were likely wider than these recent
# samples, so the research script also reports every recipe at 2x slippage (cfg.ext_slip_mult).
EXT_SLIP_BPS = {"rth": 3.0, "pre": 20.0, "ah": 20.0, "overnight": 25.0}       # symbols not measured
_P90 = {"AAPL": (7.7, 8.2, 9.1, 1.5), "AMD": (10.7, 12.8, 17.9, 4.4), "AMZN": (9.2, 10.8, 14.2, 1.8),
        "IWM": (1.9, 3.6, 1.8, 0.7), "META": (10.4, 14.6, 16.2, 5.3), "MSFT": (9.7, 12.3, 12.5, 2.6),
        "NVDA": (4.8, 5.6, 4.7, 1.1), "QQQ": (1.0, 1.7, 1.0, 0.4), "SPY": (0.9, 2.0, 0.8, 0.3),
        "TSLA": (4.6, 7.3, 7.6, 3.3)}                                          # (ah, overnight, pre, rth) p90 bps
EXT_SLIP_BPS_SYMBOL = {sym: {s: max(3.0, round(1.5 * v, 1)) for s, v in zip(("ah", "overnight", "pre", "rth"), vals)}
                       for sym, vals in _P90.items()}
EXT_ENTRY_GOOD_FOR_MIN = 20
EXT_MAX_TRADES_PER_WINDOW = 1
BASELINE_WINDOWS = 20


def load_ext(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, index_col="ts", parse_dates=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    if "session" not in df:
        df["session"] = xs.session_of_index(df.index)
    return df.sort_index()


def _baselines(sess: np.ndarray, win_id: np.ndarray, minutes_in: np.ndarray, vol: np.ndarray):
    """Per bar: cumulative volume in its (window, session) so far, and the median over the prior
    BASELINE_WINDOWS windows of the same session's cumulative volume at the same minutes-into-session."""
    n = len(vol)
    cum = np.zeros(n)
    base = np.zeros(n)
    groups: Dict[str, List[tuple]] = {}
    keys = pd.Series(list(zip(win_id, sess)))
    for (w, s), idx in keys.groupby(keys).groups.items():
        idx = np.asarray(sorted(idx))
        cum[idx] = np.cumsum(vol[idx])
        groups.setdefault(s, []).append((w, idx))
    for s, lst in groups.items():
        lst.sort(key=lambda x: x[0])
        for k, (w, idx) in enumerate(lst):
            prior = lst[max(0, k - BASELINE_WINDOWS):k]
            if len(prior) < 5:
                continue
            m = minutes_in[idx]
            mat = []
            for _w, pidx in prior:
                pm = minutes_in[pidx]
                j = np.searchsorted(pm, m, side="right") - 1
                pc = cum[pidx]
                mat.append(np.where(j >= 0, pc[np.clip(j, 0, None)], 0.0))
            base[idx] = np.median(np.vstack(mat), axis=0)
    return cum, base


_PREP: Dict[tuple, dict] = {}


def prepare(bars: pd.DataFrame, atr_period: int = 14) -> Optional[dict]:
    """Strategy-independent arrays for one symbol's history (cached: the tuner runs many recipes on it)."""
    key = (id(bars), len(bars), str(bars.index[0]) if len(bars) else "", str(bars.index[-1]) if len(bars) else "", atr_period)
    if key in _PREP:
        return _PREP[key]
    days = xs.rth_days(bars)
    if days.empty:
        return None
    sess = bars["session"].values.astype(object)
    et = bars.index.tz_convert(ET)
    mod = np.asarray(et.hour * 60 + et.minute)          # ET minute of day of the bar OPEN
    v = bars["volume"].values.astype(float)
    start_min = np.select([sess == "pre", sess == "rth", sess == "ah", sess == "overnight"], [240, 570, 960, 1200], default=0)
    # minutes into session, measured on the bar's open time (overnight wraps midnight)
    minutes_in = np.where((sess == "overnight") & (mod < 240), mod + 1440 - 1200, mod - start_min).astype(int)
    anchor = xs.anchor_index(bars.index, days["close_ts"].values)
    ext_mask = np.isin(sess, list(xs.EXT_SESSIONS))
    # The first after-hours bar (16:00, or 13:00 on early closes) carries the closing-auction print on the SIP
    # tape: a huge, scheduled volume that says nothing about after-hours interest. It is left out of the
    # session-volume baselines (prices are untouched) so a news spike is measured against real after-hours flow.
    vol_for_base = np.where(ext_mask & ~((sess == "ah") & (minutes_in < 5)), v, 0.0)
    cum, base = _baselines(np.where(ext_mask, sess, "rth_or_closed"), anchor, minutes_in, vol_for_base)
    p = {"index": bars.index, "days": days, "atr": xs.wilder_atr(days, atr_period).values, "closes": days["close"].values,
         "anchor": anchor, "open_ts": list(days["open_ts"]), "sess": sess, "mod": mod, "minutes_in": minutes_in,
         "cum": cum, "base": base, "o": bars["open"].values.astype(float), "h": bars["high"].values.astype(float),
         "lo": bars["low"].values.astype(float), "c": bars["close"].values.astype(float), "v": v}
    if len(_PREP) > 16:
        _PREP.clear()
    _PREP[key] = p
    return p


def make_snapshot(p: dict, i: int, symbol: str, cfg, win_start: Optional[int] = None, bar_min: int = 5) -> Optional[ExtSnapshot]:
    """The decision snapshot at the close of bar i, from prepare()'s arrays. Shared by the backtester and the
    live desk so both judge a signal on identical inputs. None when there is no completed anchor session,
    no ATR yet, or the bar closes at/after the next regular open."""
    a = int(p["anchor"][i])
    atr = p["atr"]
    if a < 1 or np.isnan(atr[a]) or atr[a] <= 0:
        return None
    if win_start is None:
        win_start = int(np.searchsorted(p["anchor"], a, side="left"))
    ts = p["index"][i]
    close_et = (ts + timedelta(minutes=bar_min)).tz_convert(ET).to_pydatetime()
    open_ts = p["open_ts"]
    nxt_open = open_ts[a + 1].tz_convert(ET).to_pydatetime() if a + 1 < len(open_ts) else None
    if nxt_open is not None and close_et >= nxt_open:
        return None
    days, closes = p["days"], p["closes"]
    o, h, lo, c, v = p["o"], p["h"], p["lo"], p["c"], p["v"]
    w = slice(win_start, i + 1)
    return ExtSnapshot(symbol=symbol, now_et=close_et, session=p["sess"][i],
                       price=c[i], bar_open=o[i], bar_high=h[i], bar_low=lo[i], bar_volume=v[i],
                       ref_close=float(closes[a]), ref_open=float(days["open"].values[a]),
                       ref_high=float(days["high"].values[a]), ref_low=float(days["low"].values[a]),
                       atr=float(atr[a]), prev_ref_close=float(closes[a - 1]),
                       daily_closes=closes[max(0, a - 59):a + 1],
                       minutes_into_session=int(p["minutes_in"][i]) + bar_min, next_open_et=nxt_open,
                       win=np.column_stack([o[w], h[w], lo[w], c[w], v[w]]), win_sessions=p["sess"][w],
                       win_minutes=p["mod"][w], session_vol=float(p["cum"][i]), typical_session_vol=float(p["base"][i]),
                       shortable=bool(getattr(cfg, "ext_shortable", False)))


class OvernightBacktester:
    def __init__(self, cfg, strategy: ExtStrategy, symbol: str):
        self.cfg = cfg
        self.strategy = strategy
        self.symbol = symbol

    def _slip(self, session: str, price: float) -> float:
        table = dict(EXT_SLIP_BPS)
        table.update(EXT_SLIP_BPS_SYMBOL.get(self.symbol, {}))
        table.update(getattr(self.cfg, "ext_slip_bps", None) or {})
        mult = float(getattr(self.cfg, "ext_slip_mult", 1.0) or 1.0)
        return price * table.get(session, table["overnight"]) * mult / 1e4

    def run(self, bars: pd.DataFrame, start: Optional[str] = None, end: Optional[str] = None) -> BtResult:
        cfg, strat = self.cfg, self.strategy
        if not bars.index.is_monotonic_increasing:
            bars = bars.sort_index()
        p = prepare(bars, int(getattr(cfg, "atr_period", 14) or 14))
        if p is None:
            return BtResult(trades=[], rejections={"no_rth_days": 1}, sessions=0)
        days, atr, closes, anchor, open_ts = p["days"], p["atr"], p["closes"], p["anchor"], p["open_ts"]
        sess, mod, minutes_in, cum, base = p["sess"], p["mod"], p["minutes_in"], p["cum"], p["base"]
        o, h, lo, c, v = p["o"], p["h"], p["lo"], p["c"], p["v"]
        bar_min = int(getattr(cfg, "bar_minutes", 5) or 5)
        good_for = timedelta(minutes=int(getattr(cfg, "ext_entry_good_for_min", EXT_ENTRY_GOOD_FOR_MIN)))
        max_per_win = int(getattr(cfg, "ext_max_trades_per_window", EXT_MAX_TRADES_PER_WINDOW))
        comm = float(getattr(cfg, "bt_commission", 0.0) or 0.0)
        lo_ts = pd.Timestamp(start, tz="UTC") if start else None
        hi_ts = pd.Timestamp(end, tz="UTC") if end else None
        sessions_allowed = set(strat.knob(cfg, "ext_sessions_override", None) or strat.ext_sessions)

        trades: List[BtTrade] = []
        rejections: Dict[str, int] = {}
        open_t: Optional[BtTrade] = None
        open_meta: dict = {}
        pending: Optional[dict] = None
        per_window: Dict[int, int] = {}
        windows_seen = set()
        win_start = 0
        cur_win = None

        def rej(k):
            rejections[k] = rejections.get(k, 0) + 1

        def close_trade(t: BtTrade, i: int, px: float, reason: str, marketable: bool):
            t.exit_ts = bars.index[i].to_pydatetime()
            t.exit_price = float(px)
            t.exit_reason = reason
            t.costs += comm + (self._slip(sess[i], px) if marketable else 0.0)

        idx_ts = bars.index
        tns = idx_ts.asi8
        allowed_mask = np.isin(sess, list(sessions_allowed))
        dw = strat.decision_window(cfg)
        if dw:
            close_mod = (mod + bar_min) % 1440
            in_w = np.zeros(len(mod), dtype=bool)
            for w_lo, w_hi in dw:
                in_w |= (close_mod >= w_lo) & (close_mod <= w_hi)
            allowed_mask &= in_w
        lo_ns = lo_ts.value if lo_ts is not None else None
        hi_ns = hi_ts.value if hi_ts is not None else None
        for i in range(len(bars)):
            a = anchor[i]
            if a != cur_win:
                cur_win, win_start = a, i
            if open_t is None and pending is None and not allowed_mask[i]:
                continue                          # fast path: nothing to manage, not a decision session
            if lo_ns is not None and tns[i] < lo_ns:
                continue
            if hi_ns is not None and tns[i] > hi_ns:
                break
            ts = idx_ts[i]
            bar_close_ts = ts + timedelta(minutes=bar_min)

            # ---- 1. fill a pending entry on this bar ---------------------------------
            if pending is not None and open_t is None:
                sig = pending["sig"]
                if ts - pending["placed"] > good_for or sess[i] != pending["session"]:
                    rej("unfilled")
                    pending = None
                else:
                    long_ = sig.direction == "long"
                    through = (lo[i] < sig.entry) if long_ else (h[i] > sig.entry)
                    if through:
                        fill = min(sig.entry, o[i]) if long_ else max(sig.entry, o[i])
                        t = BtTrade(symbol=self.symbol, setup=sig.setup, direction=sig.direction,
                                    entry_ts=ts.to_pydatetime(), exit_ts=None, entry=float(fill), stop=sig.stop,
                                    target=sig.target, max_hold_min=sig.max_hold_min, level_targeted=0.0,
                                    gap_entry=bool(fill != sig.entry), signal_ts=pending["placed"].to_pydatetime(),
                                    costs=comm, initial_stop=sig.stop, context=dict(sig.context))
                        t.context["entry_session"] = sess[i]
                        trades.append(t)
                        open_t = t
                        open_meta = {"exit_by": pd.Timestamp(sig.context["exit_by"]).tz_convert("UTC")}
                        pending = None
                        stop_hit = (lo[i] <= t.stop) if long_ else (h[i] >= t.stop)
                        if stop_hit:
                            px = min(t.stop, o[i]) if long_ else max(t.stop, o[i])
                            close_trade(t, i, px, "stop", True)
                            open_t = None
                        continue

            # ---- 2. manage the open trade ------------------------------------------
            if open_t is not None:
                t = open_t
                long_ = t.direction == "long"
                held = ts - pd.Timestamp(t.entry_ts)
                if ts >= open_meta["exit_by"] or held >= timedelta(minutes=t.max_hold_min):
                    close_trade(t, i, o[i], "time", True)
                    open_t = None
                elif (lo[i] <= t.stop) if long_ else (h[i] >= t.stop):
                    px = min(t.stop, o[i]) if long_ else max(t.stop, o[i])
                    close_trade(t, i, px, "stop", True)
                    open_t = None
                elif (h[i] > t.target) if long_ else (lo[i] < t.target):
                    close_trade(t, i, t.target, "target", False)
                    open_t = None
                continue

            if pending is not None:
                continue
            # ---- 3. decide at the close of this bar ---------------------------------
            if sess[i] not in sessions_allowed or a < 1:
                continue
            windows_seen.add(a)
            if per_window.get(a, 0) >= max_per_win:
                continue
            snap = make_snapshot(p, i, self.symbol, cfg, win_start, bar_min)
            if snap is None:
                continue
            why = strat.session_ok(snap, cfg)
            if why:
                rej(why)
                continue
            sig, why = strat.evaluate(snap, cfg)
            if sig is None:
                if why:
                    rej(why)
                continue
            if sig.direction == "short" and not snap.shortable:
                rej("no_shorting")
                continue
            per_window[a] = per_window.get(a, 0) + 1
            # the order rests from the next bar on; its life is measured from this bar's open time
            pending = {"sig": sig, "placed": ts, "session": sess[i]}

        if open_t is not None:     # data ended with a position: mark at the last close, flagged
            close_trade(open_t, len(bars) - 1, c[-1], "data_end", True)
        return BtResult(trades=trades, rejections=rejections, sessions=len(windows_seen))


def split_trades(trades: List[BtTrade], splits) -> Dict[str, List[float]]:
    """R lists per named split; splits = ((name, start_iso, end_iso), ...) on the entry time (UTC date)."""
    out: Dict[str, List[float]] = {s[0]: [] for s in splits}
    for t in trades:
        if t.exit_price is None:
            continue
        d = pd.Timestamp(t.entry_ts).date().isoformat()
        for name, lo_, hi_ in splits:
            if lo_ <= d <= hi_:
                out[name].append(round(float(t.r), 4))
                break
    return out
