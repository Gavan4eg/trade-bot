import logging
from datetime import datetime, timedelta
from typing import Optional, List
from ..models.range import Range
from ..models.sweep import SweepEvent, SweepDirection

logger = logging.getLogger(__name__)


class LiquidityTracker:
    """Track liquidity sweeps and detect liquidity grabs"""

    def __init__(
        self,
        sweep_threshold_percent: float = 0.1,
        volume_spike_multiplier: float = 2.0,
        reversal_candles: int = 3
    ):
        self.sweep_threshold_percent = sweep_threshold_percent
        self.volume_spike_multiplier = volume_spike_multiplier
        self.reversal_candles = reversal_candles
        self.pending_sweeps: List[SweepEvent] = []

    def detect_sweep(
        self,
        current_price: float,
        price_range: Range,
        candle: Optional[dict] = None
    ) -> Optional[SweepEvent]:
        """
        Detect if current price has swept liquidity

        Returns SweepEvent if sweep detected, None otherwise
        """
        sweep = None

        if current_price > price_range.local_high:
            sweep = self._create_sweep_event(
                price_range=price_range,
                direction=SweepDirection.HIGH,
                sweep_price=current_price,
                level_swept=price_range.local_high
            )
            logger.info(
                f"High sweep detected: price {current_price:.2f} > "
                f"local high {price_range.local_high:.2f}"
            )

        elif current_price < price_range.local_low:
            sweep = self._create_sweep_event(
                price_range=price_range,
                direction=SweepDirection.LOW,
                sweep_price=current_price,
                level_swept=price_range.local_low
            )
            logger.info(
                f"Low sweep detected: price {current_price:.2f} < "
                f"local low {price_range.local_low:.2f}"
            )

        if sweep and candle:
            sweep = self._analyze_candle(sweep, candle)

        return sweep

    def _create_sweep_event(
        self,
        price_range: Range,
        direction: SweepDirection,
        sweep_price: float,
        level_swept: float
    ) -> SweepEvent:
        """Create a new sweep event"""
        sweep = SweepEvent(
            range_id=price_range.id or 0,
            alert_id=price_range.alert_id,
            direction=direction,
            sweep_price=sweep_price,
            level_swept=level_swept
        )
        sweep.calculate_wick_metrics()
        return sweep

    def _analyze_candle(
        self,
        sweep: SweepEvent,
        candle: dict
    ) -> SweepEvent:
        """Analyze candle characteristics for sweep quality"""
        open_price = candle.get("open", 0)
        close_price = candle.get("close", 0)
        high_price = candle.get("high", 0)
        low_price = candle.get("low", 0)

        candle_body = abs(close_price - open_price)
        candle_range = high_price - low_price

        if candle_range > 0:
            if sweep.direction == SweepDirection.HIGH:
                upper_wick = high_price - max(open_price, close_price)
                wick_ratio = upper_wick / candle_range
            else:
                lower_wick = min(open_price, close_price) - low_price
                wick_ratio = lower_wick / candle_range

            if wick_ratio > 0.5:
                sweep.quick_reversal = True

        return sweep

    def is_valid_sweep(
        self,
        sweep: SweepEvent
    ) -> bool:
        """Validate if sweep is significant enough"""
        if sweep.wick_percent < self.sweep_threshold_percent:
            logger.debug(f"Sweep wick too small: {sweep.wick_percent:.3f}%")
            return False

        return True

    def check_volume_spike(
        self,
        current_volume: float,
        avg_volume: float
    ) -> bool:
        """Check if volume spiked during sweep"""
        if avg_volume <= 0:
            return False

        spike = current_volume / avg_volume >= self.volume_spike_multiplier
        if spike:
            logger.info(
                f"Volume spike detected: {current_volume:.2f} vs avg {avg_volume:.2f}"
            )
        return spike

    def check_quick_reversal(
        self,
        candles: List[dict],
        sweep: SweepEvent
    ) -> bool:
        """
        Check if price reversed quickly after sweep

        Looks for price returning back in opposite direction within reversal_candles
        """
        if len(candles) < self.reversal_candles:
            return False

        recent = candles[-self.reversal_candles:]

        if sweep.direction == SweepDirection.HIGH:
            closes_declining = all(
                recent[i]["close"] < recent[i - 1]["high"]
                for i in range(1, len(recent))
            )
            return closes_declining

        else:
            closes_rising = all(
                recent[i]["close"] > recent[i - 1]["low"]
                for i in range(1, len(recent))
            )
            return closes_rising

    def check_price_back_in_range(
        self,
        current_price: float,
        price_range: Range,
        sweep: SweepEvent
    ) -> bool:
        """Check if price has returned back into the range after sweep"""
        in_range = price_range.is_price_in_range(current_price)

        if in_range:
            sweep.price_back_in_range = True
            logger.info(
                f"Price {current_price:.2f} returned to range "
                f"[{price_range.local_low:.2f} - {price_range.local_high:.2f}]"
            )

        return in_range

    def get_trade_direction(
        self,
        sweep: SweepEvent
    ) -> str:
        """
        Get trade direction based on sweep

        High sweep -> SHORT (price swept highs, expecting reversal down)
        Low sweep -> LONG (price swept lows, expecting reversal up)
        """
        if sweep.direction == SweepDirection.HIGH:
            return "short"
        else:
            return "long"

    def register_sweep(self, sweep: SweepEvent) -> None:
        """Register sweep for tracking"""
        self.pending_sweeps.append(sweep)
        logger.info(
            f"Registered {sweep.direction.value} sweep at {sweep.sweep_price:.2f}"
        )

    def get_pending_sweeps(
        self,
        alert_id: Optional[int] = None
    ) -> List[SweepEvent]:
        """Get pending sweeps, optionally filtered by alert_id"""
        if alert_id is None:
            return self.pending_sweeps

        return [s for s in self.pending_sweeps if s.alert_id == alert_id]

    def clear_sweep(self, sweep: SweepEvent) -> None:
        """Remove sweep from pending list"""
        if sweep in self.pending_sweeps:
            self.pending_sweeps.remove(sweep)

    def expire_old_sweeps(self, max_age_minutes: int = 60) -> int:
        """Remove sweeps older than max_age_minutes"""
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        original_count = len(self.pending_sweeps)

        self.pending_sweeps = [
            s for s in self.pending_sweeps
            if s.sweep_time > cutoff
        ]

        expired = original_count - len(self.pending_sweeps)
        if expired > 0:
            logger.info(f"Expired {expired} old sweeps")

        return expired
