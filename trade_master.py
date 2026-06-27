from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List

from models import Signal, Trade


@dataclass(frozen=True)
class MasterDecision:
    signal: Signal
    upgrade_pct: float
    confidence_adjustment: float
    rules: List[str]
    reason: str


class TradeMaster:
    """Learns immediately from closed trades and exposes per-agent upgrade state."""

    def __init__(self, max_upgrade_pct: float = 100.0) -> None:
        self.max_upgrade_pct = max_upgrade_pct
        self._agent_stats: Dict[str, Dict[str, Any]] = {}
        self._concept_stats: Dict[str, Dict[str, Any]] = {}
        self._trained_trades = 0

    def train(self, trades: Iterable[Trade]) -> None:
        closed = [trade for trade in trades if trade.closed_at is not None]
        agent_stats: Dict[str, Dict[str, Any]] = defaultdict(self._blank_stats)
        concept_stats: Dict[str, Dict[str, Any]] = defaultdict(self._blank_stats)

        for trade in closed[-500:]:
            pnl = float(trade.pnl)
            agent = self._agent_name(trade.signal)
            self._add(agent_stats[agent], pnl)
            for concept in self._tradeable_concepts(trade.signal.concepts):
                self._add(concept_stats[concept], pnl)

        self._finalize(agent_stats)
        self._finalize(concept_stats)
        self._agent_stats = dict(agent_stats)
        self._concept_stats = dict(concept_stats)
        self._trained_trades = len(closed)

    def apply(self, signal: Signal) -> MasterDecision:
        agent = self._agent_name(signal)
        agent_row = self._agent_stats.get(agent, self._blank_stats())
        concept_rows = [self._concept_stats.get(item, self._blank_stats()) for item in self._tradeable_concepts(signal.concepts)]
        rows = [agent_row] + concept_rows
        upgrade_pct = max(float(row.get("upgrade_pct", 0.0) or 0.0) for row in rows) if rows else 0.0
        losses = sum(int(row.get("losses", 0) or 0) for row in rows)
        wins = sum(int(row.get("wins", 0) or 0) for row in rows)
        adjustment = 0.0
        rules: List[str] = []

        if upgrade_pct >= 70.0:
            rules.append("level 4 recovery: backtest and tighten notes are active, entries remain enabled")
        elif upgrade_pct >= 35.0:
            rules.append("loss recovery: prefer cleaner entries for this concept, entries remain enabled")
        if wins > losses and wins >= 2:
            adjustment += 0.8
            rules.append("promote: recent concept history is positive")
        if not rules:
            rules.append("explore: collect more closed-trade evidence")

        metadata = dict(signal.metadata)
        metadata["trade_master"] = agent
        metadata["master_upgrade_pct"] = round(upgrade_pct, 1)
        metadata["master_upgrade_level"] = self._upgrade_level(upgrade_pct)
        metadata["master_confidence_adjustment"] = round(adjustment, 1)
        metadata["master_rules"] = "; ".join(rules)
        metadata["master_trained_trades"] = self._trained_trades
        adjusted = replace(signal, confidence=round(max(0.0, min(96.0, signal.confidence + adjustment)), 1), metadata=metadata)
        reason = f"{agent} master {metadata['master_upgrade_level']} upgrade {upgrade_pct:.1f}%: {metadata['master_rules']}"
        return MasterDecision(adjusted, round(upgrade_pct, 1), round(adjustment, 1), rules, reason)

    def report(self) -> Dict[str, Any]:
        agents = sorted(self._agent_stats.items(), key=lambda item: float(item[1].get("upgrade_pct", 0.0)), reverse=True)
        concepts = sorted(self._concept_stats.items(), key=lambda item: float(item[1].get("upgrade_pct", 0.0)), reverse=True)
        return {
            "trained_trades": self._trained_trades,
            "agents": dict(agents),
            "concepts": dict(concepts[:20]),
        }

    def _agent_name(self, signal: Signal) -> str:
        return str(signal.metadata.get("strategy_agent") or signal.metadata.get("setup_model") or "Unknown")

    def _tradeable_concepts(self, concepts: Iterable[str]) -> List[str]:
        return [str(item) for item in concepts]

    def _blank_stats(self) -> Dict[str, Any]:
        return {"trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "net_pnl": 0.0, "winrate": 0.0, "upgrade_pct": 0.0}

    def _add(self, row: Dict[str, Any], pnl: float) -> None:
        row["trades"] += 1
        row["wins"] += 1 if pnl > 0 else 0
        row["losses"] += 1 if pnl < 0 else 0
        row["breakeven"] += 1 if pnl == 0 else 0
        row["net_pnl"] = round(float(row["net_pnl"]) + pnl, 2)

    def _finalize(self, stats: Dict[str, Dict[str, Any]]) -> None:
        for row in stats.values():
            trades = int(row["trades"])
            wins = int(row["wins"])
            losses = int(row["losses"])
            breakeven = int(row["breakeven"])
            row["winrate"] = round(wins / trades * 100, 2) if trades else 0.0
            row["upgrade_pct"] = round(max(0.0, min(self.max_upgrade_pct, losses * 18.0 + breakeven * 5.0 - wins * 7.0)), 1)
            row["upgrade_level"] = self._upgrade_level(float(row["upgrade_pct"]))

    def _upgrade_level(self, upgrade_pct: float) -> str:
        if upgrade_pct >= 75:
            return "Level 4 - Backtest And Tighten"
        if upgrade_pct >= 50:
            return "Level 3 - Add Confirmation"
        if upgrade_pct >= 25:
            return "Level 2 - Refine Entry"
        return "Level 1 - Base Setup"
