from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum
from typing import Optional
from .trade import TradeDirection


class PositionStatus(str, Enum):
    OPEN = "open"
    TP1_HIT = "tp1_hit"
    TP2_HIT = "tp2_hit"
    TRAILING = "trailing"
    CLOSED = "closed"
    STOPPED = "stopped"


class Position(BaseModel):
    id: Optional[int] = None
    trade_id: int
    direction: TradeDirection

    # Position sizing
    initial_quantity: float
    current_quantity: float

    # Price levels
    entry_price: float
    current_price: float = 0.0
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    trailing_stop: Optional[float] = None

    # Status tracking
    status: PositionStatus = PositionStatus.OPEN
    tp1_filled: bool = False
    tp2_filled: bool = False

    # P&L
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    # Timestamps
    opened_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None

    # Bybit order IDs
    position_id: Optional[str] = None
    stop_order_id: Optional[str] = None

    # Exchange this position is on
    exchange: str = "binance"

    tp1_order_id: Optional[str] = None
    tp2_order_id: Optional[str] = None

    class Config:
        from_attributes = True

    def calculate_unrealized_pnl(self) -> float:
        """Calculate current unrealized P&L"""
        if self.direction == TradeDirection.LONG:
            pnl = (self.current_price - self.entry_price) * self.current_quantity
        else:
            pnl = (self.entry_price - self.current_price) * self.current_quantity
        self.unrealized_pnl = round(pnl, 2)
        return self.unrealized_pnl
