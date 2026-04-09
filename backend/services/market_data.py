import logging
import asyncio
from typing import Optional, List, Callable
from datetime import datetime
from ..trading.bybit_client import BybitClient
from .websocket_manager import ws_manager

logger = logging.getLogger(__name__)


class MarketDataService:
    """Service for real-time market data"""

    def __init__(self, bybit_client: BybitClient):
        self.client = bybit_client
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable] = []

        # Cache
        self.current_price: float = 0.0
        self.last_update: Optional[datetime] = None

    async def start(self, interval: float = 1.0) -> None:
        """Start market data polling"""
        if self.is_running:
            return

        self.is_running = True
        self._task = asyncio.create_task(self._poll_loop(interval))
        logger.info("Market data service started")

    async def stop(self) -> None:
        """Stop market data polling"""
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Market data service stopped")

    async def _poll_loop(self, interval: float) -> None:
        """Main polling loop"""
        while self.is_running:
            try:
                await self._fetch_and_broadcast()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in market data poll: {e}")
                await asyncio.sleep(interval)

    async def _fetch_and_broadcast(self) -> None:
        """Fetch market data and broadcast to WebSocket clients"""
        ticker = self.client.get_ticker()
        if not ticker:
            return

        self.current_price = ticker["last_price"]
        self.last_update = datetime.utcnow()

        # Broadcast to WebSocket clients
        await ws_manager.send_price_update({
            "symbol": ticker["symbol"],
            "price": ticker["last_price"],
            "bid": ticker["bid"],
            "ask": ticker["ask"],
            "volume_24h": ticker["volume_24h"],
            "high_24h": ticker["high_24h"],
            "low_24h": ticker["low_24h"],
            "timestamp": self.last_update.isoformat()
        })

        # Call registered callbacks
        for callback in self._callbacks:
            try:
                await callback(ticker)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def register_callback(self, callback: Callable) -> None:
        """Register callback for price updates"""
        self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable) -> None:
        """Unregister callback"""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def get_current_price(self) -> float:
        """Get cached current price"""
        return self.current_price

    async def get_candles(
        self,
        interval: str = "60",
        limit: int = 24
    ) -> List[dict]:
        """Get candle data"""
        return self.client.get_klines(interval=interval, limit=limit)

    async def get_ticker(self) -> Optional[dict]:
        """Get current ticker"""
        return self.client.get_ticker()
