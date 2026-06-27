from __future__ import annotations

import numpy as np
import pandas as pd

from backtester import Backtester
from indicators import resample_ohlcv
from signal_engine import SignalEngine


def synthetic_xauusd(rows: int = 700) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    index = pd.date_range("2026-01-01", periods=rows, freq="1min", tz="UTC")
    drift = np.linspace(0, 16, rows)
    cycle = np.sin(np.linspace(0, 22, rows)) * 3.5
    noise = rng.normal(0, 0.42, rows).cumsum() * 0.12
    close = 3350 + drift + cycle + noise
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0.55, 0.18, rows))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.integers(120, 650, rows)

    # Inject a closed-candle ICT-like sequence: sweep, displacement, and FVG.
    i = rows - 9
    low[i] = low[i - 20 : i].min() - 3.0
    close[i] = open_[i] + 1.2
    open_[i + 1] = close[i]
    close[i + 1] = close[i] + 4.8
    high[i + 1] = close[i + 1] + 0.9
    low[i + 1] = open_[i + 1] - 0.15
    low[i + 2] = high[i] + 0.8
    open_[i + 2] = low[i + 2] + 0.2
    close[i + 2] = open_[i + 2] + 1.3
    high[i + 2] = close[i + 2] + 0.5

    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index)


def main() -> None:
    df = synthetic_xauusd()
    frames = {tf: (df if tf == "1m" else resample_ohlcv(df, tf)) for tf in ["1m", "5m", "15m", "1h"]}
    engine = SignalEngine()
    snapshots = engine.analyze(frames)
    signals = engine.generate_all(frames, {"bid": df["close"].iloc[-1] - 0.05, "ask": df["close"].iloc[-1] + 0.05, "spread": 1.0})
    print("snapshots", sorted(snapshots))
    print("primary_bias", snapshots["1m"].bias)
    print("concept_count", len(snapshots["1m"].concepts))
    print("signals", [signal.format_terminal().splitlines()[0] for signal in signals] or ["none"])
    result = Backtester().run(df.tail(280), warmup=276)
    print("backtest_trades", result.trades)
    print("backtest_net_pnl", result.net_pnl)


if __name__ == "__main__":
    main()
