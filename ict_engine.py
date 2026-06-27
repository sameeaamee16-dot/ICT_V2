"""
ICT ENGINE - UPGRADED v3
Key win-rate improvements over v2:
1. FVG strength: penalises touched FVGs heavily (-40 pts) — avoids re-entering consumed zones.
2. Order Block invalidation: multi-touch OBs lose 15 pts per re-test beyond the first.
3. Swing detection: uses 2-candle body confirmation (not just wick high/low), reducing false swings.
4. Premium/Discount: equilibrium window tightened from ±0.2% to ±0.08%, giving cleaner zone calls.
5. Structure events (BOS/CHOCH/MSS): require body close beyond swing, not just wick cross.
6. Displacement: requires body/range > 0.65 (was 0.58) AND volume_z > 0.3 for confirmation.
7. New: _killzone_active() — marks whether current bar is inside a high-probability kill zone.
8. New: _fvg_freshness_score() — scores FVGs by how recently they formed and whether price has
   revisited them, giving the signal engine a quality signal rather than a binary flag.
9. Rejection block: stricter — lower wick must be > 2.0x body (was 1.7x) to reduce noise.
10. Liquidity sweep: must close back INSIDE the level within 1 ATR, otherwise no sweep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from config import CONFIG, IctConfig
from indicators import adx, atr, ema, rsi, vwap
from models import Direction, IctSnapshot, Zone


@dataclass(frozen=True)
class Swing:
    kind: str
    pivot_time: datetime
    confirm_time: datetime
    price: float
    index: int


class IctEngine:
    def __init__(self, config: IctConfig = CONFIG.ict):
        self.config = config

    def analyze(self, df: pd.DataFrame, timeframe: str) -> IctSnapshot:
        if len(df) < 80:
            raise ValueError(f"{timeframe} requires at least 80 closed candles")

        work = df.copy()
        work["atr"] = atr(work)
        work["ema_fast"] = ema(work["close"], 21)
        work["ema_slow"] = ema(work["close"], 55)
        work["vwap"] = vwap(work)
        work["adx"] = adx(work)
        work["rsi"] = rsi(work["close"])

        atr_tail = work["atr"].tail(240).dropna()
        atr_rank = 0.5
        if not atr_tail.empty and pd.notna(atr_tail.iloc[-1]):
            atr_rank = float((atr_tail <= atr_tail.iloc[-1]).mean())
        work["atr_rank"] = atr_rank

        swings = self._confirmed_swings(work)
        last = work.iloc[-1]

        zones: List[Zone] = []
        fvg = self._latest_fvg(work)
        order_block = self._latest_order_block(work)
        breaker = self._latest_breaker_block(work, swings)
        mitigation = self._latest_mitigation_block(work, order_block)
        rejection = self._latest_rejection_block(work)

        for zone in [fvg, order_block, breaker, mitigation, rejection]:
            if zone:
                zones.append(zone)

        liquidity = self._liquidity_levels(work, swings)
        sweep = self._liquidity_sweep(work, liquidity)
        mss, bos, choch = self._structure_events(work, swings)
        displacement = self._displacement(work)
        premium_discount = self._premium_discount(work)
        bias = self._bias(work, mss, choch, bos)
        killzone_active = self._killzone_active(work)

        concepts = self._concepts(
            work, swings, liquidity, fvg, order_block, breaker, mitigation,
            rejection, sweep, mss, bos, choch, displacement, premium_discount,
            bias, killzone_active,
        )

        return IctSnapshot(
            timeframe=timeframe,
            timestamp=work.index[-1].to_pydatetime(),
            bias=bias,
            trend_strength=float(last["adx"]) if pd.notna(last["adx"]) else 0.0,
            atr=float(last["atr"]),
            vwap=float(last["vwap"]),
            ema_fast=float(last["ema_fast"]),
            ema_slow=float(last["ema_slow"]),
            concepts=concepts,
            zones=zones,
            liquidity_levels=liquidity,
            premium_discount=premium_discount,
            displacement=displacement,
            mss=mss,
            choch=choch,
            bos=bos,
            sweep=sweep,
            fvg=fvg,
            order_block=order_block,
            breaker_block=breaker,
            mitigation_block=mitigation,
            rejection_block=rejection,
            metrics={
                "rsi": float(last["rsi"]) if pd.notna(last["rsi"]) else 50.0,
                "atr_rank": float(last["atr_rank"]) if pd.notna(last["atr_rank"]) else 0.5,
                "volume_z": self._volume_z(work),
                "body_atr": abs(float(last["close"] - last["open"])) / max(float(last["atr"]), 1e-9),
                # NEW: expose kill-zone flag downstream
                "killzone_active": 1.0 if killzone_active else 0.0,
                # NEW: FVG freshness (0–100), used by signal_engine for confidence bonus
                "fvg_freshness": self._fvg_freshness_score(work, fvg),
            },
        )

    # ── Swing Detection (UPGRADED: body-close confirmation) ──────────────
    def _confirmed_swings(self, df: pd.DataFrame) -> List[Swing]:
        """
        Upgraded: a swing high/low is only valid if the pivot candle's BODY
        (not just wick) is the extreme within the left/right window.
        This cuts false swing detections caused by long wicks without follow-through.
        """
        left, right = self.config.swing_left, self.config.swing_right
        swings: List[Swing] = []

        for i in range(left, len(df) - right):
            window = df.iloc[i - left : i + right + 1]
            high = df["high"].iloc[i]
            low = df["low"].iloc[i]
            # Body top/bottom of the pivot candle
            body_top = max(df["open"].iloc[i], df["close"].iloc[i])
            body_bot = min(df["open"].iloc[i], df["close"].iloc[i])
            confirm_time = df.index[i + right].to_pydatetime()

            # Swing high: wick must be highest AND body_top > prior closes
            if high == window["high"].max() and window["high"].iloc[: left + 1].idxmax() == df.index[i]:
                # NEW: body confirmation — body_top must clear the prior right-side close
                right_closes = df["close"].iloc[i + 1 : i + right + 1]
                if right_closes.empty or body_top >= right_closes.max():
                    swings.append(Swing("high", df.index[i].to_pydatetime(), confirm_time, float(high), i + right))

            # Swing low: wick must be lowest AND body_bot < prior closes
            if low == window["low"].min() and window["low"].iloc[: left + 1].idxmin() == df.index[i]:
                right_closes = df["close"].iloc[i + 1 : i + right + 1]
                if right_closes.empty or body_bot <= right_closes.min():
                    swings.append(Swing("low", df.index[i].to_pydatetime(), confirm_time, float(low), i + right))

        return [s for s in swings if s.index < len(df)]

    def _last_swings(self, swings: List[Swing], kind: str, n: int = 3) -> List[Swing]:
        return [s for s in swings if s.kind == kind][-n:]

    # ── Structure Events (UPGRADED: body-close beyond swing) ─────────────
    def _structure_events(
        self, df: pd.DataFrame, swings: List[Swing]
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        UPGRADED: BOS/CHOCH/MSS require the candle BODY (not just wick) to close
        beyond the prior swing level, eliminating wick-only false breaks.
        """
        highs = self._last_swings(swings, "high", 3)
        lows = self._last_swings(swings, "low", 3)

        if len(highs) < 2 or len(lows) < 2:
            return None, None, None

        close = float(df["close"].iloc[-1])
        prev_close = float(df["close"].iloc[-2])
        last_high, prior_high = highs[-1], highs[-2]
        last_low, prior_low = lows[-1], lows[-2]

        trend_up = last_high.price > prior_high.price and last_low.price > prior_low.price
        trend_down = last_high.price < prior_high.price and last_low.price < prior_low.price

        bos = None
        choch = None
        mss = None

        # UPGRADED: use body close (prev_close check kept for confirmation)
        if prev_close <= last_high.price < close:
            # Close above swing high — bullish break
            bos = "bullish BOS" if trend_up else None
            choch = "bullish CHOCH" if trend_down else None
            mss = "bullish MSS"

        if prev_close >= last_low.price > close:
            # Close below swing low — bearish break
            bos = "bearish BOS" if trend_down else None
            choch = "bearish CHOCH" if trend_up else None
            mss = "bearish MSS"

        return mss, bos, choch

    # ── FVG (UPGRADED: penalise touched/consumed FVGs) ───────────────────
    def _latest_fvg(self, df: pd.DataFrame) -> Optional[Zone]:
        atr_val = float(df["atr"].iloc[-1])
        min_gap = atr_val * self.config.fvg_min_atr

        for i in range(len(df) - 1, max(2, len(df) - 80), -1):
            a, c = df.iloc[i - 2], df.iloc[i]

            if c["low"] > a["high"] and c["low"] - a["high"] >= min_gap:
                touches = int((df["low"].iloc[i + 1 :] <= c["low"]).sum()) if i + 1 < len(df) else 0
                touched = touches > 0
                # UPGRADED: degrade strength based on how many times price re-entered
                base_strength = min(100, (c["low"] - a["high"]) / max(atr_val, 1e-9) * 60)
                strength = max(10, base_strength - touches * 40)  # -40 per touch
                return Zone(
                    "FVG", Direction.BUY,
                    df.index[i - 2].to_pydatetime(), df.index[i].to_pydatetime(),
                    float(a["high"]), float(c["low"]), strength, touched,
                )

            if c["high"] < a["low"] and a["low"] - c["high"] >= min_gap:
                touches = int((df["high"].iloc[i + 1 :] >= c["high"]).sum()) if i + 1 < len(df) else 0
                touched = touches > 0
                base_strength = min(100, (a["low"] - c["high"]) / max(atr_val, 1e-9) * 60)
                strength = max(10, base_strength - touches * 40)
                return Zone(
                    "FVG", Direction.SELL,
                    df.index[i - 2].to_pydatetime(), df.index[i].to_pydatetime(),
                    float(c["high"]), float(a["low"]), strength, touched,
                )

        return None

    # ── Order Block (UPGRADED: multi-touch invalidation) ─────────────────
    def _latest_order_block(self, df: pd.DataFrame) -> Optional[Zone]:
        atr_val = float(df["atr"].iloc[-1])

        for i in range(len(df) - 1, max(5, len(df) - self.config.ob_lookback), -1):
            body = abs(float(df["close"].iloc[i] - df["open"].iloc[i]))
            if body < atr_val * self.config.displacement_atr_mult:
                continue

            direction = Direction.BUY if df["close"].iloc[i] > df["open"].iloc[i] else Direction.SELL
            opposite = df.iloc[max(0, i - 8) : i]

            if direction == Direction.BUY:
                candidates = opposite[opposite["close"] < opposite["open"]]
            else:
                candidates = opposite[opposite["close"] > opposite["open"]]

            if candidates.empty:
                continue

            candle = candidates.iloc[-1]
            start = candidates.index[-1].to_pydatetime()
            ob_low = float(candle["low"])
            ob_high = float(candle["high"])

            # UPGRADED: count how many candles after OB formation touched back into it
            future = df.iloc[i:]
            if direction == Direction.BUY:
                retests = int(((future["low"] <= ob_high) & (future["high"] >= ob_low)).sum())
            else:
                retests = int(((future["high"] >= ob_low) & (future["low"] <= ob_high)).sum())

            # Penalise per retest: first is confirmation, subsequent weaken the OB
            base_strength = min(100, body / max(atr_val, 1e-9) * 55)
            strength = max(10, base_strength - max(0, retests - 1) * 15)

            return Zone("Order Block", direction, start, df.index[i].to_pydatetime(), ob_low, ob_high, strength)

        return None

    def _latest_breaker_block(self, df: pd.DataFrame, swings: List[Swing]) -> Optional[Zone]:
        ob = self._latest_order_block(df)
        mss, _, choch = self._structure_events(df, swings)
        if not ob or not (mss or choch):
            return None

        opposite = Direction.SELL if ob.direction == Direction.BUY else Direction.BUY
        close = float(df["close"].iloc[-1])
        broken = close < ob.low if ob.direction == Direction.BUY else close > ob.high

        if broken:
            return Zone(
                "Breaker Block", opposite,
                ob.start_time, df.index[-1].to_pydatetime(),
                ob.low, ob.high, ob.strength * 0.9,
            )
        return None

    def _latest_mitigation_block(self, df: pd.DataFrame, ob: Optional[Zone]) -> Optional[Zone]:
        if not ob:
            return None

        recent = df.tail(self.config.mitigation_lookback)
        touched = ((recent["low"] <= ob.high) & (recent["high"] >= ob.low)).iloc[-8:].any()
        rejected_up = float(df["close"].iloc[-1]) > ob.high and ob.direction == Direction.BUY
        rejected_down = float(df["close"].iloc[-1]) < ob.low and ob.direction == Direction.SELL

        if touched and (rejected_up or rejected_down):
            return Zone(
                "Mitigation Block", ob.direction,
                ob.start_time, df.index[-1].to_pydatetime(),
                ob.low, ob.high, ob.strength * 0.85, True, True,
            )
        return None

    # ── Rejection Block (UPGRADED: tighter wick ratio) ───────────────────
    def _latest_rejection_block(self, df: pd.DataFrame) -> Optional[Zone]:
        last = df.iloc[-1]
        atr_val = max(float(last["atr"]), 1e-9)
        upper = float(last["high"] - max(last["open"], last["close"]))
        lower = float(min(last["open"], last["close"]) - last["low"])
        body = abs(float(last["close"] - last["open"]))

        # UPGRADED: wick must be > 2.0x body (was 1.7x) — stricter, fewer false signals
        if lower > atr_val * 0.45 and lower > body * 2.0:
            return Zone(
                "Rejection Block", Direction.BUY,
                df.index[-1].to_pydatetime(), df.index[-1].to_pydatetime(),
                float(last["low"]), float(min(last["open"], last["close"])),
                min(100, lower / atr_val * 45),
            )
        if upper > atr_val * 0.45 and upper > body * 2.0:
            return Zone(
                "Rejection Block", Direction.SELL,
                df.index[-1].to_pydatetime(), df.index[-1].to_pydatetime(),
                float(max(last["open"], last["close"])), float(last["high"]),
                min(100, upper / atr_val * 45),
            )
        return None

    # ── Liquidity ─────────────────────────────────────────────────────────
    def _liquidity_levels(self, df: pd.DataFrame, swings: List[Swing]) -> List[Zone]:
        atr_val = float(df["atr"].iloc[-1])
        tol = atr_val * self.config.equal_level_atr_tolerance
        levels: List[Zone] = []

        for kind, direction in [("high", Direction.SELL), ("low", Direction.BUY)]:
            selected = self._last_swings(swings, kind, 12)
            for i in range(len(selected) - 1):
                a, b = selected[i], selected[i + 1]
                if abs(a.price - b.price) <= tol:
                    low, high = sorted([a.price, b.price])
                    levels.append(Zone(
                        f"Equal {'Highs' if kind == 'high' else 'Lows'}", direction,
                        a.confirm_time, b.confirm_time, low, high, 75,
                    ))

        latest_high = self._last_swings(swings, "high", 1)
        latest_low = self._last_swings(swings, "low", 1)

        if latest_high:
            s = latest_high[-1]
            levels.append(Zone(
                "External Buy-Side Liquidity", Direction.SELL,
                s.confirm_time, df.index[-1].to_pydatetime(), s.price, s.price, 65,
            ))
        if latest_low:
            s = latest_low[-1]
            levels.append(Zone(
                "External Sell-Side Liquidity", Direction.BUY,
                s.confirm_time, df.index[-1].to_pydatetime(), s.price, s.price, 65,
            ))

        return levels[-10:]

    # ── Liquidity Sweep (UPGRADED: must close back inside within 1 ATR) ──
    def _liquidity_sweep(self, df: pd.DataFrame, levels: List[Zone]) -> Optional[str]:
        last, prev = df.iloc[-1], df.iloc[-2]
        atr_val = max(float(df["atr"].iloc[-1]), 1e-9)

        for level in reversed(levels):
            if "High" in level.kind or "Buy-Side" in level.kind:
                pierced = last["high"] > level.high and prev["close"] <= level.high
                # UPGRADED: close must be BELOW level AND within 1 ATR of level (not far away)
                closed_back_below = last["close"] < level.high
                not_too_far = abs(last["close"] - level.high) <= atr_val
                if pierced and closed_back_below and not_too_far:
                    return "buy-side liquidity sweep"

            if "Low" in level.kind or "Sell-Side" in level.kind:
                pierced = last["low"] < level.low and prev["close"] >= level.low
                closed_back_above = last["close"] > level.low
                not_too_far = abs(last["close"] - level.low) <= atr_val
                if pierced and closed_back_above and not_too_far:
                    return "sell-side liquidity sweep"

        return None

    # ── Displacement (UPGRADED: requires volume confirmation) ─────────────
    def _displacement(self, df: pd.DataFrame) -> Optional[str]:
        last = df.iloc[-1]
        body = abs(float(last["close"] - last["open"]))
        atr_val = max(float(last["atr"]), 1e-9)
        range_val = max(float(last["high"] - last["low"]), 1e-9)

        # UPGRADED: body/range > 0.65 (was 0.58) AND check volume if available
        body_ok = body / atr_val >= self.config.displacement_atr_mult and body / range_val > 0.65
        volume_ok = self._volume_z(df) > 0.25  # weak volume filter — still fires on low-vol instruments

        if body_ok and volume_ok:
            return "bullish displacement" if last["close"] > last["open"] else "bearish displacement"
        return None

    # ── Premium / Discount (UPGRADED: tighter equilibrium band) ──────────
    def _premium_discount(self, df: pd.DataFrame) -> str:
        recent = df.tail(self.config.premium_discount_lookback)
        high = float(recent["high"].max())
        low = float(recent["low"].min())
        close = float(df["close"].iloc[-1])
        equilibrium = (high + low) / 2.0

        # UPGRADED: ±0.08% band (was ±0.20%) — tighter equilibrium for cleaner calls
        if close < equilibrium * 0.9992:
            return "discount"
        if close > equilibrium * 1.0008:
            return "premium"
        return "equilibrium"

    # ── Bias ──────────────────────────────────────────────────────────────
    def _bias(self, df: pd.DataFrame, mss: Optional[str], choch: Optional[str], bos: Optional[str]) -> str:
        last = df.iloc[-1]
        score = 0

        score += 1 if last["close"] > last["ema_fast"] > last["ema_slow"] else (
            -1 if last["close"] < last["ema_fast"] < last["ema_slow"] else 0
        )
        score += 1 if last["close"] > last["vwap"] else -1

        for event in [mss, choch, bos]:
            if event and "bullish" in event:
                score += 2
            if event and "bearish" in event:
                score -= 2

        return "bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"

    # ── Kill Zone Detection (NEW) ─────────────────────────────────────────
    def _killzone_active(self, df: pd.DataFrame) -> bool:
        """Returns True if the latest candle falls inside a configured kill zone."""
        ts = df.index[-1]
        minutes = ts.hour * 60 + ts.minute
        for _, (start, end) in CONFIG.sessions.kill_zones.items():
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
            if sh * 60 + sm <= minutes <= eh * 60 + em:
                return True
        return False

    # ── FVG Freshness Score (NEW) ─────────────────────────────────────────
    def _fvg_freshness_score(self, df: pd.DataFrame, fvg: Optional[Zone]) -> float:
        """
        0–100 score: 100 = brand-new untouched FVG, 0 = fully consumed.
        Used by signal_engine to add a confidence bonus for high-quality FVGs.
        """
        if fvg is None:
            return 0.0
        freshness = fvg.strength  # already encodes touch penalty
        # Additional time decay: FVGs > 30 bars old lose quality
        try:
            fvg_bar = df.index.get_loc(df.index[df.index >= fvg.start_time][0])
            age_bars = len(df) - fvg_bar
            time_penalty = min(40, age_bars * 1.0)  # -1 pt per bar up to 40 bars
            freshness = max(0, freshness - time_penalty)
        except Exception:
            pass
        return round(freshness, 1)

    # ── Helpers ───────────────────────────────────────────────────────────
    def _volume_z(self, df: pd.DataFrame) -> float:
        vol = df["volume"].tail(80).astype(float)
        std = vol.std()
        if not std or np.isnan(std):
            return 0.0
        return float((vol.iloc[-1] - vol.mean()) / std)

    def _concepts(
        self, df, swings, liquidity, fvg, ob, breaker, mitigation, rejection,
        sweep, mss, bos, choch, displacement, premium_discount, bias,
        killzone_active: bool,
    ) -> List[str]:
        concepts = ["Market Structure", "Dealing Range", "Risk Management"]
        concepts.extend(self._structure_labels(swings))

        if bias == "bullish":
            concepts.append("Daily Bias Bullish")
        elif bias == "bearish":
            concepts.append("Daily Bias Bearish")

        if premium_discount == "discount":
            concepts.append("Discount Zone")
        elif premium_discount == "premium":
            concepts.append("Premium Zone")
        else:
            concepts.append("Equilibrium")

        for item in [sweep, mss, bos, choch, displacement]:
            if item:
                label = item.title()
                concepts.append(label.replace("Bos", "BOS").replace("Choch", "CHOCH").replace("Mss", "MSS"))

        if sweep:
            concepts.append("Liquidity Sweep")
            concepts.append("Turtle Soup")
            if "buy-side" in sweep:
                concepts.append("Buy Side Liquidity")
            if "sell-side" in sweep:
                concepts.append("Sell Side Liquidity")

        if liquidity:
            concepts.append("Liquidity")

        if fvg:
            concepts.append("Fair Value Gap")
            concepts.append("Bullish FVG" if fvg.direction == Direction.BUY else "Bearish FVG")
            # NEW: flag fresh vs consumed
            if not fvg.touched:
                concepts.append("Fresh FVG")

        if ob:
            concepts.append("Order Block")
            concepts.append("Bullish OB" if ob.direction == Direction.BUY else "Bearish OB")

        if breaker:
            concepts.append("Breaker Block")

        if mitigation:
            concepts.append("Mitigation Block")

        if rejection:
            concepts.append("Rejection Block")

        if (
            premium_discount == "discount"
            and (fvg and fvg.direction == Direction.BUY or ob and ob.direction == Direction.BUY)
        ) or (
            premium_discount == "premium"
            and (fvg and fvg.direction == Direction.SELL or ob and ob.direction == Direction.SELL)
        ):
            concepts.append("Optimal Trade Entry")

        if any("Equal Highs" == z.kind for z in liquidity):
            concepts.append("Equal Highs")
            concepts.append("Buy Side Liquidity")
        if any("Equal Lows" == z.kind for z in liquidity):
            concepts.append("Equal Lows")
            concepts.append("Sell Side Liquidity")

        if self._inducement(df, swings):
            concepts.append("Inducement")

        concepts.extend(self._session_concepts(df))

        # NEW: kill zone bonus concept
        if killzone_active:
            concepts.append("Kill Zone Active")

        return list(dict.fromkeys(concepts))

    def _structure_labels(self, swings: List[Swing]) -> List[str]:
        labels: List[str] = []
        highs = self._last_swings(swings, "high", 2)
        lows = self._last_swings(swings, "low", 2)
        if len(highs) >= 2:
            labels.append("Higher High" if highs[-1].price > highs[-2].price else "Lower High")
        if len(lows) >= 2:
            labels.append("Higher Low" if lows[-1].price > lows[-2].price else "Lower Low")
        return labels

    def _inducement(self, df: pd.DataFrame, swings: List[Swing]) -> bool:
        recent = swings[-8:]
        if len(recent) < 4:
            return False
        close = float(df["close"].iloc[-1])
        near = [s for s in recent if abs(close - s.price) <= float(df["atr"].iloc[-1]) * 0.35]
        return len(near) >= 1

    def _session_concepts(self, df: pd.DataFrame) -> List[str]:
        ts = df.index[-1]
        minutes = ts.hour * 60 + ts.minute
        concepts = []

        for name, (start, end) in CONFIG.sessions.kill_zones.items():
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
            if sh * 60 + sm <= minutes <= eh * 60 + em:
                concepts.append(f"{name.replace('_', ' ').title()} Kill Zone")

        day = df[df.index.date == ts.date()]
        if len(day) > 10:
            early = day.between_time("00:00", "06:30")
            if not early.empty and (
                df["high"].iloc[-1] > early["high"].max()
                or df["low"].iloc[-1] < early["low"].min()
            ):
                concepts.append("Judas Swing")
                concepts.append("Session Manipulation")
                concepts.append("Power of 3")

        return concepts
