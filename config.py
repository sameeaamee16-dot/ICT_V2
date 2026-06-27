from __future__ import annotations
"""
config.py — ICT_V2 FINAL
=========================
All bugs fixed, all upgrades applied vs the GitHub version.

Changes from repo version:
1. Silver Bullet session added to kill_zones (10:00–11:00 UTC)
2. break_even_at_r raised 1.5 → 1.8 (stops noise-stopping at BE)
3. trail_after_r raised 2.5 → 2.8 (let winners run further)
4. partial_tp_ratio: 0.35 → 0.0 (disabled — kills edge on 0.01 lot)
5. micro_max_sl_points: 10 → 12 (XAUUSD 1m needs space)
6. micro_max_tp_points: 25 → 30
7. agent_max_consecutive_losses: 4 → 3 (faster agent cooldown)
8. high_winrate_min_confidence: 72 → 74 (tighter quality gate)
9. sideways_adx_threshold: 15 → 18 (more aggressive sideways block)
10. kill_zones: added "silver_bullet" 10:00–11:00 UTC
11. htf_min_aligned: 1 → 2 (at least 2 HTFs must agree)
12. protect_win_streak: 10 → 8 (protect streaks sooner)
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

    # FIX: partial_tp disabled — on 0.01 lot XAUUSD the half-position
    # (0.005 lot) earns ~$5 then the remainder can barely cover spread.
    # Better to run the full position to TP.
    partial_tp_ratio: float = 0.0       # was 0.35 — DISABLED
    partial_tp_at_r: float = 2.0

    # FIX: break_even moved to 1.8R (was 1.5). At 1.5R on 1m XAUUSD
    # normal retracements stop trades at BE before the move continues.
    break_even_at_r: float = 1.8        # was 1.5

    # FIX: trail starts at 2.8R (was 2.5) — lets winners run further
    trail_after_r: float = 2.8          # was 2.5

    atr_sl_mult: float = 0.85
    fixed_lot_size: float = 0.01
    use_micro_scalp_exits: bool = True
    micro_min_rr: float = 2.0
    micro_sl_points: float = 5.0
    micro_min_sl_points: float = 3.0

    # FIX: raised from 10 → 12 — XAUUSD 1m spreads spike to 8–10 pts
    # on news, so 10-point max was getting hit on legitimate setups
    micro_max_sl_points: float = 12.0   # was 10

    micro_tp_points: float = 14.0
    micro_min_tp_points: float = 10.0
    micro_max_tp_points: float = 30.0   # was 25

    fixed_profit_target_usd: float = 0.0  # DISABLED — was destroying RR

    # Agent guard — slightly stricter consecutive loss limit
    min_agent_trades_for_guard: int = 10
    agent_min_winrate_pct: float = 45.0
    agent_max_recent_loss: float = -15.0
    agent_recent_window: int = 15
    agent_max_consecutive_losses: int = 3   # was 4 — faster cooldown
    agent_loss_window: int = 8
    agent_max_losses_in_window: int = 6

    min_lot: float = 0.01
    max_lot: float = 10.0
    lot_step: float = 0.01

    calibration_min_samples: int = 15
    calibration_warn_winrate_pct: float = 45.0

    high_winrate_mode: bool = True
    target_winrate_pct: float = 60.0
    high_winrate_min_confidence: float = 74.0   # was 72 — tighter
    high_winrate_min_rr: float = 2.0
    high_winrate_min_entry_score: float = 65.0
    high_winrate_min_timing_score: float = 62.0
    mtf_alignment_floor: float = 0.50
    htf_min_aligned: int = 2               # was 1 — need 2 HTFs to agree

    protect_win_streak: int = 8            # was 10 — protect sooner
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
    displacement_atr_mult: float = 0.9
    fvg_min_atr: float = 0.08
    ob_lookback: int = 20
    mitigation_lookback: int = 80
    premium_discount_lookback: int = 120
    inducement_lookback: int = 45
    min_confirmations: int = 3
    min_confidence: float = 62.0
    # FIX: raised 15 → 18 — ADX < 18 is genuinely choppy on XAUUSD 1m
    sideways_adx_threshold: float = 18.0   # was 15
    low_atr_percentile: float = 0.12


@dataclass(frozen=True)
class SessionConfig:
    timezone: str = "UTC"
    kill_zones: Dict[str, tuple] = field(
        default_factory=lambda: {
            "london": ("06:30", "10:30"),
            # NEW: Silver Bullet — NY open first hour, highest ICT win-rate
            "silver_bullet": ("10:00", "11:00"),
            "new_york_am": ("12:00", "16:30"),
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
        default_factory=lambda: int(os.getenv("MINIMUM_ACTIVITY_MINUTES", "25"))
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
