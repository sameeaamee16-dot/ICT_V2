from __future__ import annotations

from typing import Any, Dict, List

from config import CONFIG


class AIAdvisor:
    """Local rules-based advisor for explaining bot analysis without changing execution."""

    def build(
        self,
        snapshot: Dict[str, Any] | None,
        strategy_status: Dict[str, Any],
        last_signal: Dict[str, Any] | None,
        stats: Dict[str, Any],
        agent_performance: Dict[str, Dict[str, Any]],
        calibration: Dict[str, Any],
        signal_journal: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        cards: List[Dict[str, str]] = []
        actions: List[str] = []
        risk_flags: List[str] = []

        cards.append(self._market_card(snapshot, strategy_status))
        cards.append(self._quality_card(strategy_status, last_signal))
        cards.append(self._protection_card(stats))
        cards.append(self._learning_card(agent_performance, calibration))

        last_rejection = next((row for row in reversed(signal_journal) if row.get("status") == "REJECTED"), None)
        if last_rejection:
            cards.append(
                {
                    "title": "Last Rejection",
                    "state": "Blocked",
                    "tone": "warn",
                    "text": f"{last_rejection.get('agent') or 'Signal'} was rejected: {last_rejection.get('reason') or 'no reason recorded'}",
                }
            )

        if stats.get("win_streak_protection_active"):
            actions.append("Keep streak protection active; only A+ setups should pass now.")
        if not last_signal:
            actions.append("No valid signal yet; wait for all quality gates to align.")
        else:
            meta = last_signal.get("metadata") or {}
            actions.append(str(meta.get("target_winrate_filter") or "Review the latest signal against high-win-rate rules."))
            if meta.get("streak_guard"):
                actions.append(str(meta["streak_guard"]))

        pipeline = strategy_status.get("analysis_progress") or []
        blocked = [row for row in pipeline if row.get("state") == "BLOCK"]
        waiting = [row for row in pipeline if row.get("state") == "WAIT"]
        if blocked:
            risk_flags.extend(f"{row.get('name')}: {row.get('detail')}" for row in blocked[:3])
        elif waiting:
            risk_flags.extend(f"{row.get('name')}: {row.get('detail')}" for row in waiting[:3])
        else:
            risk_flags.append("All visible analysis gates are currently passing.")

        confidence = float(last_signal.get("confidence", 0.0)) if last_signal else 0.0
        summary = self._summary(stats, blocked, waiting, confidence)
        return {
            "summary": summary,
            "mode": "Local AI Advisor",
            "cards": cards,
            "actions": actions[:4],
            "risk_flags": risk_flags[:5],
        }

    def _market_card(self, snapshot: Dict[str, Any] | None, strategy_status: Dict[str, Any]) -> Dict[str, str]:
        snap = snapshot or {}
        progress = strategy_status.get("analysis_progress") or []
        market = next((row for row in progress if row.get("name") == "Market State"), {})
        state = str(market.get("state") or "WAIT")
        tone = "ok" if state == "PASS" else "bad" if state == "BLOCK" else "warn"
        text = str(market.get("detail") or f"Bias {snap.get('bias', '--')}, ADX {snap.get('trend_strength', '--')}")
        return {"title": "Market Read", "state": state, "tone": tone, "text": text}

    def _quality_card(self, strategy_status: Dict[str, Any], last_signal: Dict[str, Any] | None) -> Dict[str, str]:
        progress = strategy_status.get("analysis_progress") or []
        quality = next((row for row in progress if row.get("name") == "Quality Gate"), {})
        if not last_signal:
            return {
                "title": "Quality Gate",
                "state": str(quality.get("state") or "WAIT"),
                "tone": "warn",
                "text": str(quality.get("detail") or "Waiting for a high-confidence setup."),
            }
        meta = last_signal.get("metadata") or {}
        return {
            "title": "Signal Quality",
            "state": last_signal.get("strength", "Signal"),
            "tone": "ok" if float(last_signal.get("confidence", 0.0)) >= CONFIG.risk.high_winrate_min_confidence else "warn",
            "text": (
                f"{last_signal.get('setup_model')} at {last_signal.get('confidence')}% confidence, "
                f"RR {last_signal.get('rr')}. {meta.get('target_winrate_filter', '')}"
            ).strip(),
        }

    def _protection_card(self, stats: Dict[str, Any]) -> Dict[str, str]:
        active = bool(stats.get("win_streak_protection_active"))
        streak = int(stats.get("current_streak") or 0)
        return {
            "title": "Protection",
            "state": "Active" if active else "Standby",
            "tone": "ok" if active else "warn",
            "text": f"Current win streak is {streak}. Protection starts at {CONFIG.risk.protect_win_streak} wins.",
        }

    def _learning_card(self, agent_performance: Dict[str, Dict[str, Any]], calibration: Dict[str, Any]) -> Dict[str, str]:
        weak_agents = [
            name
            for name, row in agent_performance.items()
            if int(row.get("trades") or 0) >= CONFIG.risk.min_agent_trades_for_guard
            and float(row.get("winrate") or 0.0) < CONFIG.risk.target_winrate_pct
        ]
        reliable_buckets = [row for row in calibration.get("buckets", []) if row.get("reliable")]
        weak_buckets = [row.get("bucket") for row in reliable_buckets if float(row.get("winrate") or 0.0) < CONFIG.risk.target_winrate_pct]
        if weak_agents or weak_buckets:
            text = "Under target: " + ", ".join((weak_agents + [f"{bucket}% bucket" for bucket in weak_buckets])[:4])
            return {"title": "Learning Guard", "state": "Watch", "tone": "warn", "text": text}
        return {
            "title": "Learning Guard",
            "state": "Clean",
            "tone": "ok",
            "text": f"{calibration.get('trained_trades', 0)} closed trades learned; no reliable weak bucket detected.",
        }

    def _summary(self, stats: Dict[str, Any], blocked: List[Dict[str, Any]], waiting: List[Dict[str, Any]], confidence: float) -> str:
        if blocked:
            return f"Do not chase. {len(blocked)} gate(s) are blocking the market right now."
        if waiting:
            return f"Patience mode. {len(waiting)} gate(s) still need confirmation before a 90% target setup."
        if confidence >= CONFIG.risk.protect_streak_min_confidence:
            return "A+ conditions are visible. Execution guard will still verify streak, calibration, spread, and agent history."
        if stats.get("win_streak_protection_active"):
            return "Streak protection is active. The bot will reject normal setups and wait for exceptional quality."
        return "Analysis is healthy, but the bot should still wait for the full high-win-rate checklist."
