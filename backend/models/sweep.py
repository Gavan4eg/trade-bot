from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum
from typing import Optional


class SweepDirection(str, Enum):
    HIGH = "high"  # Sweep above range -> potential SHORT
    LOW = "low"    # Sweep below range -> potential LONG


class SweepEvent(BaseModel):
    id: Optional[int] = None
    range_id: int
    alert_id: int

    direction: SweepDirection
    sweep_price: float
    level_swept: float  # The high/low that was swept

    # Sweep characteristics
    wick_size: float = 0.0  # How far price went beyond the level
    wick_percent: float = 0.0
    volume_spike: bool = False
    quick_reversal: bool = False

    # Confirmation
    price_back_in_range: bool = False
    confirmation_count: int = 0

    # Timestamps
    sweep_time: datetime = Field(default_factory=datetime.utcnow)
    confirmation_time: Optional[datetime] = None

    class Config:
        from_attributes = True

    def calculate_wick_metrics(self):
        """Calculate wick size and percentage"""
        if self.direction == SweepDirection.HIGH:
            self.wick_size = self.sweep_price - self.level_swept
        else:
            self.wick_size = self.level_swept - self.sweep_price

        if self.level_swept > 0:
            self.wick_percent = round(
                (self.wick_size / self.level_swept) * 100,
                3
            )
