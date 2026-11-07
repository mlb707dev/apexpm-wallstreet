"""Per-strategy time-split tuner with memory.

Each base strategy owns a search space (strategies/spaces.py). A run samples
combinations it has NOT evaluated before (the results store remembers every
(combo, symbol) ever run, so the search keeps widening night after night),
backtests each on every paper symbol with next-open fills, and scores it on
three time splits: train (<= 2023), validate (2024), test (2025 on).

Selection is deliberately strict:
  * pooled across symbols, every split must be positive with enough trades;
  * the test profit factor must clear a floor;
  * a challenger must beat the incumbent on BOTH validate and test expectancy
    by a margin (unless the incumbent fails the pass rule itself);
  * the symbol allowlist is the set of symbols where validate and test are
    both positive, so a variant is tracked per asset, not blanket.
A selected challenger is only a PROPOSAL: it enters the shadow book and earns
live evidence before the owner can promote it through the usual gate.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from .backtest import Backtester
from .config import Config

STORE = "data/tuning/results.jsonl"
SPLITS = (("train", 0, 2023), ("val", 2024, 2024), ("test", 2025, 9999))
MIN_TRADES = {"train": 60, "val": 20, "test": 20}       # pooled across symbols
MIN_TRADES_SYMBOL = 5                                  # per symbol, for the allowlist
MIN_TEST_PF = 1.10
IMPROVE_MARGIN_R = 0.02


def combo_key(base: str, overrides: dict) -> str:
    s = json.dumps({"base": base, "o": overrides}, sort_keys=True, default=str)
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def _has_short(o: dict) -> bool:
    return any(k.endswith("_directions") and "short" in (v or []) for k, v in o.items())


def long_only_overrides(o: dict) -> dict:
    """The same recipe restricted to longs (direction lists become ["long"])."""
    return {k: (["long"] if k.endswith("_directions") else v) for k, v in o.items()}


def incumbent_overrides(base: str, long_only: bool = False) -> dict:
    from .strategies.spaces import INCUMBENTS
    inc = INCUMBENTS[base]["overrides"]
    return long_only_overrides(inc) if long_only else inc


def all_combos(base: str, long_only: bool = False) -> List[dict]:
    """long_only drops every combination that would short: a cash account (Alpaca sets 1x and no
    shorting under $2,000) cannot fill them, so a short-side winner would be a false challenger."""
    from .strategies.spaces import SPACES
    space = SPACES[base]
    keys = list(space)
    out = []
    for vals in itertools.product(*(space[k] for k in keys)):
        o = {k: v for k, v in zip(keys, vals) if v is not None}
        if long_only and _has_short(o):
            continue
        out.append(o)
    return out


def sample_combos(base: str, n: int, seed: int, exclude: Iterable[str] = (), long_only: bool = False) -> List[dict]:
    """Unexplored combinations first (random order, seeded), incumbent always included."""
    ex = set(exclude)
    pool = [o for o in all_combos(base, long_only) if combo_key(base, o) not in ex]
    rng = random.Random(seed)
    rng.shuffle(pool)
    inc = incumbent_overrides(base, long_only)
    picked = pool[:n]
    if combo_key(base, inc) not in ex and all(combo_key(base, o) != combo_key(base, inc) for o in picked):
        picked = [inc] + picked[: max(0, n - 1)]
    return picked


# ---- evaluation ---------------------------------------------------------------
_DATA: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}


def load_history(sym: str):
    if sym not in _DATA:
        sfx = "_alpaca"
        daily = pd.read_csv(f"data/history/{sym}_daily{sfx}.csv", index_col="date", parse_dates=True)
        ltf = pd.read_csv(f"data/history/{sym}_5m{sfx}.csv", index_col="ts", parse_dates=True)
        htf = pd.read_csv(f"data/history/{sym}_15m{sfx}.csv", index_col="ts", parse_dates=True)
        _DATA[sym] = (daily, ltf, htf)
    return _DATA[sym]


def _split_of(ts) -> str:
    y = pd.Timestamp(ts).year
    for name, lo, hi in SPLITS:
        if lo <= y <= hi:
            return name
    return "test"


def evaluate_task(task: dict) -> dict:
    """One (base, overrides, symbol) backtest. Returns R values per split (compact)."""
    from .strategies.spaces import BASES
    base, overrides, sym = task["base"], task["overrides"], task["symbol"]
    cfg = Config()
    cfg.bt_fill_model = "next_open"
    ov = {k: (tuple(v) if isinstance(v, list) else v) for k, v in overrides.items()}
    strat = BASES[base](name=f"{base}_tune", overrides=ov)
    daily, ltf, htf = load_history(sym)
    res = Backtester(cfg, strat, sym).run(daily, ltf, htf)
    rs: Dict[str, List[float]] = {"train": [], "val": [], "test": []}
    for t in res.trades:
        if t.exit_price is None:
            continue
        rs[_split_of(t.entry_ts)].append(round(float(t.r), 4))
    return {"key": combo_key(base, overrides), "base": base, "overrides": overrides, "symbol": sym,
            "rs": rs, "data_end": str(ltf.index.max().date()), "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def stats(rs: List[float]) -> dict:
    n = len(rs)
    if n == 0:
        return {"n": 0, "exp": 0.0, "pf": 0.0, "win": 0.0}
    w = sum(r for r in rs if r > 0)
    l = -sum(r for r in rs if r <= 0)
    return {"n": n, "exp": round(sum(rs) / n, 4), "pf": round(w / l, 3) if l else 99.0,
            "win": round(sum(1 for r in rs if r > 0) / n, 3)}


# ---- results store ----------------------------------------------------------------
def load_store(path: str = STORE) -> Dict[Tuple[str, str], dict]:
    out: Dict[Tuple[str, str], dict] = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            out[(r["key"], r["symbol"])] = r
    return out


def append_store(records: List[dict], path: str = STORE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")


# ---- selection ---------------------------------------------------------------------
@dataclass
class Candidate:
    base: str
    key: str
    overrides: dict
    pooled: Dict[str, dict]
    per_symbol: Dict[str, Dict[str, dict]]
    symbols_ok: List[str] = field(default_factory=list)
    passes: bool = False
    score: float = 0.0
    is_incumbent: bool = False
    reasons: List[str] = field(default_factory=list)


def assess(base: str, key: str, store: Dict[Tuple[str, str], dict], symbols: List[str]) -> Optional[Candidate]:
    recs = [store.get((key, s)) for s in symbols]
    if any(r is None for r in recs):
        return None
    pooled_rs = {sp: [] for sp, _, _ in SPLITS}
    per_symbol = {}
    for r in recs:
        per_symbol[r["symbol"]] = {sp: stats(r["rs"].get(sp, [])) for sp in pooled_rs}
        for sp in pooled_rs:
            pooled_rs[sp] += r["rs"].get(sp, [])
    pooled = {sp: stats(v) for sp, v in pooled_rs.items()}
    c = Candidate(base=base, key=key, overrides=recs[0]["overrides"], pooled=pooled, per_symbol=per_symbol)
    reasons = []
    for sp, need in MIN_TRADES.items():
        if pooled[sp]["n"] < need:
            reasons.append(f"{sp}: {pooled[sp]['n']} trades < {need}")
        elif pooled[sp]["exp"] <= 0:
            reasons.append(f"{sp}: expectancy {pooled[sp]['exp']:+.3f}R")
    if pooled["test"]["pf"] < MIN_TEST_PF:
        reasons.append(f"test pf {pooled['test']['pf']:.2f} < {MIN_TEST_PF}")
    c.reasons = reasons
    c.passes = not reasons
    c.score = min(pooled["val"]["exp"], pooled["test"]["exp"])
    c.symbols_ok = [s for s, st in per_symbol.items()
                    if st["val"]["n"] >= MIN_TRADES_SYMBOL and st["test"]["n"] >= MIN_TRADES_SYMBOL
                    and st["val"]["exp"] > 0 and st["test"]["exp"] > 0]
    return c


def select(base: str, store: Dict[Tuple[str, str], dict], symbols: List[str],
           long_only: bool = False) -> Tuple[Optional[Candidate], Optional[Candidate], List[Candidate]]:
    """Returns (incumbent, challenger-or-None, all assessed candidates sorted by score)."""
    inc_key = combo_key(base, incumbent_overrides(base, long_only))
    keys = {k for (k, _s) in store if store[(k, _s)]["base"] == base
            and not (long_only and _has_short(store[(k, _s)]["overrides"]))}
    cands = []
    for k in keys:
        c = assess(base, k, store, symbols)
        if c is None:
            continue
        c.is_incumbent = (k == inc_key)
        cands.append(c)
    cands.sort(key=lambda c: (c.passes, c.score), reverse=True)
    inc = next((c for c in cands if c.is_incumbent), None)
    best = next((c for c in cands if c.passes and not c.is_incumbent and c.symbols_ok), None)
    if best is None:
        return inc, None, cands
    if inc is not None and inc.passes:
        if not (best.pooled["val"]["exp"] >= inc.pooled["val"]["exp"] + IMPROVE_MARGIN_R
                and best.pooled["test"]["exp"] >= inc.pooled["test"]["exp"] + IMPROVE_MARGIN_R):
            return inc, None, cands
    return inc, best, cands


def to_spec(c: Candidate, note: str = "") -> dict:
    return {"base": c.base, "overrides": c.overrides, "symbols": c.symbols_ok,
            "evidence": {"pooled": c.pooled, "per_symbol": c.per_symbol, "score": c.score,
                         "rule": f"all splits > 0, pooled n >= {MIN_TRADES}, test pf >= {MIN_TEST_PF}, beats incumbent by {IMPROVE_MARGIN_R}R"},
            "note": note or "tuner time-split"}


# ---- orchestration -------------------------------------------------------------------
def run(bases: List[str], symbols: List[str], n_per_base: int, seed: int, workers: int = 0,
        store_path: str = STORE, log=print, long_only: bool = False) -> Dict[str, dict]:
    """Evaluate unexplored combos, append to the store, then select per base."""
    from concurrent.futures import ProcessPoolExecutor
    store = load_store(store_path)
    tasks = []
    for base in bases:
        explored = {k for (k, s) in store if store[(k, s)]["base"] == base and all((k, sym) in store for sym in symbols)}
        for ov in sample_combos(base, n_per_base, seed, explored, long_only):
            k = combo_key(base, ov)
            for sym in symbols:
                if (k, sym) not in store:
                    tasks.append({"base": base, "overrides": ov, "symbol": sym})
    log(f"tuner: {len(tasks)} backtests to run ({len(bases)} bases x {len(symbols)} symbols)")
    done = []
    if tasks:
        workers = workers or max(1, (os.cpu_count() or 2) - 1)
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for i, rec in enumerate(ex.map(evaluate_task, tasks, chunksize=1), 1):
                done.append(rec)
                store[(rec["key"], rec["symbol"])] = rec
                if i % 10 == 0 or i == len(tasks):
                    append_store(done, store_path)
                    done = []
                    log(f"tuner: {i}/{len(tasks)}")
    out = {}
    for base in bases:
        inc, best, cands = select(base, store, symbols, long_only)
        out[base] = {"incumbent": inc, "challenger": best, "candidates": cands}
    return out


def report_md(results: Dict[str, dict], symbols: List[str], when: str) -> str:
    md = [f"# Tuning run {when}", "", f"Symbols: {', '.join(symbols)}. Fills: next_open with slippage and commission. "
          f"Splits: train <= 2023, validate 2024, test 2025 on. Pass rule: every pooled split positive with "
          f"n >= {MIN_TRADES}, test PF >= {MIN_TEST_PF}; a challenger must beat the incumbent on validate AND test by {IMPROVE_MARGIN_R}R.", ""]
    for base, r in results.items():
        inc, best, cands = r["incumbent"], r["challenger"], r["candidates"]
        md.append(f"## {base}  ({len(cands)} combos assessed)")
        md.append("")
        if inc:
            p = inc.pooled
            md.append(f"- incumbent: train {p['train']['exp']:+.3f}R n{p['train']['n']} | val {p['val']['exp']:+.3f}R n{p['val']['n']} | "
                      f"test {p['test']['exp']:+.3f}R pf {p['test']['pf']:.2f} n{p['test']['n']} | {'PASS' if inc.passes else 'FAIL: ' + '; '.join(inc.reasons)}")
        if best:
            p = best.pooled
            md.append(f"- **challenger** {json.dumps(best.overrides)}: train {p['train']['exp']:+.3f}R n{p['train']['n']} | "
                      f"val {p['val']['exp']:+.3f}R n{p['val']['n']} | test {p['test']['exp']:+.3f}R pf {p['test']['pf']:.2f} n{p['test']['n']} | symbols {', '.join(best.symbols_ok)}")
        else:
            md.append("- no challenger beat the rule this run")
        md.append("")
        md.append("| overrides | train n/exp | val n/exp | test n/exp/pf | symbols ok | pass |")
        md.append("|---|---|---|---|---|---|")
        for c in cands[:12]:
            p = c.pooled
            md.append(f"| {'(incumbent) ' if c.is_incumbent else ''}{json.dumps(c.overrides)} | {p['train']['n']}/{p['train']['exp']:+.3f} | "
                      f"{p['val']['n']}/{p['val']['exp']:+.3f} | {p['test']['n']}/{p['test']['exp']:+.3f}/{p['test']['pf']:.2f} | "
                      f"{','.join(c.symbols_ok) or '-'} | {'yes' if c.passes else 'no'} |")
        md.append("")
    return "\n".join(md) + "\n"
