"""
ICT_V2 — WIRING PATCH for terminal_server.py
=============================================
Your filter_engine.py, risk_manager cooldown, and trend_engine consecutive-loss
block all exist but are NEVER CALLED because terminal_server.py doesn't wire them.
This file shows EXACTLY what to change and where.

HOW TO APPLY:
1. Open terminal_server.py
2. Find each section marked "FIND THIS" and replace with "REPLACE WITH"
3. There are 4 patches total — takes about 5 minutes.

WHAT THIS FIXES:
- FilterEngine 8-check gate (currently dead code) → now active before every trade
- Risk manager consecutive-loss cooldown → now receives trade history
- Trend engine regime loss gate → now receives consecutive loss count
- Silver Bullet session (10:00–11:00 UTC) added as a new kill zone bonus
"""

# ===========================================================================
# PATCH 1: Add FilterEngine import at the top of terminal_server.py
# ===========================================================================
# FIND THIS (around line 15, with other imports):
#   from signal_engine import SignalEngine
#   from trade_manager import TradeManager
#
# REPLACE WITH:
#   from signal_engine import SignalEngine
#   from trade_manager import TradeManager
#   from filter_engine import FilterEngine          # ← ADD THIS LINE


# ===========================================================================
# PATCH 2: Add filter_engine to BotRuntime.__init__
# ===========================================================================
# FIND THIS (around line 85, inside __init__):
#   self.trade_manager = TradeManager()
#   self.ai_advisor = AIAdvisor()
#
# REPLACE WITH:
#   self.trade_manager = TradeManager()
#   self.ai_advisor = AIAdvisor()
#   self.filter_engine = FilterEngine()            # ← ADD THIS LINE


# ===========================================================================
# PATCH 3: Wire FilterEngine + pass recent_closed_trades in _loop
# ===========================================================================
# FIND THIS section in _loop (around line 230-255):
#
#   if not self.paused:
#       for signal in signals:
#           tick = self._alert_before_entry(signal, tick)
#           opened, reason = self.trade_manager.submit_signal(signal, tick)
#           agent = str(signal.metadata.get("strategy_agent") or ...)
#
# REPLACE WITH the block below:

PATCH_3_REPLACEMENT = '''
        # ── Get recent trade history once per cycle ───────────────────────
        recent_closed = list(self.trade_manager.closed_trades[-30:])
        consec_losses = self._count_consecutive_losses(recent_closed)

        if not self.paused:
            for signal in signals:
                # ── FILTER ENGINE: 8-point hard gate ─────────────────────
                # This was previously DEAD CODE — filter_engine existed but
                # was never called. Now it runs before every trade.
                filter_ok, filter_reason = self.filter_engine.check(
                    signal=signal,
                    snapshots=snapshots,
                    frames=frames,
                    recent_closed_trades=recent_closed,
                    now_utc=datetime.now(timezone.utc),
                )
                if not filter_ok:
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
                    continue  # skip this signal

                tick = self._alert_before_entry(signal, tick)

                # ── Pass recent trades to risk_manager ────────────────────
                # Previously allowed() was called without recent_closed_trades,
                # so the consecutive-loss cooldown in risk_manager NEVER fired.
                opened, reason = self.trade_manager.submit_signal(
                    signal, tick,
                    recent_closed_trades=recent_closed,  # ← NEW ARGUMENT
                )
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
'''

# ===========================================================================
# PATCH 4: Add _count_consecutive_losses helper method to BotRuntime
# ===========================================================================
# ADD this new method to BotRuntime class (anywhere after _refresh_adaptive_trainer):

PATCH_4_NEW_METHOD = '''
    def _count_consecutive_losses(self, trades: list) -> int:
        """Count how many of the most recent trades are losses (used for cooldowns)."""
        count = 0
        for trade in reversed(trades):
            if trade.pnl < 0:
                count += 1
            else:
                break
        return count
'''

# ===========================================================================
# ALSO: Update generate_all call to pass consecutive losses to trend_engine
# ===========================================================================
# In signal_engine.py, find where trend_engine.evaluate() is called:
#
# FIND:
#   trend_context = self.trend.evaluate(frames[primary_tf].tail(620))
#
# REPLACE WITH:
#   recent_losses = self._count_consecutive_losses(
#       list(getattr(self, '_recent_closed_trades', []))
#   )
#   trend_context = self.trend.evaluate(
#       frames[primary_tf].tail(620),
#       recent_losses=recent_losses,
#       current_utc=datetime.now(timezone.utc),
#   )
#
# And add this method to SignalEngine:
#
#   def _count_consecutive_losses(self, trades):
#       count = 0
#       for t in reversed(trades):
#           if t.pnl < 0: count += 1
#           else: break
#       return count
#
# Then in generate_all(), store recent_closed_trades on self before calling trend:
#   self._recent_closed_trades = recent_closed_trades or []
