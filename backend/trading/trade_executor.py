import logging
from typing import Optional, List, Tuple
from datetime import datetime
from ..models.trade import Trade, TradeDirection, TradeStatus
from ..models.position import Position, PositionStatus
from ..config import settings
from .bybit_client import BybitClient
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Execute trades on Bybit exchange"""

    def __init__(
        self,
        bybit_client: BybitClient,
        risk_manager: RiskManager
    ):
        self.client = bybit_client
        self.risk_manager = risk_manager
        self.symbol = "BTCUSDT"

    def open_position(
        self,
        direction: TradeDirection,
        entry_price: float,
        stop_loss: float,
        alert_id: int,
        order_type: str = "Market"
    ) -> Optional[Trade]:
        """
        Open a new position

        Args:
            direction: LONG or SHORT
            entry_price: Desired entry price
            stop_loss: Stop loss price
            alert_id: Associated alert ID
            order_type: Market or Limit
        """
        # Check if we can open position
        if not self.risk_manager.can_open_position(direction.value):
            logger.warning("Cannot open position: risk limits exceeded")
            return None

        # Calculate position size
        balance = self.client.get_balance()
        if not balance:
            logger.error("Failed to get balance")
            return None

        quantity = self.risk_manager.calculate_position_size(
            balance=balance["available_balance"],
            entry_price=entry_price,
            stop_loss=stop_loss
        )

        if quantity <= 0:
            logger.error("Calculated position size is 0")
            return None

        # Calculate take profits
        tp1, tp2 = self._calculate_take_profits(
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss
        )

        # Create trade object
        trade = Trade(
            alert_id=alert_id,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            quantity=quantity,
            status=TradeStatus.PENDING
        )
        trade.risk_reward = trade.calculate_rr()

        # Validate RR
        if not self.risk_manager.is_valid_rr(trade.risk_reward):
            logger.warning(f"Trade rejected: RR {trade.risk_reward} below minimum")
            return None

        # Execute order
        side = "Buy" if direction == TradeDirection.LONG else "Sell"

        ticker = self.client.get_ticker()
        current_price = ticker["last_price"] if ticker else entry_price

        # Testnet has a circuit breaker on Market orders (ErrCode 30208) — use Limit there.
        # Paper trading and real mainnet: use Market (fills instantly).
        if self.client.testnet and not self.client.paper_trading:
            exec_price = round(current_price * 1.001, 2) if side == "Buy" else round(current_price * 0.999, 2)
            order_params = {"side": side, "qty": quantity, "order_type": "Limit", "price": exec_price}
        else:
            exec_price = current_price
            order_params = {"side": side, "qty": quantity, "order_type": "Market"}

        order = self.client.place_order(**order_params)

        if order:
            trade.order_id = order["order_id"]
            trade.status = TradeStatus.OPEN
            trade.opened_at = datetime.utcnow()
            trade.executed_price = exec_price
            trade.executed_quantity = quantity

            logger.info(
                f"Position opened: {direction.value} {quantity} BTC @ {exec_price}, "
                f"SL: {stop_loss}, TP1: {tp1}, TP2: {tp2}, RR: {trade.risk_reward}"
            )
            return trade
        else:
            logger.error("Failed to place order")
            return None

    def _calculate_take_profits(
        self,
        direction: TradeDirection,
        entry_price: float,
        stop_loss: float
    ) -> Tuple[float, float]:
        """Calculate TP1 and TP2 based on RR ratios"""
        risk = abs(entry_price - stop_loss)

        if direction == TradeDirection.LONG:
            tp1 = entry_price + (risk * settings.tp1_rr)
            tp2 = entry_price + (risk * settings.tp2_rr)
        else:
            tp1 = entry_price - (risk * settings.tp1_rr)
            tp2 = entry_price - (risk * settings.tp2_rr)

        return round(tp1, 2), round(tp2, 2)

    def close_position(
        self,
        trade: Trade,
        close_price: Optional[float] = None,
        reason: str = "manual"
    ) -> bool:
        """Close entire position"""
        if trade.status not in [TradeStatus.OPEN, TradeStatus.PARTIAL_CLOSE]:
            logger.warning(f"Cannot close trade {trade.id}: status is {trade.status}")
            return False

        side = "Sell" if trade.direction == TradeDirection.LONG else "Buy"
        quantity = trade.executed_quantity or trade.quantity

        order = self.client.place_order(
            side=side,
            qty=quantity,
            order_type="Market",
            reduce_only=True
        )

        if order:
            trade.status = TradeStatus.CLOSED
            trade.closed_at = datetime.utcnow()

            if close_price:
                if trade.direction == TradeDirection.LONG:
                    trade.realized_pnl = (close_price - trade.entry_price) * quantity
                else:
                    trade.realized_pnl = (trade.entry_price - close_price) * quantity

            logger.info(
                f"Position closed: {trade.direction.value} {quantity} BTC, "
                f"reason: {reason}, PnL: {trade.realized_pnl:.2f}"
            )
            return True
        else:
            logger.error(f"Failed to close position for trade {trade.id}")
            return False

    def partial_close(
        self,
        trade: Trade,
        percent: int,
        close_price: Optional[float] = None
    ) -> bool:
        """Close partial position (for TP1, TP2)"""
        if trade.status not in [TradeStatus.OPEN, TradeStatus.PARTIAL_CLOSE]:
            return False

        current_qty = trade.executed_quantity or trade.quantity
        close_qty = current_qty * (percent / 100)

        if close_qty <= 0:
            return False

        side = "Sell" if trade.direction == TradeDirection.LONG else "Buy"

        order = self.client.place_order(
            side=side,
            qty=close_qty,
            order_type="Market",
            reduce_only=True
        )

        if order:
            trade.status = TradeStatus.PARTIAL_CLOSE
            trade.executed_quantity = current_qty - close_qty

            if close_price:
                if trade.direction == TradeDirection.LONG:
                    pnl = (close_price - trade.entry_price) * close_qty
                else:
                    pnl = (trade.entry_price - close_price) * close_qty
                trade.realized_pnl += pnl

            logger.info(
                f"Partial close: {percent}% of position, "
                f"closed {close_qty} BTC, remaining {trade.executed_quantity}"
            )
            return True
        else:
            return False

    def update_stop_loss(
        self,
        trade: Trade,
        new_stop: float
    ) -> bool:
        """Update stop loss for position"""
        if trade.status not in [TradeStatus.OPEN, TradeStatus.PARTIAL_CLOSE]:
            return False

        success = self.client.set_trading_stop(
            symbol=self.symbol,
            stop_loss=new_stop
        )

        if success:
            trade.stop_loss = new_stop
            logger.info(f"Stop loss updated to {new_stop}")
            return True
        else:
            return False

    def set_take_profit(
        self,
        trade: Trade,
        take_profit: float
    ) -> bool:
        """Set take profit for position"""
        if trade.status not in [TradeStatus.OPEN, TradeStatus.PARTIAL_CLOSE]:
            return False

        success = self.client.set_trading_stop(
            symbol=self.symbol,
            take_profit=take_profit
        )

        if success:
            logger.info(f"Take profit set to {take_profit}")
            return True
        else:
            return False

    def enable_trailing_stop(
        self,
        trade: Trade,
        trailing_distance: float
    ) -> bool:
        """Enable trailing stop for position"""
        if trade.status not in [TradeStatus.OPEN, TradeStatus.PARTIAL_CLOSE]:
            return False

        success = self.client.set_trading_stop(
            symbol=self.symbol,
            trailing_stop=trailing_distance
        )

        if success:
            logger.info(f"Trailing stop enabled: {trailing_distance}")
            return True
        else:
            return False

    def get_current_position(self) -> Optional[dict]:
        """Get current open position for symbol"""
        positions = self.client.get_positions(symbol=self.symbol)
        return positions[0] if positions else None

    def sync_position_state(self, trade: Trade) -> Trade:
        """Sync trade state with actual position on exchange"""
        position = self.get_current_position()

        if not position:
            if trade.status in [TradeStatus.OPEN, TradeStatus.PARTIAL_CLOSE]:
                trade.status = TradeStatus.CLOSED
                trade.closed_at = datetime.utcnow()
            return trade

        trade.executed_quantity = position["size"]
        trade.unrealized_pnl = position["unrealized_pnl"]

        if position.get("stop_loss"):
            trade.stop_loss = position["stop_loss"]

        return trade
