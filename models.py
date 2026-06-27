from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from config import CONFIG


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class Zone:
    kind: str
    direction: Direction | str
    start_time: datetime
    end_time: datetime
    low: float
    high: float
    strength: float
    touched: bool = False
    mitigated: bool = False


@dataclass(frozen=True)
class IctSnapshot:
    timeframe: str
    timestamp: datetime
    bias: str
    trend_strength: float
    atr: float
    vwap: float
    ema_fast: float
    ema_slow: float
    concepts: List[str]
    zones: List[Zone]
    liquidity_levels: List[Zone]
    premium_discount: str
    displacement: Optional[str]
    mss: Optional[str]
    choch: Optional[str]
    bos: Optional[str]
    sweep: Optional[str]
    fvg: Optional[Zone]
    order_block: Optional[Zone]
    breaker_block: Optional[Zone]
    mitigation_block: Optional[Zone]
    rejection_block: Optional[Zone]
    metrics: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    direction: Direction
    symbol: str
    timeframe: str
    timestamp: datetime
    entry: float
    stop_loss: float
    take_profit: float
    rr: float
    confidence: float
    strength: str
    concepts: List[str]
    reason: str
    metadata: Dict[str, float | str]

    def format_terminal(self) -> str:
        detected = "\n".join(f"- {item}" for item in self.concepts)
        return (
            f"{self.direction.value} {self.symbol}\n"
            f"Entry: {self.entry:.2f}\n"
            f"SL: {self.stop_loss:.2f}\n"
            f"TP: {self.take_profit:.2f}\n"
            f"RR: 1:{self.rr:.2f}\n"
            f"Confidence: {self.confidence:.0f}%\n\n"
            f"Detected:\n{detected}\n\n"
            f"Reason:\n{self.reason}"
        )


@dataclass
class Trade:
    ticket: int
    signal: Signal
    lot_size: float
    opened_at: datetime
    status: TradeStatus = TradeStatus.OPEN
    closed_at: Optional[datetime] = None
    close_price: Optional[float] = None
    pnl: float = 0.0
    rr_achieved: float = 0.0
    partial_closed: bool = False
    tp1_price: Optional[float] = None
    tp1_hit_at: Optional[datetime] = None
    current_sl: Optional[float] = None
    current_tp: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.tp1_price is None:
            risk = abs(self.signal.entry - self.signal.stop_loss)
            partial_r = max(1.0, CONFIG.risk.partial_tp_at_r)
            self.tp1_price = self.signal.entry + risk * partial_r if self.signal.direction == Direction.BUY else self.signal.entry - risk * partial_r
        self.current_sl = self.signal.stop_loss if self.current_sl is None else self.current_sl
        self.current_tp = self.signal.take_profit if self.current_tp is None else self.current_tp
