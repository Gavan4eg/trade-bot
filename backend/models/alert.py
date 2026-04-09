from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum
from typing import Optional, List


class AlertType(str, Enum):
    BTC_DOUBLE_DIAMOND = "BTC Double Diamond"
    BTC_DIAMOND = "BTC Diamond"
    DIAMOND_TOP_LEVELS = "Diamond Top Levels"
    AGGREGATED_LIQUIDATION = "Aggregated Liquidation"


class AlertStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RANGE_DETECTED = "range_detected"
    WAITING_SWEEP = "waiting_sweep"
    SWEEP_DETECTED = "sweep_detected"
    CONFIRMED = "confirmed"
    TRADED = "traded"
    EXPIRED = "expired"
    REJECTED = "rejected"


# Signal priority (lower = higher priority)
ALERT_PRIORITY = {
    AlertType.BTC_DOUBLE_DIAMOND: 1,
    AlertType.BTC_DIAMOND: 2,
    AlertType.DIAMOND_TOP_LEVELS: 3,
    AlertType.AGGREGATED_LIQUIDATION: 4,
}


class Alert(BaseModel):
    id: Optional[int] = None
    alert_type: AlertType
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    price: float
    levels: List[float] = Field(default_factory=list)
    status: AlertStatus = AlertStatus.PENDING
    priority: int = Field(default=4)
    raw_data: Optional[dict] = None

    def __init__(self, **data):
        super().__init__(**data)
        if self.priority == 4 and self.alert_type in ALERT_PRIORITY:
            self.priority = ALERT_PRIORITY[self.alert_type]

    class Config:
        from_attributes = True


class AlertWebhook(BaseModel):
    """Schema for incoming webhook from trdr.io"""
    # Core alert fields
    type: Optional[str] = None       # Alert type (legacy)
    name: Optional[str] = None       # Alert name from trdr.io (maps to type)
    # Symbol / price
    symbol: str = "BTCUSDT"
    ticker: Optional[str] = None     # e.g. "BTCUSDT", "BTCUSD_PERP"
    base: Optional[str] = None       # e.g. "BTC"
    price: Optional[float] = None
    levels: Optional[List[float]] = None
    # Side: which side was liquidated ("long" | "short")
    side: Optional[str] = None
    # Timing
    timestamp: Optional[str] = None  # Legacy timestamp field
    time: Optional[str] = None       # trdr.io timestamp field
    timeframe: Optional[str] = None
    cooldown: Optional[int] = None
    continuation: Optional[int] = None
    # Meta
    message: Optional[str] = None
    exchange: Optional[str] = None

    class Config:
        extra = "allow"

    def effective_type(self) -> Optional[str]:
        return self.type or self.name

    def effective_timestamp(self) -> Optional[str]:
        return self.timestamp or self.time
