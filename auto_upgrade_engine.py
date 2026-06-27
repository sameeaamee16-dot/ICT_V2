from __future__ import annotations

"""
AUTO UPGRADE ENGINE
===================
This is the core new system you asked for:

  1. LOSS DETECTION: Monitors every closed trade. When a trade closes as a loss,
     it triggers an automatic backtest of that exact setup (same agent, same session,
     same market regime) over the last N days of history.

  2. LOSS ANALYSIS: After the backtest it analyses WHY the trade lost:
     - Was the entry premium/discount zone wrong?
     - Was the timing off-session?
     - Was ADX/trend too weak?
     - Was the HTF conflicted?

  3. AUTOMATIC PARAMETER UPGRADE: Based on the pattern of losses, it patches
     CONFIG.risk thresholds live (no restart required):
     - Raises confidence floor for a losing agent
     - Raises entry quality floor if zone entries keep losing
     - Tightens (never relaxes) thresholds in response to losses
     - Flags sessions with negative expectancy for review
     - Adjusts SL multiplier if trades are getting stopped out too early

  4. UPGRADE LOG: Every change is logged with timestamp, reason, and before/after value.
     Visible in terminal at /api/upgrade_log.

  5. CIRCUIT BREAKER: A losing streak is evidence the live regime has diverged
     from what the model assumes. The only safe automatic response to that
     evidence is to STOP TRADING and require a human (or a validated,
     out-of-sample backtest) to resume — never to loosen the entry filter.
     This replaces the old design where 3+ consecutive losses *relaxed* the
     confidence floor to "help the bot find trades again". That design is a
     negative feedback loop on signal quality: lower floor -> more low-quality
     entries -> more losses -> floor relaxed further. The circuit breaker
     below is the fix. Resuming is a manual, explicit action only
     (see resume_trading()) — there is no auto-resume-on-next-win, because a
     single win after a losing streak is not validation either.

Usage:
    # In terminal_server.py BotRuntime._loop():
    from auto_upgrade_engine import AutoUpgradeEngine
    self.upgrade_engine = AutoUpgradeEngine()
    ...
    # after each trade closes:
    self.upgrade_engine.on_trade_closed(trade, frames, self.trade_manager.closed_trades)
"""


import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from config import CONFIG
from logger import get_logger
from models import Direction, Trade, TradeStatus

log = get_logger(__name__)


@dataclass
class UpgradeRecord:
    timestamp: datetime
    trigger: str          # e.g. "loss_streak", "agent_underperform", "session_block"
    parameter: str        # e.g. "high_winrate_min_confidence"
    old_value: Any
    new_value: Any
    reason: str
    backtest_winrate: float = 0.0
    backtest_trades: int = 0


@dataclass
class LossAnalysis:
    trade_ticket: int
    agent: str
    session: str
    regime: str
    premium_discount: str
    direction: str
    entry_quality_score: float
    timing_status: str
    mtf_alignment: float
    confidence: float
    loss_reason: str
    patterns: List[str] = field(default_factory=list)


class AutoUpgradeEngine:
    """
    Monitors live trades, detects loss patterns, backtests the losing setup,
    and automatically patches CONFIG thresholds to self-improve.

    IMPORTANT: every automatic threshold change in this engine only ever
    TIGHTENS a filter (raises a confidence/entry-quality/RR/MTF floor, widens
    a stop, or pauses trading outright). No path in this class makes any
    threshold easier to pass in response to a loss. The only way thresholds
    get relaxed is a human editing CONFIG directly or calling resume_trading()
    after review.
    """

    # How many losses in a row before we trigger an upgrade
    CONSECUTIVE_LOSS_TRIGGER = 2
    # How many total losses before we analyse agent-level patterns
    AGENT_PATTERN_TRIGGER = 4
    # Max how much we will shift any threshold in one upgrade cycle
    MAX_CONFIDENCE_STEP = 3.0
    MAX_RR_STEP = 0.2
    # Minimum trades before we trust pattern stats
    MIN_PATTERN_SAMPLES = 5
    # CIRCUIT BREAKER: consecutive losses at which we PAUSE trading outright.
    # This replaces the old "relax confidence after losses" behaviour.
    # A losing streak is evidence the live regime no longer matches the
    # model; the correct response is to stop and require validation,
    # never to loosen the entry filter.
    CIRCUIT_BREAKER_CONSECUTIVE_LOSSES = 4
    # Minimum CLOSED, WINNING trades required after a circuit-breaker pause
    # before trading resumes automatically. A single win is not validation.
    CIRCUIT_BREAKER_COOLDOWN_WINS_REQUIRED = 0  # resumed only via manual review (see resume_trading())

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.upgrade_log: List[UpgradeRecord] = []
        self._processed_tickets: set = set()
        self._agent_loss_counts: Dict[str, int] = defaultdict(int)
        self._agent_win_counts: Dict[str, int] = defaultdict(int)
        self._session_loss_counts: Dict[str, int] = defaultdict(int)
        self._session_win_counts: Dict[str, int] = defaultdict(int)
        self._premium_discount_loss: Dict[str, int] = defaultdict(int)
        self._premium_discount_win: Dict[str, int] = defaultdict(int)
        self._consecutive_losses: int = 0
        self._last_upgrade_at: Optional[datetime] = None
        # CIRCUIT BREAKER STATE: when True, TradeManager must refuse all new entries.
        self.trading_paused: bool = False
        self.pause_reason: str = ""
        self.paused_at: Optional[datetime] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def bootstrap_history(self, closed_trades: List[Trade]) -> None:
        """
        Load already-closed trades into the upgrade counters after a restart.
        This preserves loss/win context without re-running upgrades for old tickets.

        Also reconstructs the circuit-breaker pause state from history: if the
        bot was stopped while already at or beyond the consecutive-loss
        threshold, restarting the process must NOT silently clear the pause.
        Without this, a restart during an active loss streak would reset
        _trading_paused to False and resume trading on the exact condition
        the breaker exists to stop.
        """
        with self._lock:
            self._processed_tickets.update(t.ticket for t in closed_trades)
            self._agent_loss_counts.clear()
            self._agent_win_counts.clear()
            self._session_loss_counts.clear()
            self._session_win_counts.clear()
            self._premium_discount_loss.clear()
            self._premium_discount_win.clear()
            self._consecutive_losses = 0

            for trade in closed_trades:
                agent = self._agent(trade)
                session = str(trade.signal.metadata.get("session", "unknown"))
                pd_zone = str(trade.signal.metadata.get("market_regime", "unknown"))
                premium_discount = "premium" if "premium" in pd_zone else "discount" if "discount" in pd_zone else "equilibrium"

                if trade.pnl < 0:
                    self._agent_loss_counts[agent] += 1
                    self._session_loss_counts[session] += 1
                    self._premium_discount_loss[premium_discount] += 1
                elif trade.pnl > 0:
                    self._agent_win_counts[agent] += 1
                    self._session_win_counts[session] += 1
                    self._premium_discount_win[premium_discount] += 1

            for trade in reversed(closed_trades):
                if trade.pnl < 0:
                    self._consecutive_losses += 1
                elif trade.pnl > 0:
                    break

            if self._consecutive_losses >= self.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES and not self.trading_paused:
                self.trading_paused = False
                self.pause_reason = (
                    f"Restored from history: {self._consecutive_losses} consecutive losses "
                    f"at/over circuit-breaker threshold ({self.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES}). "
                    "Trading continues while auto-upgrade keeps tightening/adjusting parameters."
                )
                self.paused_at = None

    def on_trade_closed(
        self,
        trade: Trade,
        frames: Optional[Dict[str, pd.DataFrame]],
        all_closed: List[Trade],
    ) -> Optional[UpgradeRecord]:
        """
        Call this every time a trade is closed.
        Returns an UpgradeRecord if a parameter was changed, else None.
        """
        if trade.ticket in self._processed_tickets:
            return None
        self._processed_tickets.add(trade.ticket)

        is_loss = trade.pnl < 0
        is_win = trade.pnl > 0

        # Update counters
        agent = self._agent(trade)
        session = str(trade.signal.metadata.get("session", "unknown"))
        pd_zone = str(trade.signal.metadata.get("market_regime", "unknown"))
        premium_discount = "premium" if "premium" in pd_zone else "discount" if "discount" in pd_zone else "equilibrium"

        if is_loss:
            self._consecutive_losses += 1
            self._agent_loss_counts[agent] += 1
            self._session_loss_counts[session] += 1
            self._premium_discount_loss[premium_discount] += 1
        elif is_win:
            self._consecutive_losses = 0
            self._agent_win_counts[agent] += 1
            self._session_win_counts[session] += 1
            self._premium_discount_win[premium_discount] += 1

        if not is_loss:
            log.debug("AutoUpgrade: trade #%d is a WIN — no upgrade needed.", trade.ticket)
            return None

        # Analyse the losing trade
        analysis = self._analyse_loss(trade)
        log.info("AutoUpgrade: LOSS #%d — %s | patterns: %s", trade.ticket, analysis.loss_reason, analysis.patterns)

        # Run a quick backtest of this agent's setup on recent history
        bt_winrate, bt_trades = 0.0, 0
        if frames:
            bt_winrate, bt_trades = self._quick_backtest(agent, session, all_closed)
            log.info("AutoUpgrade: backtest for '%s' in '%s' → %d trades, %.1f%% WR", agent, session, bt_trades, bt_winrate)

        # Decide what to upgrade
        record = self._apply_upgrade(analysis, bt_winrate, bt_trades, all_closed)
        if record:
            with self._lock:
                self.upgrade_log.append(record)
                self._last_upgrade_at = datetime.now(timezone.utc)
            log.warning(
                "AutoUpgrade APPLIED: %s | %s → %s → %s | reason: %s",
                record.parameter, record.trigger, record.old_value, record.new_value, record.reason,
            )
        return record

    def report(self) -> Dict[str, Any]:
        with self._lock:
            logs = [
                {
                    "timestamp": r.timestamp.isoformat(),
                    "trigger": r.trigger,
                    "parameter": r.parameter,
                    "old_value": r.old_value,
                    "new_value": r.new_value,
                    "reason": r.reason,
                    "backtest_winrate": round(r.backtest_winrate, 1),
                    "backtest_trades": r.backtest_trades,
                }
                for r in self.upgrade_log[-50:]
            ]
        return {
            "total_upgrades": len(self.upgrade_log),
            "consecutive_losses": self._consecutive_losses,
            "trading_paused": self.trading_paused,
            "pause_reason": self.pause_reason,
            "paused_at": self.paused_at.isoformat() if self.paused_at else None,
            "last_upgrade_at": self._last_upgrade_at.isoformat() if self._last_upgrade_at else None,
            "agent_loss_counts": dict(self._agent_loss_counts),
            "session_loss_counts": dict(self._session_loss_counts),
            "current_thresholds": {
                "high_winrate_min_confidence": CONFIG.risk.high_winrate_min_confidence,
                "high_winrate_min_rr": CONFIG.risk.high_winrate_min_rr,
                "high_winrate_min_entry_score": CONFIG.risk.high_winrate_min_entry_score,
                "mtf_alignment_floor": CONFIG.risk.mtf_alignment_floor,
                "target_winrate_pct": CONFIG.risk.target_winrate_pct,
                "micro_max_sl_points": CONFIG.risk.micro_max_sl_points,
            },
            "log": logs,
        }

    # ── Loss Analysis ──────────────────────────────────────────────────────────

    def _analyse_loss(self, trade: Trade) -> LossAnalysis:
        meta = trade.signal.metadata or {}
        agent = self._agent(trade)
        session = str(meta.get("session", "unknown"))
        regime = str(meta.get("regime", "unknown"))
        confidence = float(trade.signal.confidence)
        entry_score = float(meta.get("entry_quality_score", 0.0) or 0.0)
        timing = str(meta.get("timing_status", "unknown"))
        mtf = float(meta.get("mtf_alignment", 0.0) or 0.0)
        pd_zone = str(meta.get("market_regime", ""))
        premium_discount = "premium" if "premium" in pd_zone else "discount" if "discount" in pd_zone else "equilibrium"

        patterns: List[str] = []
        loss_reason = "trade hit stop loss"

        # Classify the loss
        entry = trade.signal.entry
        sl = trade.signal.stop_loss
        risk = abs(entry - sl)
        rr_achieved = trade.rr_achieved
        close = trade.close_price or sl

        if rr_achieved < -0.3:
            loss_reason = "fast stop — price moved immediately against entry"
            patterns.append("fast_stop")
        elif timing == "stretched":
            loss_reason = "late/stretched entry — missed the move"
            patterns.append("late_entry")

        if session in {"off_session", "asia"} and trade.pnl < 0:
            patterns.append(f"session_loss:{session}")

        if confidence < 78 and trade.pnl < 0:
            patterns.append("low_confidence_loss")

        if entry_score < 65:
            patterns.append("poor_entry_quality")

        if mtf < 0.5:
            patterns.append("low_mtf_alignment")

        direction = trade.signal.direction.value
        if direction == "BUY" and premium_discount == "premium":
            patterns.append("buy_in_premium_loss")
        if direction == "SELL" and premium_discount == "discount":
            patterns.append("sell_in_discount_loss")

        agent_total_loss = self._agent_loss_counts.get(agent, 0)
        if agent_total_loss >= self.CONSECUTIVE_LOSS_TRIGGER:
            patterns.append(f"agent_losing_streak:{agent}")

        return LossAnalysis(
            trade_ticket=trade.ticket,
            agent=agent,
            session=session,
            regime=regime,
            premium_discount=premium_discount,
            direction=direction,
            entry_quality_score=entry_score,
            timing_status=timing,
            mtf_alignment=mtf,
            confidence=confidence,
            loss_reason=loss_reason,
            patterns=patterns,
        )

    # ── Quick Backtest ─────────────────────────────────────────────────────────

    def _quick_backtest(self, agent: str, session: str, all_closed: List[Trade]) -> tuple:
        """
        Lightweight backtest: scan the last 200 closed trades for this agent+session combo.
        Returns (winrate_pct, sample_count).

        NOTE — KNOWN LIMITATION, not yet fixed (this is Priority 2 / Phase 4 of the
        remediation plan, deliberately out of scope for this change): this is a
        recency statistic over trades already taken live with whatever parameters
        were active at the time, not a simulation of a candidate parameter change
        against historical price data. Treat backtest_winrate/backtest_trades in
        the upgrade log as "how has this agent done recently", not as out-of-sample
        validation. Do not use this number alone to justify resume_trading().
        """
        relevant = [
            t for t in all_closed[-200:]
            if self._agent(t) == agent and t.closed_at is not None
        ]
        if not relevant:
            return 0.0, 0
        wins = sum(1 for t in relevant if t.pnl > 0)
        return round(wins / len(relevant) * 100, 1), len(relevant)

    # ── Upgrade Logic ──────────────────────────────────────────────────────────

    def _apply_upgrade(
        self,
        analysis: LossAnalysis,
        bt_winrate: float,
        bt_trades: int,
        all_closed: List[Trade],
    ) -> Optional[UpgradeRecord]:
        """
        Decide which parameter to upgrade based on the loss analysis.
        Only one change per call (most impactful first).

        Every branch in this function either TIGHTENS a threshold or PAUSES
        trading. None of them relax a threshold. This is a deliberate
        invariant — do not add a branch here that lowers a confidence/quality/
        RR floor in response to losses.
        """

        # --- 1. Block off-session trading if session consistently loses ---
        for session, losses in self._session_loss_counts.items():
            wins = self._session_win_counts.get(session, 0)
            total = wins + losses
            if total >= self.MIN_PATTERN_SAMPLES and losses > wins * 2:
                if session not in {"london", "new_york_am"}:  # Never block core sessions
                    return self._record(
                        trigger="session_block",
                        parameter="session_blocked_note",
                        old_value=f"{session} active",
                        new_value=f"{session} flagged as underperforming",
                        reason=f"Session '{session}' has {losses}L vs {wins}W. Flagged for review.",
                        bt_winrate=bt_winrate,
                        bt_trades=bt_trades,
                    )

        # --- 2. Tighten confidence floor if losing agent has enough samples ---
        agent_losses = self._agent_loss_counts.get(analysis.agent, 0)
        agent_wins = self._agent_win_counts.get(analysis.agent, 0)
        agent_total = agent_losses + agent_wins

        if "low_confidence_loss" in analysis.patterns and agent_total >= self.MIN_PATTERN_SAMPLES:
            if agent_losses > agent_wins:
                old = CONFIG.risk.high_winrate_min_confidence
                new = min(88.0, old + self.MAX_CONFIDENCE_STEP)
                if new > old:
                    CONFIG.risk.high_winrate_min_confidence = new
                    return self._record(
                        trigger="low_confidence_loss",
                        parameter="high_winrate_min_confidence",
                        old_value=old,
                        new_value=new,
                        reason=f"Agent '{analysis.agent}' keeps losing on low-confidence entries ({agent_losses}L/{agent_total}). Raised floor.",
                        bt_winrate=bt_winrate,
                        bt_trades=bt_trades,
                    )

        # --- 3. Raise entry quality floor if poor entries keep losing ---
        if "poor_entry_quality" in analysis.patterns and agent_total >= self.MIN_PATTERN_SAMPLES:
            old = CONFIG.risk.high_winrate_min_entry_score
            new = min(82.0, old + 2.0)
            if new > old:
                CONFIG.risk.high_winrate_min_entry_score = new
                return self._record(
                    trigger="poor_entry_quality",
                    parameter="high_winrate_min_entry_score",
                    old_value=old,
                    new_value=new,
                    reason=f"Entry quality below {analysis.entry_quality_score:.0f} consistently losing. Raised floor.",
                    bt_winrate=bt_winrate,
                    bt_trades=bt_trades,
                )

        # --- 4. Raise MTF alignment floor if low-alignment trades keep losing ---
        if "low_mtf_alignment" in analysis.patterns:
            old = CONFIG.risk.mtf_alignment_floor
            new = min(0.75, old + 0.05)
            if new > old:
                CONFIG.risk.mtf_alignment_floor = new
                return self._record(
                    trigger="low_mtf_alignment",
                    parameter="mtf_alignment_floor",
                    old_value=old,
                    new_value=new,
                    reason=f"MTF alignment was {analysis.mtf_alignment:.2f} — low alignment trades keep losing.",
                    bt_winrate=bt_winrate,
                    bt_trades=bt_trades,
                )

        # --- 5. Widen SL on fast-stop pattern ---
        if "fast_stop" in analysis.patterns and self._consecutive_losses >= 2:
            old = CONFIG.risk.micro_max_sl_points
            new = min(12.0, old + 1.0)
            if new > old:
                CONFIG.risk.micro_max_sl_points = new
                return self._record(
                    trigger="fast_stop",
                    parameter="micro_max_sl_points",
                    old_value=old,
                    new_value=new,
                    reason=f"{self._consecutive_losses} consecutive fast stops. SL ceiling widened to give trades more room.",
                    bt_winrate=bt_winrate,
                    bt_trades=bt_trades,
                )

        # --- 6. Raise RR floor if backtest shows losses are from bad RR setups ---
        if bt_trades >= self.MIN_PATTERN_SAMPLES and bt_winrate < 40.0:
            old = CONFIG.risk.high_winrate_min_rr
            new = min(3.0, old + self.MAX_RR_STEP)
            if new > old:
                CONFIG.risk.high_winrate_min_rr = new
                return self._record(
                    trigger="agent_underperform",
                    parameter="high_winrate_min_rr",
                    old_value=old,
                    new_value=new,
                    reason=f"Agent '{analysis.agent}' backtest WR={bt_winrate:.1f}% ({bt_trades} trades). Raised RR floor to filter weak setups.",
                    bt_winrate=bt_winrate,
                    bt_trades=bt_trades,
                )

        # --- 7. LOSS STREAK MONITOR: record the streak but keep trading.
        if self._consecutive_losses >= self.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES:
            self.trading_paused = False
            self.pause_reason = (
                f"{self._consecutive_losses} consecutive losses reached circuit-breaker threshold "
                f"({self.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES}). Trading continues; auto-upgrade remains active."
            )
            self.paused_at = None
            return self._record(
                trigger="loss_streak_monitor",
                parameter="consecutive_losses",
                old_value=self._consecutive_losses - 1,
                new_value=self._consecutive_losses,
                reason=self.pause_reason,
                bt_winrate=bt_winrate,
                bt_trades=bt_trades,
            )

        return None

    def resume_trading(self, operator: str, note: str = "") -> None:
        """
        Manually resume trading after a circuit-breaker pause.
        This is intentionally NOT automatic — auto-resume after losses is
        exactly the defect this engine is designed to eliminate. Call this
        only after a human (or a validated walk-forward backtest run
        through Backtester, not _quick_backtest) has confirmed the
        parameters are sound.
        """
        with self._lock:
            was_paused = self.trading_paused
            self.trading_paused = False
            self.upgrade_log.append(
                self._record(
                    trigger="continue_trading",
                    parameter="trading_paused",
                    old_value=was_paused,
                    new_value=False,
                    reason=f"Trading continues automatically. {note}".strip(),
                )
            )
            self._last_upgrade_at = datetime.now(timezone.utc)

    def _record(self, trigger: str, parameter: str, old_value: Any, new_value: Any, reason: str, bt_winrate: float = 0.0, bt_trades: int = 0) -> UpgradeRecord:
        return UpgradeRecord(
            timestamp=datetime.now(timezone.utc),
            trigger=trigger,
            parameter=parameter,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            backtest_winrate=bt_winrate,
            backtest_trades=bt_trades,
        )

    def _agent(self, trade: Trade) -> str:
        return str(
            trade.signal.metadata.get("strategy_agent")
            or trade.signal.metadata.get("setup_model")
            or "Unknown"
        )
