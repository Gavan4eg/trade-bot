import logging
import random
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime
from ..config import settings

logger = logging.getLogger(__name__)


class OKXClient:
    """Client for OKX Futures API with Paper Trading support.

    Uses python-okx package for real trading:
        import okx.Account as Account
        import okx.Trade as Trade
        import okx.MarketData as MarketData

    OKX perpetual instrument: BTC-USDT-SWAP
    """

    SYMBOL = "BTC-USDT-SWAP"  # OKX USDT perpetual
    BYBIT_SYMBOL = "BTCUSDT"  # alias used by callers that pass bybit-style symbols

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        testnet: bool = False,
        paper_trading: bool = None,
    ):
        self.api_key = api_key or settings.okx_api_key
        self.api_secret = api_secret or settings.okx_api_secret
        self.passphrase = passphrase or settings.okx_passphrase
        self.testnet = testnet if testnet is not None else settings.okx_testnet
        self.paper_trading = paper_trading if paper_trading is not None else settings.paper_trading

        # Paper trading state
        self._paper_balance = 10000.0
        self._paper_positions: List[dict] = []
        self._paper_orders: List[dict] = []
        self._simulated_price = 65000.0

        if self.paper_trading:
            logger.info("OKXClient initialized in PAPER TRADING mode")
            self._account_api = None
            self._trade_api = None
            self._market_api = None
        else:
            flag = "1" if self.testnet else "0"  # OKX: "1"=simulated, "0"=live
            try:
                import okx.Account as Account
                import okx.Trade as Trade
                import okx.MarketData as MarketData

                self._account_api = Account.AccountAPI(
                    self.api_key, self.api_secret, self.passphrase,
                    flag=flag, debug=False
                )
                self._trade_api = Trade.TradeAPI(
                    self.api_key, self.api_secret, self.passphrase,
                    flag=flag, debug=False
                )
                self._market_api = MarketData.MarketAPI(flag=flag, debug=False)
                logger.info(f"OKXClient initialized (testnet={self.testnet})")
            except ImportError:
                logger.error("python-okx package not installed. Run: pip install python-okx==0.3.8")
                raise

    def _normalize_symbol(self, symbol: Optional[str]) -> str:
        """Accept both BTCUSDT and BTC-USDT-SWAP and return OKX format."""
        if symbol is None:
            return self.SYMBOL
        s = symbol.upper()
        if s in ("BTCUSDT", "BTC_USDT"):
            return self.SYMBOL
        return s

    def _simulate_price_movement(self) -> float:
        change = random.uniform(-0.002, 0.002)
        self._simulated_price *= (1 + change)
        return self._simulated_price

    def set_simulated_price(self, price: float) -> None:
        if self.paper_trading:
            self._simulated_price = price
            logger.info(f"[OKX TEST] Simulated price set to {price}")

    def get_ticker(self, symbol: str = None) -> Optional[dict]:
        """Get current ticker. Returns dict with last_price, high_24h, low_24h, volume_24h."""
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
                "timestamp": datetime.utcnow(),
            }

        inst_id = self._normalize_symbol(symbol)
        try:
            resp = self._market_api.get_ticker(instId=inst_id)
            if resp.get("code") != "0":
                logger.error(f"OKX get_ticker error: {resp}")
                return None
            d = resp["data"][0]
            return {
                "symbol": inst_id,
                "last_price": float(d["last"]),
                "bid": float(d["bidPx"]),
                "ask": float(d["askPx"]),
                "volume_24h": float(d["vol24h"]),
                "high_24h": float(d["high24h"]),
                "low_24h": float(d["low24h"]),
                "timestamp": datetime.utcnow(),
            }
        except Exception as e:
            logger.error(f"OKX get_ticker exception: {e}")
            return None

    def get_klines(self, interval: str = "60", limit: int = 24, symbol: str = None) -> List[dict]:
        """Get candlestick data. interval in minutes (OKX bar: '1m','3m','5m','15m','30m','1H','2H','4H','6H','12H','1D').
        We accept minute strings and convert."""
        if self.paper_trading:
            price = self._simulated_price
            candles = []
            for i in range(limit):
                o = price * random.uniform(0.998, 1.002)
                h = o * random.uniform(1.000, 1.005)
                l = o * random.uniform(0.995, 1.000)
                c = random.uniform(l, h)
                candles.append({
                    "timestamp": datetime.utcnow(),
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": random.uniform(100, 500),
                })
            return candles

        inst_id = self._normalize_symbol(symbol)
        # Map minute-count strings to OKX bar values
        bar_map = {
            "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
            "60": "1H", "120": "2H", "240": "4H", "360": "6H", "720": "12H",
            "1440": "1D",
        }
        bar = bar_map.get(str(interval), "1H")
        try:
            resp = self._market_api.get_candlesticks(instId=inst_id, bar=bar, limit=str(limit))
            if resp.get("code") != "0":
                logger.error(f"OKX get_klines error: {resp}")
                return []
            candles = []
            for row in resp["data"]:
                # OKX format: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
                candles.append({
                    "timestamp": datetime.utcfromtimestamp(int(row[0]) / 1000),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            return candles
        except Exception as e:
            logger.error(f"OKX get_klines exception: {e}")
            return []

    def get_balance(self) -> Optional[dict]:
        """Get USDT balance. Returns dict with available_balance, total_balance."""
        if self.paper_trading:
            return {
                "available_balance": self._paper_balance,
                "total_balance": self._paper_balance,
                "currency": "USDT",
            }

        try:
            resp = self._account_api.get_account_balance(ccy="USDT")
            if resp.get("code") != "0":
                logger.error(f"OKX get_balance error: {resp}")
                return None
            details = resp["data"][0]["details"]
            usdt = next((d for d in details if d["ccy"] == "USDT"), None)
            if not usdt:
                return {"available_balance": 0.0, "total_balance": 0.0}
            return {
                "available_balance": float(usdt["availEq"]),
                "total_balance": float(usdt["eq"]),
                "currency": "USDT",
            }
        except Exception as e:
            logger.error(f"OKX get_balance exception: {e}")
            return None

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for the instrument."""
        if self.paper_trading:
            logger.info(f"[OKX PAPER] set_leverage {leverage}x (no-op)")
            return True

        inst_id = self._normalize_symbol(symbol)
        try:
            resp = self._account_api.set_leverage(
                instId=inst_id,
                lever=str(leverage),
                mgnMode="cross",
            )
            if resp.get("code") == "0":
                logger.info(f"OKX leverage set to {leverage}x on {inst_id}")
                return True
            logger.error(f"OKX set_leverage error: {resp}")
            return False
        except Exception as e:
            logger.error(f"OKX set_leverage exception: {e}")
            return False

    def place_order(
        self,
        side: str,
        qty: float,
        order_type: str = "Market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
        symbol: str = None,
    ) -> Optional[dict]:
        """Place a futures order.

        side: 'buy' or 'sell' (case-insensitive)
        order_type: 'Market' or 'Limit'
        Returns dict with order_id, or None on failure.
        """
        inst_id = self._normalize_symbol(symbol)
        side_lower = side.lower()
        okx_type = "market" if order_type.lower() == "market" else "limit"

        if self.paper_trading:
            order_id = str(uuid.uuid4())[:8]
            ticker = self.get_ticker(inst_id)
            exec_price = ticker["last_price"] if ticker else self._simulated_price

            if not reduce_only:
                pos_side = "long" if side_lower == "buy" else "short"
                self._paper_positions.append({
                    "symbol": inst_id,
                    "side": pos_side,
                    "size": qty,
                    "entry_price": exec_price,
                    "unrealized_pnl": 0.0,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "order_id": order_id,
                })
                cost = exec_price * qty / settings.leverage if settings.leverage else exec_price * qty
                self._paper_balance -= cost
                logger.info(f"[OKX PAPER] Opened {pos_side} {qty} BTC @ {exec_price:.2f} (order {order_id})")
            else:
                logger.info(f"[OKX PAPER] Closed position {qty} BTC (reduce_only)")

            return {"order_id": order_id, "executed_price": exec_price, "qty": qty}

        # Real order
        params: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side_lower,
            "ordType": okx_type,
            "sz": str(qty),
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if okx_type == "limit" and price:
            params["px"] = str(price)
        # OKX attach-algo (SL/TP) via attachAlgoOrds
        attach_algos = []
        if stop_loss:
            attach_algos.append({
                "attachAlgoClOrdId": str(uuid.uuid4())[:16],
                "slTriggerPx": str(stop_loss),
                "slOrdPx": "-1",  # market
            })
        if take_profit:
            attach_algos.append({
                "attachAlgoClOrdId": str(uuid.uuid4())[:16],
                "tpTriggerPx": str(take_profit),
                "tpOrdPx": "-1",
            })
        if attach_algos:
            params["attachAlgoOrds"] = attach_algos

        try:
            resp = self._trade_api.place_order(**params)
            if resp.get("code") == "0":
                order_id = resp["data"][0]["ordId"]
                logger.info(f"OKX order placed: {order_id} ({side} {qty} {inst_id})")
                return {"order_id": order_id, "qty": qty}
            logger.error(f"OKX place_order error: {resp}")
            return None
        except Exception as e:
            logger.error(f"OKX place_order exception: {e}")
            return None

    def cancel_order(self, order_id: str, symbol: str = None) -> bool:
        """Cancel an open order."""
        if self.paper_trading:
            logger.info(f"[OKX PAPER] cancel_order {order_id} (no-op)")
            return True

        inst_id = self._normalize_symbol(symbol)
        try:
            resp = self._trade_api.cancel_order(instId=inst_id, ordId=order_id)
            if resp.get("code") == "0":
                return True
            logger.error(f"OKX cancel_order error: {resp}")
            return False
        except Exception as e:
            logger.error(f"OKX cancel_order exception: {e}")
            return False

    def get_positions(self, symbol: str = None) -> List[dict]:
        """Get open positions."""
        if self.paper_trading:
            return [
                {
                    "symbol": p["symbol"],
                    "side": p["side"],
                    "size": p["size"],
                    "entry_price": p["entry_price"],
                    "unrealized_pnl": p.get("unrealized_pnl", 0.0),
                    "stop_loss": p.get("stop_loss"),
                    "take_profit": p.get("take_profit"),
                }
                for p in self._paper_positions
            ]

        inst_id = self._normalize_symbol(symbol) if symbol else None
        try:
            kwargs = {"instType": "SWAP"}
            if inst_id:
                kwargs["instId"] = inst_id
            resp = self._account_api.get_positions(**kwargs)
            if resp.get("code") != "0":
                logger.error(f"OKX get_positions error: {resp}")
                return []
            positions = []
            for p in resp["data"]:
                pos_qty = abs(float(p.get("pos", 0)))
                if pos_qty == 0:
                    continue
                positions.append({
                    "symbol": p["instId"],
                    "side": "long" if float(p.get("pos", 0)) > 0 else "short",
                    "size": pos_qty,
                    "entry_price": float(p.get("avgPx", 0)),
                    "unrealized_pnl": float(p.get("upl", 0)),
                    "stop_loss": None,
                    "take_profit": None,
                })
            return positions
        except Exception as e:
            logger.error(f"OKX get_positions exception: {e}")
            return []

    def get_order_history(self, symbol: str = None, limit: int = 50) -> List[dict]:
        """Get order history."""
        if self.paper_trading:
            return self._paper_orders[-limit:]

        inst_id = self._normalize_symbol(symbol) if symbol else None
        try:
            kwargs = {"instType": "SWAP", "limit": str(limit)}
            if inst_id:
                kwargs["instId"] = inst_id
            resp = self._trade_api.get_orders_history(**kwargs)
            if resp.get("code") != "0":
                logger.error(f"OKX get_order_history error: {resp}")
                return []
            orders = []
            for o in resp["data"]:
                orders.append({
                    "order_id": o["ordId"],
                    "symbol": o["instId"],
                    "side": o["side"],
                    "qty": float(o.get("sz", 0)),
                    "price": float(o.get("avgPx", 0)) if o.get("avgPx") else None,
                    "status": o["state"],
                    "created_at": datetime.utcfromtimestamp(int(o["cTime"]) / 1000),
                })
            return orders
        except Exception as e:
            logger.error(f"OKX get_order_history exception: {e}")
            return []

    def close_paper_position(self, symbol: str = None) -> bool:
        """Close paper trading position (resets paper state)."""
        if not self.paper_trading:
            logger.warning("close_paper_position called on non-paper OKXClient")
            return False
        inst_id = self._normalize_symbol(symbol) if symbol else None
        if inst_id:
            self._paper_positions = [p for p in self._paper_positions if p["symbol"] != inst_id]
        else:
            self._paper_positions.clear()
        logger.info(f"[OKX PAPER] Closed paper position(s) for {inst_id or 'all'}")
        return True

    def set_trading_stop(
        self,
        symbol: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trailing_stop: Optional[float] = None,
        position_idx: int = 0,
    ) -> bool:
        """Set/update stop-loss, take-profit, or trailing stop on an open position."""
        if self.paper_trading:
            inst_id = self._normalize_symbol(symbol)
            for p in self._paper_positions:
                if p["symbol"] == inst_id:
                    if stop_loss is not None:
                        p["stop_loss"] = stop_loss
                    if take_profit is not None:
                        p["take_profit"] = take_profit
            logger.info(f"[OKX PAPER] set_trading_stop on {inst_id}: sl={stop_loss}, tp={take_profit}")
            return True

        inst_id = self._normalize_symbol(symbol)
        try:
            positions = self.get_positions(inst_id)
            if not positions:
                logger.warning(f"OKX set_trading_stop: no open position for {inst_id}")
                return False

            pos = positions[0]
            pos_side = pos["side"]

            params: Dict[str, Any] = {
                "instId": inst_id,
                "posSide": pos_side,
            }
            if stop_loss is not None:
                params["slTriggerPx"] = str(stop_loss)
                params["slOrdPx"] = "-1"
            if take_profit is not None:
                params["tpTriggerPx"] = str(take_profit)
                params["tpOrdPx"] = "-1"

            resp = self._trade_api.place_algo_order(
                instId=inst_id,
                tdMode="cross",
                side="sell" if pos_side == "long" else "buy",
                ordType="oco",
                sz=str(pos["size"]),
                **{k: v for k, v in params.items() if k not in ("instId", "posSide")}
            )
            if resp.get("code") == "0":
                return True
            logger.error(f"OKX set_trading_stop error: {resp}")
            return False
        except Exception as e:
            logger.error(f"OKX set_trading_stop exception: {e}")
            return False
