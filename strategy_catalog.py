from __future__ import annotations

from typing import Dict, Iterable, List

import pandas as pd

from config import CONFIG
from models import Direction, IctSnapshot
from smc_engine import SmcContext
from trend_engine import TrendContext


class StrategyCatalog:
    def evaluate(
        self,
        frames: Dict[str, pd.DataFrame],
        snapshots: Dict[str, IctSnapshot],
        smc: SmcContext,
        trend: TrendContext,
    ) -> Dict[str, object]:
        primary_tf = CONFIG.timeframes.primary
        if primary_tf not in frames or primary_tf not in snapshots:
            return {"active": [], "groups": []}
        df = frames[primary_tf].tail(720).copy()
        snap = snapshots[primary_tf]
        active = set(snap.concepts + smc.confirmations)
        active.update(self._ict_extensions(df, snap))

        groups = [
            self._group(
                "ICT Concepts",
                [
                    "Market Structure",
                    "Higher High",
                    "Higher Low",
                    "Lower High",
                    "Lower Low",
                    "Break of Structure",
                    "Change of Character",
                    "Liquidity Sweep",
                    "Buy Side Liquidity",
                    "Sell Side Liquidity",
                    "Fair Value Gap",
                    "Bullish FVG",
                    "Bearish FVG",
                    "Order Block",
                    "Bullish OB",
                    "Bearish OB",
                    "Mitigation Block",
                    "Premium & Discount Zones",
                    "Optimal Trade Entry",
                    "Kill Zones",
                    "Judas Swing",
                    "SMT Divergence",
                    "Dealing Range",
                    "Turtle Soup",
                    "Power of 3",
                    "Daily Bias",
                    "Multi Time Frame Analysis",
                    "Risk Management",
                ],
                active,
                "ict-only",
            ),
        ]
        return {
            "active": sorted(active),
            "groups": groups,
        }

    def _group(self, name: str, items: Iterable[str], active: set[str], mode: str) -> Dict[str, object]:
        concepts = [{"name": item, "active": self._is_active(item, active)} for item in items]
        score = round(sum(1 for item in concepts if item["active"]) / max(len(concepts), 1) * 100, 1)
        return {"name": name, "mode": mode, "score": score, "concepts": concepts}

    def _is_active(self, item: str, active: set[str]) -> bool:
        aliases = {
            "BOS": ["Bullish Bos", "Bearish Bos"],
            "Break of Structure": ["Bullish BOS", "Bearish BOS"],
            "CHOCH": ["Bullish Choch", "Bearish Choch", "MSS/CHOCH"],
            "Change of Character": ["Bullish CHOCH", "Bearish CHOCH", "MSS/CHOCH"],
            "Order Blocks": ["Buy Order Block", "Sell Order Block", "Order Block"],
            "Order Block": ["Order Block", "Bullish OB", "Bearish OB"],
            "Bullish OB": ["Bullish OB", "Buy Order Block"],
            "Bearish OB": ["Bearish OB", "Sell Order Block"],
            "Liquidity Grab": ["Liquidity Sweep", "Buy-Side Liquidity Sweep", "Sell-Side Liquidity Sweep", "Liquidity Raid"],
            "Fair Value Gap": ["Buy Fair Value Gap", "Sell Fair Value Gap", "Fair Value Gap"],
            "Bullish FVG": ["Bullish FVG", "Buy Fair Value Gap"],
            "Bearish FVG": ["Bearish FVG", "Sell Fair Value Gap"],
            "Kill Zones": ["London Kill Zone", "New York Am Kill Zone", "New York Pm Kill Zone", "Asia Kill Zone"],
            "Premium & Discount Zones": ["Premium", "Discount", "Equilibrium", "Premium Zone", "Discount Zone"],
            "Daily Bias": ["Daily Bias Bullish", "Daily Bias Bearish"],
            "Multi Time Frame Analysis": ["Multi Timeframe Bias"],
            "Market Structure": ["Market Structure", "Bullish BOS", "Bearish BOS", "Bullish CHOCH", "Bearish CHOCH", "MSS/CHOCH"],
            "Risk Management": ["Risk Management"],
        }
        candidates = aliases.get(item, [item])
        return any(candidate in active for candidate in candidates)

    def _ict_extensions(self, df: pd.DataFrame, snap: IctSnapshot) -> List[str]:
        out = ["ICT Concepts"]
        if snap.sweep:
            out.append("Liquidity Raid")
        if snap.premium_discount:
            out.append(snap.premium_discount.title())
        if snap.premium_discount in {"discount", "premium"} and (snap.fvg or snap.order_block):
            out.append("Optimal Trade Entry")
            out.append("Sniper Entry")
            out.append("Precision Entry")
            out.append("Sniper Zone")
        day = df[df.index.date == df.index[-1].date()]
        if len(day) > 20:
            first = day.iloc[: max(5, min(30, len(day) // 4))]
            if df["close"].iloc[-1] > first["high"].max() or df["close"].iloc[-1] < first["low"].min():
                out.append("Power of 3")
        return out
