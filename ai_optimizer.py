from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable

from models import Trade


@dataclass
class AdaptiveConfluenceModel:
    weights: Dict[str, float] = field(default_factory=lambda: defaultdict(lambda: 1.0))
    regime_scores: Dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def update_from_trades(self, trades: Iterable[Trade]) -> None:
        counts = defaultdict(int)
        pnl_by_concept = defaultdict(float)
        regime_counts = defaultdict(int)
        regime_pnl = defaultdict(float)
        for trade in trades:
            result = 1.0 if trade.pnl > 0 else -1.0
            for concept in trade.signal.concepts:
                counts[concept] += 1
                pnl_by_concept[concept] += result
            regime = str(trade.signal.metadata.get("regime", "unknown"))
            regime_counts[regime] += 1
            regime_pnl[regime] += result
        for concept, count in counts.items():
            edge = pnl_by_concept[concept] / max(count, 1)
            self.weights[concept] = max(0.55, min(1.55, 1.0 + edge * 0.25))
        for regime, count in regime_counts.items():
            self.regime_scores[regime] = regime_pnl[regime] / max(count, 1)

    def adjusted_confidence(self, base: float, concepts: Iterable[str], regime: str) -> float:
        multiplier = 1.0
        used = list(concepts)
        if used:
            multiplier = sum(self.weights[c] for c in used) / len(used)
        regime_adj = self.regime_scores.get(regime, 0.0) * 3.0
        return max(0.0, min(100.0, base * multiplier + regime_adj))

