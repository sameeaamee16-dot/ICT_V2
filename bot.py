from __future__ import annotations

import time
from datetime import datetime, timezone

from config import CONFIG
from data_feed import create_feed
from signal_engine import SignalEngine
from trade_manager import TradeManager


def main() -> None:
    feed = create_feed()
    signal_engine = SignalEngine()
    trade_manager = TradeManager()
    last_entry_at = datetime.now(timezone.utc)
    last_activity_attempt_at: datetime | None = None
    print(f"{CONFIG.data.symbol} ICT/Crypto Signal Bot started.")
    print("Mode: MT5 closed-candle analysis with virtual execution.")
    while True:
        try:
            frames = feed.get_multi_timeframe(CONFIG.data.history_bars)
            tick = feed.get_tick()
            primary = frames[CONFIG.timeframes.primary]
            snapshots = signal_engine.analyze(frames)
            if CONFIG.timeframes.primary not in snapshots:
                print("Waiting for enough closed candles...")
                time.sleep(CONFIG.data.poll_seconds)
                continue
            last = primary.iloc[-1]
            trade_manager.update(last.to_dict(), primary.index[-1].to_pydatetime())
            signals = signal_engine.generate_all(frames, tick)
            snap = snapshots[CONFIG.timeframes.primary]
            source = getattr(feed, "source_symbol", CONFIG.data.symbol) or CONFIG.data.symbol
            print(
                f"{primary.index[-1]} | {source} close={last['close']:.2f} "
                f"bias={snap.bias} concepts={len(snap.concepts)} open={len(trade_manager.open_trades)}"
            )
            for signal in signals:
                opened, reason = trade_manager.submit_signal(signal, tick)
                if opened:
                    last_entry_at = datetime.now(timezone.utc)
                    print(reason)
                else:
                    print(f"Rejected: {reason}")
            if CONFIG.data.minimum_activity_enabled and len(trade_manager.open_trades) < CONFIG.risk.max_concurrent_trades:
                now = datetime.now(timezone.utc)
                idle_minutes = (now - last_entry_at).total_seconds() / 60.0
                attempted_recently = last_activity_attempt_at and (now - last_activity_attempt_at).total_seconds() < 60
                if idle_minutes >= CONFIG.data.minimum_activity_minutes and not attempted_recently:
                    last_activity_attempt_at = now
                    fallback = signal_engine.activity_fallback_signal(frames, tick, idle_minutes)
                    if fallback:
                        opened, reason = trade_manager.submit_signal(fallback, tick)
                        if opened:
                            last_entry_at = now
                            print(f"Minimum activity fallback: {reason}")
                        else:
                            print(f"Minimum activity fallback: Rejected: {reason}")
                    else:
                        print(f"Minimum activity check: no fallback direction after {idle_minutes:.0f} idle minutes.")
            time.sleep(CONFIG.data.poll_seconds)
        except KeyboardInterrupt:
            print("Bot stopped.")
            break
        except Exception as exc:
            print(f"Data/analysis unavailable: {exc}")
            time.sleep(max(CONFIG.data.poll_seconds, 10))


if __name__ == "__main__":
    main()
