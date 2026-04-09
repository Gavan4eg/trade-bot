import logging
from datetime import datetime
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from ..models.range import Range
from ..models.sweep import SweepEvent, SweepDirection

logger = logging.getLogger(__name__)


@dataclass
class ConfirmationResult:
    """Result of confirmation checks"""
    is_confirmed: bool = False
    confirmations_met: int = 0
    required_confirmations: int = 2
    details: Dict[str, bool] = field(default_factory=dict)
    trade_direction: Optional[str] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


class ConfirmationEngine:
    """
    Confirm trade entry after liquidity sweep

    Entry is confirmed when multiple conditions are met:
    1. Price returned back into range
    2. Impulse candle in opposite direction
    3. Liquidation spike in sweep zone (if available)
    4. Volume confirmation
    """

    def __init__(
        self,
        min_confirmations: int = 2,
        impulse_body_ratio: float = 0.6,
        volume_confirmation_multiplier: float = 1.5
    ):
        self.min_confirmations = min_confirmations
        self.impulse_body_ratio = impulse_body_ratio
        self.volume_confirmation_multiplier = volume_confirmation_multiplier

    def check_confirmation(
        self,
        sweep: SweepEvent,
        price_range: Range,
        current_price: float,
        recent_candles: List[dict],
        volume_data: Optional[dict] = None,
        liquidation_data: Optional[dict] = None
    ) -> ConfirmationResult:
        """
        Check all confirmation conditions

        Args:
            sweep: The sweep event to confirm
            price_range: The detected price range
            current_price: Current market price
            recent_candles: Recent candle data for analysis
            volume_data: Optional volume/CVD data
            liquidation_data: Optional aggregated liquidation data
        """
        result = ConfirmationResult(
            required_confirmations=self.min_confirmations
        )

        # 1. Check price back in range (current price)
        in_range = self.price_back_in_range(current_price, price_range)
        result.details["price_in_range"] = in_range
        if in_range:
            result.confirmations_met += 1

        # 2. Check candle CLOSED inside range (stronger than just current price)
        if recent_candles:
            candle_closed = self.candle_closed_in_range(recent_candles[-1], price_range)
            result.details["candle_closed_in_range"] = candle_closed
            if candle_closed:
                result.confirmations_met += 1

        # 3. Check impulse reversal
        if recent_candles:
            impulse = self.impulse_reversal(recent_candles, sweep.direction)
            result.details["impulse_reversal"] = impulse
            if impulse:
                result.confirmations_met += 1

        # 4. Check liquidation spike
        if liquidation_data:
            liq_spike = self.liquidation_spike(liquidation_data, sweep)
            result.details["liquidation_spike"] = liq_spike
            if liq_spike:
                result.confirmations_met += 1

        # 5. Check volume confirmation
        if volume_data:
            vol_confirm = self.volume_confirmation(volume_data, sweep.direction)
            result.details["volume_confirmation"] = vol_confirm
            if vol_confirm:
                result.confirmations_met += 1

        # Determine if confirmed
        result.is_confirmed = result.confirmations_met >= self.min_confirmations

        if result.is_confirmed:
            # Set trade direction (opposite to sweep direction)
            if sweep.direction == SweepDirection.HIGH:
                result.trade_direction = "short"
                result.entry_price = current_price
                result.stop_loss = sweep.sweep_price * 1.001  # Slightly above sweep high
            else:
                result.trade_direction = "long"
                result.entry_price = current_price
                result.stop_loss = sweep.sweep_price * 0.999  # Slightly below sweep low

            logger.info(
                f"Confirmation achieved: {result.confirmations_met}/{self.min_confirmations} "
                f"conditions met. Direction: {result.trade_direction}"
            )
        else:
            logger.debug(
                f"Confirmation pending: {result.confirmations_met}/{self.min_confirmations} "
                f"conditions met"
            )

        return result

    def price_back_in_range(
        self,
        current_price: float,
        price_range: Range
    ) -> bool:
        """Check if price has returned to range after sweep"""
        return price_range.is_price_in_range(current_price)

    def candle_closed_in_range(
        self,
        candle: dict,
        price_range: Range
    ) -> bool:
        """
        Check that the last candle CLOSED inside the range.
        Stronger signal than just current price being in range —
        confirms the sweep rejection and return to range on a closed bar.
        """
        close = candle.get("close", 0)
        if not close:
            return False
        in_range = price_range.is_price_in_range(close)
        if in_range:
            logger.debug(f"Candle closed inside range at {close:.2f}")
        return in_range

    def impulse_reversal(
        self,
        candles: List[dict],
        sweep_direction: SweepDirection
    ) -> bool:
        """
        Check for impulse candle in opposite direction

        For high sweep: looking for bearish impulse (close < open with large body)
        For low sweep: looking for bullish impulse (close > open with large body)
        """
        if not candles:
            return False

        # Check last 2-3 candles for impulse
        check_candles = candles[-3:] if len(candles) >= 3 else candles

        for candle in check_candles:
            open_price = candle.get("open", 0)
            close_price = candle.get("close", 0)
            high_price = candle.get("high", 0)
            low_price = candle.get("low", 0)

            if high_price == low_price:
                continue

            body = abs(close_price - open_price)
            candle_range = high_price - low_price
            body_ratio = body / candle_range

            # Check if body is significant
            if body_ratio < self.impulse_body_ratio:
                continue

            # Check direction
            if sweep_direction == SweepDirection.HIGH:
                # After high sweep, looking for bearish candle
                if close_price < open_price:
                    logger.debug(f"Bearish impulse detected: body ratio {body_ratio:.2f}")
                    return True
            else:
                # After low sweep, looking for bullish candle
                if close_price > open_price:
                    logger.debug(f"Bullish impulse detected: body ratio {body_ratio:.2f}")
                    return True

        return False

    def liquidation_spike(
        self,
        liquidation_data: dict,
        sweep: SweepEvent
    ) -> bool:
        """
        Check if aggregated liquidations show spike in sweep zone

        liquidation_data format:
        {
            "long_liquidations": float,
            "short_liquidations": float,
            "avg_liquidations": float,
            "price_at_spike": float
        }
        """
        if not liquidation_data:
            return False

        avg = liquidation_data.get("avg_liquidations", 0)
        if avg <= 0:
            return False

        # For high sweep, check long liquidations spike
        if sweep.direction == SweepDirection.HIGH:
            long_liqs = liquidation_data.get("long_liquidations", 0)
            if long_liqs > avg * 2:
                logger.debug(f"Long liquidation spike: {long_liqs:.2f} vs avg {avg:.2f}")
                return True

        # For low sweep, check short liquidations spike
        else:
            short_liqs = liquidation_data.get("short_liquidations", 0)
            if short_liqs > avg * 2:
                logger.debug(f"Short liquidation spike: {short_liqs:.2f} vs avg {avg:.2f}")
                return True

        return False

    def volume_confirmation(
        self,
        volume_data: dict,
        sweep_direction: SweepDirection
    ) -> bool:
        """
        Check volume/CVD for confirmation

        volume_data format:
        {
            "current_volume": float,
            "avg_volume": float,
            "cvd": float,  # Cumulative Volume Delta
            "delta": float  # Buy volume - Sell volume
        }
        """
        if not volume_data:
            return False

        current = volume_data.get("current_volume", 0)
        avg = volume_data.get("avg_volume", 0)
        delta = volume_data.get("delta", 0)

        # Volume should be elevated
        if avg > 0 and current < avg * self.volume_confirmation_multiplier:
            return False

        # Check delta direction
        if sweep_direction == SweepDirection.HIGH:
            # After high sweep, expecting selling pressure (negative delta)
            if delta < 0:
                logger.debug(f"Volume confirmation: negative delta {delta:.2f}")
                return True
        else:
            # After low sweep, expecting buying pressure (positive delta)
            if delta > 0:
                logger.debug(f"Volume confirmation: positive delta {delta:.2f}")
                return True

        return False

    def get_entry_zone(
        self,
        sweep: SweepEvent,
        price_range: Range,
        current_price: float
    ) -> dict:
        """Calculate optimal entry zone"""
        if sweep.direction == SweepDirection.HIGH:
            # For short after high sweep
            entry_zone = {
                "optimal": price_range.local_high,
                "aggressive": current_price,
                "conservative": price_range.mid_range,
                "direction": "short"
            }
        else:
            # For long after low sweep
            entry_zone = {
                "optimal": price_range.local_low,
                "aggressive": current_price,
                "conservative": price_range.mid_range,
                "direction": "long"
            }

        return entry_zone

    def calculate_stop_loss(
        self,
        sweep: SweepEvent,
        imbalance_zone: Optional[float] = None,
        buffer_percent: float = 0.1
    ) -> float:
        """
        Calculate stop loss price

        Uses imbalance zone if available, otherwise uses sweep price
        """
        if imbalance_zone:
            base_price = imbalance_zone
        else:
            base_price = sweep.sweep_price

        buffer = base_price * (buffer_percent / 100)

        if sweep.direction == SweepDirection.HIGH:
            # For short, stop above sweep high
            return round(base_price + buffer, 2)
        else:
            # For long, stop below sweep low
            return round(base_price - buffer, 2)
