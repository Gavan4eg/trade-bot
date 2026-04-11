import logging
import json
import asyncio
from typing import Dict, Set, Any
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manage WebSocket connections for real-time updates"""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept new WebSocket connection"""
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove WebSocket connection"""
        async with self._lock:
            self.active_connections.discard(websocket)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: dict) -> None:
        """Broadcast message to all connected clients"""
        if not self.active_connections:
            return

        message_json = json.dumps(message)
        disconnected = set()

        for connection in list(self.active_connections):
            try:
                await connection.send_text(message_json)
            except Exception as e:
                logger.warning(f"Failed to send to WebSocket: {e}")
                disconnected.add(connection)

        # Remove disconnected clients
        if disconnected:
            async with self._lock:
                self.active_connections -= disconnected

    async def send_alert(self, alert_data: dict) -> None:
        """Send alert notification"""
        await self.broadcast({
            "type": "alert",
            "data": alert_data
        })

    async def send_trade_update(self, trade_data: dict) -> None:
        """Send trade update"""
        await self.broadcast({
            "type": "trade",
            "data": trade_data
        })

    async def send_position_update(self, position_data: dict) -> None:
        """Send position update"""
        await self.broadcast({
            "type": "position",
            "data": position_data
        })

    async def send_price_update(self, price_data: dict) -> None:
        """Send price update"""
        await self.broadcast({
            "type": "price",
            "data": price_data
        })

    async def send_range_update(self, range_data: dict) -> None:
        """Send range detection update"""
        await self.broadcast({
            "type": "range",
            "data": range_data
        })

    async def send_sweep_update(self, sweep_data: dict) -> None:
        """Send sweep detection update"""
        await self.broadcast({
            "type": "sweep",
            "data": sweep_data
        })

    async def send_error(self, error_message: str) -> None:
        """Send error notification"""
        await self.broadcast({
            "type": "error",
            "data": {"message": error_message}
        })

    async def send_status(self, status_data: dict) -> None:
        """Send bot status update"""
        await self.broadcast({
            "type": "status",
            "data": status_data
        })

    async def send_log(self, message: str, level: str = "info", source: str = "") -> None:
        """Send log message to UI"""
        from datetime import datetime
        await self.broadcast({
            "type": "log",
            "data": {
                "message": message,
                "level": level,  # info | success | warning | error
                "source": source,
                "timestamp": datetime.utcnow().strftime("%H:%M:%S")
            }
        })

    def get_connection_count(self) -> int:
        """Get number of active connections"""
        return len(self.active_connections)


# Global WebSocket manager instance
ws_manager = WebSocketManager()
