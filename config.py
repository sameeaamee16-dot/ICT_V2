from __future__ import annotations

"""
WINRATE UPGRADE v2
==================
Goals:
  1. 65-70% winrate (was 43%)
  2. At least 1 trade per 30 minutes
  
Root causes of 43% winrate identified:
  A. fixed_profit_target_usd=$4 was closing winning trades FAR too early at 1m 
     scalp levels — killing RR and creating artificial losses when price continued.
  B. Agent guard still blocked after 6 trades with winrate < 60% — too strict.
  C. Adaptive trainer blocked after only 3 samples — not enough data.
  D. Calibration at 55% warn threshold still rejecting many valid signals.
  E. Off-session trades blocked — but Asia and late NY have real setups.

Fixes in this file:
  - fixed_profit_target_usd: 4.0 -> 0.0 (DISABLED — was destroying RR)
  - minimum_activity_minutes: 30 -> 25 (slightly more aggressive fallback)
  - calibration_warn_winrate_pct: 55 -> 45
  - agent_min_winrate_pct: 52 -> 45
  - min_agent_trades_for_guard: 6 -> 10 (more data before blocking)
  - high_winrate_min_confidence: 75 -> 72 (slight relaxation)
  - break_even_at_r: 1.2 -> 1.5 (give trades more room before BE move)
  - trail_after_r: 2.0 -> 2.5 (let winners run longer)
  - partial_tp_at_r: 1.5 -> 2.0 (take partial at better level)
  - micro_max_sl_points: 8 -> 10 (prevent fast stops on XAUUSD volatility)
  - micro_max_tp_points: 20 -> 25 (let TP reach further)
"""

from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional
import os


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"


@dataclass(frozen=True)
class TimeframeConfig:
    primary: str = "1m"
    execution: str = "1m"
    confluence: List[str] = field(default_factory=lambda: ["5m", "15m", "1h"])
    all: List[str] = field(default_factory=lambda: ["1m", "5m", "15m", "1h"])


@dataclass
class RiskConfig:
    account_equity: float = 100_000.0
    risk_per_trade_pct: float = 0.5
    max_daily_drawdown_pct: float = 3.0
    max_concurrent_trades: int = 1
    max_spread_points: float = 55.0
    min_rr: float = 2.0
    partial_tp_ratio: float = 0.35
    partial_tp_at_r: float = 2.0           # was 1.5 → give more room before partial
    break_even_at_r: float = 1.5           # was 1.2 → stopped killing trades early
    trail_after_r: float = 2.5             # was 2.0 → let winners run
    atr_sl_mult: float = 0.85
    fixed_lot_size: float = 0.01
    use_micro_scalp_exits: bool = True
    micro_min_rr: float = 2.0
    micro_sl_points: float = 5.0
    micro_min_sl_points: float = 3.0
    micro_max_sl_points: float = 10.0      # was 8 → XAUUSD needs room on 1m
    micro_tp_points: float = 14.0          # was 12 → slightly wider TP
    micro_min_tp_points: float = 10.0
    micro_max_tp_points: float = 25.0      # was 20 → let price run
    fixed_profit_target_usd: float = 0.0   # DISABLED — was destroying RR by closing at $4
    min_agent_trades_for_guard: int = 10   # was 6 → need more samples before blocking
    agent_min_winrate_pct: float = 45.0    # was 52 → more lenient
    agent_max_recent_loss: float = -15.0   # was -12 → more tolerance
    agent_recent_window: int = 15          # was 12 → larger sample window
    agent_max_consecutive_losses: int = 4  # was 3 → more tolerance
    agent_loss_window: int = 8             # was 6
    agent_max_losses_in_window: int = 6    # was 5
    min_lot: float = 0.01
    max_lot: float = 10.0
    lot_step: float = 0.01
    calibration_min_samples: int = 15      # was 20 → calibrate faster
    calibration_warn_winrate_pct: float = 45.0   # was 55 → less aggressive blocking
    high_winrate_mode: bool = True
    target_winrate_pct: float = 60.0
    high_winrate_min_confidence: float = 72.0    # was 75 → slight relaxation
    high_winrate_min_rr: float = 2.0
    high_winrate_min_entry_score: float = 65.0   # was 68 → small relaxation
    high_winrate_min_timing_score: float = 62.0  # was 65
    mtf_alignment_floor: float = 0.50            # was 0.55 → easier to meet
    htf_min_aligned: int = 1
    protect_win_streak: int = 10                 # was 8 → protection kicks in later
    protect_streak_min_confidence: float = 78.0
    protect_streak_min_rr: float = 2.2
    protect_streak_min_entry_score: float = 70.0


@dataclass(frozen=True)
class BacktestCostConfig:
    default_spread_points: float = 25.0
    slippage_points: float = 3.0
    commission_per_lot_round_turn: float = 7.0
    spread_column: str = "spread"


@dataclass(frozen=True)
class AssetProfile:
    name: str
    symbols: tuple
    contract_size: float
    max_spread_points: float
    min_rr: float
    min_confidence: float
    atr_sl_mult: float
    htf_bias_lock: bool = True
    max_same_setup_open: int = 1
    duplicate_entry_atr: float = 0.55


@dataclass(frozen=True)
class IctConfig:
    swing_left: int = 3
    swing_right: int = 2
    equal_level_atr_tolerance: float = 0.18
    displacement_atr_mult: float = 0.9       # slightly relaxed from 1.0
    fvg_min_atr: float = 0.08               # was 0.10 → catch more FVGs
    ob_lookback: int = 20                   # was 16 → look further back
    mitigation_lookback: int = 80
    premium_discount_lookback: int = 120
    inducement_lookback: int = 45
    min_confirmations: int = 3              # was 4 → easier to get a signal
    min_confidence: float = 62.0            # was 65
    sideways_adx_threshold: float = 15.0    # was 17 → less aggressive sideways filter
    low_atr_percentile: float = 0.12        # was 0.15


@dataclass(frozen=True)
class SessionConfig:
    timezone: str = "UTC"
    kill_zones: Dict[str, tuple] = field(
        default_factory=lambda: {
            "london": ("06:30", "10:30"),      # extended slightly
            "new_york_am": ("12:00", "16:30"), # extended slightly
            "new_york_pm": ("17:30", "20:30"),
            "asia": ("00:00", "03:30"),
        }
    )


@dataclass(frozen=True)
class DataConfig:
    symbol: str = field(default_factory=lambda: os.getenv("TRADING_SYMBOL", "XAUUSD"))
    tradingview_symbol: str = field(default_factory=lambda: os.getenv("TRADINGVIEW_SYMBOL", "OANDA:XAUUSD"))
    mt5_symbol_candidates: List[str] = field(
        default_factory=lambda: [
            item.strip()
            for item in os.getenv(
                "MT5_SYMBOL_CANDIDATES",
                "XAUUSD,XAUUSDm,GOLD,XAUUSD.pro,GOLDmicro,XAUUSD.a",
            ).split(",")
            if item.strip()
        ]
    )
    news_blackout_utc: str = field(default_factory=lambda: os.getenv("NEWS_BLACKOUT_UTC", ""))
    history_bars: int = field(default_factory=lambda: int(os.getenv("HISTORY_BARS", "1500")))
    poll_seconds: float = field(default_factory=lambda: float(os.getenv("POLL_SECONDS", "0.25")))
    closed_candle_refresh_seconds: float = field(
        default_factory=lambda: float(os.getenv("CLOSED_CANDLE_REFRESH_SECONDS", "1.0"))
    )
    aggressive_intrabar_mode: bool = field(
        default_factory=lambda: os.getenv("AGGRESSIVE_INTRABAR_MODE", "true").lower() in {"1", "true", "yes", "on"}
    )
    execution_countdown_seconds: int = field(
        default_factory=lambda: int(os.getenv("EXECUTION_COUNTDOWN_SECONDS", "3"))
    )
    execution_countdown_mode: str = field(
        default_factory=lambda: os.getenv("EXECUTION_COUNTDOWN_MODE", "visual").lower()
    )
    dashboard_refresh_ms: int = field(default_factory=lambda: int(os.getenv("DASHBOARD_REFRESH_MS", "500")))
    minimum_activity_minutes: int = field(
        default_factory=lambda: int(os.getenv("MINIMUM_ACTIVITY_MINUTES", "25"))  # was 30
    )
    minimum_activity_enabled: bool = field(
        default_factory=lambda: os.getenv("MINIMUM_ACTIVITY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    )


@dataclass(frozen=True)
class MySQLConfig:
    host: str = field(default_factory=lambda: os.getenv("MYSQL_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("MYSQL_PORT", "3307")))
    user: str = field(default_factory=lambda: os.getenv("MYSQL_USER", "root"))
    password: str = field(default_factory=lambda: os.getenv("MYSQL_PASSWORD", "Admin"))
    database: str = field(default_factory=lambda: os.getenv("MYSQL_DATABASE", "ict"))


@dataclass
class AppConfig:
    data: DataConfig = field(default_factory=DataConfig)
    timeframes: TimeframeConfig = field(default_factory=TimeframeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest_costs: BacktestCostConfig = field(default_factory=BacktestCostConfig)
    ict: IctConfig = field(default_factory=IctConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    mysql: MySQLConfig = field(default_factory=MySQLConfig)
    asset_profiles: Dict[str, AssetProfile] = field(
        default_factory=lambda: {
            "XAU": AssetProfile(
                name="XAU",
                symbols=("XAU", "GOLD"),
                contract_size=100.0,
                max_spread_points=55.0,
                min_rr=2.0,
                min_confidence=62.0,
                atr_sl_mult=1.0,
                htf_bias_lock=True,
                max_same_setup_open=1,
                duplicate_entry_atr=0.6,
            ),
            "DEFAULT": AssetProfile(
                name="DEFAULT",
                symbols=(),
                contract_size=100.0,
                max_spread_points=55.0,
                min_rr=2.0,
                min_confidence=62.0,
                atr_sl_mult=1.0,
            ),
        }
    )


CONFIG = AppConfig()


def asset_profile(symbol: str | None = None) -> AssetProfile:
    target = (symbol or CONFIG.data.symbol).upper()
    for profile in CONFIG.asset_profiles.values():
        if profile.name == "DEFAULT":
            continue
        if any(term in target for term in profile.symbols):
            return profile
    return CONFIG.asset_profiles["DEFAULT"]


def active_news_blackout(now: Optional[datetime] = None) -> str | None:
    raw = CONFIG.data.news_blackout_utc.strip()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if raw:
        for window in raw.split(";"):
            if "/" not in window:
                continue
            start_raw, end_raw = [part.strip() for part in window.split("/", 1)]
            try:
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if start <= now <= end:
                return f"{start.isoformat()} to {end.isoformat()}"
    csv_path = os.getenv("ECONOMIC_NEWS_CSV", "").strip()
    if csv_path:
        before = int(os.getenv("NEWS_BLACKOUT_BEFORE_MIN", "20"))
        after = int(os.getenv("NEWS_BLACKOUT_AFTER_MIN", "20"))
        try:
            from datetime import timedelta
            for line in Path(csv_path).read_text(encoding="utf-8").splitlines():
                if not line.strip() or line.lower().startswith("time"):
                    continue
                parts = [part.strip() for part in line.split(",")]
                try:
                    event_time = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
                except (IndexError, ValueError):
                    continue
                impact = parts[2].lower() if len(parts) > 2 else "high"
                if impact not in {"high", "red", "major"}:
                    continue
                if event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=timezone.utc)
                start = event_time - timedelta(minutes=before)
                end = event_time + timedelta(minutes=after)
                if start <= now <= end:
                    title = parts[1] if len(parts) > 1 else "economic news"
                    return f"{title}: {start.isoformat()} to {end.isoformat()}"
        except OSError:
            return None
    return None
