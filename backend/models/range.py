from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class Range(BaseModel):
    id: Optional[int] = None
    alert_id: int

    # Range boundaries
    local_high: float
    local_low: float
    mid_range: float = 0.0
    width_percent: float = 0.0

    # Detection parameters
    timeframe: str = "1h"
    candles_used: int = 24

    # Timestamps
    start_time: datetime
    end_time: datetime
    detected_at: datetime = Field(default_factory=datetime.utcnow)

    # Validation
    is_valid: bool = True

    class Config:
        from_attributes = True

    def __init__(self, **data):
        super().__init__(**data)
        self.calculate_metrics()

    def calculate_metrics(self):
        """Calculate mid-range and width percentage"""
        self.mid_range = (self.local_high + self.local_low) / 2
        if self.local_low > 0:
            self.width_percent = round(
                ((self.local_high - self.local_low) / self.local_low) * 100,
                2
            )

    def is_price_in_range(self, price: float) -> bool:
        """Check if price is within the range"""
        return self.local_low <= price <= self.local_high

    def is_price_above_range(self, price: float) -> bool:
        """Check if price is above the range"""
        return price > self.local_high

    def is_price_below_range(self, price: float) -> bool:
        """Check if price is below the range"""
        return price < self.local_low
