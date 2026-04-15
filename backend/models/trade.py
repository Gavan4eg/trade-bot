from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum
from typing import Optional, List


class TradeDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class TradeStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIAL_CLOSE = "partial_close"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class Trade(BaseModel):
    id: Optional[int] = None
    alert_id: int
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    quantity: float
    status: TradeStatus = TradeStatus.PENDING

    # Execution details
    order_id: Optional[str] = None
    executed_price: Optional[float] = None
    executed_quantity: Optional[float] = None

    # P&L tracking
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None

    # Risk/Reward
    risk_reward: float = 0.0

    # Exchange this trade was executed on
    exchange: str = "binance"

    class Config:
        from_attributes = True

    def calculate_rr(self) -> float:
        """Calculate risk/reward ratio"""
        if self.direction == TradeDirection.LONG:
            risk = self.entry_price - self.stop_loss
            reward = self.take_profit_1 - self.entry_price
        else:
            risk = self.stop_loss - self.entry_price
            reward = self.entry_price - self.take_profit_1

        if risk <= 0:
            return 0.0
        return round(reward / risk, 2)
