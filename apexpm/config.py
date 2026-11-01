"""Environment-driven configuration with conservative defaults.

Every tunable lives here. Nothing else reads os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _b(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _f(key: str, default: float) -> float:
    raw = os.getenv(key)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _i(key: str, default: int) -> int:
    raw = os.getenv(key)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _s(key: str, default: str) -> str:
    raw = os.getenv(key)
    return raw if raw not in (None, "") else default


def _list(key: str, default: List[str]) -> List[str]:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


@dataclass
class Config:
    # Credentials and mode
    alpaca_api_key: str = field(default_factory=lambda: _s("ALPACA_API_KEY", ""), repr=False)
    alpaca_secret_key: str = field(default_factory=lambda: _s("ALPACA_SECRET_KEY", ""), repr=False)
    paper: bool = field(default_factory=lambda: _b("PAPER", True))
    dry_run: bool = field(default_factory=lambda: _b("DRY_RUN", True))

    # Universe and session
    symbols: List[str] = field(default_factory=lambda: _list("SYMBOLS", ["SPY"]))
    scan_interval_sec: int = field(default_factory=lambda: _i("SCAN_INTERVAL_SEC", 15))
    stream_enabled: bool = field(default_factory=lambda: _b("STREAM_ENABLED", True))
    data_feed: str = field(default_factory=lambda: _s("DATA_FEED", "iex"))   # iex (free) | sip (paid)
    bar_minutes: int = field(default_factory=lambda: _i("BAR_MINUTES", 5))
    htf_bar_minutes: int = field(default_factory=lambda: _i("HTF_BAR_MINUTES", 15))
    no_entry_after: str = field(default_factory=lambda: _s("NO_ENTRY_AFTER", "15:30"))
    flat_by: str = field(default_factory=lambda: _s("FLAT_BY", "15:55"))

    # Levels and ribbon
    atr_period: int = field(default_factory=lambda: _i("ATR_PERIOD", 14))
    ema_fast: int = field(default_factory=lambda: _i("EMA_FAST", 9))
    ema_mid: int = field(default_factory=lambda: _i("EMA_MID", 21))
    ema_slow: int = field(default_factory=lambda: _i("EMA_SLOW", 50))

    level_tolerance_atr: float = field(default_factory=lambda: _f("LEVEL_TOLERANCE_ATR", 0.02))
    allow_gap_fills: bool = field(default_factory=lambda: _b("ALLOW_GAP_FILLS", False))

    # Regime
    blackout_dates: List[str] = field(default_factory=lambda: _list("BLACKOUT_DATES", []))

    # Sizing
    kelly_fraction: float = field(default_factory=lambda: _f("KELLY_FRACTION", 0.25))
    prior_win_rate: float = field(default_factory=lambda: _f("PRIOR_WIN_RATE", 0.60))
    min_trades_for_stats: int = field(default_factory=lambda: _i("MIN_TRADES_FOR_STATS", 20))
    max_pos_pct: float = field(default_factory=lambda: _f("MAX_POS_PCT", 0.02))
    max_pos_dollars: float = field(default_factory=lambda: _f("MAX_POS_DOLLARS", 5000.0))
    fractional_shares: bool = field(default_factory=lambda: _b("FRACTIONAL_SHARES", True))
    min_notional: float = field(default_factory=lambda: _f("MIN_NOTIONAL", 1.0))
    max_notional_pct: float = field(default_factory=lambda: _f("MAX_NOTIONAL_PCT", 0.95))  # of equity (no margin)
    target_daily_vol: float = field(default_factory=lambda: _f("TARGET_DAILY_VOL", 0.01))
    vol_scale_min: float = field(default_factory=lambda: _f("VOL_SCALE_MIN", 0.25))
    vol_scale_max: float = field(default_factory=lambda: _f("VOL_SCALE_MAX", 1.5))

    # Risk
    kill_dd_pct: float = field(default_factory=lambda: _f("KILL_DD_PCT", 0.05))
    max_daily_loss: float = field(default_factory=lambda: _f("MAX_DAILY_LOSS", 200.0))
    max_trades_per_day: int = field(default_factory=lambda: _i("MAX_TRADES_PER_DAY", 4))
    max_positions: int = field(default_factory=lambda: _i("MAX_POSITIONS", 2))
    stop_cooldown_min: int = field(default_factory=lambda: _i("STOP_COOLDOWN_MIN", 30))
    live_abort_delay_sec: int = field(default_factory=lambda: _i("LIVE_ABORT_DELAY_SEC", 10))



    # EMA hold (owner's recipe) false-breakout switches
    eh_require_htf: bool = field(default_factory=lambda: _b("EH_REQUIRE_HTF", True))
    eh_require_volume: bool = field(default_factory=lambda: _b("EH_REQUIRE_VOLUME", True))
    eh_vol_mult: float = field(default_factory=lambda: _f("EH_VOL_MULT", 1.0))
    eh_require_room: bool = field(default_factory=lambda: _b("EH_REQUIRE_ROOM", True))
    eh_min_room_atr: float = field(default_factory=lambda: _f("EH_MIN_ROOM_ATR", 0.1))
    eh_first_minutes: int = field(default_factory=lambda: _i("EH_FIRST_MINUTES", 210))
    eh_skip_lunch: bool = field(default_factory=lambda: _b("EH_SKIP_LUNCH", True))

    # Governance / learning loop
    review_window_trades: int = field(default_factory=lambda: _i("REVIEW_WINDOW_TRADES", 30))
    review_min_trades: int = field(default_factory=lambda: _i("REVIEW_MIN_TRADES", 20))
    suspend_expectancy_r: float = field(default_factory=lambda: _f("SUSPEND_EXPECTANCY_R", 0.0))
    suspend_profit_factor: float = field(default_factory=lambda: _f("SUSPEND_PROFIT_FACTOR", 1.0))
    suspend_dd_r: float = field(default_factory=lambda: _f("SUSPEND_DD_R", 6.0))
    suspend_discipline: float = field(default_factory=lambda: _f("SUSPEND_DISCIPLINE", 0.70))
    suspend_loss_streak: int = field(default_factory=lambda: _i("SUSPEND_LOSS_STREAK", 6))

    # Webhook
    webhook_enabled: bool = field(default_factory=lambda: _b("WEBHOOK_ENABLED", False))
    webhook_port: int = field(default_factory=lambda: _i("WEBHOOK_PORT", 8787))
    webhook_secret: str = field(default_factory=lambda: _s("WEBHOOK_SECRET", ""), repr=False)
    webhook_bind: str = field(default_factory=lambda: _s("WEBHOOK_BIND", "127.0.0.1"))
    webhook_max_body: int = field(default_factory=lambda: _i("WEBHOOK_MAX_BODY", 65536))
    entry_good_for_bars: int = field(default_factory=lambda: _i("ENTRY_GOOD_FOR_BARS", 3))
    max_price_age_sec: int = field(default_factory=lambda: _i("MAX_PRICE_AGE_SEC", 120))

    # Exit engine
    exit_be_at_r: float = field(default_factory=lambda: _f("EXIT_BE_AT_R", 1.0))
    exit_be_offset_atr: float = field(default_factory=lambda: _f("EXIT_BE_OFFSET_ATR", 0.02))
    exit_runner: bool = field(default_factory=lambda: _b("EXIT_RUNNER", True))
    exit_trail_atr: float = field(default_factory=lambda: _f("EXIT_TRAIL_ATR", 0.1))
    max_daily_loss_pct: float = field(default_factory=lambda: _f("MAX_DAILY_LOSS_PCT", 0.06))  # of session start equity

    # Backtest realism
    bt_fill_model: str = field(default_factory=lambda: _s("BT_FILL_MODEL", "limit"))  # limit | next_open
    bt_slippage: float = field(default_factory=lambda: _f("BT_SLIPPAGE", 0.03))        # $/share per marketable side
    bt_commission: float = field(default_factory=lambda: _f("BT_COMMISSION", 0.01))    # $/share per side

    # Dashboard
    dashboard_bind: str = field(default_factory=lambda: _s("DASHBOARD_BIND", "127.0.0.1"))
    dashboard_port: int = field(default_factory=lambda: _i("PORT", _i("DASHBOARD_PORT", 8788)))  # Render sets PORT
    run_dashboard: bool = field(default_factory=lambda: _b("RUN_DASHBOARD", False))            # serve alongside the bot
    dashboard_token: str = field(default_factory=lambda: _s("DASHBOARD_TOKEN", ""), repr=False)

    # Storage
    db_path: str = field(default_factory=lambda: _s("DB_PATH", "data/apexpm.db"))
    log_path: str = field(default_factory=lambda: _s("LOG_PATH", "logs/apexpm.log"))

    def validate(self) -> List[str]:
        """Return a list of problems. Empty list means usable."""
        problems = []
        if not self.alpaca_api_key or not self.alpaca_secret_key:
            problems.append("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
        if not self.paper and self.dry_run is False:
            problems.append("LIVE MODE: PAPER=false and DRY_RUN=false")
        if not 0 < self.kelly_fraction <= 1:
            problems.append("KELLY_FRACTION must be in (0, 1]")
        if not 0 < self.max_pos_pct <= 0.2:
            problems.append("MAX_POS_PCT must be in (0, 0.2]")
        if self.webhook_enabled and not self.webhook_secret:
            problems.append("WEBHOOK_ENABLED requires WEBHOOK_SECRET")
        if self.dashboard_bind not in ("127.0.0.1", "localhost") and not self.dashboard_token:
            problems.append("DASHBOARD_BIND beyond localhost requires DASHBOARD_TOKEN")
        return problems

    @property
    def mode(self) -> str:
        if self.dry_run:
            return "DRY_RUN"
        return "PAPER" if self.paper else "LIVE"


def load() -> Config:
    return Config()
