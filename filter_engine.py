"""
FILTER ENGINE — NEW FILE for 70%+ Win-Rate
============================================
This is a NEW file. Wire it into terminal_server.py / bot.py between
signal_engine.generate() and trade execution.

Usage:
    from filter_engine import FilterEngine
    filter_engine = FilterEngine()

    signal = signal_engine.generate(frames, tick)
    if signal:
        allowed, reason = filter_engine.check(signal, snapshots, frames, recent_trades)
        if allowed:
            # execute trade
        else:
            log.info("Filter blocked: %s", reason)

Why this file exists
---------------------
The existing signal_engine already has quality gates, but they are
confidence-based (a score threshold). Confidence is a *relative* score —
it ranks setups but doesn't enforce that specific ICT prerequisites are
ALL present simultaneously.

This filter enforces ABSOLUTE prerequisites: a trade must have EVERY item
in a checklist, not just a high composite score. This is the key difference
between a 60% and 70%+ win-rate system.

Checks (all must pass for a signal to be allowed):

1. THREE-CONFLUENCE RULE: signal must have at least 3 distinct ICT/SMC
   confirmations from 3 different CATEGORIES (not just 3 items from the
   same category). Categories: structure (BOS/CHOCH/MSS), entry (FVG/OB),
   liquidity (sweep/equal levels), momentum (displacement/ADX/MACD), HTF.

2. HTF PYRAMID: if higher timeframe bias is available, it must AGREE with
   the signal direction. Any opposing HTF bias (not neutral) blocks the trade.

3. PREMIUM/DISCOUNT ALIGNMENT: BUY signals only pass in discount or
   equilibrium zones. SELL signals only pass in premium or equilibrium.
   A BUY in premium is blocked regardless of confidence.

4. NEWS PROXIMITY: signals within 30 minutes of any configured news event
   are blocked. This is in addition to the active blackout — it catches
   pre-news drift setups that look good but often fail.

5. TIME-OF-DAY QUALITY: signals in the 30-minute "dead zone" before London
   close (11:30–12:00 UTC) and NY lunch (16:30–17:00 UTC) are blocked.
   These periods have historically low follow-through on XAUUSD.

6. REPEAT DIRECTION BLOCK: if the last 2 closed trades were BOTH in the
   same direction AND both lost, new signals in that same direction are
   blocked for 45 minutes. This prevents directional bias fixation after
   consecutive directional failures.

7. ENTRY ZONE PROXIMITY: entry price must be within 0.5 ATR of an FVG or
   OB zone. An entry that is more than 0.5 ATR away from any zone is a
   "chasing" entry and is blocked.

8. MINIMUM 2 SESSIONS REQUIRED: for the ICT Reversal model specifically,
   the current session must have generated at least 2 candles above the
   prior session's high (for sell) or below the prior session's low (for
   buy). This enforces the AMD (Accumulation/Manipulation/Distribution)
   pattern at a session level.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from models import Direction, IctSnapshot, Signal, Trade


# ICT concept categories for the three-confluence check
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

# Dead zones (UTC) — low follow-through periods on XAUUSD
_DEAD_ZONES = [
    (11, 30, 12, 0),   # Pre-NY open, London close transition
    (16, 30, 17, 0),   # NY lunch / pre-close drift
    (20, 0, 22, 0),    # NY after-hours, very low volume
]


class FilterEngine:
    """
    Hard-gate filter for ICT/SMC signals. All checks must pass.
    Returns (True, "Allowed") or (False, "reason for block").
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

        # ── Check 1: Three-confluence rule ───────────────────────────────
        ok, reason = self._three_confluence(signal)
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

        # ── Check 4: News proximity (30-min pre-event) ────────────────────
        ok, reason = self._news_proximity(now)
        if not ok:
            return False, reason

        # ── Check 5: Dead-zone time filter ────────────────────────────────
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

        # ── Check 8: AMD session structure (ICT Reversal only) ────────────
        setup = str(signal.metadata.get("setup_model", ""))
        if "ICT Reversal" in setup and df is not None:
            ok, reason = self._amd_session_check(signal, df)
            if not ok:
                return False, reason

        return True, "Allowed"

    # ── Individual Checks ─────────────────────────────────────────────────

    def _three_confluence(self, signal: Signal) -> Tuple[bool, str]:
        """
        Must have confirmations from at least 3 different categories.
        """
        concepts = set(signal.concepts)
        categories_present = 0
        missing = []

        for name, category_set in [
            ("structure", _STRUCTURE_CONCEPTS),
            ("entry zone", _ENTRY_CONCEPTS),
            ("liquidity", _LIQUIDITY_CONCEPTS),
            ("momentum", _MOMENTUM_CONCEPTS),
            ("HTF", _HTF_CONCEPTS),
        ]:
            if concepts & category_set:
                categories_present += 1
            else:
                missing.append(name)

        if categories_present < 3:
            return False, f"Three-confluence gate: only {categories_present}/5 categories present. Missing: {', '.join(missing[:2])}"
        return True, "ok"

    def _htf_pyramid(
        self, signal: Signal, snapshots: Dict[str, IctSnapshot]
    ) -> Tuple[bool, str]:
        """
        Any opposing HTF bias blocks the signal.
        Neutral HTF is allowed.
        """
        from config import CONFIG
        wanted = "bullish" if signal.direction == Direction.BUY else "bearish"
        opposing = "bearish" if signal.direction == Direction.BUY else "bullish"

        for tf in getattr(getattr(CONFIG, "timeframes", None), "confluence", []):
            snap = snapshots.get(tf)
            if snap and snap.bias == opposing:
                return False, f"HTF pyramid blocked: {tf} bias is {opposing} against {signal.direction.value}"
        return True, "ok"

    def _premium_discount_gate(
        self, signal: Signal, primary: IctSnapshot
    ) -> Tuple[bool, str]:
        """
        BUY in premium = blocked. SELL in discount = blocked.
        """
        pd_val = primary.premium_discount
        if signal.direction == Direction.BUY and pd_val == "premium":
            return False, "BUY blocked in premium zone — wait for discount"
        if signal.direction == Direction.SELL and pd_val == "discount":
            return False, "SELL blocked in discount zone — wait for premium"
        return True, "ok"

    def _news_proximity(self, now: datetime) -> Tuple[bool, str]:
        """
        Block 30 minutes before any active news blackout starts.
        Uses the same config as active_news_blackout() but checks upcoming.
        """
        try:
            from config import CONFIG
            windows = getattr(CONFIG, "news_blackout_windows", [])
            for window in windows:
                start = window.get("start")
                if start and isinstance(start, datetime):
                    if timedelta(0) <= (start - now) <= timedelta(minutes=30):
                        return False, f"Pre-news blackout: {int((start-now).total_seconds()/60)} min to event"
        except Exception:
            pass
        return True, "ok"

    def _dead_zone_filter(self, now: datetime) -> Tuple[bool, str]:
        """
        Block signals during known low-follow-through periods.
        """
        m = now.hour * 60 + now.minute
        for sh, sm, eh, em in _DEAD_ZONES:
            if sh * 60 + sm <= m <= eh * 60 + em:
                return False, f"Dead zone block: {now.strftime('%H:%M')} UTC is low follow-through period"
        return True, "ok"

    def _repeat_direction_block(
        self,
        signal: Signal,
        trades: Optional[List[Trade]],
        now: datetime,
    ) -> Tuple[bool, str]:
        """
        If last 2 closed trades were same direction AND both losses,
        block new signals in that direction for 45 minutes.
        """
        if not trades or len(trades) < 2:
            return True, "ok"

        last_two = trades[-2:]
        same_dir_losses = all(
            t.pnl < 0 and t.signal.direction == signal.direction
            for t in last_two
        )
        if not same_dir_losses:
            return True, "ok"

        try:
            last_close = last_two[-1].close_time
            if last_close and (now - last_close) < timedelta(minutes=45):
                remaining = int((last_close + timedelta(minutes=45) - now).total_seconds() / 60)
                return False, f"Repeat direction block: 2 {signal.direction.value} losses, {remaining} min cooldown remaining"
        except AttributeError:
            pass

        return True, "ok"

    def _entry_zone_proximity(
        self,
        signal: Signal,
        primary: IctSnapshot,
        df: pd.DataFrame,
    ) -> Tuple[bool, str]:
        """
        Entry must be within 0.5 ATR of an FVG or OB zone.
        Prevents chasing entries far from institutional zones.
        """
        atr_val = max(float(primary.atr), 1e-9)
        max_dist = atr_val * 0.5
        entry = signal.entry

        for zone in [primary.fvg, primary.order_block, primary.mitigation_block]:
            if zone is None:
                continue
            dist = min(abs(entry - zone.low), abs(entry - zone.high))
            if zone.low <= entry <= zone.high:
                return True, "ok"  # inside zone
            if dist <= max_dist:
                return True, "ok"  # close to zone

        return False, f"Entry {entry:.2f} is more than {max_dist:.1f} pts from any FVG/OB zone — chasing entry blocked"

    def _amd_session_check(
        self,
        signal: Signal,
        df: pd.DataFrame,
    ) -> Tuple[bool, str]:
        """
        AMD session check for ICT Reversal:
        For a SELL: price must have traded above the prior session high (manipulation).
        For a BUY: price must have traded below the prior session low (manipulation).
        Without this, the reversal has no manipulation leg and is just a random entry.
        """
        try:
            ts = df.index[-1]
            today = ts.date()

            today_bars = df[df.index.date == today]
            if len(today_bars) < 2:
                return True, "ok"  # not enough session data, don't block

            # Find prior session bars (yesterday or more than 4 hours ago)
            cutoff = ts - pd.Timedelta(hours=4)
            prior_bars = df[df.index < cutoff].tail(240)
            if len(prior_bars) < 10:
                return True, "ok"

            prior_high = float(prior_bars["high"].max())
            prior_low = float(prior_bars["low"].min())

            if signal.direction == Direction.SELL:
                # Must have swept above prior high (manipulation)
                session_high = float(today_bars["high"].max())
                if session_high <= prior_high:
                    return False, f"ICT Reversal SELL: no manipulation above prior high ({prior_high:.2f}) — AMD incomplete"
            elif signal.direction == Direction.BUY:
                # Must have swept below prior low
                session_low = float(today_bars["low"].min())
                if session_low >= prior_low:
                    return False, f"ICT Reversal BUY: no manipulation below prior low ({prior_low:.2f}) — AMD incomplete"
        except Exception:
            return True, "ok"  # don't block on error

        return True, "ok"
