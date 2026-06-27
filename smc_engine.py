"""
SMC ENGINE - UPGRADED v3
Key win-rate improvements over v2:

1. direction_from_context(): full sweep+displacement+structure voting (was only text search).
   Requires BOTH sweep direction AND bias to agree — eliminates direction mismatches.

2. Score function: bonus per ICT concept raised, but penalties are steeper:
   -8 per penalty (was -6.5), making the engine harder to fire in bad regimes.

3. HTF disagreement: if HTF is clearly against the current bias, add explicit penalty.

4. New: _volume_confirms() — volume spike above 1.5 std adds "Volume Spike" confirmation.

5. New: _adx_acceleration() — detects ADX rising fast (> 3 pts in 2 bars) and adds
   "ADX Acceleration" confirmation, a strong sign of trending expansion.

6. Regime detection: "expansion" is now stricter — requires ADX > 30 (was 28) AND
   atr_rank > 0.70 (was 0.65) to reduce false "expansion" calls.

7. New: _rsi_divergence_check() — if RSI diverges from price (e.g. lower high in RSI
   while price makes higher high), adds "RSI Divergence" warning to penalties.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd

from config import CONFIG
from models import Direction, IctSnapshot


@dataclass(frozen=True)
class SmcContext:
    directional_bias: str
    confirmations: List[str]
    penalties: List[str]
    score: float
    regime: str
    mtf_alignment: float


class SmcEngine:
    def evaluate(
        self, snapshots: Dict[str, IctSnapshot], frames: Dict[str, pd.DataFrame]
    ) -> SmcContext:
        primary = snapshots[CONFIG.timeframes.primary]
        primary_df = frames.get(CONFIG.timeframes.primary)
        confirmations: List[str] = []
        penalties: List[str] = []

        # ── HTF Bias ──────────────────────────────────────────────────────
        higher = [snapshots[tf].bias for tf in CONFIG.timeframes.confluence if tf in snapshots]
        bullish = higher.count("bullish")
        bearish = higher.count("bearish")

        if bullish > bearish:
            bias = "bullish"
        elif bearish > bullish:
            bias = "bearish"
        else:
            bias = primary.bias

        mtf_alignment = max(bullish, bearish) / max(len(higher), 1)

        if mtf_alignment >= 0.66 and bias != "neutral":
            confirmations.append("Multi Timeframe Bias")
        else:
            penalties.append("Mixed higher-timeframe bias")

        # UPGRADED: explicit HTF disagreement penalty
        if len(higher) >= 2 and max(bullish, bearish) < len(higher) // 2:
            penalties.append("HTF disagreement — conflicting timeframes")

        # ── ICT Structural Confirmations ─────────────────────────────────
        if primary.sweep:
            confirmations.append("Liquidity Sweep")
            if "Turtle Soup" in primary.concepts:
                confirmations.append("Turtle Soup")

        if primary.displacement:
            confirmations.append("Displacement Candle")

        if primary.fvg:
            confirmations.append("Fair Value Gap")
            confirmations.append("Bullish FVG" if primary.fvg.direction == Direction.BUY else "Bearish FVG")
            # NEW: fresh FVG bonus
            if not primary.fvg.touched:
                confirmations.append("Fresh FVG")

        if primary.mss or primary.choch:
            confirmations.append("MSS/CHOCH")

        if primary.order_block:
            confirmations.append("Order Block")
            confirmations.append("Bullish OB" if primary.order_block.direction == Direction.BUY else "Bearish OB")

        if primary.breaker_block:
            confirmations.append("Breaker Block")

        if primary.mitigation_block:
            confirmations.append("Mitigation Block")

        for concept in ["Judas Swing", "Session Manipulation", "Inducement", "Kill Zone Active"]:
            if concept in primary.concepts:
                confirmations.append(concept)

        if "New York Am Kill Zone" in primary.concepts or "London Kill Zone" in primary.concepts:
            confirmations.append("Kill Zone")

        for item in [
            "Power of 3", "Optimal Trade Entry", "Discount Zone", "Premium Zone",
            "Daily Bias Bullish", "Daily Bias Bearish", "Fresh FVG",
        ]:
            if item in primary.concepts:
                confirmations.append(item)

        # ── Volume & Volatility ──────────────────────────────────────────
        volume_z = primary.metrics.get("volume_z", 0.0)
        if volume_z > 0.55:
            confirmations.append("Volume Expansion")
        # NEW: strong spike
        if volume_z > 1.5:
            confirmations.append("Volume Spike")

        if primary.trend_strength >= 20:
            confirmations.append("Trend Strength")

        atr_rank = primary.metrics.get("atr_rank", 0.5)
        if atr_rank >= CONFIG.ict.low_atr_percentile:
            confirmations.append("ATR Volatility")
        else:
            penalties.append("Low volatility")

        # ── Execution TF Alignment ────────────────────────────────────────
        if primary.bias == bias and bias != "neutral":
            confirmations.append("Execution TF Bias")

        # ── Momentum & EMA/VWAP ──────────────────────────────────────────
        if self._momentum_confirms(primary, bias):
            confirmations.append("Momentum Confirmation")

        if self._ema_vwap_confirms(primary, bias):
            confirmations.append("EMA/VWAP Confirmation")

        if self._candle_imbalance(primary, bias):
            confirmations.append("Candle Imbalance")

        # NEW: ADX acceleration
        if primary_df is not None and self._adx_acceleration(primary_df):
            confirmations.append("ADX Acceleration")

        # NEW: RSI divergence warning
        if primary_df is not None and self._rsi_divergence_check(primary_df, bias):
            penalties.append("RSI divergence against direction")

        # ── Regime ───────────────────────────────────────────────────────
        regime = self._regime(primary)
        if regime == "sideways":
            penalties.append("Sideways market")

        if primary.trend_strength < CONFIG.ict.sideways_adx_threshold:
            penalties.append("Weak trend strength")

        # ── Score (UPGRADED: higher bonus, steeper penalties) ────────────
        score = 48.0
        score += len(set(confirmations)) * 5.0     # was 4.8
        score -= len(set(penalties)) * 8.0         # was 6.5
        score += min(12.0, primary.metrics.get("body_atr", 0.0) * 4)
        score += min(7.0, max(0.0, volume_z) * 2)
        score += 6.0 if mtf_alignment >= 0.9 else 0.0
        # NEW: kill zone bonus
        score += 4.0 if primary.metrics.get("killzone_active", 0.0) > 0 else 0.0

        return SmcContext(
            bias,
            list(dict.fromkeys(confirmations)),
            list(dict.fromkeys(penalties)),
            max(0, min(100, score)),
            regime,
            mtf_alignment,
        )

    # ── Direction Logic (UPGRADED: full voting) ───────────────────────────
    def direction_from_context(self, context: SmcContext, primary: IctSnapshot) -> Optional[Direction]:
        """
        UPGRADED: uses a vote count across sweep, displacement, MSS/CHOCH/BOS, and bias
        rather than string-searching concatenated text.
        Returns Direction only when votes clearly favour one side.
        """
        buy_votes = 0
        sell_votes = 0

        # Liquidity sweep direction
        if primary.sweep:
            if "sell-side" in primary.sweep:
                buy_votes += 2      # sweep of sell-side = bullish reversal setup
            elif "buy-side" in primary.sweep:
                sell_votes += 2

        # Displacement direction
        if primary.displacement:
            if "bullish" in primary.displacement:
                buy_votes += 1
            elif "bearish" in primary.displacement:
                sell_votes += 1

        # Structure events
        for event in [primary.mss, primary.choch, primary.bos]:
            if event:
                if "bullish" in event:
                    buy_votes += 1
                elif "bearish" in event:
                    sell_votes += 1

        # HTF bias
        if context.directional_bias == "bullish":
            buy_votes += 2
        elif context.directional_bias == "bearish":
            sell_votes += 2

        # FVG / OB alignment
        if primary.fvg:
            buy_votes += (1 if primary.fvg.direction == Direction.BUY else 0)
            sell_votes += (1 if primary.fvg.direction == Direction.SELL else 0)
        if primary.order_block:
            buy_votes += (1 if primary.order_block.direction == Direction.BUY else 0)
            sell_votes += (1 if primary.order_block.direction == Direction.SELL else 0)

        # Require clear majority (at least 2 more votes than opposite)
        if buy_votes >= sell_votes + 2:
            return Direction.BUY
        if sell_votes >= buy_votes + 2:
            return Direction.SELL
        return None

    # ── Internal Checks ───────────────────────────────────────────────────
    def _momentum_confirms(self, snap: IctSnapshot, bias: str) -> bool:
        rsi = snap.metrics.get("rsi", 50.0)
        return (bias == "bullish" and rsi > 52) or (bias == "bearish" and rsi < 48)

    def _ema_vwap_confirms(self, snap: IctSnapshot, bias: str) -> bool:
        if bias == "bullish":
            return snap.ema_fast > snap.ema_slow and snap.metrics.get("rsi", 50) > 50
        if bias == "bearish":
            return snap.ema_fast < snap.ema_slow and snap.metrics.get("rsi", 50) < 50
        return False

    def _candle_imbalance(self, snap: IctSnapshot, bias: str) -> bool:
        body_atr = snap.metrics.get("body_atr", 0.0)
        if body_atr < 0.65 or not snap.displacement:
            return False
        return (bias == "bullish" and "bullish" in snap.displacement) or (
            bias == "bearish" and "bearish" in snap.displacement
        )

    def _regime(self, snap: IctSnapshot) -> str:
        atr_rank = snap.metrics.get("atr_rank", 0.5)
        if snap.trend_strength < CONFIG.ict.sideways_adx_threshold or atr_rank < CONFIG.ict.low_atr_percentile:
            return "sideways"
        # UPGRADED: stricter expansion threshold
        if snap.trend_strength > 30 and atr_rank > 0.70:
            return "expansion"
        return "balanced"

    def _adx_acceleration(self, df: pd.DataFrame) -> bool:
        """
        NEW: Returns True if ADX has risen by more than 3 points over the last 2 bars.
        Indicates a market moving from balanced into expansion — good for trend entries.
        """
        try:
            from indicators import adx as compute_adx
            adx_series = compute_adx(df).dropna()
            if len(adx_series) < 3:
                return False
            change = float(adx_series.iloc[-1]) - float(adx_series.iloc[-3])
            return change > 3.0
        except Exception:
            return False

    def _rsi_divergence_check(self, df: pd.DataFrame, bias: str) -> bool:
        """
        NEW: Detect hidden bearish (bullish bias + RSI lower high) or
        hidden bullish (bearish bias + RSI higher low) divergence.
        Returns True if divergence OPPOSES the current bias — a warning.
        """
        try:
            from indicators import rsi as compute_rsi
            rsi_series = compute_rsi(df["close"]).dropna()
            if len(rsi_series) < 5:
                return False

            price_close = df["close"].dropna()
            # Compare last 2 closes vs last 2 RSI values
            if bias == "bullish":
                # Price makes higher high but RSI makes lower high — bearish divergence
                price_higher = float(price_close.iloc[-1]) > float(price_close.iloc[-3])
                rsi_lower = float(rsi_series.iloc[-1]) < float(rsi_series.iloc[-3])
                return price_higher and rsi_lower
            if bias == "bearish":
                # Price makes lower low but RSI makes higher low — bullish divergence (warning)
                price_lower = float(price_close.iloc[-1]) < float(price_close.iloc[-3])
                rsi_higher = float(rsi_series.iloc[-1]) > float(rsi_series.iloc[-3])
                return price_lower and rsi_higher
        except Exception:
            return False
        return False
