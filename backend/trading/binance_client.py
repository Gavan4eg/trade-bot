import logging
import random
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from ..config import settings

logger = logging.getLogger(__name__)

# Bybit interval → Binance interval
INTERVAL_MAP = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "60": "1h", "120": "2h", "240": "4h", "360": "6h", "720": "12h",
    "D": "1d", "W": "1w", "M": "1M"
}


class BinanceClient:
    """
    Binance Futures client — тот же интерфейс что и BybitClient.
    Переключается через ENV: EXCHANGE=binance
    """

    SYMBOL = "BTCUSDT"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        testnet: bool = True,
        paper_trading: bool = None
    ):
        self.api_key = api_key or settings.binance_api_key
        self.api_secret = api_secret or settings.binance_api_secret
        self.testnet = testnet if testnet is not None else settings.bybit_testnet
        self.paper_trading = paper_trading if paper_trading is not None else settings.paper_trading

        # Paper trading state (такой же как у Bybit)
        self._paper_balance = 10000.0
        self._paper_positions: List[dict] = []
        self._paper_orders: List[dict] = []
        self._simulated_price = 65000.0

        if self.paper_trading:
            logger.info("BinanceClient initialized in PAPER TRADING mode")
            self.client = None
        else:
            from binance.client import Client
            self.client = Client(
                api_key=self.api_key,
                api_secret=self.api_secret,
                testnet=self.testnet
            )
            # Для фьючерсного тестнета нужен отдельный URL
            if self.testnet:
                self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
            logger.info(f"BinanceClient initialized (testnet={self.testnet})")

    def _to_binance_interval(self, interval: str) -> str:
        """Конвертирует Bybit формат интервала в Binance формат"""
        return INTERVAL_MAP.get(str(interval), "1h")

    def _simulate_price_movement(self) -> float:
        change = random.uniform(-0.002, 0.002)
        self._simulated_price *= (1 + change)
        return self._simulated_price

    def set_simulated_price(self, price: float) -> None:
        if self.paper_trading:
            self._simulated_price = price
            logger.info(f"[TEST] Simulated price set to {price}")

    # ──────────────────────────────────────────────────────────────────────────
    # TICKER
    # ──────────────────────────────────────────────────────────────────────────

    def get_ticker(self, symbol: str = None) -> Optional[dict]:
        """Получить текущую цену"""
        if self.paper_trading:
            price = self._simulate_price_movement()
            spread = price * 0.0001
            return {
                "symbol": symbol or self.SYMBOL,
                "last_price": price,
                "bid": price - spread,
                "ask": price + spread,
                "volume_24h": random.uniform(50000, 100000),
                "high_24h": price * 1.02,
                "low_24h": price * 0.98,
                "timestamp": datetime.utcnow()
            }

        try:
            symbol = symbol or self.SYMBOL
            ticker = self.client.futures_symbol_ticker(symbol=symbol)
            book = self.client.futures_order_book(symbol=symbol, limit=5)
            stats = self.client.futures_ticker(symbol=symbol)

            return {
                "symbol": symbol,
                "last_price": float(ticker["price"]),
                "bid": float(book["bids"][0][0]) if book["bids"] else float(ticker["price"]),
                "ask": float(book["asks"][0][0]) if book["asks"] else float(ticker["price"]),
                "volume_24h": float(stats["volume"]),
                "high_24h": float(stats["highPrice"]),
                "low_24h": float(stats["lowPrice"]),
                "timestamp": datetime.utcnow()
            }
        except Exception as e:
            logger.error(f"Error getting ticker: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # KLINES
    # ──────────────────────────────────────────────────────────────────────────

    def get_klines(
        self,
        interval: str = "60",
        limit: int = 24,
        symbol: str = None
    ) -> List[dict]:
        """Получить свечи"""
        if self.paper_trading:
            candles = []
            base_price = self._simulated_price
            now = datetime.utcnow()
            for i in range(limit):
                time_offset = timedelta(hours=limit - i - 1)
                timestamp = now - time_offset
                open_price = base_price * (1 + random.uniform(-0.0015, 0.0015))
                close_price = base_price * (1 + random.uniform(-0.0015, 0.0015))
                high_price = max(open_price, close_price) * (1 + random.uniform(0, 0.001))
                low_price = min(open_price, close_price) * (1 - random.uniform(0, 0.001))
                candles.append({
                    "timestamp": timestamp,
                    "open": round(open_price, 2),
                    "high": round(high_price, 2),
                    "low": round(low_price, 2),
                    "close": round(close_price, 2),
                    "volume": random.uniform(1000, 5000),
                    "turnover": random.uniform(50000000, 200000000)
                })
                base_price = close_price
            return candles

        try:
            symbol = symbol or self.SYMBOL
            binance_interval = self._to_binance_interval(interval)
            raw = self.client.futures_klines(
                symbol=symbol,
                interval=binance_interval,
                limit=limit
            )
            candles = []
            for item in raw:
                # [open_time, open, high, low, close, volume, close_time, ...]
                candles.append({
                    "timestamp": datetime.fromtimestamp(item[0] / 1000),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                    "turnover": float(item[7])  # quote asset volume
                })
            return candles
        except Exception as e:
            logger.error(f"Error getting klines: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────────────
    # BALANCE
    # ──────────────────────────────────────────────────────────────────────────

    def get_balance(self) -> Optional[dict]:
        """Получить баланс USDT фьючерсного аккаунта"""
        if self.paper_trading:
            unrealized_pnl = 0.0
            for pos in self._paper_positions:
                if pos["side"] == "Buy":
                    unrealized_pnl += (self._simulated_price - pos["entry_price"]) * pos["size"]
                else:
                    unrealized_pnl += (pos["entry_price"] - self._simulated_price) * pos["size"]
            return {
                "total_balance": self._paper_balance + unrealized_pnl,
                "available_balance": self._paper_balance,
                "unrealized_pnl": unrealized_pnl,
                "margin_used": sum(p["size"] * p["entry_price"] * 0.1 for p in self._paper_positions)
            }

        try:
            balances = self.client.futures_account_balance()
            for b in balances:
                if b["asset"] == "USDT":
                    return {
                        "total_balance": float(b["balance"]),
                        "available_balance": float(b["availableBalance"]),
                        "unrealized_pnl": float(b.get("crossUnPnl", 0)),
                        "margin_used": float(b["balance"]) - float(b["availableBalance"])
                    }
            logger.warning("USDT not found in Binance futures account")
            return None
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # ORDERS
    # ──────────────────────────────────────────────────────────────────────────

    def place_order(
        self,
        side: str,                          # "Buy" / "Sell"
        qty: float,
        order_type: str = "Market",         # "Market" / "Limit"
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
        symbol: str = None
    ) -> Optional[dict]:
        """Разместить ордер на Binance Futures"""
        if self.paper_trading:
            order_id = str(uuid.uuid4())[:8]
            exec_price = price if order_type == "Limit" else self._simulated_price
            order = {
                "order_id": f"PAPER-{order_id}",
                "order_link_id": f"paper_{order_id}",
                "symbol": symbol or self.SYMBOL,
                "side": side,
                "qty": qty,
                "order_type": order_type,
                "price": exec_price,
                "status": "Filled"
            }
            self._paper_orders.append(order)
            if not reduce_only:
                self._paper_positions.append({
                    "symbol": symbol or self.SYMBOL,
                    "side": side,
                    "size": qty,
                    "entry_price": exec_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit
                })
            logger.info(f"[PAPER] Order placed: {side} {qty} @ {exec_price}")
            return order

        try:
            symbol = symbol or self.SYMBOL

            # Binance использует BUY/SELL (верхний регистр)
            binance_side = "BUY" if side.upper() in ("BUY", "BUY") else "SELL"
            if side.lower() == "buy":
                binance_side = "BUY"
            elif side.lower() == "sell":
                binance_side = "SELL"

            binance_type = "MARKET" if order_type.lower() == "market" else "LIMIT"

            params: Dict[str, Any] = {
                "symbol": symbol,
                "side": binance_side,
                "type": binance_type,
                "quantity": qty,
            }

            if binance_type == "LIMIT":
                params["price"] = str(price)
                params["timeInForce"] = "GTC"

            if reduce_only:
                params["reduceOnly"] = "true"

            logger.info(f"Request → Binance futures_create_order: {params}")
            result = self.client.futures_create_order(**params)

            logger.info(f"Order placed: {side} {qty} {symbol} @ {order_type}")
            return {
                "order_id": str(result["orderId"]),
                "order_link_id": str(result.get("clientOrderId", "")),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "order_type": order_type,
                "price": price,
                "status": "created"
            }

        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None

    def cancel_order(self, order_id: str, symbol: str = None) -> bool:
        """Отменить ордер"""
        if self.paper_trading:
            logger.info(f"[PAPER] Order cancelled: {order_id}")
            return True

        try:
            symbol = symbol or self.SYMBOL
            self.client.futures_cancel_order(symbol=symbol, orderId=int(order_id))
            logger.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Error cancelling order: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # POSITIONS
    # ──────────────────────────────────────────────────────────────────────────

    def get_positions(self, symbol: str = None) -> List[dict]:
        """Получить открытые позиции"""
        if self.paper_trading:
            positions = []
            for pos in self._paper_positions:
                if symbol and pos["symbol"] != symbol:
                    continue
                if pos["side"] == "Buy":
                    unrealized = (self._simulated_price - pos["entry_price"]) * pos["size"]
                else:
                    unrealized = (pos["entry_price"] - self._simulated_price) * pos["size"]
                positions.append({
                    "symbol": pos["symbol"],
                    "side": pos["side"],
                    "size": pos["size"],
                    "entry_price": pos["entry_price"],
                    "mark_price": self._simulated_price,
                    "unrealized_pnl": round(unrealized, 2),
                    "leverage": "10",
                    "position_idx": 0,
                    "stop_loss": pos.get("stop_loss"),
                    "take_profit": pos.get("take_profit")
                })
            return positions

        try:
            params = {}
            if symbol:
                params["symbol"] = symbol
            raw = self.client.futures_position_information(**params)

            positions = []
            for pos in raw:
                size = float(pos["positionAmt"])
                if size == 0:
                    continue
                # Binance: positionAmt > 0 = Long, < 0 = Short
                side = "Buy" if size > 0 else "Sell"
                positions.append({
                    "symbol": pos["symbol"],
                    "side": side,
                    "size": abs(size),
                    "entry_price": float(pos["entryPrice"]),
                    "mark_price": float(pos["markPrice"]),
                    "unrealized_pnl": float(pos["unRealizedProfit"]),
                    "leverage": pos["leverage"],
                    "position_idx": 0,
                    "stop_loss": None,
                    "take_profit": None
                })
            return positions
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────────────
    # TP / SL
    # ──────────────────────────────────────────────────────────────────────────

    def set_trading_stop(
        self,
        symbol: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trailing_stop: Optional[float] = None,
        position_idx: int = 0
    ) -> bool:
        """
        Выставить SL/TP на Binance Futures.
        На Binance SL и TP — это отдельные STOP_MARKET / TAKE_PROFIT_MARKET ордера.
        """
        if self.paper_trading:
            for pos in self._paper_positions:
                if pos["symbol"] == symbol:
                    if stop_loss:
                        pos["stop_loss"] = stop_loss
                    if take_profit:
                        pos["take_profit"] = take_profit
            logger.info(f"[PAPER] Trading stop set for {symbol}")
            return True

        try:
            # Определяем сторону позиции чтобы понять какой side ставить на закрывающий ордер
            positions = self.get_positions(symbol=symbol)
            if not positions:
                logger.warning(f"No open position found for {symbol} to set SL/TP")
                return False

            pos = positions[0]
            # Если Long → SL/TP ставим как SELL. Если Short → BUY.
            close_side = "SELL" if pos["side"] == "Buy" else "BUY"

            success = True

            pos_size = str(round(pos["size"], 3))

            if stop_loss:
                try:
                    self.client.futures_create_order(
                        symbol=symbol,
                        side=close_side,
                        type="STOP_MARKET",
                        stopPrice=str(round(stop_loss, 2)),
                        quantity=pos_size,
                        reduceOnly="true"
                    )
                    logger.info(f"Stop loss set @ {stop_loss} for {symbol}")
                except Exception as e:
                    # -4120 = аккаунт требует Algo API — SL мониторится внутри бота
                    if "-4120" in str(e):
                        logger.warning(f"Exchange-side SL not supported on this account — using software SL (bot monitors price internally)")
                    else:
                        logger.error(f"Failed to set stop loss: {e}")
                        success = False

            if take_profit:
                try:
                    self.client.futures_create_order(
                        symbol=symbol,
                        side=close_side,
                        type="TAKE_PROFIT_MARKET",
                        stopPrice=str(round(take_profit, 2)),
                        quantity=pos_size,
                        reduceOnly="true"
                    )
                    logger.info(f"Take profit set @ {take_profit} for {symbol}")
                except Exception as e:
                    if "-4120" in str(e):
                        logger.warning(f"Exchange-side TP not supported on this account — using software TP")
                    else:
                        logger.error(f"Failed to set take profit: {e}")
                        success = False

            if trailing_stop:
                try:
                    self.client.futures_create_order(
                        symbol=symbol,
                        side=close_side,
                        type="TRAILING_STOP_MARKET",
                        callbackRate=str(round(trailing_stop, 2)),
                        quantity=pos_size,
                        reduceOnly="true"
                    )
                    logger.info(f"Trailing stop set @ {trailing_stop}% for {symbol}")
                except Exception as e:
                    if "-4120" in str(e):
                        logger.warning(f"Exchange-side trailing stop not supported — using software trailing stop")
                    else:
                        logger.error(f"Failed to set trailing stop: {e}")
                        success = False

            return success

        except Exception as e:
            logger.error(f"Error setting trading stop: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # LEVERAGE
    # ──────────────────────────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Установить плечо"""
        if self.paper_trading:
            logger.info(f"[PAPER] Leverage set to {leverage}x for {symbol}")
            return True

        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            logger.info(f"Leverage set to {leverage}x for {symbol}")
            return True
        except Exception as e:
            logger.error(f"Error setting leverage: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # HISTORY
    # ──────────────────────────────────────────────────────────────────────────

    def get_order_history(self, symbol: str = None, limit: int = 50) -> List[dict]:
        """История ордеров"""
        if self.paper_trading:
            return self._paper_orders[-limit:]

        try:
            params = {"limit": limit}
            if symbol:
                params["symbol"] = symbol
            raw = self.client.futures_get_all_orders(**params)
            orders = []
            for order in raw[-limit:]:
                orders.append({
                    "order_id": str(order["orderId"]),
                    "symbol": order["symbol"],
                    "side": order["side"].capitalize(),
                    "order_type": order["type"].capitalize(),
                    "qty": float(order["origQty"]),
                    "price": float(order.get("price", 0)),
                    "avg_price": float(order.get("avgPrice", 0)),
                    "status": order["status"],
                    "created_time": datetime.fromtimestamp(order["time"] / 1000)
                })
            return orders
        except Exception as e:
            logger.error(f"Error getting order history: {e}")
            return []

    def get_closed_pnl(self, symbol: str = None, limit: int = 50) -> List[dict]:
        """История закрытых позиций с PnL"""
        if self.paper_trading:
            return []

        try:
            params = {"incomeType": "REALIZED_PNL", "limit": limit}
            if symbol:
                params["symbol"] = symbol
            raw = self.client.futures_income_history(**params)
            records = []
            for item in raw:
                records.append({
                    "symbol": item["symbol"],
                    "side": "unknown",
                    "qty": 0,
                    "entry_price": 0,
                    "exit_price": 0,
                    "closed_pnl": float(item["income"]),
                    "created_time": datetime.fromtimestamp(item["time"] / 1000)
                })
            return records
        except Exception as e:
            logger.error(f"Error getting closed PnL: {e}")
            return []

    def close_paper_position(self, symbol: str = None) -> bool:
        """Закрыть бумажную позицию"""
        if not self.paper_trading:
            return False

        symbol = symbol or self.SYMBOL
        for i, pos in enumerate(self._paper_positions):
            if pos["symbol"] == symbol:
                if pos["side"] == "Buy":
                    pnl = (self._simulated_price - pos["entry_price"]) * pos["size"]
                else:
                    pnl = (pos["entry_price"] - self._simulated_price) * pos["size"]
                self._paper_balance += pnl
                self._paper_positions.pop(i)
                logger.info(f"[PAPER] Position closed, PnL: {pnl:.2f}")
                return True
        return False
