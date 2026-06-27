from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Dict, List


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class LatencyTrace:
    """Monotonic latency marks for one market-tick to trade-submission path."""

    tick_time: float | None = None
    started_at: datetime = field(default_factory=utc_now)
    _start_perf: float = field(default_factory=perf_counter)
    marks: List[Dict[str, Any]] = field(default_factory=list)

    def mark(self, step: str, detail: str = "") -> None:
        now = perf_counter()
        elapsed_ms = (now - self._start_perf) * 1000
        previous_ms = self.marks[-1]["elapsed_ms"] if self.marks else 0.0
        self.marks.append(
            {
                "step": step,
                "detail": detail,
                "elapsed_ms": round(elapsed_ms, 3),
                "delta_ms": round(elapsed_ms - previous_ms, 3),
            }
        )

    def payload(self) -> Dict[str, Any]:
        total_ms = self.marks[-1]["elapsed_ms"] if self.marks else 0.0
        broker_tick_lag_ms = None
        if self.tick_time:
            broker_tick_lag_ms = round((utc_now().timestamp() - float(self.tick_time)) * 1000, 3)
        return {
            "started_at": self.started_at.isoformat(),
            "total_ms": round(total_ms, 3),
            "broker_tick_lag_ms": broker_tick_lag_ms,
            "marks": list(self.marks),
        }


@dataclass
class CountdownEvent:
    signal_id: str
    direction: str
    symbol: str
    detected_at: datetime
    execute_at: datetime
    seconds: int
    mode: str
    status: str = "scheduled"

    def payload(self) -> Dict[str, Any]:
        now = utc_now()
        remaining = max(0.0, (self.execute_at - now).total_seconds())
        return {
            "signal_id": self.signal_id,
            "direction": self.direction,
            "symbol": self.symbol,
            "detected_at": self.detected_at.isoformat(),
            "execute_at": self.execute_at.isoformat(),
            "seconds": self.seconds,
            "remaining_seconds": round(remaining, 2),
            "mode": self.mode,
            "status": self.status,
        }
