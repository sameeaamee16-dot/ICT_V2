from __future__ import annotations
"""
filter_engine.py — ICT_V2 FINAL
=================================
Complete working version. All 11 checks active.

vs GitHub version (8 checks):
  Added check 9:  Candle count gate — 3+ bars must close after FVG/OB forms
  Added check 10: HTF displacement agreement — 5m/15m must show same-direction flow
  Added check 11: Equal high/low trap gate — price must move AWAY from swept level

vs previous patch version:
  Off-session signals now require 4/5 confluence categories (kill-zone: 3/5)
  Silver Bullet session (10:00–11:00 UTC) added to kill zones
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from models import Direction, IctSnapshot, Signal, Trade

_STRUCTURE_CONCEPTS = {
    "BOS", "CHOCH", "MSS", "bullish BOS", "bearish BOS",
    "bullish CHOCH", "bearish CHOCH", "bullish MSS", "bearish MSS",
    "Higher High", "Higher Low", "Lower High", "Lower Low",
}
_ENTRY_CONCEPTS = {
    "Fair Value Gap", "Bullish FVG", "Bearish FVG", "Fresh FVG",
    "Order Block", "Bullish OB", "Bearish OB",
    "Breaker Block", "Mitigation Block", "Rejection Block", "Optimal Trade Entry",
}
_LIQUIDITY_CONCEPTS = {
    "Liquidity Sweep", "Turtle Soup", "Buy Side Liquidity", "Sell Side Liquidity",
    "Equal Highs", "Equal Lows", "Inducement", "Judas Swing",
}
_MOMENTUM_CONCEPTS = {
    "Displacement Candle", "Volume Expansion", "Volume Spike",
    "ADX Trending Market", "ADX Acceleration", "MACD Momentum Expansion",
    "Momentum Confirmation", "Supertrend Bullish", "Supertrend Bearish",
    "Bollinger Expansion Breakout", "Bollinger Expansion Breakdown",
    "Donchian Breakout", "Donchian Breakdown",
}
_HTF_CONCEPTS = {
    "Multi Timeframe Bias", "200 EMA Bull Regime", "200 EMA Bear Regime",
    "EMA Trend Stack", "VWAP Bull Control", "VWAP Bear Control",
    "Daily Bias Bullish", "Daily Bias Bearish",
}

_DEAD_ZONES = [
    (11, 30, 12, 0),
    (16, 30, 17, 0),
    (20, 0, 22, 0),
]

_KILL_ZONES = [
    (6, 30, 10, 30),
    (10, 0, 11, 0),   # Silver Bullet
    (12, 0, 16, 30),
    (17, 30, 20, 30),
    (0, 0, 3, 30),
]


class FilterEngine:
    """All 11 checks must pass. Returns (True, "Allowed") or (False, reason)."""

    def check(
        self,
        signal: Signal,
        snapshots: Dict[str, IctSnapshot],
        frames: Dict[str, pd.DataFrame],
        recent_closed_trades: Optional[List[Trade]] = None,
        now_utc: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        now = now_utc or datetime.now(timezone.utc)
        primary_tf = list(snapshots.keys())[0] if snapshots else None
        primary = snapshots.get(primary_tf) if primary_tf else None
        df = frames.get(primary_tf) if primary_tf else None

        ok, reason = self._three_confluence(signal, now)
        if not ok:
            return False, reason

        ok, reason = self._htf_pyramid(signal, snapshots)
        if not ok:
            return False, reason

        if primary:
            ok, reason = self._premium_discount_gate(signal, primary)
            if not ok:
                return False, reason

        ok, reason = self._news_proximity(now)
        if not ok:
            return False, reason

        ok, reason = self._dead_zone_filter(now)
        if not ok:
            return False, reason

        ok, reason = self._repeat_direction_block(signal, recent_closed_trades, now)
        if not ok:
            return False, reason

        if primary and df is not None:
            ok, reason = self._entry_zone_proximity(signal, primary)
            if not ok:
                return False, reason

        setup = str(signal.metadata.get("setup_model", ""))
        if "ICT Reversal" in setup and df is not None:
            ok, reason = self._amd_session_check(signal, df)
            if not ok:
                return False, reason

        # Check 9 — new
        if primary and df is not None:
            ok, reason = self._candle_count_gate(signal, primary, df)
            if not ok:
                return False, reason

        # Check 10 — new
        if primary and len(snapshots) > 1:
            ok, reason = self._htf_displacement_check(signal, snapshots)
            if not ok:
                return False, reason

        # Check 11 — new
        if df is not None:
            ok, reason = self._eq_level_trap_gate(signal, df)
            if not ok:
                return False, reason

        return True, "Allowed"

    # ── Checks 1–8 (original, plus off-session tightening) ───────────────

    def _three_confluence(self, signal: Signal, now: datetime) -> Tuple[bool, str]:
        concepts = set(signal.concepts)
        categories_present = 0
        missing = []
        for name, cat in [
            ("structure", _STRUCTURE_CONCEPTS),
            ("entry zone", _ENTRY_CONCEPTS),
            ("liquidity", _LIQUIDITY_CONCEPTS),
            ("momentum", _MOMENTUM_CONCEPTS),
            ("HTF", _HTF_CONCEPTS),
        ]:
            if concepts & cat:
                categories_present += 1
            else:
                missing.append(name)

        # Off-session needs 4 categories; kill zone needs only 3
        required = 3 if self._in_killzone(now) else 4
        if categories_present < required:
            note = "" if required == 3 else " (off-session requires 4)"
            return False, (
                f"Three-confluence gate{note}: {categories_present}/5 categories. "
                f"Missing: {', '.join(missing[:2])}"
            )
        return True, "ok"

    def _htf_pyramid(self, signal: Signal, snapshots: Dict[str, IctSnapshot]) -> Tuple[bool, str]:
        from config import CONFIG
        opposing = "bearish" if signal.direction == Direction.BUY else "bullish"
        for tf in getattr(getattr(CONFIG, "timeframes", None), "confluence", []):
            snap = snapshots.get(tf)
            if snap and snap.bias == opposing:
                return False, f"HTF pyramid: {tf} is {opposing} against {signal.direction.value}"
        return True, "ok"

    def _premium_discount_gate(self, signal: Signal, primary: IctSnapshot) -> Tuple[bool, str]:
        pd_val = primary.premium_discount
        if signal.direction == Direction.BUY and pd_val == "premium":
            return False, "BUY blocked in premium zone"
        if signal.direction == Direction.SELL and pd_val == "discount":
            return False, "SELL blocked in discount zone"
        return True, "ok"

    def _news_proximity(self, now: datetime) -> Tuple[bool, str]:
        try:
            from config import CONFIG
            windows = getattr(CONFIG, "news_blackout_windows", [])
            for w in windows:
                start = w.get("start")
                if start and isinstance(start, datetime):
                    if timedelta(0) <= (start - now) <= timedelta(minutes=30):
                        return False, f"Pre-news block: {int((start-now).total_seconds()/60)} min to event"
        except Exception:
            pass
        return True, "ok"

    def _dead_zone_filter(self, now: datetime) -> Tuple[bool, str]:
        m = now.hour * 60 + now.minute
        for sh, sm, eh, em in _DEAD_ZONES:
            if sh * 60 + sm <= m <= eh * 60 + em:
                return False, f"Dead zone: {now.strftime('%H:%M')} UTC"
        return True, "ok"

    def _repeat_direction_block(
        self, signal: Signal, trades: Optional[List[Trade]], now: datetime
    ) -> Tuple[bool, str]:
        if not trades or len(trades) < 2:
            return True, "ok"
        last_two = trades[-2:]
        if not all(t.pnl < 0 and t.signal.direction == signal.direction for t in last_two):
            return True, "ok"
        try:
            last_close = last_two[-1].close_time
            if last_close and (now - last_close) < timedelta(minutes=45):
                remaining = int((last_close + timedelta(minutes=45) - now).total_seconds() / 60)
                return False, f"Repeat direction block: 2 {signal.direction.value} losses, {remaining} min cooldown"
        except AttributeError:
            pass
        return True, "ok"

    def _entry_zone_proximity(self, signal: Signal, primary: IctSnapshot) -> Tuple[bool, str]:
        atr_val = max(float(primary.atr), 1e-9)
        max_dist = atr_val * 0.5
        entry = signal.entry
        for zone in [primary.fvg, primary.order_block, primary.mitigation_block]:
            if zone is None:
                continue
            if zone.low <= entry <= zone.high:
                return True, "ok"
            if min(abs(entry - zone.low), abs(entry - zone.high)) <= max_dist:
                return True, "ok"
        return False, f"Entry {entry:.2f} > {max_dist:.1f} pts from any zone — chasing blocked"

    def _amd_session_check(self, signal: Signal, df: pd.DataFrame) -> Tuple[bool, str]:
        try:
            ts = df.index[-1]
            today_bars = df[df.index.date == ts.date()]
            if len(today_bars) < 2:
                return True, "ok"
            cutoff = ts - pd.Timedelta(hours=4)
            prior_bars = df[df.index < cutoff].tail(240)
            if len(prior_bars) < 10:
                return True, "ok"
            prior_high = float(prior_bars["high"].max())
            prior_low = float(prior_bars["low"].min())
            if signal.direction == Direction.SELL:
                if float(today_bars["high"].max()) <= prior_high:
                    return False, f"ICT Reversal SELL: no manipulation above {prior_high:.2f}"
            elif signal.direction == Direction.BUY:
                if float(today_bars["low"].min()) >= prior_low:
                    return False, f"ICT Reversal BUY: no manipulation below {prior_low:.2f}"
        except Exception:
            pass
        return True, "ok"

    # ── Checks 9–11 (new) ─────────────────────────────────────────────────

    def _candle_count_gate(
        self, signal: Signal, primary: IctSnapshot, df: pd.DataFrame
    ) -> Tuple[bool, str]:
        """3+ candles must close after FVG/OB forms before entry allowed."""
        zone = primary.fvg or primary.order_block
        if zone is None:
            return True, "ok"
        try:
            zone_time = zone.end_time
            candles_since = int((df.index > zone_time).sum())
            if candles_since < 3:
                return False, (
                    f"Zone too fresh: {candles_since} candles since {zone.kind} formed — need 3+"
                )
        except Exception:
            return True, "ok"
        return True, "ok"

    def _htf_displacement_check(
        self, signal: Signal, snapshots: Dict[str, IctSnapshot]
    ) -> Tuple[bool, str]:
        """5m or 15m must show displacement in same direction."""
        direction_str = "bullish" if signal.direction == Direction.BUY else "bearish"
        displacement_concept = f"{direction_str} displacement"

        higher_tfs_checked = 0
        for tf in ["5m", "15m"]:
            snap = snapshots.get(tf)
            if snap is None:
                continue
            higher_tfs_checked += 1
            has_disp = (
                snap.displacement == displacement_concept
                or displacement_concept in (snap.concepts or [])
            )
            if has_disp:
                return True, "ok"

        if higher_tfs_checked == 0:
            return True, "ok"  # no HTF data available, don't block

        return False, (
            f"No {direction_str} displacement on 5m/15m — "
            "1m signal lacks HTF institutional flow confirmation"
        )

    def _eq_level_trap_gate(self, signal: Signal, df: pd.DataFrame) -> Tuple[bool, str]:
        """After sweeping EQ High/Low, price must move AWAY — not drift back."""
        concepts = set(signal.concepts)
        try:
            close = float(df["close"].iloc[-1])
            prev_close = float(df["close"].iloc[-2])
            if signal.direction == Direction.SELL and "Equal Highs" in concepts:
                if close > prev_close:
                    return False, "Equal High trap: SELL but price still rising — wait for bearish close"
            if signal.direction == Direction.BUY and "Equal Lows" in concepts:
                if close < prev_close:
                    return False, "Equal Low trap: BUY but price still falling — wait for bullish close"
        except Exception:
            pass
        return True, "ok"

    def _in_killzone(self, now: datetime) -> bool:
        m = now.hour * 60 + now.minute
        for sh, sm, eh, em in _KILL_ZONES:
            if sh * 60 + sm <= m <= eh * 60 + em:
                return True
        return False
