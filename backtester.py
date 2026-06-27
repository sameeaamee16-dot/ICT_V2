from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd

from config import CONFIG, asset_profile
from indicators import resample_ohlcv
from signal_engine import SignalEngine
from trade_manager import TradeManager


@dataclass(frozen=True)
class BacktestResult:
    trades: int
    wins: int
    losses: int
    winrate: float
    profit_factor: float
    sharpe: float
    max_drawdown: float
    average_rr: float
    net_pnl: float
    agent_stats: dict
    equity_curve: pd.Series
    closed_trades: list


class Backtester:
    def __init__(self) -> None:
        self.signal_engine = SignalEngine()

    def run(self, one_minute: pd.DataFrame, warmup: int = 240) -> BacktestResult:
        manager = TradeManager()
        equity = []
        equity_index = []
        closed_seen = 0
        for i in range(warmup, len(one_minute)):
            history = one_minute.iloc[: i + 1]
            frames = self._frames(history)
            candle = history.iloc[-1].to_dict()
            manager.update(candle, history.index[-1].to_pydatetime())
            if len(manager.closed_trades) != closed_seen:
                for trade in manager.closed_trades[closed_seen:]:
                    self._apply_execution_costs(trade)
                closed_seen = len(manager.closed_trades)
            tick = self._tick_from_candle(candle)
            signals = self.signal_engine.generate_all(frames, tick)
            for signal in signals:
                manager.submit_signal(signal, tick)
            equity.append(sum(t.pnl for t in manager.closed_trades))
            equity_index.append(history.index[-1])
        return self._metrics(manager, pd.Series(equity, index=equity_index, dtype=float))

    def _frames(self, base: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return {tf: (base if tf == "1m" else resample_ohlcv(base, tf)) for tf in CONFIG.timeframes.all}

    def _tick_from_candle(self, candle: dict) -> Dict[str, float]:
        costs = CONFIG.backtest_costs
        spread_points = float(candle.get(costs.spread_column, costs.default_spread_points) or costs.default_spread_points)
        spread_price = spread_points / 10.0
        close = float(candle["close"])
        return {"bid": close - spread_price / 2.0, "ask": close + spread_price / 2.0, "spread": spread_points}

    def _apply_execution_costs(self, trade) -> None:
        costs = CONFIG.backtest_costs
        profile = asset_profile(trade.signal.symbol)
        slippage_price = costs.slippage_points / 10.0
        slippage_cost = slippage_price * trade.lot_size * profile.contract_size * 2
        commission = costs.commission_per_lot_round_turn * trade.lot_size
        total = round(slippage_cost + commission, 2)
        if total <= 0:
            return
        trade.pnl = round(trade.pnl - total, 2)
        trade.notes.append(f"Backtest execution costs: -{total:.2f}")

    def _metrics(self, manager: TradeManager, equity: pd.Series) -> BacktestResult:
        trades = manager.closed_trades
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        returns = equity.diff().fillna(0)
        sharpe = 0.0 if returns.std() == 0 else float((returns.mean() / returns.std()) * np.sqrt(252))
        peak = equity.cummax()
        dd = (equity - peak).min() if len(equity) else 0.0
        avg_rr = float(np.mean([t.rr_achieved for t in trades])) if trades else 0.0
        return BacktestResult(
            trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            winrate=round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
            profit_factor=round(gross_win / gross_loss, 2) if gross_loss else float("inf") if gross_win else 0.0,
            sharpe=round(sharpe, 2),
            max_drawdown=round(float(dd), 2),
            average_rr=round(avg_rr, 2),
            net_pnl=round(sum(t.pnl for t in trades), 2),
            agent_stats=manager.agent_stats(),
            equity_curve=equity,
            closed_trades=trades,
        )
