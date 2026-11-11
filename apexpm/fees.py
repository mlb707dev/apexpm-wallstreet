"""Estimated broker pass-through fees per trade (owner, 2026-11-10: "where are fees on the trade results?").

Alpaca charges no commission on self-directed stock and option trades; what remains are the regulatory and
clearing fees it passes through. Rates below are from the Alpaca Clearing Brokerage Fee Schedule dated
2026-10-12 (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf) and the crypto fee page
(tier 1). They change; every rate can be overridden by an environment variable of the same name.

  stocks    SEC fee on sells: 0.0000206 x trade value; FINRA TAF on sells: $0 per share (as the schedule
            states); CAT fee on buys and sells: $0.000003 per share
  options   the same SEC fee on sells; TAF on sells: $0 per contract; CAT on both sides: $0.000003 x 100 per
            contract; Options Regulatory Fee $0.015 and OCC clearing $0.025 per contract, both sides
  crypto    0.25% of trade value per side (taker, lowest volume tier)

The broker adds fees up per account per day and rounds each total up to a cent, so a per-trade figure is an
ESTIMATE and is kept unrounded here (fractions of a cent matter when summed). Index options carry extra
exchange fees that are not modelled. Paper fills carry no fee at the broker; the estimate shows what the
same trade would cost with real money. Nothing here changes a journaled P&L.
"""
from __future__ import annotations

import os
from typing import Any, Dict


def _r(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def rates() -> Dict[str, float]:
    return {
        "FEE_SEC_RATE": _r("FEE_SEC_RATE", 0.0000206),            # x sell trade value, stocks and options
        "FEE_TAF_SHARE": _r("FEE_TAF_SHARE", 0.0),                # per share sold
        "FEE_TAF_CONTRACT": _r("FEE_TAF_CONTRACT", 0.0),          # per contract sold
        "FEE_CAT_SHARE": _r("FEE_CAT_SHARE", 0.000003),           # per executed equivalent share, both sides
        "FEE_ORF_CONTRACT": _r("FEE_ORF_CONTRACT", 0.015),        # per contract, both sides
        "FEE_OCC_CONTRACT": _r("FEE_OCC_CONTRACT", 0.025),        # per contract, both sides
        "FEE_CRYPTO_RATE": _r("FEE_CRYPTO_RATE", 0.0025),         # x trade value, both sides
    }


SCHEDULE_DATE = "2026-10-12"


def estimate(t: Dict[str, Any]) -> float:
    """Estimated total fees in dollars for one trade row (entry and, when closed, exit). Unrounded."""
    r = rates()
    qty = abs(float(t.get("qty") or 0.0))
    entry = float(t.get("entry") or 0.0)
    exit_ = t.get("exit_price")
    exit_ = float(exit_) if exit_ is not None else None
    if qty <= 0 or entry <= 0:
        return 0.0
    long = (t.get("direction") or "long") == "long"
    sym = t.get("symbol") or ""
    if "/" in sym:                                             # crypto
        return r["FEE_CRYPTO_RATE"] * qty * (entry + (exit_ or 0.0))
    option = (t.get("asset") or "stock") == "option"
    mult = 100.0 if option else 1.0
    sides = 2 if exit_ is not None else 1
    # the sell leg: the exit of a long, the entry of a short
    sell_px = (exit_ if long else entry)
    sold = sell_px is not None
    fee = 0.0
    if sold:
        fee += r["FEE_SEC_RATE"] * qty * mult * sell_px
        fee += (r["FEE_TAF_CONTRACT"] if option else r["FEE_TAF_SHARE"]) * qty
    fee += r["FEE_CAT_SHARE"] * qty * mult * sides
    if option:
        fee += (r["FEE_ORF_CONTRACT"] + r["FEE_OCC_CONTRACT"]) * qty * sides
    return fee
