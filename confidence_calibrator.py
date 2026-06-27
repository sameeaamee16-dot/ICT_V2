from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from config import CONFIG
from models import Signal, Trade


@dataclass(frozen=True)
class CalibrationDecision:
    bucket: str
    samples: int
    winrate: float
    reliable: bool
    note: str


@dataclass
class ConfidenceCalibrator:
    min_samples: int = CONFIG.risk.calibration_min_samples
    warn_winrate_pct: float = CONFIG.risk.calibration_warn_winrate_pct
    _buckets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _trained_trades: int = 0

    def train(self, trades: Iterable[Trade]) -> None:
        rows: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"samples": 0, "wins": 0, "losses": 0, "breakeven": 0, "net_pnl": 0.0, "winrate": 0.0})
        closed = [trade for trade in trades if trade.closed_at is not None]
        for trade in closed[-1000:]:
            bucket = self.bucket_for(float(trade.signal.confidence))
            row = rows[bucket]
            row["samples"] += 1
            row["wins"] += 1 if trade.pnl > 0 else 0
            row["losses"] += 1 if trade.pnl < 0 else 0
            row["breakeven"] += 1 if trade.pnl == 0 else 0
            row["net_pnl"] = round(float(row["net_pnl"]) + float(trade.pnl), 2)
        for row in rows.values():
            samples = int(row["samples"])
            row["winrate"] = round(int(row["wins"]) / samples * 100, 2) if samples else 0.0
            row["reliable"] = samples >= self.min_samples
        self._buckets = dict(sorted(rows.items()))
        self._trained_trades = len(closed)

    def annotate(self, signal: Signal) -> CalibrationDecision:
        bucket = self.bucket_for(float(signal.confidence))
        row = self._buckets.get(bucket)
        if not row:
            decision = CalibrationDecision(bucket, 0, 0.0, False, "No closed trades in this confidence bucket yet")
        else:
            samples = int(row["samples"])
            winrate = float(row["winrate"])
            reliable = bool(row.get("reliable"))
            if not reliable:
                note = f"Needs {self.min_samples - samples} more closed trade(s) before this bucket is reliable"
            elif winrate < self.warn_winrate_pct:
                note = f"Bucket under review: historical winrate {winrate:.1f}%"
            else:
                note = f"Bucket calibrated: historical winrate {winrate:.1f}%"
            decision = CalibrationDecision(bucket, samples, winrate, reliable, note)
        signal.metadata["confidence_bucket"] = decision.bucket
        signal.metadata["calibrated_samples"] = decision.samples
        signal.metadata["calibrated_winrate"] = decision.winrate
        signal.metadata["calibration_reliable"] = str(decision.reliable)
        signal.metadata["calibration_note"] = decision.note
        return decision

    def report(self) -> Dict[str, Any]:
        buckets: List[Dict[str, Any]] = []
        for bucket, row in self._buckets.items():
            buckets.append(
                {
                    "bucket": bucket,
                    "samples": int(row["samples"]),
                    "winrate": float(row["winrate"]),
                    "net_pnl": float(row["net_pnl"]),
                    "reliable": bool(row.get("reliable")),
                }
            )
        return {
            "trained_trades": self._trained_trades,
            "min_samples": self.min_samples,
            "warn_winrate_pct": self.warn_winrate_pct,
            "buckets": buckets,
        }

    @staticmethod
    def bucket_for(confidence: float) -> str:
        lower = int(confidence // 5 * 5)
        upper = lower + 4
        if lower >= 95:
            return "95-100"
        return f"{lower}-{upper}"
