import logging
import random
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from ..config import settings

logger = logging.getLogger(__name__)


class BybitClient:
    """Client for Bybit API interactions with Paper Trading support"""

    SYMBOL = "BTCUSDT"
    CATEGORY = "linear"  # USDT perpetual

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        testnet: bool = True,
        paper_trading: bool = None
    ):
        self.api_key = api_key or settings.bybit_api_key
        self.api_secret = api_secret or settings.bybit_api_secret
        self.testnet = testnet if testnet is not None else settings.bybit_testnet
        self.paper_trading = paper_trading if paper_trading is not None else settings.paper_trading

        # Paper trading state
        self._paper_balance = 10000.0  # Starting balance
        self._paper_positions: List[dict] = []
        self._paper_orders: List[dict] = []
        self._simulated_price = 65000.0  # Starting BTC price

        if self.paper_trading:
            logger.info("BybitClient initialized in PAPER TRADING mode")
            self.client = None
        else:
            from pybit.unified_trading import HTTP
            self.client = HTTP(
                testnet=self.testnet,
                api_key=self.api_key,
                api_secret=self.api_secret,
            )
            logger.info(f"BybitClient initialized (testnet={self.testnet})")

    def _simulate_price_movement(self) -> float:
        """Simulate realistic price movement"""
        change = random.uniform(-0.002, 0.002)  # ±0.2% movement
        self._simulated_price *= (1 + change)
        return self._simulated_price

    def set_simulated_price(self, price: float) -> None:
        """Принудительно задать цену (только для тестов в paper trading)"""
        if self.paper_trading:
            self._simulated_price = price
            logger.info(f"[TEST] Simulated price set to {price}")

    def get_ticker(self, symbol: str = None) -> Optional[dict]:
        """Get current ticker data"""
        if self.paper_trading:
            price = self._simulate_price_movement()
            spread = price * 0.0001  # 0.01% spread
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
            response = self.client.get_tickers(
                category=self.CATEGORY,
                symbol=symbol
            )

            if response["retCode"] == 0:
                ticker = response["result"]["list"][0]
                return {
                    "symbol": ticker["symbol"],
                    "last_price": float(ticker["lastPrice"]),
                    "bid": float(ticker["bid1Price"]),
                    "ask": float(ticker["ask1Price"]),
                    "volume_24h": float(ticker["volume24h"]),
                    "high_24h": float(ticker["highPrice24h"]),
                    "low_24h": float(ticker["lowPrice24h"]),
                    "timestamp": datetime.utcnow()
                }
            else:
                logger.error(f"Failed to get ticker: {response['retMsg']}")
                return None

        except Exception as e:
            logger.error(f"Error getting ticker: {e}")
            return None

    def get_klines(
        self,
        interval: str = "60",
        limit: int = 24,
        symbol: str = None
    ) -> List[dict]:
        """Get kline/candlestick data"""
        if self.paper_trading:
            candles = []
            base_price = self._simulated_price
            now = datetime.utcnow()

            # Generate realistic candles — tight range so RangeDetector accepts them
            # ±0.15% per candle keeps total width well under 5%
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
            response = self.client.get_kline(
                category=self.CATEGORY,
                symbol=symbol,
                interval=interval,
                limit=limit
            )

            if response["retCode"] == 0:
                candles = []
                for item in response["result"]["list"]:
                    candles.append({
                        "timestamp": datetime.fromtimestamp(int(item[0]) / 1000),
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                        "volume": float(item[5]),
                        "turnover": float(item[6])
                    })
                return list(reversed(candles))
            else:
                logger.error(f"Failed to get klines: {response['retMsg']}")
                return []

        except Exception as e:
            logger.error(f"Error getting klines: {e}")
            return []

    def get_balance(self) -> Optional[dict]:
        """Get wallet balance"""
        if self.paper_trading:
            # Calculate unrealized PnL from open positions
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
            response = self.client.get_wallet_balance(accountType="UNIFIED")

            if response["retCode"] == 0:
                account = response["result"]["list"][0]
                usdt_info = None

                for coin in account["coin"]:
                    if coin["coin"] == "USDT":
                        usdt_info = coin
                        break

                if usdt_info:
                    def safe_float(val, default=0.0):
                        try:
                            return float(val) if val not in (None, "", "N/A") else default
                        except (ValueError, TypeError):
                            return default

                    logger.info(f"USDT fields from Bybit: {usdt_info}")
                    available = safe_float(usdt_info.get("availableToWithdraw")) or \
                                safe_float(usdt_info.get("availableToBorrow")) or \
                                safe_float(usdt_info.get("walletBalance"))
                    return {
                        "total_balance": safe_float(usdt_info.get("walletBalance")),
                        "available_balance": available,
                        "unrealized_pnl": safe_float(usdt_info.get("unrealisedPnl")),
                        "margin_used": safe_float(account.get("totalMarginBalance"))
                    }

                # USDT не найден — показываем что есть в аккаунте
                coins = [c["coin"] for c in account.get("coin", [])]
                logger.warning(
                    f"USDT не найден в Unified аккаунте. "
                    f"Доступные монеты: {coins}. "
                    f"Пополните testnet баланс на testnet.bybit.com"
                )
                return None

            logger.error(f"Failed to get balance: {response.get('retMsg', 'Unknown error')}")
            return None

        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return None

    def place_order(
        self,
        side: str,
        qty: float,
        order_type: str = "Market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
        symbol: str = None
    ) -> Optional[dict]:
        """Place an order"""
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

            # Update positions for paper trading
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

            params = {
                "category": self.CATEGORY,
                "symbol": symbol,
                "side": side,
                "orderType": order_type,
                "qty": str(qty),
                "reduceOnly": reduce_only
            }

            if order_type == "Limit" and price:
                params["price"] = str(price)

            # Note: stopLoss/takeProfit NOT set in initial order to avoid
            # Bybit price-distance restrictions on testnet. Set separately via set_position_stop.

            response = self.client.place_order(**params)

            if response["retCode"] == 0:
                result = response["result"]
                logger.info(f"Order placed: {side} {qty} {symbol} @ {order_type}")
                return {
                    "order_id": result["orderId"],
                    "order_link_id": result.get("orderLinkId"),
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "order_type": order_type,
                    "price": price,
                    "status": "created"
                }
            else:
                logger.error(f"Order failed: {response['retMsg']}")
                return None

        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None

    def cancel_order(
        self,
        order_id: str,
        symbol: str = None
    ) -> bool:
        """Cancel an order"""
        if self.paper_trading:
            logger.info(f"[PAPER] Order cancelled: {order_id}")
            return True

        try:
            symbol = symbol or self.SYMBOL
            response = self.client.cancel_order(
                category=self.CATEGORY,
                symbol=symbol,
                orderId=order_id
            )

            if response["retCode"] == 0:
                logger.info(f"Order cancelled: {order_id}")
                return True
            else:
                logger.error(f"Cancel failed: {response['retMsg']}")
                return False

        except Exception as e:
            logger.error(f"Error cancelling order: {e}")
            return False

    def get_positions(self, symbol: str = None) -> List[dict]:
        """Get open positions"""
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
            params = {"category": self.CATEGORY}
            if symbol:
                params["symbol"] = symbol

            response = self.client.get_positions(**params)

            if response["retCode"] == 0:
                positions = []
                for pos in response["result"]["list"]:
                    if float(pos["size"]) > 0:
                        positions.append({
                            "symbol": pos["symbol"],
                            "side": pos["side"],
                            "size": float(pos["size"]),
                            "entry_price": float(pos["avgPrice"]),
                            "mark_price": float(pos["markPrice"]),
                            "unrealized_pnl": float(pos["unrealisedPnl"]),
                            "leverage": pos["leverage"],
                            "position_idx": pos["positionIdx"],
                            "stop_loss": float(pos.get("stopLoss", 0)) or None,
                            "take_profit": float(pos.get("takeProfit", 0)) or None
                        })
                return positions
            else:
                logger.error(f"Failed to get positions: {response['retMsg']}")
                return []

        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    def set_trading_stop(
        self,
        symbol: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trailing_stop: Optional[float] = None,
        position_idx: int = 0
    ) -> bool:
        """Set stop loss, take profit, or trailing stop for position"""
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
            params = {
                "category": self.CATEGORY,
                "symbol": symbol,
                "positionIdx": position_idx
            }

            if stop_loss:
                params["stopLoss"] = str(stop_loss)
            if take_profit:
                params["takeProfit"] = str(take_profit)
            if trailing_stop:
                params["trailingStop"] = str(trailing_stop)

            response = self.client.set_trading_stop(**params)

            if response["retCode"] == 0:
                logger.info(f"Trading stop set for {symbol}")
                return True
            else:
                logger.error(f"Failed to set trading stop: {response['retMsg']}")
                return False

        except Exception as e:
            logger.error(f"Error setting trading stop: {e}")
            return False

    def get_order_history(
        self,
        symbol: str = None,
        limit: int = 50
    ) -> List[dict]:
        """Get order history"""
        if self.paper_trading:
            return self._paper_orders[-limit:]

        try:
            params = {"category": self.CATEGORY, "limit": limit}
            if symbol:
                params["symbol"] = symbol

            response = self.client.get_order_history(**params)

            if response["retCode"] == 0:
                orders = []
                for order in response["result"]["list"]:
                    orders.append({
                        "order_id": order["orderId"],
                        "symbol": order["symbol"],
                        "side": order["side"],
                        "order_type": order["orderType"],
                        "qty": float(order["qty"]),
                        "price": float(order.get("price", 0)),
                        "avg_price": float(order.get("avgPrice", 0)),
                        "status": order["orderStatus"],
                        "created_time": datetime.fromtimestamp(int(order["createdTime"]) / 1000)
                    })
                return orders
            else:
                logger.error(f"Failed to get order history: {response['retMsg']}")
                return []

        except Exception as e:
            logger.error(f"Error getting order history: {e}")
            return []

    def get_closed_pnl(
        self,
        symbol: str = None,
        limit: int = 50
    ) -> List[dict]:
        """Get closed P&L records"""
        if self.paper_trading:
            return []  # No closed positions in paper trading yet

        try:
            params = {"category": self.CATEGORY, "limit": limit}
            if symbol:
                params["symbol"] = symbol

            response = self.client.get_closed_pnl(**params)

            if response["retCode"] == 0:
                records = []
                for record in response["result"]["list"]:
                    records.append({
                        "symbol": record["symbol"],
                        "side": record["side"],
                        "qty": float(record["qty"]),
                        "entry_price": float(record["avgEntryPrice"]),
                        "exit_price": float(record["avgExitPrice"]),
                        "closed_pnl": float(record["closedPnl"]),
                        "created_time": datetime.fromtimestamp(int(record["createdTime"]) / 1000)
                    })
                return records
            else:
                logger.error(f"Failed to get closed PnL: {response['retMsg']}")
                return []

        except Exception as e:
            logger.error(f"Error getting closed PnL: {e}")
            return []

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for symbol"""
        if self.paper_trading:
            logger.info(f"[PAPER] Leverage set to {leverage}x for {symbol}")
            return True

        try:
            response = self.client.set_leverage(
                category=self.CATEGORY,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage)
            )

            if response["retCode"] == 0 or response["retCode"] == 110043:
                logger.info(f"Leverage set to {leverage}x for {symbol}")
                return True
            else:
                logger.error(f"Failed to set leverage: {response['retMsg']}")
                return False

        except Exception as e:
            logger.error(f"Error setting leverage: {e}")
            return False

    def close_paper_position(self, symbol: str = None) -> bool:
        """Close paper trading position"""
        if not self.paper_trading:
            return False

        symbol = symbol or self.SYMBOL
        for i, pos in enumerate(self._paper_positions):
            if pos["symbol"] == symbol:
                # Calculate PnL
                if pos["side"] == "Buy":
                    pnl = (self._simulated_price - pos["entry_price"]) * pos["size"]
                else:
                    pnl = (pos["entry_price"] - self._simulated_price) * pos["size"]

                self._paper_balance += pnl
                self._paper_positions.pop(i)
                logger.info(f"[PAPER] Position closed, PnL: {pnl:.2f}")
                return True
        return False
