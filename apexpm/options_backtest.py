"""Options backtest: price the underlying strategy's trades through real option bars.

For each underlying trade (entry/exit timestamps and direction) choose a
contract from that session's downloaded chain (data/options/<SYM>/<date>.csv):
  expiry: 0DTE (same day) or 1DTE (next session)
  type:   call for long, put for short
  strike: nearest to the entry price ('atm') or one strike out of the money ('otm1')
Entry at the option bar's OPEN at the underlying fill time, plus slippage and
commission. Exit at the underlying exit time's bar close, or earlier on a
premium stop (bar low through entry × (1 - stop)), or at the last bar of the
day for 0DTE. Everything is per contract (×100).
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ET = "America/New_York"
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


@dataclass
class OptTrade:
    date: str
    direction: str
    contract: str
    strike: float
    expiry: str
    dte: int
    entry_ts: datetime
    exit_ts: datetime
    entry_prem: float
    exit_prem: float
    exit_reason: str
    pnl: float            # $ per contract, net
    risk: float           # $ per contract at the premium stop
    r: float
    under_r: float


class OptionsBacktester:
    def __init__(self, symbol: str = "SPY", slippage: float = 0.02, commission: float = 0.65,
                 stop_pct: float = 0.5, dte: int = 0, strike_mode: str = "atm"):
        self.symbol = symbol
        self.slip = slippage
        self.comm = commission
        self.stop_pct = stop_pct
        self.dte = dte
        self.strike_mode = strike_mode
        self._cache: Dict[str, pd.DataFrame] = {}

    def _chain(self, date: str) -> Optional[pd.DataFrame]:
        if date in self._cache:
            return self._cache[date]
        p = f"data/options/{self.symbol}/{date}.csv"
        if not os.path.exists(p) or os.path.getsize(p) < 50:
            self._cache[date] = None
            return None
        df = pd.read_csv(p, parse_dates=["timestamp"])
        if df.empty or "symbol" not in df:
            self._cache[date] = None
            return None
        parts = df["symbol"].str.extract(OCC)
        df["cp"] = parts[2]
        df["strike"] = parts[3].astype(float) / 1000
        df["expiry"] = pd.to_datetime(parts[1], format="%y%m%d").dt.date.astype(str)
        self._cache[date] = df
        return df

    def pick(self, chain: pd.DataFrame, date: str, direction: str, entry_price: float) -> Optional[str]:
        cp = "C" if direction == "long" else "P"
        exps = sorted(chain["expiry"].unique())
        if not exps:
            return None
        if self.dte == 0:
            exp = exps[0] if exps[0] == date else None
        else:
            later = [e for e in exps if e > date]
            exp = later[0] if later else None
        if exp is None:
            return None
        sub = chain[(chain["cp"] == cp) & (chain["expiry"] == exp)]
        if sub.empty:
            return None
        strikes = sorted(sub["strike"].unique())
        nearest = min(strikes, key=lambda k: abs(k - entry_price))
        if self.strike_mode == "otm1":
            i = strikes.index(nearest)
            j = i + 1 if direction == "long" else i - 1
            if 0 <= j < len(strikes):
                nearest = strikes[j]
        row = sub[sub["strike"] == nearest].iloc[0]
        step = min(np.diff(strikes)) if len(strikes) > 1 else 1.0
        if self.strike_mode == "atm" and abs(nearest - entry_price) > step:   # entry is off the downloaded chain
            return None
        return row["symbol"]

    def price_trade(self, t) -> Optional[OptTrade]:
        entry_ts = pd.Timestamp(t.entry_ts)
        exit_ts = pd.Timestamp(t.exit_ts) if t.exit_ts is not None else None
        if exit_ts is None:
            return None
        date = entry_ts.tz_convert(ET).date().isoformat()
        chain = self._chain(date)
        if chain is None:
            return None
        sym = self.pick(chain, date, t.direction, t.entry)
        if sym is None:
            return None
        bars = chain[chain["symbol"] == sym].sort_values("timestamp")
        bars = bars[bars["timestamp"] >= entry_ts]
        if bars.empty:
            return None
        first = bars.iloc[0]
        entry_prem = float(first["open"]) + self.slip
        if entry_prem <= 0.05:
            return None
        stop_level = entry_prem * (1 - self.stop_pct) if self.stop_pct else None
        exit_prem, reason, ex_ts = None, None, None
        for _, b in bars.iterrows():
            ts = b["timestamp"]
            if stop_level is not None and float(b["low"]) <= stop_level and ts > entry_ts:
                exit_prem, reason, ex_ts = stop_level - self.slip, "premium_stop", ts
                break
            if ts >= exit_ts:
                exit_prem, reason, ex_ts = float(b["close"]) - self.slip, f"under_{t.exit_reason}", ts
                break
        if exit_prem is None:   # underlying exit after the option's last bar (expiry close)
            last = bars.iloc[-1]
            exit_prem, reason, ex_ts = float(last["close"]) - self.slip, "last_bar", last["timestamp"]
        exit_prem = max(exit_prem, 0.0)
        pnl = (exit_prem - entry_prem) * 100 - 2 * self.comm
        risk = (entry_prem * self.stop_pct if self.stop_pct else entry_prem) * 100 + 2 * self.comm
        m = OCC.match(sym)
        return OptTrade(date=date, direction=t.direction, contract=sym, strike=float(m.group(4)) / 1000,
                        expiry=m.group(2), dte=self.dte, entry_ts=entry_ts, exit_ts=ex_ts,
                        entry_prem=round(entry_prem, 2), exit_prem=round(exit_prem, 2), exit_reason=reason,
                        pnl=round(pnl, 2), risk=round(risk, 2), r=round(pnl / risk, 3) if risk else 0.0, under_r=round(t.r, 3))

    def run(self, under_trades: List) -> List[OptTrade]:
        out = []
        for t in under_trades:
            if t.exit_price is None:
                continue
            ot = self.price_trade(t)
            if ot:
                out.append(ot)
        return out


def summarize(trades: List[OptTrade], start_equity: float = 500.0, risk_pct: float = 0.03,
              max_premium_pct: float = 0.5) -> Dict:
    if not trades:
        return {"trades": 0}
    pnls = np.array([t.pnl for t in trades])
    rs = np.array([t.r for t in trades])
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    cum = np.cumsum(pnls)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    # $-account simulation: 1+ contracts sized to risk_pct of equity at the premium stop,
    # skipping trades whose premium exceeds max_premium_pct of equity
    eq, peak, mdd, taken, skipped = start_equity, start_equity, 0.0, 0, 0
    curve = []
    for t in trades:
        n = int((eq * risk_pct) // t.risk) if t.risk else 0
        if n < 1 and risk_pct >= 0.25:        # hyper-style band: allow one contract if the premium fits
            n = 1
        if n >= 1 and t.entry_prem * 100 * n > eq * max_premium_pct:
            n = int(eq * max_premium_pct // (t.entry_prem * 100))
        if n < 1:
            skipped += 1
            continue
        eq += t.pnl * n
        taken += 1
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak else 0)
        curve.append(round(eq, 2))
        if eq <= 0:
            break
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    return {
        "trades": len(trades), "win_rate": round(float((pnls > 0).mean()), 3),
        "avg_pnl_per_contract": round(float(pnls.mean()), 2), "expectancy_r": round(float(rs.mean()), 3),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if len(losses) and losses.sum() else None,
        "total_pnl_1_contract": round(float(cum[-1]), 2), "max_dd_1_contract": round(dd, 2),
        "avg_entry_premium": round(float(np.mean([t.entry_prem for t in trades])), 2),
        "account_sim": {"start": start_equity, "end": round(eq, 2), "taken": taken, "skipped_too_expensive": skipped,
                        "max_dd_pct": round(mdd * 100, 1), "min_equity": round(min(curve), 2) if curve else None},
        "exit_reasons": reasons,
    }
