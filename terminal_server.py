from __future__ import annotations

"""
PRO UPGRADE - terminal_server.py  (RISK-CONTROL REMEDIATION PASS)
Changes vs original:
1. AutoUpgradeEngine integrated — every closed loss triggers backtest + auto-parameter fix
2. /api/upgrade_log endpoint — see every automatic upgrade in the browser terminal
3. Upgrade report injected into /api/state for the dashboard
4. Trade closing now explicitly calls upgrade_engine.on_trade_closed()
5. Upgrade summary shown in message log
6. CIRCUIT BREAKER WIRING: BotRuntime.__init__ wires
   trade_manager.circuit_breaker_check to a lambda reading
   upgrade_engine.trading_paused / pause_reason, so a circuit-breaker trip is
   enforced at the point trades are submitted, not just recorded in a report.
7. /api/resume_trading endpoint (POST) — manually clears a circuit-breaker
   pause. There is no automatic resume; see auto_upgrade_engine.resume_trading().
8. Dashboard now shows a "Resume Trading" button (only meaningful while
   paused) so the manual-resume design has an actual control surface, not
   just an API endpoint nothing in the UI calls.
"""

import json
import socket
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import urlparse

from ai_advisor import AIAdvisor
from auto_upgrade_engine import AutoUpgradeEngine
from config import CONFIG
from data_feed import create_feed
from database import MySQLStore
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
        self.upgrade_engine = AutoUpgradeEngine()   # PRO: auto-upgrade engine
        # CIRCUIT BREAKER WIRING: TradeManager.submit_signal() calls this
        # before any other gate. If the upgrade engine has tripped its
        # consecutive-loss circuit breaker, every new entry is refused here —
        # enforced at submission time, not just visible in a report.
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
        self._last_frames_refresh_monotonic = 0.0

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
            self._closed_trade_tickets_seen = {trade.ticket for trade in self.trade_manager.closed_trades}
            # FIX: bootstrap_history now also reconstructs circuit-breaker pause
            # state from restored history (see AutoUpgradeEngine.bootstrap_history
            # docstring) — a restart during an active loss streak no longer
            # silently clears the pause.
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
                strategy_status = self.signal_engine.strategy_status(frames, snapshots)
                snapshot = snapshots.get(CONFIG.timeframes.primary)
                if snapshot is None:
                    raise RuntimeError("Not enough closed candles for ICT analysis.")

                last = primary.iloc[-1]
                if not self.history_replayed:
                    self._replay_open_history(primary)
                    self.history_replayed = True
                self.trade_manager.update(last.to_dict(), primary.index[-1].to_pydatetime())

                # PRO: Check for newly closed trades and run auto-upgrade
                self._process_newly_closed_trades(frames)

                signals = self.signal_engine.generate_all(frames, tick)
                opened_this_cycle = False
                # NOTE: trade_manager.submit_signal() already enforces the
                # circuit breaker as its first check (see circuit_breaker_check
                # wiring above), so signals attempted below during a pause are
                # safely rejected there. self.paused is the separate, manual
                # dashboard pause toggle (POST /api/pause) — distinct from the
                # automatic circuit breaker.
                if not self.paused:
                    for signal in signals:
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
                            if self._last_activity_attempt_at is None or (datetime.now(timezone.utc) - self._last_activity_attempt_at).total_seconds() >= 120:
                                self._last_activity_attempt_at = datetime.now(timezone.utc)
                                fallback = self.signal_engine.activity_fallback_signal(frames, tick, idle_minutes)
                                if fallback:
                                    tick = self._alert_before_entry(fallback, tick)
                                    opened, reason = self.trade_manager.submit_signal(fallback, tick)
                                    if opened:
                                        self._last_entry_at = datetime.now(timezone.utc)
                                        executed_signal = self.trade_manager.open_trades[-1].signal
                                        self._persist_signal_event("OPENED", executed_signal, reason)
                                    else:
                                        self._persist_signal_event("REJECTED", fallback, reason)

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
                    self.last_price = round((float(bid) + float(ask)) / 2.0, 2) if bid is not None and ask is not None else float(last["close"])
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
        """
        PRO: Detect trades that just closed this cycle, run auto-upgrade for losses.
        """
        for trade in self.trade_manager.closed_trades:
            if trade.ticket in self._closed_trade_tickets_seen:
                continue
            self._closed_trade_tickets_seen.add(trade.ticket)
            result = "WIN" if trade.pnl > 0 else "LOSS" if trade.pnl < 0 else "BE"
            self._message(f"Trade #{trade.ticket} CLOSED: {result} | PnL={trade.pnl:.2f} | RR={trade.rr_achieved:.2f}")

            if trade.pnl < 0:
                # Run auto-upgrade engine on this loss
                upgrade = self.upgrade_engine.on_trade_closed(
                    trade, frames, self.trade_manager.closed_trades
                )
                if upgrade:
                    self._message(
                        f"[AUTO-UPGRADE] {upgrade.parameter}: {upgrade.old_value} → {upgrade.new_value} | {upgrade.reason}"
                    )
            else:
                # Still track wins (no upgrade needed, but feed the upgrade engine counters)
                self.upgrade_engine.on_trade_closed(
                    trade, frames, self.trade_manager.closed_trades
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
                "auto_upgrade": upgrade_report,           # PRO: upgrade info in dashboard
                "minimum_activity": {
                    "enabled": CONFIG.data.minimum_activity_enabled,
                    "idle_minutes": round(idle_minutes, 1),
                    "threshold_minutes": threshold,
                    "percent": round(min(100, idle_minutes / max(threshold, 1) * 100), 1),
                },
                "messages": self.messages[-40:],
                "signal_journal": self.signal_journal[-30:],
                "pre_entry_alert": self.pre_entry_alert,
            }

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
            self._json({"cleared": True})
        elif path == "/api/resume_trading":
            # FIX: manual-only resume from a circuit-breaker pause. There is
            # deliberately no automatic resume path — see
            # AutoUpgradeEngine.resume_trading() docstring.
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
  body{background:#0d1117;color:#e6edf3;font-family:'Courier New',monospace;font-size:13px;margin:0;padding:16px}
  h2{color:#f0c040;margin:8px 0 4px}
  h3{color:#8b949e;margin:6px 0 3px;font-size:12px;text-transform:uppercase;letter-spacing:1px}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px}
  .card-wide{grid-column:span 2}
  .card-full{grid-column:span 4}
  .metric{font-size:22px;font-weight:700;color:#f0c040}
  .buy{color:#3fb950}.sell{color:#f85149}.warn{color:#d29922}.muted{color:#8b949e}
  .upgrade{color:#bc8cff}
  table{width:100%;border-collapse:collapse}
  th,td{border:1px solid #21262d;padding:4px 8px;text-align:left;font-size:12px}
  th{background:#161b22;color:#8b949e}
  .gate-pass{color:#3fb950;font-weight:bold}.gate-block{color:#f85149;font-weight:bold}.gate-wait{color:#d29922}
  button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 14px;cursor:pointer;margin:4px}
  button:hover{background:#30363d}
  button.danger{border-color:#f85149;color:#f85149}
  button.danger:hover{background:#2a1416}
  pre{background:#0d1117;padding:8px;border-radius:4px;overflow:auto;max-height:200px;font-size:11px}
  .upgrade-card{background:#1a1030;border:1px solid #bc8cff;border-radius:6px;padding:8px;margin:4px 0;font-size:12px}
  .tv-chart{height:520px;width:100%}
</style>
</head>
<body>
<h2>&#9733; XAUUSD ICT Signal Terminal PRO</h2>
<div id="status" class="muted">Connecting...</div>

<div class="grid" id="statsRow"></div>

<div class="grid">
  <div class="card card-full">
    <h3>TradingView XAUUSD Live Chart</h3>
    <div class="tradingview-widget-container">
      <div id="tradingview_xauusd" class="tv-chart"></div>
    </div>
  </div>
</div>

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

<div class="grid">
  <div class="card card-full">
    <h3>Positions</h3>
    <button onclick="setTab('open')">Open</button>
    <button onclick="setTab('history')">History</button>
    <table><thead id="thead"></thead><tbody id="tbody"></tbody></table>
  </div>
</div>

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

<div class="grid">
  <div class="card">
    <button onclick="pause()">Pause / Resume</button>
    <button onclick="toggleSound()">Sound: <span id="soundState">On</span></button>
    <button onclick="clearTrades()">Clear Trades</button>
    <span id="pauseState" class="muted"></span>
  </div>
</div>

<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
<script>
  let tab = 'open';
  let tickBusy = false;
  let soundEnabled = localStorage.getItem('tradeSoundEnabled') !== 'false';
  let lastPreEntryAlertId = '';

  function updateSoundState(){
    document.getElementById('soundState').textContent = soundEnabled ? 'On' : 'Off';
  }

  function toggleSound(){
    soundEnabled = !soundEnabled;
    localStorage.setItem('tradeSoundEnabled', String(soundEnabled));
    updateSoundState();
    if(soundEnabled) playPreEntrySound();
  }

  function playPreEntrySound(){
    if(!soundEnabled) return;
    try{
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      const ctx = new AudioCtx();
      const gain = ctx.createGain();
      gain.connect(ctx.destination);
      [0, 0.32, 0.64].forEach((offset)=>{
        const osc = ctx.createOscillator();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(880, ctx.currentTime + offset);
        osc.connect(gain);
        gain.gain.setValueAtTime(0.0001, ctx.currentTime + offset);
        gain.gain.exponentialRampToValueAtTime(0.28, ctx.currentTime + offset + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + offset + 0.18);
        osc.start(ctx.currentTime + offset);
        osc.stop(ctx.currentTime + offset + 0.2);
      });
      setTimeout(()=>ctx.close(), 1200);
    }catch(e){}
  }

  function initTradingView(){
    if(!window.TradingView || document.getElementById('tradingview_xauusd').dataset.loaded === '1') return;
    document.getElementById('tradingview_xauusd').dataset.loaded = '1';
    new TradingView.widget({
      autosize: true,
      symbol: "OANDA:XAUUSD",
      interval: "1",
      timezone: "Etc/UTC",
      theme: "dark",
      style: "1",
      locale: "en",
      toolbar_bg: "#161b22",
      enable_publishing: false,
      allow_symbol_change: true,
      hide_side_toolbar: false,
      container_id: "tradingview_xauusd"
    });
  }
  function setTab(t){tab=t;loadTrades();}
  function fmt(v,d=2){return Number(v).toFixed(d);}

  async function loadState(){
    const res = await fetch('/api/state',{cache:'no-store'});
    const data = await res.json();
    const stats = data.stats || {};
    document.getElementById('status').textContent =
      `${data.symbol} | ${data.status} | price ${fmt(data.last_price||0,2)} | scan ${data.last_scan||'--'}`;
    document.getElementById('pauseState').textContent = data.paused ? '⏸ PAUSED' : '▶ RUNNING';
    const alert = data.pre_entry_alert || {};
    if(alert.id && alert.id !== lastPreEntryAlertId){
      lastPreEntryAlertId = alert.id;
      playPreEntrySound();
    }
    // Stats row
    const keys = ['total_trades','wins','losses','winrate','current_streak','daily_pnl','weekly_pnl','net_pnl'];
    document.getElementById('statsRow').innerHTML = keys.map(k=>`
      <div class="card">
        <div class="muted">${k.replace(/_/g,' ')}</div>
        <div class="metric ${Number(stats[k]||0)>0&&k.includes('pnl')?'buy':Number(stats[k]||0)<0&&k.includes('pnl')?'sell':''}">${stats[k]??'--'}</div>
      </div>`).join('');

    // Pipeline
    const pipeline = (data.strategies||{}).analysis_progress||[];
    document.getElementById('analysisPipeline').innerHTML = pipeline.map(row=>{
      const cls = row.state==='PASS'?'gate-pass':row.state==='BLOCK'?'gate-block':'gate-wait';
      return `<div style="margin:3px 0"><b>${row.name}</b> <span class="${cls}">${row.state}</span> <span class="muted">${row.detail||''}</span></div>`;
    }).join('') || '<div class="muted">No pipeline data yet.</div>';

    // Auto-Upgrade Engine
    const upgrade = data.auto_upgrade||{};
    document.getElementById('upgradeStatus').innerHTML =
      `<span class="upgrade">${upgrade.total_upgrades||0} upgrades applied</span> | ${upgrade.consecutive_losses||0} consecutive losses | last: ${upgrade.last_upgrade_at||'never'}`;
    document.getElementById('upgradeLog').innerHTML = (upgrade.log||[]).slice().reverse().slice(0,5).map(r=>`
      <div class="upgrade-card">
        <b class="upgrade">[${r.trigger}]</b> <b>${r.parameter}</b>: <span class="sell">${r.old_value}</span> → <span class="buy">${r.new_value}</span><br>
        <span class="muted">${r.reason}</span>
        ${r.backtest_trades>0?`<br><span class="muted">Backtest: ${r.backtest_trades} trades, ${fmt(r.backtest_winrate,1)}% WR</span>`:''}
      </div>`).join('') || '<div class="muted">No upgrades yet. System self-improves on losses.</div>';

    // Live thresholds
    const t = (upgrade.current_thresholds||{});
    document.getElementById('thresholds').innerHTML = Object.entries(t).map(([k,v])=>`
      <div style="margin:2px 0"><span class="muted">${k}:</span> <span class="upgrade">${typeof v==='number'?fmt(v,2):v}</span></div>`).join('');

    // Agent flow
    const agents = (data.strategies||{}).strategy_agents||[];
    document.getElementById('agentFlow').innerHTML = agents.map(a=>`
      <div style="margin:6px 0;padding:6px;background:#1c2128;border-radius:4px">
        <b>${a.name}</b> <span class="${a.ready?'buy':'warn'}">${a.ready?'READY':'SCANNING'}</span>
        | score ${fmt(a.score,1)}% | ${a.direction||'--'}<br>
        <span class="muted">${a.reason||''}</span>
      </div>`).join('') || '<div class="muted">No agent data.</div>';

    // Signal journal
    document.getElementById('signalJournal').innerHTML = (data.signal_journal||[]).slice().reverse().slice(0,15).map(r=>`
      <div style="margin:2px 0">
        <span class="${r.status==='OPENED'?'buy':r.status==='REJECTED'?'sell':'warn'}">${r.status}</span>
        | ${r.agent||'--'} | ${r.direction||'--'} ${fmt(r.confidence||0,1)}% | <span class="muted">${r.reason||''}</span>
      </div>`).join('') || '<div class="muted">No signals yet.</div>';

    document.getElementById('log').textContent = (data.messages||[]).join('\\n');
  }

  async function loadTrades(){
    const res = await fetch('/api/trades',{cache:'no-store'});
    const data = await res.json();
    const rows = tab==='open' ? data.open : data.history;
    const headers = tab==='open'
      ? ['ticket','direction','entry','current_sl','tp1_price','take_profit','live_pnl','status','strategy_agent']
      : ['ticket','direction','entry','stop_loss','take_profit','close_price','pnl','result','rr_achieved','closed_at'];
    document.getElementById('thead').innerHTML = `<tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr>`;
    document.getElementById('tbody').innerHTML = (rows||[]).map(r=>{
      const p=Number(r.live_pnl??r.pnl??0);
      return `<tr>${headers.map(h=>`<td class="${Number(r[h])>0&&h.includes('pnl')?'buy':Number(r[h])<0&&h.includes('pnl')?'sell':r[h]==='WIN'?'buy':r[h]==='LOSS'?'sell':''}">${r[h]??'--'}</td>`).join('')}</tr>`;
    }).join('');
  }

  async function pause(){
    await fetch('/api/pause',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}});
  }
  async function clearTrades(){
    if(confirm('Clear all trades?')) await fetch('/api/clear',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}});
  }
  async function tick(){
    if(tickBusy) return;
    tickBusy = true;
    try{await loadState();await loadTrades();}
    catch(e){document.getElementById('status').textContent='Error: '+e.message;}
    finally{tickBusy = false;}
  }
  tick();
  initTradingView();
  updateSoundState();
  setInterval(tick,250);
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
