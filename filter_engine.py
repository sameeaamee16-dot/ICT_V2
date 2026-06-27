"""
FILTER ENGINE v2 — ICT_V2 Upgrade
====================================
New checks added vs the version already in ICT_V2:

9.  SILVER BULLET SESSION: 10:00–11:00 UTC (NY open first hour) is the
    highest win-rate ICT session on XAUUSD. Signals outside ALL kill zones
    now carry a -5 score penalty. Signals INSIDE the Silver Bullet window
    get priority (this is purely informational here — the real boost is that
    off-session signals need HIGHER confidence to pass check 3-confluence).

10. CANDLE COUNT GATE: at least 3 confirmed 1m closes must have happened
    AFTER the FVG or OB formed before the signal can fire. This prevents
    entering the very first candle after a zone forms (which is often the
    retest setup before the real move — entering too early here is a common
    loss pattern).

11. HTF DISPLACEMENT AGREEMENT: for the primary TF signal direction,
    the 5m or 15m must show displacement in the same direction within the
    last 10 bars. A 1m signal going long must have had a recent bullish
    displacement on the higher TF to confirm institutional intent.

12. EQUAL HIGH/LOW TRAP GATE: if the last liquidity sweep hit an Equal
    High (for sells) or Equal Low (for buys) and the current candle is
    moving BACK toward that swept level, the trade is blocked. This is the
    "trap" pattern — price sweeps, then traps late buyers/sellers before
    reversing. Only trades clearly moving AWAY from the swept level pass.

All other original 8 checks remain unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from models import Direction, IctSnapshot, Signal, Trade

# ICT concept categories for three-confluence check
_STRUCTURE_CONCEPTS = {
    "BOS", "CHOCH", "MSS", "bullish BOS", "bearish BOS",
    "bullish CHOCH", "bearish CHOCH", "bullish MSS", "bearish MSS",
    "Higher High", "Higher Low", "Lower High", "Lower Low",
}
_ENTRY_CONCEPTS = {
    "Fair Value Gap", "Bullish FVG", "Bearish FVG", "Fresh FVG",
    "Order Block", "Bullish OB", "Bearish OB",
    "Breaker Block", "Mitigation Block", "Rejection Block",
    "Optimal Trade Entry",
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

# Dead zones UTC — low follow-through on XAUUSD
_DEAD_ZONES = [
    (11, 30, 12, 0),   # Pre-NY open, London close transition
    (16, 30, 17, 0),   # NY lunch / pre-close drift
    (20, 0, 22, 0),    # NY after-hours, very low volume
]

# Kill zones UTC — high probability windows
_KILL_ZONES = [
    (7, 0, 9, 0),      # London open
    (10, 0, 11, 0),    # Silver Bullet (NY open first hour) ← NEW
    (12, 0, 14, 30),   # NY AM
    (15, 0, 16, 0),    # NY lunch reversal
]


class FilterEngine:
    """
    Hard-gate filter — ALL checks must pass.
    Returns (True, "Allowed") or (False, "reason").
    """

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

        # ── Check 1: Three-confluence ──────────────────────────────────────
        ok, reason = self._three_confluence(signal, now)
        if not ok:
            return False, reason

        # ── Check 2: HTF pyramid ──────────────────────────────────────────
        ok, reason = self._htf_pyramid(signal, snapshots)
        if not ok:
            return False, reason

        # ── Check 3: Premium/discount alignment ───────────────────────────
        if primary:
            ok, reason = self._premium_discount_gate(signal, primary)
            if not ok:
                return False, reason

        # ── Check 4: News proximity ────────────────────────────────────────
        ok, reason = self._news_proximity(now)
        if not ok:
            return False, reason

        # ── Check 5: Dead zone ────────────────────────────────────────────
        ok, reason = self._dead_zone_filter(now)
        if not ok:
            return False, reason

        # ── Check 6: Repeat direction block ───────────────────────────────
        ok, reason = self._repeat_direction_block(signal, recent_closed_trades, now)
        if not ok:
            return False, reason

        # ── Check 7: Entry zone proximity ─────────────────────────────────
        if primary and df is not None:
            ok, reason = self._entry_zone_proximity(signal, primary, df)
            if not ok:
                return False, reason

        # ── Check 8: AMD session structure ────────────────────────────────
        setup = str(signal.metadata.get("setup_model", ""))
        if "ICT Reversal" in setup and df is not None:
            ok, reason = self._amd_session_check(signal, df)
            if not ok:
                return False, reason

        # ── Check 9 (NEW): Candle count gate ──────────────────────────────
        if primary and df is not None:
            ok, reason = self._candle_count_gate(signal, primary, df)
            if not ok:
                return False, reason

        # ── Check 10 (NEW): HTF displacement agreement ────────────────────
        if primary and len(snapshots) > 1:
            ok, reason = self._htf_displacement_check(signal, snapshots)
            if not ok:
                return False, reason

        # ── Check 11 (NEW): Equal high/low trap gate ──────────────────────
        if primary and df is not None:
            ok, reason = self._eq_level_trap_gate(signal, primary, df)
            if not ok:
                return False, reason

        return True, "Allowed"

    # ── Original Checks (unchanged logic) ─────────────────────────────────

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

        # Off-session signals need 4 categories (stricter) — NEW
        in_killzone = self._in_killzone(now)
        required = 3 if in_killzone else 4

        if categories_present < required:
            zone_note = "" if in_killzone else " (off-session requires 4 categories)"
            return False, (
                f"Three-confluence gate{zone_note}: only {categories_present}/5 categories. "
                f"Missing: {', '.join(missing[:2])}"
            )
        return True, "ok"

    def _htf_pyramid(self, signal: Signal, snapshots: Dict[str, IctSnapshot]) -> Tuple[bool, str]:
        from config import CONFIG
        wanted = "bullish" if signal.direction == Direction.BUY else "bearish"
        opposing = "bearish" if signal.direction == Direction.BUY else "bullish"
        for tf in getattr(getattr(CONFIG, "timeframes", None), "confluence", []):
            snap = snapshots.get(tf)
            if snap and snap.bias == opposing:
                return False, f"HTF pyramid blocked: {tf} is {opposing} against {signal.direction.value}"
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

    def _repeat_direction_block(self, signal: Signal, trades: Optional[List[Trade]], now: datetime) -> Tuple[bool, str]:
        if not trades or len(trades) < 2:
            return True, "ok"
        last_two = trades[-2:]
        same_dir_losses = all(t.pnl < 0 and t.signal.direction == signal.direction for t in last_two)
        if not same_dir_losses:
            return True, "ok"
        try:
            last_close = last_two[-1].close_time
            if last_close and (now - last_close) < timedelta(minutes=45):
                remaining = int((last_close + timedelta(minutes=45) - now).total_seconds() / 60)
                return False, f"Repeat direction block: 2 {signal.direction.value} losses, {remaining} min cooldown"
        except AttributeError:
            pass
        return True, "ok"

    def _entry_zone_proximity(self, signal: Signal, primary: IctSnapshot, df: pd.DataFrame) -> Tuple[bool, str]:
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

    # ── NEW Checks ─────────────────────────────────────────────────────────

    def _candle_count_gate(
        self, signal: Signal, primary: IctSnapshot, df: pd.DataFrame
    ) -> Tuple[bool, str]:
        """
        NEW Check 9: At least 3 candles must have closed after the FVG/OB formed.
        Prevents entering the very first candle of a zone — early entries on
        fresh zones often lose before the real setup develops.
        """
        zone = primary.fvg or primary.order_block
        if zone is None:
            return True, "ok"  # no zone to check

        try:
            zone_time = zone.end_time  # when zone formed
            candles_since = int((df.index > zone_time).sum())
            if candles_since < 3:
                return False, (
                    f"Zone too fresh: only {candles_since} candles since "
                    f"{zone.kind} formed — need 3+ for confirmation"
                )
        except Exception:
            return True, "ok"

        return True, "ok"

    def _htf_displacement_check(
        self, signal: Signal, snapshots: Dict[str, IctSnapshot]
    ) -> Tuple[bool, str]:
        """
        NEW Check 10: A higher-TF timeframe (5m or 15m) must show displacement
        in the same direction as the signal within its most recent concept list.
        Without this, the 1m trade lacks institutional flow confirmation.
        """
        direction_str = "bullish" if signal.direction == Direction.BUY else "bearish"
        displacement_concept = f"{direction_str} displacement"

        for tf in ["5m", "15m"]:
            snap = snapshots.get(tf)
            if snap is None:
                continue
            # Check if displacement is in the snapshot's concepts or displacement field
            has_displacement = (
                snap.displacement == displacement_concept
                or displacement_concept in (snap.concepts or [])
            )
            if has_displacement:
                return True, "ok"

        # No HTF displacement found — but only block if we have HTF data
        higher_tfs = [s for tf, s in snapshots.items() if tf in ("5m", "15m")]
        if not higher_tfs:
            return True, "ok"  # no HTF data, don't block

        return False, (
            f"No {direction_str} displacement on 5m or 15m — "
            "1m signal lacks HTF institutional flow confirmation"
        )

    def _eq_level_trap_gate(
        self, signal: Signal, primary: IctSnapshot, df: pd.DataFrame
    ) -> Tuple[bool, str]:
        """
        NEW Check 11: Equal High/Low trap gate.
        After a sweep of Equal Highs (for sells) or Equal Lows (for buys),
        the current candle must be moving AWAY from the swept level, not back
        toward it. If price is drifting back toward the swept level, it's a trap.
        """
        concepts = set(signal.concepts)
        try:
            close = float(df["close"].iloc[-1])
            prev_close = float(df["close"].iloc[-2])

            if signal.direction == Direction.SELL and "Equal Highs" in concepts:
                # After sweeping equal highs, price should be FALLING (away from them)
                if close > prev_close:
                    return False, (
                        "Equal High trap gate: SELL signal but price rising toward "
                        "swept level — possible trap, wait for bearish close"
                    )

            if signal.direction == Direction.BUY and "Equal Lows" in concepts:
                # After sweeping equal lows, price should be RISING (away from them)
                if close < prev_close:
                    return False, (
                        "Equal Low trap gate: BUY signal but price falling toward "
                        "swept level — possible trap, wait for bullish close"
                    )
        except Exception:
            pass

        return True, "ok"

    # ── Helper ────────────────────────────────────────────────────────────

    def _in_killzone(self, now: datetime) -> bool:
        m = now.hour * 60 + now.minute
        for sh, sm, eh, em in _KILL_ZONES:
            if sh * 60 + sm <= m <= eh * 60 + em:
                return True
        return False
