import logging
from typing import Optional, List, Dict
from datetime import datetime, timedelta
from ..config import settings
from ..models.trade import TradeDirection

logger = logging.getLogger(__name__)


class RiskManager:
    """Manage trading risk and position limits"""

    def __init__(
        self,
        max_positions: int = None,
        risk_per_trade: float = None,
        min_rr: float = None
    ):
        self.max_positions = max_positions or settings.max_positions
        self.risk_per_trade = risk_per_trade or settings.risk_per_trade
        self.min_rr = min_rr or settings.min_rr

        # Track active positions by direction
        self.active_longs: int = 0
        self.active_shorts: int = 0

        # Track daily stats
        self.daily_trades: int = 0
        self.daily_pnl: float = 0.0
        self.last_reset: datetime = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # Cooldown tracking
        self.last_trade_time: Dict[str, datetime] = {}
        self.trade_cooldown_minutes: int = 5

    def calculate_position_size(
        self,
        balance: float,
        entry_price: float,
        stop_loss: float
    ) -> float:
        """
        Calculate position size based on risk percentage

        Args:
            balance: Available balance in USDT
            entry_price: Entry price
            stop_loss: Stop loss price

        Returns:
            Position size in BTC
        """
        # Calculate risk amount in USDT
        risk_amount = balance * (self.risk_per_trade / 100)

        # Calculate stop distance
        stop_distance = abs(entry_price - stop_loss)

        if stop_distance <= 0:
            logger.error("Invalid stop distance")
            return 0.0

        # Position size = risk amount / stop distance
        position_size = risk_amount / stop_distance

        # Round to appropriate decimals (BTC typically 3 decimals)
        position_size = round(position_size, 3)

        # Minimum position check
        min_position = 0.001  # Minimum 0.001 BTC on Bybit
        if position_size < min_position:
            logger.warning(
                f"Calculated position size {position_size} below minimum {min_position}"
            )
            return 0.0

        logger.info(
            f"Position size calculated: {position_size} BTC "
            f"(risk: ${risk_amount:.2f}, stop distance: ${stop_distance:.2f})"
        )

        return position_size

    def calculate_rr(
        self,
        entry: float,
        stop_loss: float,
        take_profit: float,
        direction: str = "long"
    ) -> float:
        """Calculate risk/reward ratio"""
        if direction == "long":
            risk = entry - stop_loss
            reward = take_profit - entry
        else:
            risk = stop_loss - entry
            reward = entry - take_profit

        if risk <= 0:
            return 0.0

        return round(reward / risk, 2)

    def is_valid_rr(self, rr: float) -> bool:
        """Check if RR meets minimum requirement"""
        is_valid = rr >= self.min_rr
        if not is_valid:
            logger.debug(f"RR {rr} below minimum {self.min_rr}")
        return is_valid

    def can_open_position(self, direction: str) -> bool:
        """Check if new position can be opened"""
        # Reset daily stats if new day
        self._check_daily_reset()

        # Check total position limit
        total_positions = self.active_longs + self.active_shorts
        if total_positions >= self.max_positions:
            logger.warning(
                f"Max positions reached: {total_positions}/{self.max_positions}"
            )
            return False

        # Check direction-specific limits (no duplicate positions in same direction)
        if direction == "long" and self.active_longs > 0:
            logger.warning("Already have an active long position")
            return False

        if direction == "short" and self.active_shorts > 0:
            logger.warning("Already have an active short position")
            return False

        # Check cooldown
        if not self._check_cooldown(direction):
            return False

        return True

    def _check_cooldown(self, direction: str) -> bool:
        """Check if cooldown period has passed"""
        if direction in self.last_trade_time:
            elapsed = datetime.utcnow() - self.last_trade_time[direction]
            if elapsed < timedelta(minutes=self.trade_cooldown_minutes):
                remaining = self.trade_cooldown_minutes - (elapsed.seconds // 60)
                logger.info(f"Trade cooldown active: {remaining}m remaining for {direction}")
                return False
        return True

    def register_trade_open(self, direction: str) -> None:
        """Register that a trade was opened"""
        if direction == "long":
            self.active_longs += 1
        else:
            self.active_shorts += 1

        self.last_trade_time[direction] = datetime.utcnow()
        self.daily_trades += 1

        logger.info(
            f"Trade registered: {direction}, "
            f"active longs: {self.active_longs}, active shorts: {self.active_shorts}"
        )

    def register_trade_close(self, direction: str, pnl: float) -> None:
        """Register that a trade was closed"""
        if direction == "long":
            self.active_longs = max(0, self.active_longs - 1)
        else:
            self.active_shorts = max(0, self.active_shorts - 1)

        self.daily_pnl += pnl

        logger.info(
            f"Trade closed: {direction}, PnL: {pnl:.2f}, "
            f"daily PnL: {self.daily_pnl:.2f}"
        )

    def _check_daily_reset(self) -> None:
        """Reset daily stats at midnight UTC"""
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if today_start > self.last_reset:
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.last_reset = today_start
            logger.info("Daily stats reset")

    def get_max_positions(self) -> int:
        """Get maximum allowed positions"""
        return self.max_positions

    def calculate_stop_from_imbalance(
        self,
        imbalance_zone: float,
        direction: str,
        buffer_percent: float = 0.1
    ) -> float:
        """Calculate stop loss based on imbalance/liquidation zone"""
        buffer = imbalance_zone * (buffer_percent / 100)

        if direction == "long":
            stop = imbalance_zone - buffer
        else:
            stop = imbalance_zone + buffer

        return round(stop, 2)

    def adjust_position_for_volatility(
        self,
        base_size: float,
        current_volatility: float,
        avg_volatility: float
    ) -> float:
        """Adjust position size based on volatility"""
        if avg_volatility <= 0:
            return base_size

        volatility_ratio = current_volatility / avg_volatility

        # Reduce size if volatility is high
        if volatility_ratio > 1.5:
            adjustment = 0.5  # Reduce to 50%
        elif volatility_ratio > 1.2:
            adjustment = 0.75  # Reduce to 75%
        else:
            adjustment = 1.0  # Full size

        adjusted_size = round(base_size * adjustment, 3)

        if adjusted_size != base_size:
            logger.info(
                f"Position adjusted for volatility: {base_size} -> {adjusted_size} "
                f"(volatility ratio: {volatility_ratio:.2f})"
            )

        return adjusted_size

    def get_daily_stats(self) -> dict:
        """Get daily trading statistics"""
        self._check_daily_reset()

        return {
            "daily_trades": self.daily_trades,
            "daily_pnl": self.daily_pnl,
            "active_longs": self.active_longs,
            "active_shorts": self.active_shorts,
            "total_active": self.active_longs + self.active_shorts,
            "max_positions": self.max_positions
        }

    def validate_trade_params(
        self,
        entry: float,
        stop_loss: float,
        take_profit: float,
        direction: str
    ) -> Dict[str, any]:
        """Validate trade parameters"""
        errors = []
        warnings = []

        # Check entry vs stop
        if direction == "long":
            if stop_loss >= entry:
                errors.append("Stop loss must be below entry for long")
            if take_profit <= entry:
                errors.append("Take profit must be above entry for long")
        else:
            if stop_loss <= entry:
                errors.append("Stop loss must be above entry for short")
            if take_profit >= entry:
                errors.append("Take profit must be below entry for short")

        # Check RR
        rr = self.calculate_rr(entry, stop_loss, take_profit, direction)
        if rr < self.min_rr:
            warnings.append(f"RR {rr} is below minimum {self.min_rr}")

        # Check stop distance
        stop_percent = abs(entry - stop_loss) / entry * 100
        if stop_percent > 5:
            warnings.append(f"Stop distance is {stop_percent:.1f}% from entry")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "rr": rr
        }
