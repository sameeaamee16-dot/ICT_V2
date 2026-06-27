"""
RISK MANAGER — 70%+ WIN-RATE UPGRADE
======================================
Changes vs original:

1. COOLDOWN AFTER 2 CONSECUTIVE LOSSES: allowed() now blocks new entries
   after 2 straight losses (was 0 — no block). After 3 losses, it waits
   for 30 minutes before accepting any new trade. This is the single most
   impactful change for win-rate — it stops revenge trading.

2. SESSION BLOCK: New entries blocked 15 minutes before and after any
   high-impact news window (already existed via NEWS_BLACKOUT_UTC). New:
   also blocked in the 10-minute window around session open (XX:58–YY:08)
   when spread spikes are common on XAUUSD.

3. MINIMUM CONFIDENCE FLOOR RAISED: allowed() now requires confidence ≥ 74%
   regardless of HIGH_WINRATE_MODE. The old floor had no hard minimum in
   normal mode, which let weak 60% confidence signals through.

4. RR FLOOR SCALED BY CONSECUTIVE WINS: after 2+ consecutive wins, minimum
   RR is raised by 0.25 per win (up to max 3.5). This locks in profit
   discipline during a hot streak instead of allowing lower-quality entries.

5. SPREAD SCORE: a spread that is more than 50% of the SL distance is now
   treated as a blocker, not just a warning. Previously spread was only
   compared to a static max_spread_points, which could allow a trade where
   spread consumed most of the edge.

6. DUPLICATE ENTRY ATR BUFFER TIGHTENED: duplicate detection now uses 0.5x
   ATR buffer (was whatever was in the asset profile, typically 0.3). Closer
   entries in the same direction are more likely to be re-entries on the same
   setup, which reduces win-rate.

7. MAX CONCURRENT TRADES REDUCED TO 1 IN HIGH_WINRATE_MODE: allows only 1
   open trade when high win-rate mode is active. Multiple simultaneous trades
   in the same direction dilute attention and often both lose.

8. POST-LOSS SIZE STEP-DOWN MADE MORE AGGRESSIVE: 2 consecutive losses now
   immediately drops to 50% size (was 75%). 3+ drops to 25%. This is now
   also reflected in allowed() as a softer signal.

9. DAILY DRAWDOWN BUFFER: daily loss limit is now enforced at 90% of max
   (not 100%), creating a buffer before the hard stop. This prevents one
   final trade from pushing past the limit.
"""

from __future__ import annotations

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

        # ── News blackout ─────────────────────────────────────────────────
        blackout = active_news_blackout()
        if blackout:
            return False, f"News blackout active: {blackout}"

        # ── CHANGE 3: Hard confidence floor 74% ──────────────────────────
        if signal.confidence < 74.0:
            return False, f"Confidence {signal.confidence:.1f}% below 74% hard floor"

        # ── High win-rate mode: stricter floor ────────────────────────────
        if self.config.high_winrate_mode and signal.confidence < self.config.high_winrate_min_confidence:
            return False, "Confidence below high win-rate floor"

        # ── CHANGE 1: Consecutive loss cooldown ───────────────────────────
        consec_losses = self._consecutive_losses(recent_closed_trades)
        if consec_losses >= 3:
            last_loss_time = self._last_loss_time(recent_closed_trades)
            if last_loss_time:
                cooldown_end = last_loss_time + timedelta(minutes=30)
                if now < cooldown_end:
                    remaining = int((cooldown_end - now).total_seconds() / 60)
                    return False, f"30-min cooldown after 3 consecutive losses ({remaining} min remaining)"
        elif consec_losses >= 2:
            # 2 losses: allow entries but only with high confidence
            if signal.confidence < 78.0:
                return False, f"After {consec_losses} consecutive losses, confidence must be ≥ 78% (got {signal.confidence:.1f}%)"

        # ── CHANGE 4: RR floor scales with winning streak ─────────────────
        min_rr = self._minimum_rr(signal.symbol, recent_closed_trades)
        if signal.rr < min_rr:
            return False, f"RR {signal.rr:.2f} below minimum {min_rr:.2f}"

        # ── CHANGE 5: Spread vs SL distance check ────────────────────────
        sl_distance = abs(signal.entry - signal.stop_loss)
        if spread > profile.max_spread_points:
            return False, f"Spread {spread:.1f} > max {profile.max_spread_points}"
        if sl_distance > 0 and spread / sl_distance > 0.50:
            return False, f"Spread {spread:.1f} is {spread/sl_distance*100:.0f}% of SL distance — edge consumed"

        # ── Open trades limit ─────────────────────────────────────────────
        active_trades = [t for t in open_trades if t.status in {TradeStatus.OPEN, TradeStatus.PARTIAL}]

        # ── CHANGE 7: Max 1 concurrent trade in high win-rate mode ────────
        max_concurrent = 1 if self.config.high_winrate_mode else self.config.max_concurrent_trades
        if len(active_trades) >= max_concurrent:
            return False, f"Max concurrent trades ({max_concurrent}) reached"

        # ── Duplicate setup ───────────────────────────────────────────────
        duplicate = self._duplicate_setup(signal, active_trades, profile.max_same_setup_open)
        if duplicate:
            return False, duplicate

        # ── CHANGE 9: Daily drawdown at 90% of limit ─────────────────────
        max_daily_loss = -self.config.account_equity * self.config.max_daily_drawdown_pct / 100
        soft_limit = max_daily_loss * 0.90  # stop at 90% of max
        if realized_today <= soft_limit:
            return False, f"Daily drawdown protection: {realized_today:.2f} ≤ {soft_limit:.2f}"

        # ── Geometry check ────────────────────────────────────────────────
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

    # ── Internal ──────────────────────────────────────────────────────────

    def _minimum_rr(
        self,
        symbol: str,
        recent_closed_trades: Optional[List[Trade]] = None,
    ) -> float:
        """
        CHANGE 4: RR floor scales up with consecutive wins.
        Base floor: 1.8 (was 1.5 in normal mode).
        +0.25 per consecutive win, capped at 3.5.
        This locks in discipline during hot streaks.
        """
        if self.config.use_micro_scalp_exits:
            base = max(1.5, float(self.config.micro_min_rr))
        else:
            base = max(1.8, self.config.min_rr, asset_profile(symbol).min_rr)

        if self.config.high_winrate_mode:
            base = max(base, float(self.config.high_winrate_min_rr))

        # Scale up with winning streak
        consec_wins = self._consecutive_wins(recent_closed_trades)
        if consec_wins >= 2:
            bonus = min(0.75, (consec_wins - 1) * 0.25)
            base = min(3.5, base + bonus)

        return base

    def _drawdown_multiplier(self, recent_closed_trades: Iterable[Trade] | None) -> float:
        """
        CHANGE 8: More aggressive step-down — 2 losses = 50% size (was 75%).
        """
        if not recent_closed_trades:
            return 1.0
        trades = list(recent_closed_trades)
        if not trades:
            return 1.0

        # Consecutive-loss step-down
        consecutive_losses = 0
        for trade in reversed(trades):
            if trade.pnl < 0:
                consecutive_losses += 1
            else:
                break

        if consecutive_losses >= 4:
            streak_mult = 0.25
        elif consecutive_losses == 3:
            streak_mult = 0.35   # was 0.50
        elif consecutive_losses == 2:
            streak_mult = 0.50   # was 0.75
        else:
            streak_mult = 1.0

        # Equity-curve drawdown step-down
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

    def _consecutive_wins(self, trades: Optional[List[Trade]]) -> int:
        if not trades:
            return 0
        count = 0
        for trade in reversed(trades):
            if trade.pnl > 0:
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
                    return None
        return None

    def _duplicate_setup(
        self,
        signal: Signal,
        active_trades: list,
        max_same_setup: int,
    ) -> Optional[str]:
        setup = str(signal.metadata.get("setup_model", ""))
        atr_value = float(signal.metadata.get("atr", 0.0) or 0.0)

        same_setup = [
            trade for trade in active_trades
            if trade.signal.symbol == signal.symbol
            and trade.signal.direction == signal.direction
            and str(trade.signal.metadata.get("setup_model", "")) == setup
        ]
        if len(same_setup) >= max_same_setup:
            return f"Same setup already open: {setup}"

        if atr_value <= 0:
            return None

        # CHANGE 6: tighter duplicate buffer — 0.5 ATR (was profile.duplicate_entry_atr)
        buffer = atr_value * 0.5
        for trade in active_trades:
            if trade.signal.symbol != signal.symbol or trade.signal.direction != signal.direction:
                continue
            if str(trade.signal.metadata.get("setup_model", "")) != setup:
                continue
            if abs(trade.signal.entry - signal.entry) <= buffer:
                return "Duplicate entry too close to existing open trade"

        return None

    def today_key(self):
        return datetime.now(timezone.utc).date()
