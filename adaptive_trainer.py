from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from models import Signal, Trade


@dataclass(frozen=True)
class AdaptiveDecision:
    allowed: bool
    reason: str
    confidence_adjustment: float = 0.0
    matched_rules: List[str] = field(default_factory=list)


class AdaptiveAgentTrainer:
    def __init__(
        self,
        min_samples: int = 20,
        block_winrate_pct: float = 34.0,
        block_net_pnl: float = -6.0,
        caution_winrate_pct: float = 45.0,
        boost_winrate_pct: float = 60.0,
    ) -> None:
        # FIX: min_samples raised 3 -> 20. A 3-trade sample cannot reliably
        # distinguish a genuine negative-expectancy pattern from noise — the
        # 95% CI on a binomial proportion from n=3 spans roughly the entire
        # [0,1] range. 20 samples gives a CI narrow enough (~±20pp at 50%
        # winrate) for a block/boost decision to mean something. This also
        # makes the trainer's own evaluate() warm-up gate (which compares
        # self._trained_trades against self.min_samples) consistent with
        # the per-condition bucket minimum used in train()/evaluate() below.
        self.min_samples = min_samples
        self.block_winrate_pct = block_winrate_pct
        self.block_net_pnl = block_net_pnl
        self.caution_winrate_pct = caution_winrate_pct
        self.boost_winrate_pct = boost_winrate_pct
        self._condition_stats: Dict[str, Dict[str, Any]] = {}
        self._agent_stats: Dict[str, Dict[str, Any]] = {}
        self._insights: List[Dict[str, Any]] = []
        self._trained_trades = 0

    def train(self, trades: Iterable[Trade]) -> None:
        closed = [trade for trade in trades if trade.closed_at is not None]
        condition_stats: Dict[str, Dict[str, Any]] = defaultdict(self._blank_stats)
        agent_stats: Dict[str, Dict[str, Any]] = defaultdict(self._blank_stats)

        for trade in closed[-500:]:
            pnl = float(trade.pnl)
            agent = self._agent_name(trade.signal)
            self._add(agent_stats[agent], pnl)
            for key, label in self._condition_keys(trade.signal):
                row = condition_stats[key]
                row["label"] = label
                row["agent"] = agent
                self._add(row, pnl)

        self._finalize(agent_stats)
        self._finalize(condition_stats)
        self._agent_stats = dict(agent_stats)
        self._condition_stats = dict(condition_stats)
        self._trained_trades = len(closed)
        self._insights = self._build_insights()

    def evaluate(self, signal: Signal) -> AdaptiveDecision:
        if self._trained_trades < self.min_samples:
            return AdaptiveDecision(True, "Adaptive trainer warming up")

        hard_blocks: List[str] = []
        cautions: List[str] = []
        boosts: List[str] = []
        adjustment = 0.0

        agent = self._agent_name(signal)
        agent_row = self._agent_stats.get(agent)
        if agent_row and int(agent_row["trades"]) >= self.min_samples:
            if float(agent_row["net_pnl"]) <= self.block_net_pnl and float(agent_row["winrate"]) < self.caution_winrate_pct:
                hard_blocks.append(f"{agent} overall weak: {agent_row['winrate']:.1f}% WR, PnL {agent_row['net_pnl']:.2f}")
            elif float(agent_row["winrate"]) < self.caution_winrate_pct and float(agent_row["net_pnl"]) < 0:
                adjustment -= 3.0
                cautions.append(f"{agent} has negative recent expectancy")
            elif float(agent_row["winrate"]) >= self.boost_winrate_pct and float(agent_row["net_pnl"]) > 0:
                adjustment += 1.5
                boosts.append(f"{agent} is performing well")

        for key, label in self._condition_keys(signal):
            row = self._condition_stats.get(key)
            if not row or int(row["trades"]) < self.min_samples:
                continue
            winrate = float(row["winrate"])
            net_pnl = float(row["net_pnl"])
            if net_pnl <= self.block_net_pnl and winrate <= self.block_winrate_pct:
                hard_blocks.append(f"{label}: {winrate:.1f}% WR, PnL {net_pnl:.2f}")
            elif winrate < self.caution_winrate_pct and net_pnl < 0:
                adjustment -= 2.0
                cautions.append(f"{label} is underperforming")
            elif winrate >= self.boost_winrate_pct and net_pnl > 0:
                adjustment += 1.0
                boosts.append(f"{label} is favorable")

        if hard_blocks:
            return AdaptiveDecision(False, "Adaptive trainer blocked repeated loss pattern: " + "; ".join(hard_blocks[:2]), adjustment, hard_blocks)

        notes = cautions + boosts
        if signal.confidence + adjustment < float(signal.metadata.get("profile_min_confidence", 70.0)):
            return AdaptiveDecision(False, f"Adaptive trainer rejected after confidence penalty ({adjustment:.1f})", adjustment, notes)
        if notes:
            return AdaptiveDecision(True, "Adaptive trainer adjusted signal: " + "; ".join(notes[:3]), adjustment, notes)
        return AdaptiveDecision(True, "Adaptive trainer found no negative pattern", adjustment, [])

    def report(self) -> Dict[str, Any]:
        return {
            "trained_trades": self._trained_trades,
            "min_samples": self.min_samples,
            "insights": self._insights,
            "agents": self._agent_stats,
        }

    def _build_insights(self) -> List[Dict[str, Any]]:
        rows = [
            {
                "label": str(row.get("label", key)),
                "agent": str(row.get("agent", "")),
                "trades": int(row["trades"]),
                "winrate": float(row["winrate"]),
                "net_pnl": float(row["net_pnl"]),
                "action": self._action_for(row),
            }
            for key, row in self._condition_stats.items()
            if int(row["trades"]) >= self.min_samples
        ]
        rows.sort(key=lambda item: (item["action"] != "block", item["net_pnl"]))
        return rows[:12]

    def _action_for(self, row: Dict[str, Any]) -> str:
        if float(row["net_pnl"]) <= self.block_net_pnl and float(row["winrate"]) <= self.block_winrate_pct:
            return "block"
        if float(row["winrate"]) < self.caution_winrate_pct and float(row["net_pnl"]) < 0:
            return "penalize"
        if float(row["winrate"]) >= self.boost_winrate_pct and float(row["net_pnl"]) > 0:
            return "boost"
        return "watch"

    def _condition_keys(self, signal: Signal) -> List[tuple[str, str]]:
        meta = signal.metadata
        agent = self._agent_name(signal)
        setup = str(meta.get("setup_model") or agent)
        regime = str(meta.get("market_regime") or meta.get("regime") or "unknown")
        session = str(meta.get("session") or "unknown")
        timing = str(meta.get("timing_status") or "unknown")
        direction = signal.direction.value
        return [
            (f"agent={agent}|regime={regime}", f"{agent} in {regime} regime"),
            (f"agent={agent}|session={session}", f"{agent} during {session}"),
            (f"agent={agent}|timing={timing}", f"{agent} with {timing} timing"),
            (f"agent={agent}|direction={direction}", f"{agent} {direction} trades"),
            (f"setup={setup}|regime={regime}", f"{setup} in {regime} regime"),
        ]

    def _agent_name(self, signal: Signal) -> str:
        return str(signal.metadata.get("strategy_agent") or signal.metadata.get("setup_model") or "Unknown")

    def _blank_stats(self) -> Dict[str, Any]:
        return {"trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "net_pnl": 0.0, "winrate": 0.0}

    def _add(self, row: Dict[str, Any], pnl: float) -> None:
        row["trades"] += 1
        row["wins"] += 1 if pnl > 0 else 0
        row["losses"] += 1 if pnl < 0 else 0
        row["breakeven"] += 1 if pnl == 0 else 0
        row["net_pnl"] = round(float(row["net_pnl"]) + pnl, 2)

    def _finalize(self, stats: Dict[str, Dict[str, Any]]) -> None:
        for row in stats.values():
            trades = int(row["trades"])
            row["winrate"] = round(int(row["wins"]) / trades * 100, 2) if trades else 0.0
