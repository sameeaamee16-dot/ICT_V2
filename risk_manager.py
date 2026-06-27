from __future__ import annotations
"""
risk_manager.py — ICT_V2 FINAL
================================
Bugs fixed vs GitHub version:

BUG 1: allowed() accepted recent_closed_trades as parameter but the caller
  (trade_manager) never passed it, so cooldowns never fired.
  Fixed in trade_manager.py — this file already had the logic, now it runs.

BUG 2: No spread-vs-SL check. A 30-point spread on a 35-point SL means
  52% of the edge is consumed before price moves at all.
  Added: block if spread > 50% of SL distance.

BUG 3: Daily drawdown check used 100% of limit. Added 90% soft stop so
  one final trade can't push past the hard limit.

All other logic (drawdown_multiplier, lot_size, etc.) unchanged — working correctly.
"""

from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from config import CONFIG, RiskConfig, active_news_blackout, asset_profile
from models import Direction, Signal, Trade, TradeStatus


class RiskManager:
    def __init__(self, config: RiskConfig = CONFIG.risk):
        self.config = config

    def allowed(
        self,
        signal: Signal,
        open_trades: Iterable[Trade],
        spread: float,
        realized_today: float,
        recent_closed_trades: Optional[List[Trade]] = None,
        now_utc: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        profile = asset_profile(signal.symbol)
        now = now_utc or datetime.now(timezone.utc)

        # News blackout
        blackout = active_news_blackout()
        if blackout:
            return False, f"News blackout active: {blackout}"

        # High winrate mode confidence floor
        if self.config.high_winrate_mode and signal.confidence < self.config.high_winrate_min_confidence:
            return False, f"Confidence {signal.confidence:.1f}% below high win-rate floor"

        # Hard floor regardless of mode
        if signal.confidence < 62.0:
            return False, f"Confidence {signal.confidence:.1f}% below 62% absolute floor"

        # Consecutive-loss cooldown (now actually fires because caller passes recent_closed_trades)
        consec_losses = self._consecutive_losses(recent_closed_trades)
        if consec_losses >= 3:
            last_loss_time = self._last_loss_time(recent_closed_trades)
            if last_loss_time:
                cooldown_end = last_loss_time + timedelta(minutes=30)
                if now < cooldown_end:
                    remaining = int((cooldown_end - now).total_seconds() / 60)
                    return False, f"30-min cooldown after 3 consecutive losses ({remaining} min remaining)"
        elif consec_losses >= 2:
            if signal.confidence < 78.0:
                return False, f"After {consec_losses} consecutive losses, confidence must be ≥ 78% (got {signal.confidence:.1f}%)"

        # RR check
        min_rr = self._minimum_rr(signal.symbol)
        if signal.rr < min_rr:
            return False, f"RR {signal.rr:.2f} below minimum {min_rr:.2f}"

        # Spread check
        if spread > profile.max_spread_points:
            return False, f"Spread {spread:.1f} > max {profile.max_spread_points}"

        # BUG FIX 2: spread vs SL distance
        sl_distance = abs(signal.entry - signal.stop_loss)
        if sl_distance > 0 and spread / sl_distance > 0.50:
            return False, (
                f"Spread {spread:.1f} is {spread/sl_distance*100:.0f}% of SL distance — "
                "edge consumed by spread"
            )

        # Concurrent trades
        active_trades = [t for t in open_trades if t.status in {TradeStatus.OPEN, TradeStatus.PARTIAL}]
        max_concurrent = 1 if self.config.high_winrate_mode else self.config.max_concurrent_trades
        if len(active_trades) >= max_concurrent:
            return False, f"Max concurrent trades ({max_concurrent}) reached"

        # Duplicate setup
        duplicate = self._duplicate_setup(signal, active_trades, profile.max_same_setup_open)
        if duplicate:
            return False, duplicate

        # BUG FIX 3: daily drawdown at 90% of limit
        max_daily_loss = -self.config.account_equity * self.config.max_daily_drawdown_pct / 100
        soft_limit = max_daily_loss * 0.90
        if realized_today <= soft_limit:
            return False, f"Daily drawdown protection: {realized_today:.2f} ≤ {soft_limit:.2f}"

        # Geometry
        if signal.direction == Direction.BUY and not (signal.stop_loss < signal.entry < signal.take_profit):
            return False, "Invalid BUY geometry"
        if signal.direction == Direction.SELL and not (signal.take_profit < signal.entry < signal.stop_loss):
            return False, "Invalid SELL geometry"

        return True, "Allowed"

    def lot_size(
        self,
        entry: float,
        stop_loss: float,
        symbol: str = "",
        recent_closed_trades: Optional[Iterable[Trade]] = None,
    ) -> float:
        if self.config.fixed_lot_size > 0:
            base = round(self.config.fixed_lot_size / self.config.lot_step) * self.config.lot_step
        else:
            profile = asset_profile(symbol)
            risk_amount = self.config.account_equity * self.config.risk_per_trade_pct / 100
            risk_points = abs(entry - stop_loss)
            raw_lots = risk_amount / max(risk_points * profile.contract_size, 1e-9)
            base = round(raw_lots / self.config.lot_step) * self.config.lot_step

        multiplier = self._drawdown_multiplier(recent_closed_trades)
        scaled = base * multiplier
        stepped = round(scaled / self.config.lot_step) * self.config.lot_step
        bounded = max(self.config.min_lot, min(self.config.max_lot, stepped))
        return round(bounded, 2)

    def today_key(self):
        return datetime.now(timezone.utc).date()

    def _minimum_rr(self, symbol: str) -> float:
        if self.config.use_micro_scalp_exits:
            base = max(1.5, float(self.config.micro_min_rr))
        else:
            base = max(1.8, self.config.min_rr, asset_profile(symbol).min_rr)
        if self.config.high_winrate_mode:
            base = max(base, float(self.config.high_winrate_min_rr))
        return base

    def _drawdown_multiplier(self, recent_closed_trades: Iterable[Trade] | None) -> float:
        if not recent_closed_trades:
            return 1.0
        trades = list(recent_closed_trades)
        if not trades:
            return 1.0

        consecutive_losses = 0
        for trade in reversed(trades):
            if trade.pnl < 0:
                consecutive_losses += 1
            else:
                break

        if consecutive_losses >= 4:
            streak_mult = 0.25
        elif consecutive_losses == 3:
            streak_mult = 0.35
        elif consecutive_losses == 2:
            streak_mult = 0.50
        else:
            streak_mult = 1.0

        window = trades[-50:]
        equity_curve = []
        running = 0.0
        for trade in window:
            running += trade.pnl
            equity_curve.append(running)

        if equity_curve:
            peak = max(equity_curve)
            current = equity_curve[-1]
            drawdown_pct = (peak - current) / max(self.config.account_equity, 1.0) * 100
        else:
            drawdown_pct = 0.0

        if drawdown_pct >= self.config.max_daily_drawdown_pct * 1.5:
            dd_mult = 0.25
        elif drawdown_pct >= self.config.max_daily_drawdown_pct:
            dd_mult = 0.50
        elif drawdown_pct >= self.config.max_daily_drawdown_pct * 0.5:
            dd_mult = 0.75
        else:
            dd_mult = 1.0

        return min(streak_mult, dd_mult)

    def _consecutive_losses(self, trades: Optional[List[Trade]]) -> int:
        if not trades:
            return 0
        count = 0
        for trade in reversed(trades):
            if trade.pnl < 0:
                count += 1
            else:
                break
        return count

    def _last_loss_time(self, trades: Optional[List[Trade]]) -> Optional[datetime]:
        if not trades:
            return None
        for trade in reversed(trades):
            if trade.pnl < 0:
                try:
                    return trade.close_time
                except AttributeError:
                    try:
                        return trade.closed_at
                    except AttributeError:
                        return None
        return None

    def _duplicate_setup(
        self, signal: Signal, active_trades: list, max_same_setup: int
    ) -> Optional[str]:
        setup = str(signal.metadata.get("setup_model", ""))
        atr_value = float(signal.metadata.get("atr", 0.0) or 0.0)

        same_setup = [
            t for t in active_trades
            if t.signal.symbol == signal.symbol
            and t.signal.direction == signal.direction
            and str(t.signal.metadata.get("setup_model", "")) == setup
        ]
        if len(same_setup) >= max_same_setup:
            return f"Same setup already open: {setup}"

        if atr_value <= 0:
            return None

        buffer = atr_value * 0.5
        for trade in active_trades:
            if trade.signal.symbol != signal.symbol or trade.signal.direction != signal.direction:
                continue
            if str(trade.signal.metadata.get("setup_model", "")) != setup:
                continue
            if abs(trade.signal.entry - signal.entry) <= buffer:
                return "Duplicate entry too close to existing open trade"

        return None
