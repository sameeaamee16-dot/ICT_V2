from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

import mysql.connector

from config import CONFIG, MySQLConfig
from logger import get_logger
from models import Direction, Signal, Trade, TradeStatus

log = get_logger(__name__)


class MySQLStore:
    def __init__(self, config: MySQLConfig = CONFIG.mysql) -> None:
        self.config = config
        self._ensure_database()
        self._ensure_tables()

    def _connect(self, database: str | None = None):
        return mysql.connector.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=database,
            autocommit=True,
        )

    def _ensure_database(self) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.config.database}`")

    def _ensure_tables(self) -> None:
        with self._connect(self.config.database) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    ticket BIGINT PRIMARY KEY,
                    symbol VARCHAR(32) NOT NULL,
                    direction VARCHAR(8) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    opened_at DATETIME(6) NOT NULL,
                    closed_at DATETIME(6) NULL,
                    entry DECIMAL(18, 5) NOT NULL,
                    stop_loss DECIMAL(18, 5) NOT NULL,
                    take_profit DECIMAL(18, 5) NOT NULL,
                    current_sl DECIMAL(18, 5) NULL,
                    current_tp DECIMAL(18, 5) NULL,
                    close_price DECIMAL(18, 5) NULL,
                    rr DECIMAL(12, 4) NOT NULL,
                    rr_achieved DECIMAL(12, 4) NOT NULL DEFAULT 0,
                    tp1_price DECIMAL(18, 5) NULL,
                    tp1_hit_at DATETIME(6) NULL,
                    partial_closed BOOLEAN NOT NULL DEFAULT FALSE,
                    confidence DECIMAL(8, 2) NOT NULL,
                    strength VARCHAR(32) NOT NULL,
                    lot_size DECIMAL(12, 4) NOT NULL,
                    pnl DECIMAL(18, 2) NOT NULL DEFAULT 0,
                    result VARCHAR(12) NULL,
                    concepts JSON NULL,
                    reason TEXT NULL,
                    metadata JSON NULL,
                    updated_at DATETIME(6) NOT NULL
                )
                """
            )
            for ddl in [
                "ALTER TABLE trades ADD COLUMN tp1_price DECIMAL(18, 5) NULL AFTER rr_achieved",
                "ALTER TABLE trades ADD COLUMN tp1_hit_at DATETIME(6) NULL AFTER tp1_price",
                "ALTER TABLE trades ADD COLUMN partial_closed BOOLEAN NOT NULL DEFAULT FALSE AFTER tp1_hit_at",
            ]:
                try:
                    cur.execute(ddl)
                except mysql.connector.Error as exc:
                    if exc.errno != 1060:
                        raise
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_events (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME(6) NOT NULL,
                    level VARCHAR(16) NOT NULL,
                    message TEXT NOT NULL,
                    payload JSON NULL
                )
                """
            )

    def upsert_trade(self, trade: Trade) -> None:
        sig = trade.signal
        result = None
        if trade.status == TradeStatus.CLOSED:
            result = "WIN" if trade.pnl > 0 else "LOSS" if trade.pnl < 0 else "BE"
        params = {
            "ticket": trade.ticket,
            "symbol": sig.symbol,
            "direction": sig.direction.value,
            "status": trade.status.value,
            "opened_at": self._dt(trade.opened_at),
            "closed_at": self._dt(trade.closed_at) if trade.closed_at else None,
            "entry": sig.entry,
            "stop_loss": sig.stop_loss,
            "take_profit": sig.take_profit,
            "current_sl": trade.current_sl,
            "current_tp": trade.current_tp,
            "close_price": trade.close_price,
            "rr": sig.rr,
            "rr_achieved": trade.rr_achieved,
            "tp1_price": trade.tp1_price,
            "tp1_hit_at": self._dt(trade.tp1_hit_at) if trade.tp1_hit_at else None,
            "partial_closed": trade.partial_closed,
            "confidence": sig.confidence,
            "strength": sig.strength,
            "lot_size": trade.lot_size,
            "pnl": trade.pnl,
            "result": result,
            "concepts": self._json(sig.concepts),
            "reason": sig.reason,
            "metadata": self._json(sig.metadata),
            "updated_at": self._dt(datetime.now(timezone.utc)),
        }
        columns = ", ".join(params)
        placeholders = ", ".join([f"%({key})s" for key in params])
        updates = ", ".join([f"{key}=VALUES({key})" for key in params if key != "ticket"])
        with self._connect(self.config.database) as conn:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO trades ({columns}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}",
                params,
            )

    def event(self, level: str, message: str, payload: Dict[str, Any] | None = None) -> None:
        with self._connect(self.config.database) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO bot_events (created_at, level, message, payload) VALUES (%s, %s, %s, %s)",
                (self._dt(datetime.now(timezone.utc)), level, message, self._json(payload or {})),
            )

    def signal_event(self, signal_status: str, signal_payload: Dict[str, Any], reason: str) -> None:
        self.event(
            "INFO" if signal_status == "OPENED" else "WARNING",
            f"Signal {signal_status}: {reason}",
            {"status": signal_status, "reason": reason, "signal": signal_payload},
        )

    def trades(self, status: str | None = None, limit: int = 200) -> List[Dict[str, Any]]:
        where = "WHERE status = %s" if status else ""
        args = (status, limit) if status else (limit,)
        with self._connect(self.config.database) as conn:
            cur = conn.cursor(dictionary=True)
            cur.execute(f"SELECT * FROM trades {where} ORDER BY opened_at DESC LIMIT %s", args)
            rows = cur.fetchall()
        for row in rows:
            for key in ["concepts", "metadata"]:
                if isinstance(row.get(key), str):
                    row[key] = json.loads(row[key])
        return rows

    def restore_open_trades(self) -> List[Trade]:
        rows = self.trades(status="OPEN", limit=200) + self.trades(status="PARTIAL", limit=200)
        return [self._row_to_trade(row) for row in rows]

    def closed_trade_objects(self, limit: int = 500) -> List[Trade]:
        rows = self.trades(status="CLOSED", limit=limit)
        return [self._row_to_trade(row) for row in rows]

    def next_ticket(self) -> int:
        with self._connect(self.config.database) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(MAX(ticket), 0) + 1 FROM trades")
            row = cur.fetchone()
        return int(row[0] if row else 1)

    def clear_trades(self) -> None:
        with self._connect(self.config.database) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM trades")
            cur.execute("DELETE FROM bot_events")

    def _row_to_trade(self, row: Dict[str, Any]) -> Trade:
        signal = Signal(
            direction=Direction(row["direction"]),
            symbol=row["symbol"],
            timeframe=str((row.get("metadata") or {}).get("timeframe", CONFIG.timeframes.primary)),
            timestamp=self._aware(row["opened_at"]),
            entry=float(row["entry"]),
            stop_loss=float(row["stop_loss"]),
            take_profit=float(row["take_profit"]),
            rr=float(row["rr"]),
            confidence=float(row["confidence"]),
            strength=row["strength"],
            concepts=row.get("concepts") or [],
            reason=row.get("reason") or "",
            metadata=row.get("metadata") or {},
        )
        return Trade(
            ticket=int(row["ticket"]),
            signal=signal,
            lot_size=float(row["lot_size"]),
            opened_at=self._aware(row["opened_at"]),
            status=TradeStatus(row["status"]),
            closed_at=self._aware(row["closed_at"]) if row.get("closed_at") else None,
            close_price=float(row["close_price"]) if row.get("close_price") is not None else None,
            pnl=float(row.get("pnl") or 0.0),
            rr_achieved=float(row.get("rr_achieved") or 0.0),
            partial_closed=bool(row.get("partial_closed")),
            tp1_price=float(row["tp1_price"]) if row.get("tp1_price") is not None else None,
            tp1_hit_at=self._aware(row["tp1_hit_at"]) if row.get("tp1_hit_at") else None,
            current_sl=float(row["current_sl"]) if row.get("current_sl") is not None else None,
            current_tp=float(row["current_tp"]) if row.get("current_tp") is not None else None,
        )

    def stats(self) -> Dict[str, Any]:
        with self._connect(self.config.database) as conn:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_closed,
                    SUM(result = 'WIN') AS wins,
                    SUM(result = 'LOSS') AS losses,
                    SUM(result = 'BE') AS breakeven,
                    COALESCE(SUM(pnl), 0) AS net_pnl,
                    COALESCE(SUM(CASE WHEN DATE(closed_at) = UTC_DATE() THEN pnl ELSE 0 END), 0) AS daily_pnl
                FROM trades
                WHERE status = 'CLOSED'
                """
            )
            row = cur.fetchone() or {}
            cur.execute("SELECT COUNT(*) AS open_count FROM trades WHERE status IN ('OPEN', 'PARTIAL')")
            open_row = cur.fetchone() or {}
        total = int(row.get("total_closed") or 0)
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        return {
            "open": int(open_row.get("open_count") or 0),
            "closed": total,
            "wins": wins,
            "losses": losses,
            "breakeven": int(row.get("breakeven") or 0),
            "winrate": round(wins / total * 100, 2) if total else 0.0,
            "net_pnl": float(row.get("net_pnl") or 0.0),
            "daily_pnl": float(row.get("daily_pnl") or 0.0),
        }

    def _dt(self, value: datetime) -> datetime:
        if value.tzinfo:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def _aware(self, value: datetime) -> datetime:
        if value.tzinfo:
            return value.astimezone(timezone.utc)
        return value.replace(tzinfo=timezone.utc)

    def _json(self, value: Any) -> str:
        return json.dumps(value, default=self._json_default)

    def _json_default(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        if hasattr(value, "value"):
            return value.value
        return str(value)
