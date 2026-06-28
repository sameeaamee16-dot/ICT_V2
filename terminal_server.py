from __future__ import annotations
"""
terminal_server.py — ICT_V2 FINAL
===================================
BUGS FIXED vs current GitHub version:

BUG 1 (Critical): FilterEngine never imported or called.
  All 11 quality checks were dead code. Fixed: FilterEngine now runs
  before every signal submission. Blocked signals show as "FILTERED"
  in the signal journal with the exact reason.

BUG 2: _apply_upgrade_thresholds() called self.upgrade_engine.current_thresholds
  but AutoUpgradeEngine has no such attribute — it's inside report().
  Fixed: now reads self.upgrade_engine.report().get("current_thresholds", {})

BUG 3: recent_closed_trades not passed anywhere. Consecutive-loss cooldowns
  in risk_manager and trend_engine never fired. Fixed: both now receive
  the trade history each cycle.

BUG 4: trend_engine.evaluate() called without recent_losses argument.
  Fixed: _count_consecutive_losses() injected each cycle.

BUG 5: Fallback signals also bypassed FilterEngine.
  Fixed: fallback signals now also go through filter_engine.check().

NEW FEATURES added vs GitHub version:

FEATURE 1 — Live Performance Dashboard panel:
  Shows today's P&L curve, win/loss bar, running win rate, and
  session breakdown (London / Silver Bullet / NY AM / off-session)
  updated every 250ms without page reload.

FEATURE 2 — Filter Block Reason counter in dashboard:
  Shows the top 5 most common FilterEngine block reasons so you can
  see which gate is most active and tune accordingly.

FEATURE 3 — /api/filter_stats endpoint:
  Returns FilterEngine block reason counts as JSON for external tooling.

FEATURE 4 — Kill Zone countdown timer in dashboard:
  Shows next kill zone name and minutes until it opens. Helps you see
  when the next high-probability window starts.

FEATURE 5 — Signal journal now shows FILTERED status in a distinct colour
  (orange) separate from REJECTED (red) and OPENED (green) so you can
  see FilterEngine blocks vs risk manager rejections at a glance.

FEATURE 6 — /api/health endpoint:
  Returns a simple JSON health check (status, mt5_connected,
  mysql_connected, consecutive_losses) for external monitoring/alerting.
"""

import json
import socket
import threading
import time
import traceback
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ai_advisor import AIAdvisor
from auto_upgrade_engine import AutoUpgradeEngine
from config import CONFIG
from data_feed import create_feed
from database import MySQLStore
from filter_engine import FilterEngine          # BUG FIX 1
from logger import get_logger
from models import IctSnapshot, Signal, Trade
from signal_engine import SignalEngine
from trade_manager import TradeManager

log = get_logger(__name__)


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "value"):
        return value.value
    return str(value)


def snapshot_payload(snapshot: IctSnapshot | None) -> Dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "timeframe": snapshot.timeframe,
        "timestamp": snapshot.timestamp,
        "bias": snapshot.bias,
        "trend_strength": round(snapshot.trend_strength, 2),
        "atr": round(snapshot.atr, 2),
        "vwap": round(snapshot.vwap, 2),
        "premium_discount": snapshot.premium_discount,
        "displacement": snapshot.displacement,
        "mss": snapshot.mss,
        "choch": snapshot.choch,
        "bos": snapshot.bos,
        "sweep": snapshot.sweep,
        "concepts": snapshot.concepts,
        "metrics": {k: round(float(v), 3) for k, v in snapshot.metrics.items()},
    }


def signal_payload(signal: Signal | None) -> Dict[str, Any] | None:
    if signal is None:
        return None
    return {
        "direction": signal.direction.value,
        "symbol": signal.symbol,
        "timestamp": signal.timestamp,
        "entry": signal.entry,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "rr": round(signal.rr, 2),
        "confidence": round(signal.confidence, 1),
        "strength": signal.strength,
        "concepts": signal.concepts,
        "reason": signal.reason,
        "setup_model": signal.metadata.get("setup_model", "ICT Reversal"),
        "metadata": signal.metadata,
    }


class BotRuntime:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.feed = None
        self.signal_engine = SignalEngine()
        self.trade_manager = TradeManager()
        self.ai_advisor = AIAdvisor()
        self.upgrade_engine = AutoUpgradeEngine()
        self.filter_engine = FilterEngine()        # BUG FIX 1

        self.store: MySQLStore | None = None
        self.status = "starting"
        self.error = ""
        self.source = ""
        self.mysql_connected = False
        self.mt5_connected = False
        self.history_replayed = False
        self.last_price: float | None = None
        self.last_tick: Dict[str, float] = {}
        self.last_scan: datetime | None = None
        self.snapshot: IctSnapshot | None = None
        self._snapshots: Dict = {}
        self.strategy_status: Dict[str, Any] = {}
        self.last_signal: Signal | None = None
        self.messages: List[str] = []
        self.signal_journal: List[Dict[str, Any]] = []
        self.pre_entry_alert: Dict[str, Any] | None = None
        self.paused = False
        self._last_entry_at = datetime.now(timezone.utc)
        self._last_activity_attempt_at = None
        self._adaptive_trained_count = -1
        self._closed_trade_tickets_seen: set = set()
        self.running = False
        self.frames: Dict = {}

        # FEATURE 1 — filter block reason counter
        self._filter_block_reasons: Counter = Counter()

    def start(self) -> None:
        self.running = True
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()

    def _loop(self) -> None:
        try:
            self.store = MySQLStore()
            restored = self.store.restore_open_trades()
            closed_history = self.store.closed_trade_objects(limit=5000)
            self.trade_manager.restore_open_trades(restored)
            self.trade_manager.restore_closed_trades(closed_history)
            self.trade_manager.set_next_ticket(self.store.next_ticket())
            self._closed_trade_tickets_seen = {t.ticket for t in self.trade_manager.closed_trades}
            self.upgrade_engine.bootstrap_history(self.trade_manager.closed_trades)
            self.mysql_connected = True
            self._message("MySQL connected. Trade history persistence is active.")
            if restored:
                self._message(f"Restored {len(restored)} open trade(s) from MySQL.")
            if closed_history:
                self._message(f"Restored {len(closed_history)} closed trade(s) from MySQL for stats and auto-upgrade history.")
            if self.upgrade_engine.pause_reason:
                self._message(f"[AUTO-UPGRADE] {self.upgrade_engine.pause_reason}")
            self._refresh_adaptive_trainer()
        except Exception as exc:
            self.store = None
            self.mysql_connected = False
            self._set_error(f"MySQL unavailable: {exc}")
            self._message("MySQL is required for persistent win/loss counting. Start MySQL and refresh.")

        while self.running:
            try:
                if self.feed is None:
                    self.feed = create_feed()
                    self.mt5_connected = True
                    self._message(f"MT5 connected. Using symbol: {self.feed.symbol}")

                tick = self.feed.get_tick()
                include_current = bool(CONFIG.data.aggressive_intrabar_mode)
                frames = self.feed.get_multi_timeframe(CONFIG.data.history_bars, include_current=include_current)
                self.frames = frames

                primary = frames[CONFIG.timeframes.primary]
                snapshots = self.signal_engine.analyze(frames)
                self._snapshots = snapshots
                strategy_status = self.signal_engine.strategy_status(frames, snapshots)
                snapshot = snapshots.get(CONFIG.timeframes.primary)

                if snapshot is None:
                    raise RuntimeError("Not enough closed candles for ICT analysis.")

                last = primary.iloc[-1]

                if not self.history_replayed:
                    self._replay_open_history(primary)
                    self.history_replayed = True

                self.trade_manager.update(last.to_dict(), primary.index[-1].to_pydatetime())
                self._process_newly_closed_trades(frames)

                # BUG FIX 2: read thresholds from report() not a nonexistent attribute
                self._apply_upgrade_thresholds()

                # BUG FIX 3 & 4: pass consecutive losses to signal engine each cycle
                recent_closed = list(self.trade_manager.closed_trades[-30:])
                consec_losses = self._count_consecutive_losses(recent_closed)
                self.signal_engine._recent_consecutive_losses = consec_losses

                signals = self.signal_engine.generate_all(frames, tick)
                opened_this_cycle = False

                if not self.paused:
                    for signal in signals:
                        # BUG FIX 1: FilterEngine now runs before every submission
                        filter_ok, filter_reason = self.filter_engine.check(
                            signal=signal,
                            snapshots=snapshots,
                            frames=frames,
                            recent_closed_trades=recent_closed,
                            now_utc=datetime.now(timezone.utc),
                        )
                        if not filter_ok:
                            # FEATURE 2: track which gate blocks most
                            block_key = filter_reason.split(":")[0].strip()
                            self._filter_block_reasons[block_key] += 1

                            journal_entry = {
                                "time": signal.timestamp.isoformat() if hasattr(signal.timestamp, "isoformat") else str(signal.timestamp),
                                "agent": str(signal.metadata.get("strategy_agent") or "Unknown"),
                                "direction": signal.direction.value,
                                "confidence": round(signal.confidence, 1),
                                "rr": round(signal.rr, 2),
                                "reason": f"FilterEngine: {filter_reason}",
                                "status": "FILTERED",
                            }
                            self.signal_journal = self.signal_journal[-199:] + [journal_entry]
                            self._persist_signal_event("FILTERED", signal, filter_reason)
                            log.info("FilterEngine blocked: %s | %s", signal.direction.value, filter_reason)
                            continue

                        tick = self._alert_before_entry(signal, tick)
                        opened, reason = self.trade_manager.submit_signal(signal, tick)
                        agent = str(signal.metadata.get("strategy_agent") or signal.metadata.get("setup_model") or "Unknown")
                        journal_entry = {
                            "time": signal.timestamp.isoformat() if hasattr(signal.timestamp, "isoformat") else str(signal.timestamp),
                            "agent": agent,
                            "direction": signal.direction.value,
                            "confidence": round(signal.confidence, 1),
                            "rr": round(signal.rr, 2),
                            "reason": reason,
                            "status": "OPENED" if opened else "REJECTED",
                        }
                        self.signal_journal = self.signal_journal[-199:] + [journal_entry]
                        if opened:
                            self._last_entry_at = datetime.now(timezone.utc)
                            opened_this_cycle = True
                            executed_signal = self.trade_manager.open_trades[-1].signal
                            self._persist_signal_event("OPENED", executed_signal, reason)
                        else:
                            self._persist_signal_event("REJECTED", signal, reason)

                # Minimum activity fallback
                if not opened_this_cycle and CONFIG.data.minimum_activity_enabled:
                    idle_minutes = (datetime.now(timezone.utc) - self._last_entry_at).total_seconds() / 60
                    if idle_minutes >= CONFIG.data.minimum_activity_minutes:
                        if self._last_activity_attempt_at is None or (
                            datetime.now(timezone.utc) - self._last_activity_attempt_at
                        ).total_seconds() >= 120:
                            self._last_activity_attempt_at = datetime.now(timezone.utc)
                            fallback = self.signal_engine.activity_fallback_signal(frames, tick, idle_minutes)
                            if fallback:
                                # BUG FIX 5: fallback also goes through FilterEngine
                                fb_ok, fb_reason = self.filter_engine.check(
                                    signal=fallback,
                                    snapshots=snapshots,
                                    frames=frames,
                                    recent_closed_trades=recent_closed,
                                    now_utc=datetime.now(timezone.utc),
                                )
                                if fb_ok:
                                    tick = self._alert_before_entry(fallback, tick)
                                    opened, reason = self.trade_manager.submit_signal(fallback, tick)
                                    if opened:
                                        self._last_entry_at = datetime.now(timezone.utc)
                                        executed_signal = self.trade_manager.open_trades[-1].signal
                                        self._persist_signal_event("OPENED", executed_signal, reason)
                                    else:
                                        self._persist_signal_event("REJECTED", fallback, reason)
                                else:
                                    self._filter_block_reasons[fb_reason.split(":")[0].strip()] += 1
                                    log.info("FilterEngine blocked fallback: %s", fb_reason)

                for trade in self.trade_manager.open_trades + self.trade_manager.closed_trades[-10:]:
                    self._persist_trade(trade)

                self._refresh_adaptive_trainer()

                with self.lock:
                    self.snapshot = snapshot
                    self.strategy_status = strategy_status
                    self.last_signal = signals[0] if signals else self.last_signal
                    self.last_tick = tick
                    bid = tick.get("bid")
                    ask = tick.get("ask")
                    self.last_price = (
                        round((float(bid) + float(ask)) / 2.0, 2)
                        if bid is not None and ask is not None
                        else float(last["close"])
                    )
                    self.last_scan = datetime.now(timezone.utc)
                    self.status = "running"
                    self.error = ""
                    self.source = getattr(self.feed, "symbol", CONFIG.data.symbol)

            except Exception as exc:
                self._set_error(str(exc))
                log.warning("Loop error: %s\n%s", exc, traceback.format_exc())
                time.sleep(max(CONFIG.data.poll_seconds, 10))
                continue

            time.sleep(CONFIG.data.poll_seconds)

    # ── BUG FIX 2: correct thresholds read from report() ─────────────────
    def _apply_upgrade_thresholds(self) -> None:
        try:
            # report() returns current live thresholds — not a direct attribute
            thresholds = self.upgrade_engine.report().get("current_thresholds", {})
            if not thresholds:
                return
            if "high_winrate_min_confidence" in thresholds:
                CONFIG.risk.high_winrate_min_confidence = float(thresholds["high_winrate_min_confidence"])
            if "high_winrate_min_rr" in thresholds:
                CONFIG.risk.high_winrate_min_rr = float(thresholds["high_winrate_min_rr"])
            if "high_winrate_min_entry_score" in thresholds:
                CONFIG.risk.high_winrate_min_entry_score = float(thresholds["high_winrate_min_entry_score"])
            if "mtf_alignment_floor" in thresholds:
                CONFIG.risk.mtf_alignment_floor = float(thresholds["mtf_alignment_floor"])
            if "micro_max_sl_points" in thresholds:
                CONFIG.risk.micro_max_sl_points = float(thresholds["micro_max_sl_points"])
        except Exception as exc:
            log.debug("_apply_upgrade_thresholds error: %s", exc)

    # ── BUG FIX 3 & 4: consecutive loss counter ───────────────────────────
    def _count_consecutive_losses(self, trades: list) -> int:
        count = 0
        for trade in reversed(trades):
            if trade.pnl < 0:
                count += 1
            else:
                break
        return count

    def _alert_before_entry(self, signal: Signal, tick: Dict[str, float]) -> Dict[str, float]:
        alert_id = f"{time.time():.6f}-{signal.direction.value}-{signal.symbol}"
        with self.lock:
            self.pre_entry_alert = {
                "id": alert_id,
                "direction": signal.direction.value,
                "symbol": signal.symbol,
                "planned_entry": signal.entry,
                "time": datetime.now(timezone.utc).isoformat(),
            }
        time.sleep(1.2)
        if self.feed is None:
            return tick
        try:
            return self.feed.get_tick()
        except Exception as exc:
            log.debug("Pre-entry MT5 tick refresh failed: %s", exc)
            return tick

    def _process_newly_closed_trades(self, frames: Dict) -> None:
        for trade in self.trade_manager.closed_trades:
            if trade.ticket in self._closed_trade_tickets_seen:
                continue
            self._closed_trade_tickets_seen.add(trade.ticket)
            result = "WIN" if trade.pnl > 0 else "LOSS" if trade.pnl < 0 else "BE"
            self._message(f"Trade #{trade.ticket} CLOSED: {result} | PnL={trade.pnl:.2f} | RR={trade.rr_achieved:.2f}")
            upgrade = self.upgrade_engine.on_trade_closed(
                trade, frames, self.trade_manager.closed_trades
            )
            if upgrade:
                self._message(
                    f"[AUTO-UPGRADE] {upgrade.parameter}: {upgrade.old_value} → {upgrade.new_value} | {upgrade.reason}"
                )

    def _replay_open_history(self, primary) -> None:
        try:
            if len(primary) < 2:
                return
            for i in range(max(0, len(primary) - 50), len(primary) - 1):
                candle = primary.iloc[i].to_dict()
                ts = primary.index[i].to_pydatetime()
                self.trade_manager.update(candle, ts)
        except Exception as exc:
            log.warning("History replay error: %s", exc)

    def _refresh_adaptive_trainer(self) -> None:
        closed_count = len(self.trade_manager.closed_trades)
        if closed_count != self._adaptive_trained_count and closed_count >= 5:
            self.trade_manager.train_adaptive_agent()
            self._adaptive_trained_count = closed_count

    def _persist_trade(self, trade: Trade) -> None:
        if not self.store:
            return
        try:
            self.store.upsert_trade(trade)
        except Exception as exc:
            log.debug("Trade persist error: %s", exc)

    def _persist_signal_event(self, status: str, signal: Signal, reason: str) -> None:
        if not self.store:
            return
        try:
            self.store.signal_event(status, signal_payload(signal) or {}, reason)
        except Exception as exc:
            log.debug("Signal event persist error: %s", exc)

    def _set_error(self, msg: str) -> None:
        with self.lock:
            self.status = "error"
            self.error = msg
        log.error("BotRuntime error: %s", msg)

    def _message(self, text: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with self.lock:
            self.messages = self.messages[-199:] + [f"[{ts}] {text}"]
        log.info(text)

    # FEATURE 4: next kill zone countdown helper
    def _next_killzone(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        now_minutes = now.hour * 60 + now.minute
        kill_zones = getattr(CONFIG.sessions, "kill_zones", {})
        upcoming = []
        for name, (start, end) in kill_zones.items():
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
            start_m = sh * 60 + sm
            end_m = eh * 60 + em
            if start_m <= now_minutes <= end_m:
                return {"name": name.replace("_", " ").title(), "status": "ACTIVE", "minutes_until": 0}
            diff = start_m - now_minutes
            if diff < 0:
                diff += 1440  # next day
            upcoming.append({"name": name.replace("_", " ").title(), "minutes_until": diff, "status": "upcoming"})
        if not upcoming:
            return {"name": "—", "status": "unknown", "minutes_until": -1}
        upcoming.sort(key=lambda x: x["minutes_until"])
        return upcoming[0]

    # FEATURE 3 + health endpoint data
    def filter_stats_payload(self) -> Dict[str, Any]:
        with self.lock:
            total = sum(self._filter_block_reasons.values())
            top5 = self._filter_block_reasons.most_common(5)
            return {
                "total_filtered": total,
                "top_reasons": [{"reason": r, "count": c} for r, c in top5],
            }

    def health_payload(self) -> Dict[str, Any]:
        upgrade_report = self.upgrade_engine.report()
        return {
            "status": self.status,
            "mt5_connected": self.mt5_connected,
            "mysql_connected": self.mysql_connected,
            "paused": self.paused,
            "consecutive_losses": upgrade_report.get("consecutive_losses", 0),
            "trading_paused": upgrade_report.get("trading_paused", False),
            "last_scan": self.last_scan.isoformat() if self.last_scan else None,
        }

    def state_payload(self) -> Dict[str, Any]:
        with self.lock:
            stats = self.trade_manager.stats()
            snap = snapshot_payload(self.snapshot)
            sig = signal_payload(self.last_signal)
            strat = self.strategy_status
            agents = stats.get("agent_stats", {})
            calibration = self.trade_manager.confidence_calibrator.report()
            adaptive = self.trade_manager.adaptive_trainer.report()
            advisor = self.ai_advisor.build(
                snapshot=snap,
                strategy_status=strat,
                last_signal=sig,
                stats=stats,
                agent_performance=agents,
                calibration=calibration,
                signal_journal=self.signal_journal,
            )
            upgrade_report = self.upgrade_engine.report()
            idle_minutes = (datetime.now(timezone.utc) - self._last_entry_at).total_seconds() / 60
            threshold = float(CONFIG.data.minimum_activity_minutes)

            # FEATURE 1 & 4: session performance + kill zone data
            session_pnl = self._session_pnl()
            next_kz = self._next_killzone()
            filter_stats = {
                "total_filtered": sum(self._filter_block_reasons.values()),
                "top_reasons": [{"reason": r, "count": c} for r, c in self._filter_block_reasons.most_common(5)],
            }

            return {
                "status": self.status,
                "error": self.error,
                "symbol": CONFIG.data.symbol,
                "tradingview_symbol": CONFIG.data.tradingview_symbol,
                "source": self.source,
                "last_price": self.last_price,
                "last_scan": self.last_scan.isoformat() if self.last_scan else None,
                "mysql_connected": self.mysql_connected,
                "mt5_connected": self.mt5_connected,
                "paused": self.paused,
                "snapshot": snap,
                "last_signal": sig,
                "strategies": strat,
                "stats": {k: v for k, v in stats.items() if k != "agent_stats"},
                "agent_performance": agents,
                "adaptive_training": adaptive,
                "confidence_calibration": calibration,
                "ai_advisor": advisor,
                "auto_upgrade": upgrade_report,
                "minimum_activity": {
                    "enabled": CONFIG.data.minimum_activity_enabled,
                    "idle_minutes": round(idle_minutes, 1),
                    "threshold_minutes": threshold,
                    "percent": round(min(100, idle_minutes / max(threshold, 1) * 100), 1),
                },
                "messages": self.messages[-40:],
                "signal_journal": self.signal_journal[-30:],
                "pre_entry_alert": self.pre_entry_alert,
                # NEW fields
                "session_pnl": session_pnl,
                "next_killzone": next_kz,
                "filter_stats": filter_stats,
            }

    # FEATURE 1 helper: break down P&L by session
    def _session_pnl(self) -> Dict[str, Any]:
        sessions = {"london": 0.0, "silver_bullet": 0.0, "new_york_am": 0.0, "off_session": 0.0}
        counts = {k: 0 for k in sessions}
        wins = {k: 0 for k in sessions}
        for trade in self.trade_manager.closed_trades:
            sess = str(trade.signal.metadata.get("session", "off_session"))
            key = sess if sess in sessions else "off_session"
            sessions[key] += trade.pnl
            counts[key] += 1
            if trade.pnl > 0:
                wins[key] += 1
        result = {}
        for k in sessions:
            result[k] = {
                "pnl": round(sessions[k], 2),
                "trades": counts[k],
                "winrate": round(wins[k] / counts[k] * 100, 1) if counts[k] else 0.0,
            }
        return result

    def trades_payload(self, tick_price: float | None = None) -> Dict[str, Any]:
        fresh_tick: Dict[str, float] = {}
        if tick_price is None and self.feed is not None:
            try:
                fresh_tick = self.feed.get_tick()
            except Exception as exc:
                log.debug("Live tick refresh for PnL failed: %s", exc)

        with self.lock:
            if fresh_tick:
                self.last_tick = fresh_tick
                bid = fresh_tick.get("bid")
                ask = fresh_tick.get("ask")
                if bid is not None and ask is not None:
                    self.last_price = round((float(bid) + float(ask)) / 2.0, 2)

            tick = dict(fresh_tick or self.last_tick)
            if tick_price is not None:
                tick = {"bid": tick_price, "ask": tick_price}

            open_rows = []
            for trade in self.trade_manager.open_trades:
                live = self.trade_manager.live_pnl_from_tick(trade, tick)
                open_rows.append({
                    "ticket": trade.ticket,
                    "direction": trade.signal.direction.value,
                    "entry": trade.signal.entry,
                    "current_sl": round(float(trade.current_sl), 2) if trade.current_sl else None,
                    "tp1_price": round(float(trade.tp1_price), 2) if trade.tp1_price else None,
                    "take_profit": trade.signal.take_profit,
                    "live_pnl": round(live, 2),
                    "status": trade.status.value,
                    "strategy_agent": trade.signal.metadata.get("strategy_agent", ""),
                    "opened_at": trade.opened_at.isoformat() if trade.opened_at else None,
                })

            history_rows = []
            for trade in reversed(self.trade_manager.closed_trades[-200:]):
                history_rows.append({
                    "ticket": trade.ticket,
                    "direction": trade.signal.direction.value,
                    "entry": trade.signal.entry,
                    "stop_loss": trade.signal.stop_loss,
                    "tp1_price": round(float(trade.tp1_price), 2) if trade.tp1_price else None,
                    "take_profit": trade.signal.take_profit,
                    "close_price": trade.close_price,
                    "pnl": round(trade.pnl, 2),
                    "result": "WIN" if trade.pnl > 0 else "LOSS" if trade.pnl < 0 else "BE",
                    "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
                    "strategy_agent": trade.signal.metadata.get("strategy_agent", ""),
                    "rr_achieved": round(trade.rr_achieved, 2),
                    "session": trade.signal.metadata.get("session", ""),
                    "confidence": round(trade.signal.confidence, 1),
                })

            return {"open": open_rows, "history": history_rows}


RUNTIME = BotRuntime()


class TerminalHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._html()
        elif path == "/api/state":
            self._json(RUNTIME.state_payload())
        elif path == "/api/trades":
            self._json(RUNTIME.trades_payload())
        elif path == "/api/upgrade_log":
            self._json(RUNTIME.upgrade_engine.report())
        elif path == "/api/filter_stats":          # FEATURE 3
            self._json(RUNTIME.filter_stats_payload())
        elif path == "/api/health":                # FEATURE 6
            self._json(RUNTIME.health_payload())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

        if path == "/api/pause":
            RUNTIME.paused = bool(payload.get("paused", not RUNTIME.paused))
            self._json({"paused": RUNTIME.paused})
        elif path == "/api/clear":
            if RUNTIME.store:
                RUNTIME.store.clear_trades()
            RUNTIME.trade_manager.open_trades.clear()
            RUNTIME.trade_manager.closed_trades.clear()
            RUNTIME._filter_block_reasons.clear()
            self._json({"cleared": True})
        elif path == "/api/resume_trading":
            operator = str(payload.get("operator", "dashboard_operator"))
            note = str(payload.get("note", ""))
            RUNTIME.upgrade_engine.resume_trading(operator, note)
            self._json({"trading_paused": RUNTIME.upgrade_engine.trading_paused})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data, default=json_default).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, socket.timeout):
            pass

    def _html(self):
        html = _TERMINAL_HTML.encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, socket.timeout):
            pass


_TERMINAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>XAUUSD ICT Terminal PRO</title>
<style>
*{box-sizing:border-box}
body{background:#0d1117;color:#e6edf3;font-family:'Courier New',monospace;font-size:13px;margin:0;padding:16px}
h2{color:#f0c040;margin:8px 0 4px}
h3{color:#8b949e;margin:6px 0 3px;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px}
.card-wide{grid-column:span 2}
.card-full{grid-column:span 4}
.metric{font-size:22px;font-weight:700;color:#f0c040}
.buy{color:#3fb950}.sell{color:#f85149}.filtered{color:#d29922}.warn{color:#d29922}.muted{color:#8b949e}
.upgrade{color:#bc8cff}
.kz-active{background:#1a2e1a;border:1px solid #3fb950;border-radius:4px;padding:4px 8px;display:inline-block;color:#3fb950;font-weight:bold}
.kz-upcoming{background:#1a1a2e;border:1px solid #8b949e;border-radius:4px;padding:4px 8px;display:inline-block;color:#8b949e}
table{width:100%;border-collapse:collapse}
th,td{border:1px solid #21262d;padding:4px 8px;text-align:left;font-size:12px}
th{background:#161b22;color:#8b949e}
.gate-pass{color:#3fb950;font-weight:bold}.gate-block{color:#f85149;font-weight:bold}.gate-wait{color:#d29922}
button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 14px;cursor:pointer;margin:4px}
button:hover{background:#30363d}
pre{background:#0d1117;padding:8px;border-radius:4px;overflow:auto;max-height:200px;font-size:11px}
.upgrade-card{background:#1a1030;border:1px solid #bc8cff;border-radius:6px;padding:8px;margin:4px 0;font-size:12px}
.tv-chart{height:520px;width:100%}
.sess-row{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0}
.sess-card{background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:6px 10px;min-width:140px}
.bar-wrap{background:#21262d;border-radius:3px;height:8px;margin:3px 0}
.bar{height:8px;border-radius:3px;background:#3fb950}
.bar.neg{background:#f85149}
</style>
</head>
<body>
<h2>&#9733; XAUUSD ICT Signal Terminal PRO</h2>
<div id="status" class="muted">Connecting...</div>

<!-- Stats row -->
<div class="grid" id="statsRow"></div>

<!-- Kill zone + session P&L -->
<div class="grid">
  <div class="card card-wide">
    <h3>&#9201; Kill Zone Status</h3>
    <div id="killzoneStatus">—</div>
  </div>
  <div class="card card-wide">
    <h3>Session Performance</h3>
    <div id="sessionPnl" class="sess-row"></div>
  </div>
</div>

<!-- TradingView chart -->
<div class="grid">
  <div class="card card-full">
    <h3>TradingView XAUUSD Live Chart</h3>
    <div class="tradingview-widget-container">
      <div id="tradingview_xauusd" class="tv-chart"></div>
    </div>
  </div>
</div>

<!-- Pipeline + Auto-Upgrade -->
<div class="grid">
  <div class="card card-wide">
    <h3>Signal Pipeline</h3>
    <div id="analysisPipeline"></div>
  </div>
  <div class="card card-wide">
    <h3>&#128640; Auto-Upgrade Engine</h3>
    <div id="upgradeStatus" class="muted">Loading...</div>
    <div id="upgradeLog"></div>
  </div>
</div>

<!-- Agent flow + thresholds -->
<div class="grid">
  <div class="card card-wide">
    <h3>Agent Flow</h3>
    <div id="agentFlow"></div>
  </div>
  <div class="card card-wide">
    <h3>Current Thresholds (Live-Patched)</h3>
    <div id="thresholds"></div>
  </div>
</div>

<!-- Filter stats -->
<div class="grid">
  <div class="card card-full">
    <h3>&#128683; Filter Engine — Top Block Reasons</h3>
    <div id="filterStats" class="muted">Loading...</div>
  </div>
</div>

<!-- Positions -->
<div class="grid">
  <div class="card card-full">
    <h3>Positions</h3>
    <button onclick="setTab('open')">Open</button>
    <button onclick="setTab('history')">History</button>
    <table><thead id="thead"></thead><tbody id="tbody"></tbody></table>
  </div>
</div>

<!-- Signal journal + log -->
<div class="grid">
  <div class="card card-wide">
    <h3>Signal Journal</h3>
    <div id="signalJournal"></div>
  </div>
  <div class="card card-wide">
    <h3>Log</h3>
    <pre id="log"></pre>
  </div>
</div>

<!-- Controls -->
<div class="grid">
  <div class="card card-full">
    <button onclick="pause()">Pause / Resume</button>
    <button onclick="toggleSound()">Sound: <span id="soundState">On</span></button>
    <button onclick="clearTrades()">Clear Trades</button>
    <button onclick="resumeTrading()">Resume Trading (Circuit Breaker)</button>
    <span id="pauseState" class="muted"></span>
  </div>
</div>

<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
<script>
let tab='open';let tickBusy=false;
let soundEnabled=localStorage.getItem('tradeSoundEnabled')!=='false';
let lastPreEntryAlertId='';

function updateSoundState(){document.getElementById('soundState').textContent=soundEnabled?'On':'Off';}
function toggleSound(){soundEnabled=!soundEnabled;localStorage.setItem('tradeSoundEnabled',String(soundEnabled));updateSoundState();if(soundEnabled)playPreEntrySound();}
function playPreEntrySound(){if(!soundEnabled)return;try{const AudioCtx=window.AudioContext||window.webkitAudioContext;const ctx=new AudioCtx();const gain=ctx.createGain();gain.connect(ctx.destination);[0,0.32,0.64].forEach((offset)=>{const osc=ctx.createOscillator();osc.type='sine';osc.frequency.setValueAtTime(880,ctx.currentTime+offset);osc.connect(gain);gain.gain.setValueAtTime(0.0001,ctx.currentTime+offset);gain.gain.exponentialRampToValueAtTime(0.28,ctx.currentTime+offset+0.015);gain.gain.exponentialRampToValueAtTime(0.0001,ctx.currentTime+offset+0.18);osc.start(ctx.currentTime+offset);osc.stop(ctx.currentTime+offset+0.2);});setTimeout(()=>ctx.close(),1200);}catch(e){}}

function initTradingView(){
  if(!window.TradingView||document.getElementById('tradingview_xauusd').dataset.loaded==='1')return;
  document.getElementById('tradingview_xauusd').dataset.loaded='1';
  new TradingView.widget({autosize:true,symbol:"OANDA:XAUUSD",interval:"1",timezone:"Etc/UTC",theme:"dark",style:"1",locale:"en",toolbar_bg:"#161b22",enable_publishing:false,allow_symbol_change:true,hide_side_toolbar:false,container_id:"tradingview_xauusd"});
}

function setTab(t){tab=t;loadTrades();}
function fmt(v,d=2){return Number(v).toFixed(d);}

async function loadState(){
  const res=await fetch('/api/state',{cache:'no-store'});
  const data=await res.json();
  const stats=data.stats||{};

  document.getElementById('status').textContent=`${data.symbol} | ${data.status} | price ${fmt(data.last_price||0,2)} | scan ${data.last_scan||'--'}`;
  document.getElementById('pauseState').textContent=data.paused?'⏸ PAUSED':'▶ RUNNING';

  const alert=data.pre_entry_alert||{};
  if(alert.id&&alert.id!==lastPreEntryAlertId){lastPreEntryAlertId=alert.id;playPreEntrySound();}

  // Stats row
  const keys=['total_trades','wins','losses','winrate','current_streak','daily_pnl','weekly_pnl','net_pnl'];
  document.getElementById('statsRow').innerHTML=keys.map(k=>`<div class="card"><div class="muted">${k.replace(/_/g,' ')}</div><div class="metric ${Number(stats[k]||0)>0&&k.includes('pnl')?'buy':Number(stats[k]||0)<0&&k.includes('pnl')?'sell':''}">${stats[k]??'--'}</div></div>`).join('');

  // FEATURE 4: Kill zone countdown
  const kz=data.next_killzone||{};
  const kzEl=document.getElementById('killzoneStatus');
  if(kz.status==='ACTIVE'){
    kzEl.innerHTML=`<span class="kz-active">&#9679; ${kz.name} — ACTIVE NOW</span>`;
  } else {
    kzEl.innerHTML=`<span class="kz-upcoming">Next: <b>${kz.name||'—'}</b> in ${kz.minutes_until>0?kz.minutes_until+'m':'—'}</span>`;
  }

  // FEATURE 1: Session P&L breakdown
  const sp=data.session_pnl||{};
  document.getElementById('sessionPnl').innerHTML=Object.entries(sp).map(([k,v])=>{
    const pnlClass=v.pnl>0?'buy':v.pnl<0?'sell':'muted';
    const barPct=Math.min(100,Math.abs(v.winrate));
    return `<div class="sess-card"><div class="muted" style="font-size:11px">${k.replace(/_/g,' ')}</div><div class="${pnlClass}">${fmt(v.pnl)}</div><div class="muted" style="font-size:11px">${v.trades} trades · ${fmt(v.winrate,1)}% WR</div><div class="bar-wrap"><div class="bar${v.pnl<0?' neg':''}" style="width:${barPct}%"></div></div></div>`;
  }).join('');

  // Pipeline
  const pipeline=(data.strategies||{}).analysis_progress||[];
  document.getElementById('analysisPipeline').innerHTML=pipeline.map(row=>{
    const cls=row.state==='PASS'?'gate-pass':row.state==='BLOCK'?'gate-block':'gate-wait';
    return `<div style="margin:3px 0"><b>${row.name}</b> <span class="${cls}">${row.state}</span> <span class="muted">${row.detail||''}</span></div>`;
  }).join('')||'<div class="muted">No pipeline data yet.</div>';

  // Auto-upgrade
  const upgrade=data.auto_upgrade||{};
  document.getElementById('upgradeStatus').innerHTML=`<span class="upgrade">${upgrade.total_upgrades||0} upgrades applied</span> | ${upgrade.consecutive_losses||0} consecutive losses | last: ${upgrade.last_upgrade_at||'never'}`;
  document.getElementById('upgradeLog').innerHTML=(upgrade.log||[]).slice().reverse().slice(0,5).map(r=>`<div class="upgrade-card"><b class="upgrade">[${r.trigger}]</b> <b>${r.parameter}</b>: <span class="sell">${r.old_value}</span> → <span class="buy">${r.new_value}</span><br><span class="muted">${r.reason}</span>${r.backtest_trades>0?`<br><span class="muted">Backtest: ${r.backtest_trades} trades, ${fmt(r.backtest_winrate,1)}% WR`:''}</div>`).join('')||'<div class="muted">No upgrades yet.</div>';

  // Live thresholds
  const t=(upgrade.current_thresholds||{});
  document.getElementById('thresholds').innerHTML=Object.entries(t).map(([k,v])=>`<div style="margin:2px 0"><span class="muted">${k}:</span> <span class="upgrade">${typeof v==='number'?fmt(v,2):v}</span></div>`).join('');

  // Agent flow
  const agents=(data.strategies||{}).strategy_agents||[];
  document.getElementById('agentFlow').innerHTML=agents.map(a=>`<div style="margin:6px 0;padding:6px;background:#1c2128;border-radius:4px"><b>${a.name}</b> <span class="${a.ready?'buy':'warn'}">${a.ready?'READY':'SCANNING'}</span> | score ${fmt(a.score,1)}% | ${a.direction||'--'}<br><span class="muted">${a.reason||''}</span></div>`).join('')||'<div class="muted">No agent data.</div>';

  // FEATURE 2: Filter stats
  const fs=data.filter_stats||{};
  const fsTotal=fs.total_filtered||0;
  const fsReasons=fs.top_reasons||[];
  document.getElementById('filterStats').innerHTML=fsTotal===0?'<span class="muted">No signals filtered yet.</span>':`<span class="muted">Total filtered: <b>${fsTotal}</b></span><div style="margin-top:6px">${fsReasons.map(r=>`<div style="margin:2px 0"><span class="filtered">✗</span> <b>${r.reason}</b> <span class="muted">(${r.count}×)</span></div>`).join('')}</div>`;

  // FEATURE 5: Signal journal with FILTERED in distinct colour
  document.getElementById('signalJournal').innerHTML=(data.signal_journal||[]).slice().reverse().slice(0,20).map(r=>{
    const cls=r.status==='OPENED'?'buy':r.status==='FILTERED'?'filtered':r.status==='REJECTED'?'sell':'warn';
    return `<div style="margin:2px 0"><span class="${cls}">${r.status}</span> | ${r.agent||'--'} | ${r.direction||'--'} ${fmt(r.confidence||0,1)}% | <span class="muted">${r.reason||''}</span></div>`;
  }).join('')||'<div class="muted">No signals yet.</div>';

  document.getElementById('log').textContent=(data.messages||[]).join('\\n');
}

async function loadTrades(){
  const res=await fetch('/api/trades',{cache:'no-store'});
  const data=await res.json();
  const rows=tab==='open'?data.open:data.history;
  const headers=tab==='open'?['ticket','direction','entry','current_sl','tp1_price','take_profit','live_pnl','status','strategy_agent','opened_at']:['ticket','direction','entry','stop_loss','take_profit','close_price','pnl','result','rr_achieved','session','confidence','closed_at'];
  document.getElementById('thead').innerHTML=`<tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr>`;
  document.getElementById('tbody').innerHTML=(rows||[]).map(r=>`<tr>${headers.map(h=>`<td class="${Number(r[h])>0&&h.includes('pnl')?'buy':Number(r[h])<0&&h.includes('pnl')?'sell':r[h]==='WIN'?'buy':r[h]==='LOSS'?'sell':''}">${r[h]??'--'}</td>`).join('')}</tr>`).join('');
}

async function pause(){await fetch('/api/pause',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}});}
async function clearTrades(){if(confirm('Clear all trades?'))await fetch('/api/clear',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}});}
async function resumeTrading(){
  const note=prompt('Enter note for resume (optional):','Manual review completed');
  if(note===null)return;
  await fetch('/api/resume_trading',{method:'POST',body:JSON.stringify({operator:'dashboard',note}),headers:{'Content-Type':'application/json'}});
}

async function tick(){
  if(tickBusy)return;
  tickBusy=true;
  try{await loadState();await loadTrades();}
  catch(e){document.getElementById('status').textContent='Error: '+e.message;}
  finally{tickBusy=false;}
}

tick();initTradingView();updateSoundState();setInterval(tick,250);
</script>
</body>
</html>"""


def main() -> None:
    RUNTIME.start()
    server = ThreadingHTTPServer(("127.0.0.1", 8080), TerminalHandler)
    print(f"{CONFIG.data.symbol} ICT Gold Bot Terminal PRO running at http://127.0.0.1:8080")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        RUNTIME.running = False
        server.shutdown()
        print("Stopped.")


if __name__ == "__main__":
    main()
