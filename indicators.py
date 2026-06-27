from __future__ import annotations

import numpy as np
import pandas as pd


def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [c.lower().replace(" ", "_") for c in out.columns]
    if "time" in out.columns:
        out["time"] = pd.to_datetime(out["time"], utc=True)
        out = out.drop_duplicates(subset=["time"], keep="last")
        out = out.sort_values("time")
        out = out.set_index("time")
    elif isinstance(out.index, pd.DatetimeIndex):
        out = out.sort_index()
        out = out[~out.index.duplicated(keep="last")]
    else:
        raise ValueError("OHLCV data requires a DatetimeIndex or a time column")
    return out


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    out = clean_ohlcv(df)
    if "tick_volume" in out.columns and "volume" not in out.columns:
        out["volume"] = out["tick_volume"]
    if "real_volume" in out.columns and "volume" not in out.columns:
        out["volume"] = out["real_volume"]
    out = out.rename(columns={"tick_volume": "volume"})
    out = out.loc[:, ~out.columns.duplicated(keep="first")]
    needed = ["open", "high", "low", "close"]
    missing = [c for c in needed if c not in out.columns]
    if missing:
        raise ValueError(f"Missing OHLC columns: {missing}")
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out = out[["open", "high", "low", "close", "volume"]].dropna()
    out = out.apply(pd.to_numeric, errors="coerce").dropna()
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [(df["high"] - df["low"]), (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def vwap(df: pd.DataFrame, window: int = 96) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, np.nan)
    pv = typical * vol
    rolling = pv.rolling(window, min_periods=8).sum() / vol.rolling(window, min_periods=8).sum()
    fallback = typical.rolling(window, min_periods=8).mean()
    return rolling.fillna(fallback)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(df)
    atr_val = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_val
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rolling_percentile(series: pd.Series, window: int = 240) -> pd.Series:
    return series.rolling(window, min_periods=max(20, window // 5)).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
    )


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    df = normalize_ohlcv(df)
    rule = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "1H": "1h"}[timeframe]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(rule, label="right", closed="right").agg(agg).dropna()
    return normalize_ohlcv(out)
