import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
import pandas as pd
import numpy as np
from ..models.range import Range
from ..models.alert import Alert

logger = logging.getLogger(__name__)


class RangeDetector:
    """Detect local price range after alert signal"""

    def __init__(
        self,
        timeframe: str = "1h",
        candles: int = 24,
        min_width_percent: float = 0.5,
        max_width_percent: float = 5.0
    ):
        self.timeframe = timeframe
        self.candles = candles
        self.min_width_percent = min_width_percent
        self.max_width_percent = max_width_percent

    def detect_range(
        self,
        alert: Alert,
        candle_data: List[dict]
    ) -> Optional[Range]:
        """
        Detect local range from candle data after alert

        candle_data format:
        [{"open": float, "high": float, "low": float, "close": float, "timestamp": datetime}, ...]
        """
        if not candle_data or len(candle_data) < 2:
            logger.warning("Insufficient candle data for range detection")
            return None

        try:
            df = pd.DataFrame(candle_data)

            # Calculate local high and low
            local_high = df["high"].max()
            local_low = df["low"].min()

            # Get time boundaries
            start_time = df["timestamp"].min()
            end_time = df["timestamp"].max()

            # Create range object
            price_range = Range(
                alert_id=alert.id or 0,
                local_high=local_high,
                local_low=local_low,
                timeframe=self.timeframe,
                candles_used=len(candle_data),
                start_time=start_time,
                end_time=end_time
            )

            # Validate range
            price_range.is_valid = self.is_range_valid(price_range)

            logger.info(
                f"Range detected: High={local_high:.2f}, Low={local_low:.2f}, "
                f"Width={price_range.width_percent:.2f}%, Valid={price_range.is_valid}"
            )

            return price_range

        except Exception as e:
            logger.error(f"Range detection failed: {e}")
            return None

    def detect_range_from_prices(
        self,
        alert: Alert,
        highs: List[float],
        lows: List[float],
        timestamps: List[datetime]
    ) -> Optional[Range]:
        """Detect range from separate price arrays"""
        if not highs or not lows:
            return None

        local_high = max(highs)
        local_low = min(lows)

        price_range = Range(
            alert_id=alert.id or 0,
            local_high=local_high,
            local_low=local_low,
            timeframe=self.timeframe,
            candles_used=len(highs),
            start_time=min(timestamps) if timestamps else datetime.utcnow(),
            end_time=max(timestamps) if timestamps else datetime.utcnow()
        )

        price_range.is_valid = self.is_range_valid(price_range)
        return price_range

    def is_range_valid(self, price_range: Range) -> bool:
        """Check if range width is within acceptable bounds"""
        if price_range.width_percent < self.min_width_percent:
            logger.info(f"Range too narrow: {price_range.width_percent:.2f}%")
            return False

        if price_range.width_percent > self.max_width_percent:
            logger.info(f"Range too wide: {price_range.width_percent:.2f}%")
            return False

        return True

    def calculate_key_levels(
        self,
        price_range: Range
    ) -> dict:
        """Calculate key levels within the range"""
        high = price_range.local_high
        low = price_range.local_low
        mid = price_range.mid_range

        # Fibonacci levels
        fib_382 = low + (high - low) * 0.382
        fib_618 = low + (high - low) * 0.618

        # Quarter levels
        quarter_1 = low + (high - low) * 0.25
        quarter_3 = low + (high - low) * 0.75

        return {
            "high": high,
            "low": low,
            "mid": mid,
            "fib_382": round(fib_382, 2),
            "fib_618": round(fib_618, 2),
            "quarter_1": round(quarter_1, 2),
            "quarter_3": round(quarter_3, 2)
        }

    def update_range(
        self,
        price_range: Range,
        new_candle: dict
    ) -> Range:
        """Update range with new candle data"""
        updated = False

        if new_candle["high"] > price_range.local_high:
            price_range.local_high = new_candle["high"]
            updated = True

        if new_candle["low"] < price_range.local_low:
            price_range.local_low = new_candle["low"]
            updated = True

        if updated:
            price_range.calculate_metrics()
            price_range.is_valid = self.is_range_valid(price_range)
            price_range.end_time = new_candle.get("timestamp", datetime.utcnow())

        return price_range

    def identify_swing_points(
        self,
        candle_data: List[dict],
        lookback: int = 3
    ) -> Tuple[List[dict], List[dict]]:
        """Identify swing highs and lows"""
        swing_highs = []
        swing_lows = []

        if len(candle_data) < lookback * 2 + 1:
            return swing_highs, swing_lows

        for i in range(lookback, len(candle_data) - lookback):
            # Check for swing high
            is_swing_high = all(
                candle_data[i]["high"] >= candle_data[i - j]["high"] and
                candle_data[i]["high"] >= candle_data[i + j]["high"]
                for j in range(1, lookback + 1)
            )
            if is_swing_high:
                swing_highs.append({
                    "price": candle_data[i]["high"],
                    "timestamp": candle_data[i]["timestamp"],
                    "index": i
                })

            # Check for swing low
            is_swing_low = all(
                candle_data[i]["low"] <= candle_data[i - j]["low"] and
                candle_data[i]["low"] <= candle_data[i + j]["low"]
                for j in range(1, lookback + 1)
            )
            if is_swing_low:
                swing_lows.append({
                    "price": candle_data[i]["low"],
                    "timestamp": candle_data[i]["timestamp"],
                    "index": i
                })

        return swing_highs, swing_lows

    def get_range_status(
        self,
        price_range: Range,
        current_price: float
    ) -> str:
        """Get current price position relative to range"""
        if price_range.is_price_above_range(current_price):
            return "above_range"
        elif price_range.is_price_below_range(current_price):
            return "below_range"
        elif current_price > price_range.mid_range:
            return "upper_half"
        else:
            return "lower_half"
