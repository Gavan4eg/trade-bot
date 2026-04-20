import logging
from typing import Optional, List, Dict
from datetime import datetime
from ..models.trade import Trade, TradeDirection, TradeStatus
from ..models.position import Position, PositionStatus
from ..config import settings
from .bybit_client import BybitClient
from .trade_executor import TradeExecutor
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)


class PositionManager:
    """Manage open positions with TP/SL and trailing stop"""

    def __init__(
        self,
        bybit_client: BybitClient,
        trade_executor: TradeExecutor,
        risk_manager: RiskManager = None
    ):
        self.client = bybit_client
        self.executor = trade_executor
        self.risk_manager = risk_manager
        self.positions: Dict[int, Position] = {}
        self._trades: Dict[int, Trade] = {}  # trade_id → Trade (для TP/SL)

    def create_position(self, trade: Trade) -> Position:
        """Create position from executed trade"""
        trade_id = trade.id or 0
        position = Position(
            trade_id=trade_id,
            direction=trade.direction,
            initial_quantity=trade.executed_quantity or trade.quantity,
            current_quantity=trade.executed_quantity or trade.quantity,
            entry_price=trade.executed_price or trade.entry_price,
            stop_loss=trade.stop_loss,
            take_profit_1=trade.take_profit_1,
            take_profit_2=trade.take_profit_2,
            status=PositionStatus.OPEN
        )

        self.positions[trade_id] = position
        self._trades[trade_id] = trade
        logger.info(f"Position created for trade {trade.id}")

        return position

    def update_position(
        self,
        position: Position,
        current_price: float
    ) -> Position:
        """Update position with current price and check TP/SL"""
        position.current_price = current_price
        position.calculate_unrealized_pnl()

        if not position.tp1_filled:
            if self._is_tp_hit(position, position.take_profit_1, current_price):
                self._handle_tp1(position, current_price)

        elif not position.tp2_filled:
            if self._is_tp_hit(position, position.take_profit_2, current_price):
                self._handle_tp2(position, current_price)

        elif position.status == PositionStatus.TP2_HIT:
            self._check_trailing_activation(position, current_price)

        if position.status == PositionStatus.TRAILING:
            self._update_trailing_stop(position, current_price)
            if position.trailing_stop:
                hit = (
                    position.direction == TradeDirection.LONG and current_price <= position.trailing_stop
                ) or (
                    position.direction == TradeDirection.SHORT and current_price >= position.trailing_stop
                )
                if hit:
                    self._close_by_stop(position, current_price, reason="trailing_stop")

        if position.status in [PositionStatus.OPEN, PositionStatus.TP1_HIT, PositionStatus.TP2_HIT]:
            sl_hit = (
                position.direction == TradeDirection.LONG and current_price <= position.stop_loss
            ) or (
                position.direction == TradeDirection.SHORT and current_price >= position.stop_loss
            )
            if sl_hit:
                self._close_by_stop(position, current_price, reason="stop_loss")

        return position

    def _is_tp_hit(
        self,
        position: Position,
        tp_price: float,
        current_price: float
    ) -> bool:
        """Check if take profit level is hit"""
        if position.direction == TradeDirection.LONG:
            return current_price >= tp_price
        else:
            return current_price <= tp_price

    def _handle_tp1(
        self,
        position: Position,
        current_price: float
    ) -> None:
        """Handle TP1 hit - close partial position"""
        close_percent = settings.tp1_close_percent

        # Get associated trade
        trade = self._get_trade_for_position(position)
        if not trade:
            return

        if self.executor.partial_close(trade, close_percent, current_price):
            position.tp1_filled = True
            position.current_quantity = trade.executed_quantity
            position.status = PositionStatus.TP1_HIT
            position.realized_pnl = trade.realized_pnl

            logger.info(
                f"TP1 hit at {current_price}, closed {close_percent}% "
                f"of position, realized PnL: {position.realized_pnl:.2f}"
            )

            self._move_stop_to_breakeven(position, trade)

            if position.current_quantity <= 0:
                position.status = PositionStatus.CLOSED
                if self.risk_manager:
                    self.risk_manager.register_trade_close(position.direction.value, position.realized_pnl)

    def _handle_tp2(
        self,
        position: Position,
        current_price: float
    ) -> None:
        """Handle TP2 hit - close more of position"""
        close_percent = settings.tp2_close_percent

        trade = self._get_trade_for_position(position)
        if not trade:
            return

        if self.executor.partial_close(trade, close_percent, current_price):
            position.tp2_filled = True
            position.current_quantity = trade.executed_quantity
            position.status = PositionStatus.TP2_HIT
            position.realized_pnl = trade.realized_pnl

            logger.info(
                f"TP2 hit at {current_price}, closed {close_percent}% "
                f"of remaining position"
            )

            if position.current_quantity <= 0:
                position.status = PositionStatus.CLOSED
                if self.risk_manager:
                    self.risk_manager.register_trade_close(position.direction.value, position.realized_pnl)

    def _move_stop_to_breakeven(
        self,
        position: Position,
        trade: Trade
    ) -> None:
        """Move stop loss to breakeven after TP1"""
        buffer = position.entry_price * 0.001

        if position.direction == TradeDirection.LONG:
            new_stop = position.entry_price + buffer
        else:
            new_stop = position.entry_price - buffer

        if self.executor.update_stop_loss(trade, new_stop):
            position.stop_loss = new_stop
            logger.info(f"Stop moved to breakeven: {new_stop}")

    def _check_trailing_activation(
        self,
        position: Position,
        current_price: float
    ) -> None:
        """Check if trailing stop should be activated"""
        risk = abs(position.entry_price - position.stop_loss)
        activation_distance = risk * settings.trailing_activation_rr

        if position.direction == TradeDirection.LONG:
            profit_distance = current_price - position.entry_price
        else:
            profit_distance = position.entry_price - current_price

        if profit_distance >= activation_distance:
            position.status = PositionStatus.TRAILING
            position.trailing_stop = self._calculate_trailing_stop(
                position, current_price
            )
            logger.info(
                f"Trailing stop activated at {current_price}, "
                f"trailing stop: {position.trailing_stop}"
            )

    def _calculate_trailing_stop(
        self,
        position: Position,
        current_price: float
    ) -> float:
        """Calculate trailing stop price"""
        trail_distance = current_price * (settings.trailing_step_percent / 100)

        if position.direction == TradeDirection.LONG:
            return round(current_price - trail_distance, 2)
        else:
            return round(current_price + trail_distance, 2)

    def _update_trailing_stop(
        self,
        position: Position,
        current_price: float
    ) -> None:
        """Update trailing stop as price moves in favor"""
        new_trail = self._calculate_trailing_stop(position, current_price)

        if position.direction == TradeDirection.LONG:
            if new_trail > (position.trailing_stop or 0):
                self._set_trailing_stop(position, new_trail)
        else:
            if position.trailing_stop is None or new_trail < position.trailing_stop:
                self._set_trailing_stop(position, new_trail)

    def _set_trailing_stop(
        self,
        position: Position,
        new_stop: float
    ) -> None:
        """Set new trailing stop"""
        trade = self._get_trade_for_position(position)
        if trade and self.executor.update_stop_loss(trade, new_stop):
            position.trailing_stop = new_stop
            position.stop_loss = new_stop
            logger.debug(f"Trailing stop updated to {new_stop}")

    def _close_by_stop(
        self,
        position: Position,
        current_price: float,
        reason: str = "stop_loss"
    ) -> None:
        """Закрыть остаток позиции по стопу (trailing или обычный)."""
        trade = self._get_trade_for_position(position)
        if not trade:
            return

        qty = trade.executed_quantity or trade.quantity
        if qty <= 0:
            return

        success = self.executor.close_position(trade, current_price, reason)
        if not success:
            logger.error(f"Failed to close position on exchange for {reason} — will retry on next tick")
            return

        if position.direction == TradeDirection.LONG:
            pnl = (current_price - position.entry_price) * qty
        else:
            pnl = (position.entry_price - current_price) * qty

        position.realized_pnl += pnl
        position.current_quantity = 0
        position.status = PositionStatus.CLOSED
        position.closed_at = datetime.utcnow()

        if self.risk_manager:
            self.risk_manager.register_trade_close(position.direction.value, pnl)

        logger.info(
            f"Position closed by {reason} at {current_price:.2f} | "
            f"PnL: ${pnl:+.2f} | Total realized: ${position.realized_pnl:+.2f}"
        )

    def _get_trade_for_position(
        self,
        position: Position
    ) -> Optional[Trade]:
        """Get trade object associated with position"""
        return self._trades.get(position.trade_id)

    def close_position(
        self,
        position: Position,
        reason: str = "manual"
    ) -> bool:
        """Close entire position"""
        trade = self._get_trade_for_position(position)
        if not trade:
            logger.error("Cannot find trade for position")
            return False

        if self.executor.close_position(trade, position.current_price, reason):
            position.status = PositionStatus.CLOSED
            position.closed_at = datetime.utcnow()
            position.realized_pnl = trade.realized_pnl

            if self.risk_manager:
                self.risk_manager.register_trade_close(position.direction.value, position.realized_pnl)

            if position.trade_id in self.positions:
                del self.positions[position.trade_id]

            logger.info(f"Position closed: {reason}, PnL: {position.realized_pnl:.2f}")
            return True
        return False

    def get_active_positions(self) -> List[Position]:
        """Get all active positions"""
        return [
            p for p in self.positions.values()
            if p.status not in [PositionStatus.CLOSED, PositionStatus.STOPPED]
        ]

    def get_position_by_trade_id(
        self,
        trade_id: int
    ) -> Optional[Position]:
        """Get position by trade ID"""
        return self.positions.get(trade_id)

    def sync_with_exchange(self) -> None:
        """Sync local positions with exchange state"""
        exchange_positions = self.client.get_positions(symbol="BTCUSDT")

        if not exchange_positions:
            for position in list(self.positions.values()):
                if position.status != PositionStatus.CLOSED:
                    position.status = PositionStatus.CLOSED
                    position.closed_at = datetime.utcnow()
                    # Notify risk manager so can_open_position() works correctly
                    if self.risk_manager:
                        self.risk_manager.register_trade_close(position.direction.value, position.realized_pnl)
            return

        for pos in exchange_positions:
            for local_pos in self.positions.values():
                if local_pos.status == PositionStatus.CLOSED:
                    continue

                if (
                    (local_pos.direction == TradeDirection.LONG and pos["side"] == "Buy") or
                    (local_pos.direction == TradeDirection.SHORT and pos["side"] == "Sell")
                ):
                    local_pos.current_quantity = pos["size"]
                    local_pos.current_price = pos["mark_price"]
                    local_pos.unrealized_pnl = pos["unrealized_pnl"]

                    if pos.get("stop_loss"):
                        local_pos.stop_loss = pos["stop_loss"]

    def get_total_unrealized_pnl(self) -> float:
        """Get total unrealized P&L across all positions"""
        return sum(p.unrealized_pnl for p in self.get_active_positions())

    def get_total_realized_pnl(self) -> float:
        """Get total realized P&L across all positions"""
        return sum(p.realized_pnl for p in self.positions.values())
