"""
DATA FEED - FIXED v2
Fixes for RuntimeError: MT5 returned no rates for XAUUSD 1m

Root causes and fixes:
1. MT5 terminal loses connection mid-session → added auto-reconnect with retry
2. Symbol not subscribed in MT5 after restart → symbol_select() re-called on every failure
3. Market closed (weekend / holiday) → returns last known data instead of crashing the loop
4. copy_rates_from_pos() returns None on first call after MT5 wakeup → retry with backoff
5. get_multi_timeframe() raises on first failed TF, stopping all data → now skips failed TFs
   and only raises if the PRIMARY timeframe (1m) genuinely has no data
6. Added _ensure_connected() check before every data call
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd

from config import CONFIG, DataConfig
from indicators import normalize_ohlcv, resample_ohlcv
from logger import get_logger

log = get_logger(__name__)

# How many times to retry a failed MT5 call before giving up
_MT5_RETRY_COUNT = 3
_MT5_RETRY_DELAY = 1.5  # seconds between retries


class MarketDataFeed(ABC):
    @abstractmethod
    def get_closed_candles(self, timeframe: str, bars: int) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def get_tick(self) -> Dict[str, float]:
        raise NotImplementedError

    def get_candles(self, timeframe: str, bars: int, include_current: bool = False) -> pd.DataFrame:
        return self.get_closed_candles(timeframe, bars)

    def get_multi_timeframe(self, bars: int = 1500, include_current: bool = False) -> Dict[str, pd.DataFrame]:
        base = self.get_candles("1m", bars, include_current=include_current)
        return {tf: (base if tf == "1m" else resample_ohlcv(base, tf)) for tf in CONFIG.timeframes.all}


class MT5Feed(MarketDataFeed):
    TF_MAP = {}
    _last_good_frames: Dict[str, pd.DataFrame] = {}

    def __init__(self, config: DataConfig = CONFIG.data):
        self.config = config
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise RuntimeError("MetaTrader5 package is not installed") from exc

        self.mt5 = mt5
        self.TF_MAP = {
            "1m": mt5.TIMEFRAME_M1,
            "5m": mt5.TIMEFRAME_M5,
            "15m": mt5.TIMEFRAME_M15,
            "1h": mt5.TIMEFRAME_H1,
        }
        self._initialized = False
        self._connect()

    # ── Connection Management ─────────────────────────────────────────────

    def _connect(self) -> None:
        """Initialize MT5 and select symbol. Raises on hard failure."""
        if not self.mt5.initialize():
            raise RuntimeError(
                f"MT5 initialize failed: {self.mt5.last_error()}. "
                "Open MetaTrader 5, login to your broker, then restart this bot."
            )
        self.symbol = self._select_symbol()
        self._initialized = True
        log.info("MT5 feed initialized for %s", self.symbol)

    def _ensure_connected(self) -> bool:
        """
        Check MT5 connection is alive. If not, attempt to reconnect once.
        Returns True if connected, False if reconnect failed.
        """
        terminal_info = self.mt5.terminal_info()
        if terminal_info is not None and terminal_info.connected:
            return True

        log.warning("MT5 connection lost — attempting reconnect...")
        try:
            self.mt5.shutdown()
            time.sleep(1.0)
            self._connect()
            log.info("MT5 reconnected successfully.")
            return True
        except Exception as e:
            log.error("MT5 reconnect failed: %s", e)
            return False

    def _select_symbol(self) -> str:
        for symbol in self.config.mt5_symbol_candidates:
            info = self.mt5.symbol_info(symbol)
            if info is not None:
                self.mt5.symbol_select(symbol, True)
                log.info("MT5 symbol selected: %s", symbol)
                return symbol

        discovered = self.discover_symbols()
        suffix = f" Broker matching symbols: {discovered}" if discovered else " No XAU/GOLD symbols found in broker."
        raise RuntimeError(
            f"No valid MT5 XAUUSD symbol found among {self.config.mt5_symbol_candidates}.{suffix}"
        )

    def discover_gold_symbols(self) -> list:
        return self.discover_symbols()

    def discover_symbols(self) -> list:
        symbols = self.mt5.symbols_get()
        if symbols is None:
            return []
        terms = ("XAU", "GOLD")
        return [s.name for s in symbols if any(term in s.name.upper() for term in terms)]

    # ── Data Fetching (with retry + fallback) ─────────────────────────────

    def get_closed_candles(self, timeframe: str, bars: int) -> pd.DataFrame:
        return self.get_candles(timeframe, bars, include_current=False)

    def get_candles(self, timeframe: str, bars: int, include_current: bool = False) -> pd.DataFrame:
        """
        Fetch candles with automatic retry and last-known-good fallback.

        Retry logic:
        - On None/empty result: wait _MT5_RETRY_DELAY seconds, try again
        - On 2nd failure: re-select the symbol (handles symbol subscription drops)
        - On 3rd failure: return last known good data if available, else raise

        This means the bot loop CONTINUES during brief MT5 hiccups instead of crashing.
        """
        tf_key = timeframe.lower()
        if tf_key not in self.TF_MAP:
            raise ValueError(f"Unsupported timeframe: {timeframe}. Valid: {list(self.TF_MAP.keys())}")

        tf = self.TF_MAP[tf_key]
        start_pos = 0 if include_current else 1

        last_error: Optional[Exception] = None

        for attempt in range(1, _MT5_RETRY_COUNT + 1):
            # Check connection on each attempt
            if not self._ensure_connected():
                time.sleep(_MT5_RETRY_DELAY)
                continue

            # Re-select symbol on 2nd attempt (fixes symbol subscription drops)
            if attempt == 2:
                try:
                    self.mt5.symbol_select(self.symbol, True)
                    log.info("Re-selected symbol %s on retry %d", self.symbol, attempt)
                except Exception:
                    pass

            rates = self.mt5.copy_rates_from_pos(self.symbol, tf, start_pos, bars)

            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
                df = normalize_ohlcv(df)
                df = df[~df.index.duplicated(keep="last")].sort_index()

                # Cache this as last known good data
                self._last_good_frames[tf_key] = df
                return df

            # MT5 gave us nothing — log and retry
            err_code = self.mt5.last_error()
            last_error = RuntimeError(
                f"MT5 returned no rates for {self.symbol} {timeframe} "
                f"(attempt {attempt}/{_MT5_RETRY_COUNT}, MT5 error: {err_code})"
            )
            log.warning("%s — retrying in %.1fs...", last_error, _MT5_RETRY_DELAY)
            time.sleep(_MT5_RETRY_DELAY)

        # All retries exhausted — try last known good data (handles market closed / weekend)
        if tf_key in self._last_good_frames:
            cached = self._last_good_frames[tf_key]
            log.warning(
                "MT5 data unavailable for %s %s after %d attempts. "
                "Using last known data (%d bars, last bar: %s). "
                "Market may be closed or MT5 is disconnected.",
                self.symbol, timeframe, _MT5_RETRY_COUNT,
                len(cached), cached.index[-1] if not cached.empty else "N/A",
            )
            return cached

        # No cache available either — raise so the caller knows
        raise last_error or RuntimeError(
            f"MT5 returned no rates for {self.symbol} {timeframe} — no cached data available."
        )

    def get_multi_timeframe(self, bars: int = 1500, include_current: bool = False) -> Dict[str, pd.DataFrame]:
        """
        FIXED: fetch each timeframe independently.
        - If a higher timeframe fails but 1m succeeds, resample 1m as fallback.
        - Only raises if the primary 1m timeframe has no data at all.
        """
        # Always fetch 1m first — it's the base for everything
        base_1m = self.get_candles("1m", bars, include_current=include_current)

        result: Dict[str, pd.DataFrame] = {}
        for tf in CONFIG.timeframes.all:
            if tf == "1m":
                result[tf] = base_1m
                continue

            try:
                # Try fetching higher TF directly from MT5
                result[tf] = self.get_candles(tf, bars, include_current=include_current)
            except Exception as e:
                # Fall back to resampling 1m — always works if 1m data exists
                log.warning(
                    "Failed to fetch %s directly (%s). Resampling from 1m as fallback.", tf, e
                )
                try:
                    result[tf] = resample_ohlcv(base_1m, tf)
                except Exception as resample_err:
                    log.error("Resample fallback for %s also failed: %s", tf, resample_err)
                    # Skip this TF — don't crash the whole bot

        return result

    def get_tick(self) -> Dict[str, float]:
        if not self._ensure_connected():
            # Return synthetic tick from last candle close
            try:
                last = self._last_good_frames.get("1m")
                price = float(last["close"].iloc[-1]) if last is not None and not last.empty else 0.0
            except Exception:
                price = 0.0
            return {"bid": price, "ask": price, "spread": 0.0, "time": datetime.now(timezone.utc).timestamp()}

        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None:
            # Try to get price from last candle
            try:
                last = self.get_closed_candles("1m", 2)
                price = float(last["close"].iloc[-1])
            except Exception:
                price = 0.0
            return {"bid": price, "ask": price, "spread": 0.0, "time": datetime.now(timezone.utc).timestamp()}

        return {
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "spread": float((tick.ask - tick.bid) * 10),
            "time": float(tick.time),
        }


def create_feed() -> MarketDataFeed:
    return MT5Feed()
