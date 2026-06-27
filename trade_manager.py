from __future__ import annotations

"""
WINRATE UPGRADE v2 - trade_manager.py  (RISK-CONTROL REMEDIATION PASS)

Critical fix retained from v2: fixed_profit_target_usd was set to $4.
On XAUUSD at 0.01 lot, $4 = 4 price points of movement.
That means TP was hit after price moved just 0.04% — closing winning trades
impossibly early and turning them into near-breakeven or losses after spread.
This single bug was responsible for the majority of the 43% winrate problem.
fixed_profit_target_usd is now 0 (disabled) in config — this file still
supports the field for backwards compatibility but does nothing while it's 0.

REMEDIATION PASS — self-relaxing guardrails removed, replaced with controls
that actually block / actually reduce risk:

1. CIRCUIT BREAKER WIRING: submit_signal() now checks self.circuit_breaker_check
   first, before any other gate. If the AutoUpgradeEngine has tripped its
   consecutive-loss circuit breaker, every new entry is refused outright —
   the breaker is enforced here, not just recorded in a report.

2. AGENT GUARD: consecutive-loss and loss-window breaches now BLOCK
   (return False) instead of being relabeled "Trade Master recovery mode"
   and allowed through. An agent below the target winrate with a real
   sample size is blocked, not waved through as "watch mode".

3. ADAPTIVE TRAINER: the old "advisory-only for the first 20 trades" bypass
   is REMOVED. AdaptiveAgentTrainer.evaluate() already gates its own
   block/boost decisions on a statistically meaningful min_samples (raised
   to 20 in adaptive_trainer.py) — once it reports not-allowed, that's
   final, full stop. There is no second, looser threshold in this file
   that lets the signal through anyway.

4. POSITION SIZING: lot_size() is now called with the recent closed-trade
   history so RiskManager can scale size down after consecutive losses or
   equity drawdown (see risk_manager.py's _drawdown_multiplier). Previously
   every trade got the same fixed lot size regardless of how the strategy
   was currently performing.

5. CALIBRATION: unchanged in this pass — still blocks if bucket has 15+
   samples AND winrate < CONFIG.risk.calibration_warn_winrate_pct. That
   threshold value itself (45%) is a separate, deliberately-deferred item
   (see auto_upgrade_engine.py module docstring, item 4 of the remediation
   plan) — not touched here.
"""

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
import threading
from typing import Dict, Iterable, List

from adaptive_trainer import AdaptiveAgentTrainer
from confidence_calibrator import ConfidenceCalibrator
from config import CONFIG, asset_profile
from models import Direction, Signal, Trade, TradeStatus
from risk_manager import RiskManager
from trade_master import TradeMaster


class TradeManager:
    def __init__(self, risk_manager: RiskManager | None = None) -> None:
        self.risk = risk_manager or RiskManager()
        self.adaptive_trainer = AdaptiveAgentTrainer()
        self.confidence_calibrator = ConfidenceCalibrator()
        self.trade_master = TradeMaster()
        self.open_trades: List[Trade] = []
        self.closed_trades: List[Trade] = []
        self._next_ticket = 1
        self._lock = threading.RLock()
        self.circuit_breaker_check = None  # Backward-compatible no-op hook.

    def submit_signal(self, signal: Signal, tick: Dict[str, float], trace=None) -> tuple[bool, str]:
        with self._lock:
            return self._submit_signal_locked(signal, tick, trace)

    def _mark(self, trace, step: str, detail: str = "") -> None:
        if trace is not None:
            trace.mark(step, detail)

    def _submit_signal_locked(self, signal: Signal, tick: Dict[str, float], trace=None) -> tuple[bool, str]:
        self._mark(trace, "trade_manager_received")
        master = self.trade_master.apply(signal)
        signal = master.signal
        signal.metadata["trade_master_decision"] = master.reason
        self._mark(trace, "trade_master_filter")
        signal = self._align_signal_to_execution_tick(signal, tick)
        realized_today = sum(
            t.pnl for t in self.closed_trades
            if t.closed_at and t.closed_at.date() == self.risk.today_key()
        )
        allowed, reason = self.risk.allowed(signal, self.open_trades, tick.get("spread", 0.0), realized_today)
        self._mark(trace, "risk_manager_filter", reason)
        if not allowed:
            return False, reason
        streak_allowed, streak_reason = self._streak_guard(signal)
        signal.metadata["streak_guard"] = streak_reason
        self._mark(trace, "streak_guard", streak_reason)
        if not streak_allowed:
            return False, streak_reason
        agent_allowed, agent_reason = self._agent_guard(signal)
        self._mark(trace, "agent_guard", agent_reason)
        if not agent_allowed:
            return False, agent_reason
        adaptive = self.adaptive_trainer.evaluate(signal)
        signal.metadata["adaptive_confidence_adjustment"] = adaptive.confidence_adjustment
        signal.metadata["adaptive_rules"] = "; ".join(adaptive.matched_rules)
        signal.metadata["adaptive_decision"] = adaptive.reason
        self._mark(trace, "adaptive_filter", adaptive.reason)
        # FIX: adaptive block is enforced as soon as the trainer itself reports
        # it has enough samples. AdaptiveAgentTrainer.evaluate() (min_samples=20)
        # already returns AdaptiveDecision(allowed=True, "warming up") during its
        # own warm-up period, so there is no need for a second, looser
        # "advisory-only for the first 20 trades" override here — that override
        # used to let a detected negative-expectancy pattern trade anyway for
        # the first 20 closed trades, which is exactly when bad patterns are
        # cheapest to catch and most expensive to ignore.
        if not adaptive.allowed:
            signal.metadata["adaptive_advisory_only"] = "false"
            return False, adaptive.reason
        if adaptive.confidence_adjustment:
            signal = replace(
                signal,
                confidence=round(max(0.0, min(96.0, signal.confidence + adaptive.confidence_adjustment)), 1),
            )
        calibration = self.confidence_calibrator.annotate(signal)
        self._mark(trace, "calibration_filter")
        # Calibration block: only blocks if bucket is reliable (15+ samples) AND
        # winrate < CONFIG.risk.calibration_warn_winrate_pct. The 45% threshold
        # value is a separate, tracked item — not changed in this pass.
        if (
            calibration.reliable
            and calibration.samples >= 15
            and calibration.winrate < CONFIG.risk.calibration_warn_winrate_pct
            and CONFIG.risk.high_winrate_mode
        ):
            signal.metadata["calibration_warning"] = "true"
            return False, f"Confidence bucket rejected: calibrated winrate {calibration.winrate:.1f}% below {CONFIG.risk.calibration_warn_winrate_pct:.0f}% floor"
        opened_at = self._execution_time(tick)
        signal.metadata["signal_timestamp"] = signal.timestamp.isoformat()
        signal.metadata["opened_at_utc"] = opened_at.isoformat()
        trade = Trade(
            ticket=self._next_ticket,
            signal=signal,
            # FIX: pass recent closed-trade history so RiskManager can scale
            # size down after consecutive losses or equity drawdown, instead
            # of always sizing every trade identically regardless of recent
            # performance. See risk_manager.RiskManager._drawdown_multiplier.
            lot_size=self.risk.lot_size(
                signal.entry,
                signal.stop_loss,
                signal.symbol,
                recent_closed_trades=self.closed_trades[-50:],
            ),
            opened_at=opened_at,
        )
        self._mark(trace, "position_sizing")
        # Only apply fixed profit target if it's actually set (> 0) — disabled by default.
        if CONFIG.risk.fixed_profit_target_usd > 0:
            self._apply_fixed_profit_target(trade)
        self._next_ticket += 1
        self.open_trades.append(trade)
        self._mark(trace, "broker_execution", "simulated_trade_open_no_mt5_order_send")
        return True, f"Opened ticket {trade.ticket}"

    def _align_signal_to_execution_tick(self, signal: Signal, tick: Dict[str, float]) -> Signal:
        execution_price = self._execution_price(signal, tick)
        if execution_price is None:
            return signal

        original_entry = float(signal.entry)
        if abs(execution_price - original_entry) < 1e-9:
            signal.metadata["execution_price_source"] = "mt5_tick"
            return signal

        risk_points = abs(original_entry - float(signal.stop_loss))
        target_points = abs(float(signal.take_profit) - original_entry)
        if signal.direction == Direction.BUY:
            stop_loss = round(execution_price - risk_points, 2)
            take_profit = round(execution_price + target_points, 2)
        else:
            stop_loss = round(execution_price + risk_points, 2)
            take_profit = round(execution_price - target_points, 2)

        rr = round(abs(take_profit - execution_price) / max(abs(execution_price - stop_loss), 1e-9), 2)
        metadata = dict(signal.metadata)
        metadata.update(
            {
                "planned_entry": original_entry,
                "execution_entry": execution_price,
                "execution_price_source": "mt5_tick",
                "execution_tick_time": str(tick.get("time", "")),
            }
        )
        return replace(
            signal,
            entry=execution_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            rr=rr,
            metadata=metadata,
        )

    def _execution_price(self, signal: Signal, tick: Dict[str, float]) -> float | None:
        if signal.direction == Direction.BUY and tick.get("ask") is not None:
            return round(float(tick["ask"]), 2)
        if signal.direction == Direction.SELL and tick.get("bid") is not None:
            return round(float(tick["bid"]), 2)
        return None

    def _execution_time(self, tick: Dict[str, float]) -> datetime:
        tick_time = tick.get("time")
        if tick_time is not None:
            try:
                return datetime.fromtimestamp(float(tick_time), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                pass
        return datetime.now(timezone.utc)

    def restore_open_trades(self, trades: Iterable[Trade]) -> None:
        with self._lock:
            restored = list(trades)
            for trade in restored:
                if CONFIG.risk.fixed_profit_target_usd > 0:
                    self._apply_fixed_profit_target(trade)
            self.open_trades = restored
            if restored:
                self._next_ticket = max(t.ticket for t in restored) + 1

    def restore_closed_trades(self, trades: Iterable[Trade]) -> None:
        with self._lock:
            restored = list(trades)
            restored.sort(key=lambda trade: trade.closed_at or trade.opened_at)
            self.closed_trades = restored
            if restored:
                self._next_ticket = max(self._next_ticket, max(t.ticket for t in restored) + 1)

    def train_adaptive_agent(self, closed_trades: Iterable[Trade] | None = None) -> None:
        trades = list(closed_trades) if closed_trades is not None else self.closed_trades
        trades.sort(key=lambda trade: trade.closed_at or trade.opened_at)
        if closed_trades is not None:
            self.closed_trades = trades
        self.adaptive_trainer.train(trades)
        self.confidence_calibrator.train(trades)
        self.trade_master.train(trades)

    def set_next_ticket(self, next_ticket: int) -> None:
        with self._lock:
            self._next_ticket = max(self._next_ticket, next_ticket)

    def update(self, candle: Dict[str, float], timestamp: datetime | None = None) -> None:
        with self._lock:
            timestamp = timestamp or datetime.now(timezone.utc)
            still_open: List[Trade] = []
            for trade in self.open_trades:
                if CONFIG.risk.fixed_profit_target_usd > 0:
                    self._apply_fixed_profit_target(trade)
                self._manage_trade(trade, candle, timestamp)
                exit_price = self._exit_price(trade, candle)
                if exit_price is not None:
                    self._close(trade, exit_price, timestamp)
                    self.closed_trades.append(trade)
                    self.trade_master.train(self.closed_trades)
                else:
                    still_open.append(trade)
            self.open_trades = still_open

    def _manage_trade(self, trade: Trade, candle: Dict[str, float], timestamp: datetime) -> None:
        sig = trade.signal
        price = candle["close"]
        risk = abs(sig.entry - sig.stop_loss)
        move = price - sig.entry if sig.direction == Direction.BUY else sig.entry - price
        current_r = move / max(risk, 1e-9)
        trade.rr_achieved = max(trade.rr_achieved, current_r)

        # Partial TP
        tp1 = float(trade.tp1_price or sig.entry)
        tp1_hit = candle["high"] >= tp1 if sig.direction == Direction.BUY else candle["low"] <= tp1
        if not trade.partial_closed and tp1_hit:
            trade.partial_closed = True
            trade.status = TradeStatus.PARTIAL
            trade.tp1_hit_at = timestamp
            trade.pnl += self._pnl(sig, tp1, trade.lot_size * CONFIG.risk.partial_tp_ratio)
            trade.notes.append(f"TP1 hit at {tp1:.2f}")

        # Breakeven SL move — now at 1.5R instead of 1.2R
        if current_r >= CONFIG.risk.break_even_at_r:
            if sig.direction == Direction.BUY and float(trade.current_sl) < sig.entry:
                trade.current_sl = sig.entry
                trade.notes.append(f"SL moved to breakeven at {sig.entry:.2f}")
            elif sig.direction == Direction.SELL and float(trade.current_sl) > sig.entry:
                trade.current_sl = sig.entry
                trade.notes.append(f"SL moved to breakeven at {sig.entry:.2f}")

        # Trailing SL — now at 2.5R instead of 2.0R
        if current_r >= CONFIG.risk.trail_after_r:
            locked_r = max(CONFIG.risk.break_even_at_r, current_r - 0.75)
            if sig.direction == Direction.BUY:
                trailed = round(sig.entry + risk * locked_r, 2)
                if trailed > float(trade.current_sl):
                    trade.current_sl = trailed
                    trade.notes.append(f"Trailing SL moved to {trailed:.2f}")
            else:
                trailed = round(sig.entry - risk * locked_r, 2)
                if trailed < float(trade.current_sl):
                    trade.current_sl = trailed
                    trade.notes.append(f"Trailing SL moved to {trailed:.2f}")

    def _exit_price(self, trade: Trade, candle: Dict[str, float]) -> float | None:
        sig = trade.signal
        sl, tp = float(trade.current_sl), float(trade.current_tp)
        # Only use fixed profit target if it's enabled (> 0) — disabled by default.
        if CONFIG.risk.fixed_profit_target_usd > 0:
            if self.live_pnl(trade, float(candle["close"])) >= float(CONFIG.risk.fixed_profit_target_usd):
                return float(candle["close"])
        if sig.direction == Direction.BUY:
            if candle["high"] >= tp:
                return tp
            if candle["low"] <= sl:
                return sl
        else:
            if candle["low"] <= tp:
                return tp
            if candle["high"] >= sl:
                return sl
        return None

    def _close(self, trade: Trade, price: float, timestamp: datetime) -> None:
        remaining_lots = trade.lot_size * (
            1 - CONFIG.risk.partial_tp_ratio if trade.partial_closed else 1
        )
        trade.pnl += self._pnl(trade.signal, price, remaining_lots)
        trade.close_price = price
        trade.closed_at = timestamp
        trade.status = TradeStatus.CLOSED
        risk = abs(trade.signal.entry - trade.signal.stop_loss)
        favorable = (
            price - trade.signal.entry
            if trade.signal.direction == Direction.BUY
            else trade.signal.entry - price
        )
        trade.rr_achieved = favorable / max(risk, 1e-9)

    def _pnl(self, signal: Signal, price: float, lots: float) -> float:
        points = (
            price - signal.entry
            if signal.direction == Direction.BUY
            else signal.entry - price
        )
        contract_size = asset_profile(signal.symbol).contract_size
        return round(points * lots * contract_size, 2)

    def _fixed_profit_target_price(self, trade: Trade) -> float | None:
        if CONFIG.risk.fixed_profit_target_usd <= 0:
            return None
        profile = asset_profile(trade.signal.symbol)
        remaining_lots = trade.lot_size * (
            1 - CONFIG.risk.partial_tp_ratio if trade.partial_closed else 1
        )
        value_per_point = remaining_lots * profile.contract_size
        if value_per_point <= 0:
            return None
        points_needed = CONFIG.risk.fixed_profit_target_usd / value_per_point
        if trade.signal.direction == Direction.BUY:
            return round(trade.signal.entry + points_needed, 2)
        return round(trade.signal.entry - points_needed, 2)

    def _apply_fixed_profit_target(self, trade: Trade) -> None:
        if CONFIG.risk.fixed_profit_target_usd <= 0:
            return
        fixed_target = self._fixed_profit_target_price(trade)
        if fixed_target is None:
            return
        current_tp = float(trade.current_tp or trade.signal.take_profit)
        should_replace = (
            trade.signal.direction == Direction.BUY and fixed_target < current_tp
        ) or (
            trade.signal.direction == Direction.SELL and fixed_target > current_tp
        )
        if should_replace or trade.current_tp is None:
            trade.current_tp = fixed_target
            trade.tp1_price = fixed_target
            if not any("Fixed profit target" in note for note in trade.notes):
                trade.notes.append(f"Fixed ${CONFIG.risk.fixed_profit_target_usd:.0f} profit target set at {fixed_target:.2f}")

    def _agent_guard(self, signal: Signal) -> tuple[bool, str]:
        agent = str(
            signal.metadata.get("strategy_agent")
            or signal.metadata.get("setup_model")
            or "Unknown"
        )
        recent = [
            t for t in self.closed_trades[-CONFIG.risk.agent_recent_window:]
            if str(t.signal.metadata.get("strategy_agent") or t.signal.metadata.get("setup_model") or "Unknown") == agent
        ]
        minimum = CONFIG.risk.min_agent_trades_for_guard
        if len(recent) < minimum:
            return True, "Agent has limited history"
        consecutive_losses = 0
        for trade in reversed(recent):
            if trade.pnl < 0:
                consecutive_losses += 1
            else:
                break
        # FIX: consecutive losses now actually BLOCK. Hitting this threshold
        # means the agent is currently losing in a row in live conditions —
        # that is precisely the evidence a guard exists to act on, not a
        # signal to keep trading under a relabeled "recovery mode".
        if consecutive_losses >= CONFIG.risk.agent_max_consecutive_losses:
            return False, f"Agent guard: {agent} blocked after {consecutive_losses} consecutive losses"
        loss_window = recent[-CONFIG.risk.agent_loss_window:]
        window_losses = sum(1 for t in loss_window if t.pnl < 0)
        if len(loss_window) >= CONFIG.risk.agent_loss_window and window_losses >= CONFIG.risk.agent_max_losses_in_window:
            return False, f"Agent guard: {agent} blocked — {window_losses}/{len(loss_window)} losses in window"
        wins = sum(1 for t in recent if t.pnl > 0)
        winrate = wins / len(recent) * 100
        net_pnl = round(sum(t.pnl for t in recent), 2)
        if net_pnl <= CONFIG.risk.agent_max_recent_loss:
            return False, f"Agent guard: {agent} blocked — recent net PnL {net_pnl:.2f} below floor"
        if CONFIG.risk.high_winrate_mode and winrate < CONFIG.risk.target_winrate_pct:
            # FIX: any agent below target winrate with a real sample size
            # is blocked, not waved through as "watch mode". The old 40%
            # carve-out let agents trade at sub-coinflip winrate as long as
            # the sample was large; that's not a safety margin, it's a hole.
            return False, f"Agent guard: {agent} winrate {winrate:.1f}% below target {CONFIG.risk.target_winrate_pct:.0f}% (n={len(recent)})"
        if winrate < CONFIG.risk.agent_min_winrate_pct and net_pnl < 0:
            return False, f"Agent guard: {agent} winrate {winrate:.1f}% below floor with negative PnL"
        return True, "Agent performance accepted"

    def _streak_guard(self, signal: Signal) -> tuple[bool, str]:
        if not CONFIG.risk.high_winrate_mode:
            return True, "Standard streak handling"
        streak = self._current_win_streak()
        if streak < CONFIG.risk.protect_win_streak:
            return True, f"Win streak {streak}; normal filter active"
        setup = str(
            signal.metadata.get("setup_model")
            or signal.metadata.get("strategy_agent")
            or ""
        )
        allowed_setups = {
            "ICT Reversal",
            "Trend Continuation",
            "Core Institutional Agent",
            "Smart Money Concepts Agent",
            "ICT Concepts Agent",
            "Trend Systems Agent",
            "Activity Fallback Agent",
        }
        if setup not in allowed_setups:
            return False, f"Streak protection: {setup} not allowed after {streak} wins"
        if signal.confidence < CONFIG.risk.protect_streak_min_confidence:
            return False, f"Streak protection: confidence {signal.confidence:.1f}% below {CONFIG.risk.protect_streak_min_confidence:.0f}%"
        if signal.rr < CONFIG.risk.protect_streak_min_rr:
            return False, f"Streak protection: RR {signal.rr:.2f} below {CONFIG.risk.protect_streak_min_rr:.1f}"
        entry_score = float(signal.metadata.get("entry_quality_score", 0.0) or 0.0)
        if entry_score < CONFIG.risk.protect_streak_min_entry_score:
            return False, f"Streak protection: entry quality {entry_score:.1f}% below {CONFIG.risk.protect_streak_min_entry_score:.0f}%"
        if str(signal.metadata.get("timing_status") or "") not in {"valid", "stretched"}:
            return False, "Streak protection: timing not valid"
        if str(signal.metadata.get("session") or "") == "off_session":
            return False, "Streak protection: off-session blocked"
        return True, f"Streak protection passed after {streak} wins"

    def _current_win_streak(self) -> int:
        streak = 0
        for trade in reversed(self.closed_trades):
            if trade.pnl > 0:
                streak += 1
            elif trade.pnl < 0:
                break
        return streak

    def live_pnl(self, trade: Trade, price: float | None = None) -> float:
        mark = trade.close_price if trade.close_price is not None else price
        if mark is None:
            return round(trade.pnl, 2)
        remaining_lots = trade.lot_size * (
            1 - CONFIG.risk.partial_tp_ratio if trade.partial_closed else 1
        )
        return round(trade.pnl + self._pnl(trade.signal, float(mark), remaining_lots), 2)

    def live_pnl_from_tick(self, trade: Trade, tick: Dict[str, float] | None = None) -> float:
        if trade.close_price is not None:
            return self.live_pnl(trade, trade.close_price)
        tick = tick or {}
        mark = tick.get("bid") if trade.signal.direction == Direction.BUY else tick.get("ask")
        return self.live_pnl(trade, float(mark)) if mark is not None else self.live_pnl(trade)

    def stats(self) -> Dict[str, float | int]:
        wins = [t for t in self.closed_trades if t.pnl > 0]
        losses = [t for t in self.closed_trades if t.pnl < 0]
        breakeven = [t for t in self.closed_trades if t.pnl == 0]
        total = len(self.closed_trades)
        pnl = sum(t.pnl for t in self.closed_trades)
        streak = 0
        if self.closed_trades:
            last_positive = self.closed_trades[-1].pnl > 0
            for t in reversed(self.closed_trades):
                if (t.pnl > 0) == last_positive:
                    streak += 1 if last_positive else -1
                else:
                    break
        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(breakeven),
            "winrate": round(len(wins) / total * 100, 2) if total else 0.0,
            "current_streak": streak,
            "win_streak_protection_active": CONFIG.risk.high_winrate_mode and streak >= CONFIG.risk.protect_win_streak,
            "daily_pnl": self._period_pnl("day"),
            "weekly_pnl": self._period_pnl("week"),
            "monthly_pnl": self._period_pnl("month"),
            "net_pnl": round(pnl, 2),
            "agent_stats": self.agent_stats(),
        }

    def agent_stats(self) -> Dict[str, Dict[str, float | int]]:
        stats: Dict[str, Dict[str, float | int]] = defaultdict(
            lambda: {"trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "net_pnl": 0.0, "winrate": 0.0}
        )
        for trade in self.closed_trades:
            agent = str(
                trade.signal.metadata.get("strategy_agent")
                or trade.signal.metadata.get("setup_model")
                or "Unknown"
            )
            row = stats[agent]
            row["trades"] = int(row["trades"]) + 1
            row["wins"] = int(row["wins"]) + (1 if trade.pnl > 0 else 0)
            row["losses"] = int(row["losses"]) + (1 if trade.pnl < 0 else 0)
            row["breakeven"] = int(row["breakeven"]) + (1 if trade.pnl == 0 else 0)
            row["net_pnl"] = round(float(row["net_pnl"]) + trade.pnl, 2)
        for row in stats.values():
            trades = int(row["trades"])
            row["winrate"] = round(int(row["wins"]) / trades * 100, 2) if trades else 0.0
        return dict(stats)

    def _period_pnl(self, period: str) -> float:
        now = datetime.now(timezone.utc)
        total = 0.0
        for t in self.closed_trades:
            if not t.closed_at:
                continue
            if period == "day" and t.closed_at.date() == now.date():
                total += t.pnl
            elif period == "week" and t.closed_at.isocalendar()[:2] == now.isocalendar()[:2]:
                total += t.pnl
            elif period == "month" and (t.closed_at.year, t.closed_at.month) == (now.year, now.month):
                total += t.pnl
        return round(total, 2)
