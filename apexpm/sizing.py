"""Fractional Kelly capped by MAX_POS_PCT, scaled by volatility targeting."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class SizeResult:
    qty: float
    dollar_risk: float
    kelly_full: float
    kelly_used: float
    vol_scale: float
    notional: float
    reason: Optional[str] = None  # set when qty == 0


def kelly_fraction_full(p: float, b: float) -> float:
    """Kelly fraction for win prob p and payoff ratio b (win size / loss size)."""
    if b <= 0:
        return 0.0
    return (p * b - (1.0 - p)) / b


def rolling_stats(pnls: Sequence[float]) -> Optional[tuple]:
    """(win_rate, payoff_ratio) from closed-trade pnls, or None if no wins/losses."""
    wins = [x for x in pnls if x > 0]
    losses = [-x for x in pnls if x < 0]
    if not wins or not losses:
        return None
    p = len(wins) / len(pnls)
    b = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    return p, b


def vol_scale(atr: float, price: float, target_daily_vol: float,
              lo: float = 0.25, hi: float = 1.5) -> float:
    if price <= 0 or atr <= 0:
        return lo
    realized = atr / price
    return max(lo, min(hi, target_daily_vol / realized))


def size_trade(*, equity: float, price: float, entry: float, stop: float, target: float,
               atr: float, closed_pnls: Sequence[float], cfg) -> SizeResult:
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return SizeResult(0, 0, 0, 0, 0, 0, "zero_risk_per_share")

    stats = rolling_stats(closed_pnls) if len(closed_pnls) >= cfg.min_trades_for_stats else None
    if stats:
        p, b = stats
    else:
        p = cfg.prior_win_rate
        b = abs(target - entry) / risk_per_share

    k_full = kelly_fraction_full(p, b)
    k_used = max(0.0, min(cfg.kelly_fraction * k_full, cfg.max_pos_pct))
    if k_used <= 0:
        return SizeResult(0, 0, k_full, 0, 0, 0, "negative_edge")

    vs = vol_scale(atr, price, cfg.target_daily_vol, cfg.vol_scale_min, cfg.vol_scale_max)
    dollar_risk = equity * k_used * vs
    raw_qty = dollar_risk / risk_per_share

    # notional caps: absolute, and a share of equity (no margin assumed)
    cap_notional = min(cfg.max_pos_dollars, equity * getattr(cfg, "max_notional_pct", 0.95))
    raw_qty = min(raw_qty, cap_notional / price)

    if getattr(cfg, "fractional_shares", False):
        qty = math.floor(raw_qty * 1000) / 1000.0           # Alpaca accepts up to 9 dp; 3 is plenty
        if qty * price < getattr(cfg, "min_notional", 1.0):
            return SizeResult(0, dollar_risk, k_full, k_used, vs, 0, "below_min_notional")
    else:
        qty = float(int(math.floor(raw_qty)))
    notional = qty * price
    if qty <= 0:
        return SizeResult(0, dollar_risk, k_full, k_used, vs, 0, "qty_rounds_to_zero")
    return SizeResult(qty, dollar_risk, k_full, k_used, vs, notional)
