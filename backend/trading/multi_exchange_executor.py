import logging
from typing import Optional, List, Dict, Tuple
from datetime import datetime
from ..models.trade import Trade, TradeDirection, TradeStatus
from ..models.position import Position, PositionStatus
from .trade_executor import TradeExecutor
from .position_manager import PositionManager

logger = logging.getLogger(__name__)


class MergedDictView:
    """Proxy view that merges multiple dicts — supports .clear(), .values(), .items(), .get()"""

    def __init__(self, dicts: List[Dict]):
        self._dicts = dicts

    def clear(self):
        for d in self._dicts:
            d.clear()

    def values(self):
        for d in self._dicts:
            yield from d.values()

    def items(self):
        for d in self._dicts:
            yield from d.items()

    def get(self, key, default=None):
        for d in self._dicts:
            if key in d:
                return d[key]
        return default

    def __len__(self):
        return sum(len(d) for d in self._dicts)

    def __iter__(self):
        for d in self._dicts:
            yield from d

    def __contains__(self, key):
        return any(key in d for d in self._dicts)


class MultiExchangeExecutor:
    """
    Drop-in replacement for TradeExecutor.
    Opens positions on ALL configured exchanges with sufficient balance.
    Routes close/partial_close/update_stop_loss to the right exchange via trade.exchange.
    """

    def __init__(self, executors: List[Tuple[str, TradeExecutor]]):
        # [(exchange_name, TradeExecutor), ...]
        self.executors = executors
        self._executor_map: Dict[str, TradeExecutor] = {name: ex for name, ex in executors}
        self._secondary_trades: List[Tuple[str, Trade]] = []  # picked up by MultiPositionManager

        # Expose attributes that trading_engine might check
        primary = executors[0][1] if executors else None
        self.client = primary.client if primary else None
        self.risk_manager = primary.risk_manager if primary else None
        self.symbol = "BTCUSDT"

    def open_position(self, direction, entry_price, stop_loss, alert_id, order_type="Market"):
        """Open on all exchanges with balance. Returns primary trade."""
        primary_trade = None
        self._secondary_trades.clear()

        for name, executor in self.executors:
            try:
                trade = executor.open_position(direction, entry_price, stop_loss, alert_id, order_type)
                if trade:
                    trade.exchange = name
                    if primary_trade is None:
                        primary_trade = trade
                        logger.info(f"[{name}] Primary position opened")
                    else:
                        self._secondary_trades.append((name, trade))
                        logger.info(f"[{name}] Secondary position opened")
            except Exception as e:
                logger.error(f"[{name}] Failed to open position: {e}")

        return primary_trade

    def close_position(self, trade, close_price=None, reason="manual"):
        ex = self._executor_map.get(getattr(trade, 'exchange', None)) or self.executors[0][1]
        return ex.close_position(trade, close_price, reason)

    def partial_close(self, trade, percent, close_price=None):
        ex = self._executor_map.get(getattr(trade, 'exchange', None)) or self.executors[0][1]
        return ex.partial_close(trade, percent, close_price)

    def update_stop_loss(self, trade, new_stop):
        ex = self._executor_map.get(getattr(trade, 'exchange', None)) or self.executors[0][1]
        return ex.update_stop_loss(trade, new_stop)

    def set_take_profit(self, trade, take_profit):
        ex = self._executor_map.get(getattr(trade, 'exchange', None)) or self.executors[0][1]
        return ex.set_take_profit(trade, take_profit)

    def enable_trailing_stop(self, trade, trailing_distance):
        ex = self._executor_map.get(getattr(trade, 'exchange', None)) or self.executors[0][1]
        return ex.enable_trailing_stop(trade, trailing_distance)

    def get_current_position(self):
        if self.executors:
            return self.executors[0][1].get_current_position()
        return None

    def sync_position_state(self, trade):
        ex = self._executor_map.get(getattr(trade, 'exchange', None)) or self.executors[0][1]
        return ex.sync_position_state(trade)

    def _calculate_take_profits(self, direction, entry_price, stop_loss):
        if self.executors:
            return self.executors[0][1]._calculate_take_profits(direction, entry_price, stop_loss)
        return entry_price, entry_price

    def get_all_balances(self) -> Dict[str, dict]:
        """Get balances from all exchanges. Used by UI balance endpoint."""
        result = {}
        for name, executor in self.executors:
            try:
                balance = executor.client.get_balance()
                if balance:
                    result[name] = balance
            except Exception as e:
                logger.debug(f"Balance fetch failed for {name}: {e}")
                result[name] = {"available_balance": 0, "total_balance": 0, "error": str(e)}
        return result


class MultiPositionManager:
    """
    Drop-in replacement for PositionManager.
    Creates and manages positions across all exchanges.
    """

    def __init__(self, managers: List[Tuple[str, PositionManager]], multi_executor: MultiExchangeExecutor):
        self.managers = managers
        self.multi_executor = multi_executor
        self._manager_map: Dict[str, PositionManager] = {name: pm for name, pm in managers}

    def _get_manager(self, exchange: str) -> PositionManager:
        return self._manager_map.get(exchange, self.managers[0][1])

    def create_position(self, trade: Trade) -> Position:
        """Create position for primary trade + secondary trades from multi_executor."""
        primary_pm = self._get_manager(getattr(trade, 'exchange', self.managers[0][0]))
        position = primary_pm.create_position(trade)
        position.exchange = getattr(trade, 'exchange', self.managers[0][0])

        # Create positions for secondary exchanges
        for name, sec_trade in self.multi_executor._secondary_trades:
            manager = self._manager_map.get(name)
            if manager:
                try:
                    sec_position = manager.create_position(sec_trade)
                    sec_position.exchange = name
                    logger.info(f"[{name}] Secondary position tracked")
                except Exception as e:
                    logger.error(f"[{name}] Failed to create secondary position: {e}")

        self.multi_executor._secondary_trades.clear()
        return position

    @property
    def positions(self) -> MergedDictView:
        return MergedDictView([pm.positions for _, pm in self.managers])

    @property
    def _trades(self) -> MergedDictView:
        return MergedDictView([pm._trades for _, pm in self.managers])

    def get_active_positions(self) -> List[Position]:
        result = []
        for _, pm in self.managers:
            result.extend(pm.get_active_positions())
        return result

    def update_position(self, position: Position, current_price: float) -> Position:
        pm = self._get_manager(getattr(position, 'exchange', self.managers[0][0]))
        return pm.update_position(position, current_price)

    def close_position(self, position: Position, reason: str = "manual") -> bool:
        pm = self._get_manager(getattr(position, 'exchange', self.managers[0][0]))
        return pm.close_position(position, reason)

    def get_position_by_trade_id(self, trade_id: int) -> Optional[Position]:
        for _, pm in self.managers:
            pos = pm.get_position_by_trade_id(trade_id)
            if pos:
                return pos
        return None

    def sync_with_exchange(self) -> None:
        for name, pm in self.managers:
            try:
                pm.sync_with_exchange()
            except Exception as e:
                logger.debug(f"Sync error on {name}: {e}")

    def get_total_unrealized_pnl(self) -> float:
        return sum(pm.get_total_unrealized_pnl() for _, pm in self.managers)

    def get_total_realized_pnl(self) -> float:
        return sum(pm.get_total_realized_pnl() for _, pm in self.managers)
