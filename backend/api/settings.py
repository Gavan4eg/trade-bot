import logging
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.db import get_db
from ..config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


class TradingSettings(BaseModel):
    max_positions: int = 3
    risk_per_trade: float = 1.0
    min_rr: float = 2.0
    range_timeframe: str = "1h"
    range_candles: int = 24
    min_confirmations: int = 2
    sweep_threshold_percent: float = 0.1
    tp1_rr: float = 2.0
    tp1_close_percent: int = 50
    tp2_rr: float = 3.0
    tp2_close_percent: int = 30
    trailing_activation_rr: float = 2.5
    trailing_step_percent: float = 0.5


class BotStatus(BaseModel):
    is_active: bool = True
    testnet: bool = True
    connected_to_exchange: bool = False
    active_positions: int = 0
    pending_alerts: int = 0


# In-memory settings (would be persisted to DB in production)
current_settings = TradingSettings()
bot_status = BotStatus()


@router.get("", response_model=TradingSettings)
async def get_settings():
    """Get current trading settings"""
    return TradingSettings(
        max_positions=settings.max_positions,
        risk_per_trade=settings.risk_per_trade,
        min_rr=settings.min_rr,
        range_timeframe=settings.range_timeframe,
        range_candles=settings.range_candles,
        min_confirmations=settings.min_confirmations,
        sweep_threshold_percent=settings.sweep_threshold_percent,
        tp1_rr=settings.tp1_rr,
        tp1_close_percent=settings.tp1_close_percent,
        tp2_rr=settings.tp2_rr,
        tp2_close_percent=settings.tp2_close_percent,
        trailing_activation_rr=settings.trailing_activation_rr,
        trailing_step_percent=settings.trailing_step_percent
    )


@router.put("", response_model=TradingSettings)
async def update_settings(new_settings: TradingSettings):
    """Update trading settings"""
    global current_settings

    # Validate settings
    if new_settings.max_positions < 1:
        raise HTTPException(status_code=400, detail="max_positions must be >= 1")

    if new_settings.risk_per_trade <= 0 or new_settings.risk_per_trade > 10:
        raise HTTPException(status_code=400, detail="risk_per_trade must be between 0 and 10")

    if new_settings.min_rr < 1:
        raise HTTPException(status_code=400, detail="min_rr must be >= 1")

    current_settings = new_settings
    logger.info(f"Settings updated: {new_settings}")

    return current_settings


@router.get("/status", response_model=BotStatus)
async def get_bot_status():
    """Get bot status"""
    return BotStatus(
        is_active=bot_status.is_active,
        testnet=settings.bybit_testnet,
        connected_to_exchange=bot_status.connected_to_exchange,
        active_positions=bot_status.active_positions,
        pending_alerts=bot_status.pending_alerts
    )


@router.post("/start")
async def start_bot():
    """Start the trading bot"""
    global bot_status
    bot_status.is_active = True
    logger.info("Bot started")
    return {"status": "started"}


@router.post("/stop")
async def stop_bot():
    """Stop the trading bot"""
    global bot_status
    bot_status.is_active = False
    logger.info("Bot stopped")
    return {"status": "stopped"}


@router.post("/close-all")
async def close_all_positions(db: AsyncSession = Depends(get_db)):
    """Close all open positions on exchange and in DB"""
    from ..main import bybit_client, position_manager
    from ..database.repositories import PositionRepository, TradeRepository
    from datetime import datetime

    if bybit_client is None:
        raise HTTPException(status_code=503, detail="bybit_client not initialized")

    closed = 0
    errors = []

    # Close all positions on exchange
    if not bybit_client.paper_trading:
        try:
            positions = bybit_client.get_positions(symbol="BTCUSDT")
            for pos in positions:
                side = "Sell" if pos["side"] == "Buy" else "Buy"
                qty = pos["size"]
                order = bybit_client.place_order(
                    side=side, qty=qty, order_type="Market", reduce_only=True
                )
                if order:
                    closed += 1
                    logger.info(f"Closed exchange position: {pos['side']} {qty} BTCUSDT")
                else:
                    errors.append(f"Failed to close {pos['side']} {qty}")
        except Exception as e:
            errors.append(str(e))
    else:
        bybit_client._paper_positions.clear()
        closed += 1

    # Close all in-memory positions
    if position_manager:
        from ..models.position import PositionStatus
        from datetime import datetime
        for pos in list(position_manager.get_active_positions()):
            pos.status = PositionStatus.CLOSED
            pos.closed_at = datetime.utcnow()
        position_manager.positions.clear()

    # Close all in DB
    try:
        pos_repo = PositionRepository(db)
        trade_repo = TradeRepository(db)
        open_positions = await pos_repo.get_active()
        for p in open_positions:
            await pos_repo.close_position(p.id, p.unrealized_pnl or 0.0, "closed")
            if p.trade_id:
                await trade_repo.update(p.trade_id,
                    status="closed",
                    closed_at=datetime.utcnow(),
                    realized_pnl=p.unrealized_pnl or 0.0
                )
    except Exception as e:
        errors.append(f"DB close error: {e}")

    logger.info(f"Close all: closed={closed}, errors={errors}")
    return {"status": "ok", "closed": closed, "errors": errors}


@router.get("/balance")
async def get_balance():
    """Get exchange balance"""
    from ..main import bybit_client
    if bybit_client:
        balance = bybit_client.get_balance()
        if balance:
            return balance
    return {
        "total_balance": 10000.0,
        "available_balance": 10000.0,
        "unrealized_pnl": 0.0,
        "margin_used": 0.0
    }


@router.post("/test/open-position")
async def test_open_position(
    direction: str = "long",
    quantity: float = 0.01,
    db: AsyncSession = Depends(get_db)
):
    """
    Test endpoint: Open a paper trading position immediately
    direction: 'long' or 'short'
    """
    from ..main import bybit_client
    from ..services.websocket_manager import ws_manager
    from ..database.repositories import TradeRepository, PositionRepository
    from datetime import datetime

    if not bybit_client:
        raise HTTPException(status_code=500, detail="Bybit client not initialized")

    if not bybit_client.paper_trading:
        raise HTTPException(status_code=400, detail="Only available in paper trading mode")

    side = "Buy" if direction == "long" else "Sell"
    ticker = bybit_client.get_ticker()
    price = ticker["last_price"] if ticker else 65000.0

    # Calculate SL/TP
    if direction == "long":
        stop_loss = price * 0.98  # 2% below
        take_profit = price * 1.04  # 4% above
    else:
        stop_loss = price * 1.02  # 2% above
        take_profit = price * 0.96  # 4% below

    order = bybit_client.place_order(
        side=side,
        qty=quantity,
        stop_loss=stop_loss,
        take_profit=take_profit
    )

    if order:
        # Save trade to DB
        trade_repo = TradeRepository(db)
        trade_db = await trade_repo.create({
            "alert_id": 0,
            "direction": direction,
            "entry_price": price,
            "stop_loss": stop_loss,
            "take_profit_1": take_profit,
            "take_profit_2": take_profit * (1.02 if direction == "long" else 0.98),
            "quantity": quantity,
            "status": "open",
            "order_id": order["order_id"],
            "executed_price": price,
            "executed_quantity": quantity,
            "risk_reward": 2.0,
            "opened_at": datetime.utcnow()
        })

        # Save position to DB
        pos_repo = PositionRepository(db)
        pos_db = await pos_repo.create({
            "trade_id": trade_db.id,
            "direction": direction,
            "initial_quantity": quantity,
            "current_quantity": quantity,
            "entry_price": price,
            "current_price": price,
            "stop_loss": stop_loss,
            "take_profit_1": take_profit,
            "take_profit_2": take_profit * (1.02 if direction == "long" else 0.98),
            "status": "open"
        })

        # Broadcast to UI
        await ws_manager.send_position_update({
            "id": pos_db.id,
            "trade_id": trade_db.id,
            "direction": direction,
            "entry_price": price,
            "current_price": price,
            "current_quantity": quantity,
            "stop_loss": stop_loss,
            "take_profit_1": take_profit,
            "status": "open",
            "unrealized_pnl": 0.0
        })

        return {
            "status": "opened",
            "trade_id": trade_db.id,
            "position_id": pos_db.id,
            "direction": direction,
            "entry_price": price,
            "quantity": quantity,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "order_id": order["order_id"]
        }

    raise HTTPException(status_code=500, detail="Failed to open position")


@router.post("/test/close-position")
async def test_close_position(db: AsyncSession = Depends(get_db)):
    """Test endpoint: Close paper trading position"""
    from ..main import bybit_client
    from ..services.websocket_manager import ws_manager
    from ..database.repositories import TradeRepository, PositionRepository
    from datetime import datetime

    if not bybit_client:
        raise HTTPException(status_code=500, detail="Bybit client not initialized")

    if not bybit_client.paper_trading:
        raise HTTPException(status_code=400, detail="Only available in paper trading mode")

    # Get current price before closing
    ticker = bybit_client.get_ticker()
    close_price = ticker["last_price"] if ticker else 65000.0

    # Get open position from DB
    pos_repo = PositionRepository(db)
    trade_repo = TradeRepository(db)

    positions = await pos_repo.get_active()
    if not positions:
        return {"status": "no_position", "message": "No open position to close"}

    position = positions[0]

    # Calculate PnL
    if position.direction == "long":
        pnl = (close_price - position.entry_price) * position.current_quantity
    else:
        pnl = (position.entry_price - close_price) * position.current_quantity

    # Close in paper trading
    bybit_client.close_paper_position()

    # Update position in DB
    await pos_repo.close_position(position.id, round(pnl, 2), "closed")

    # Update trade in DB
    await trade_repo.update(
        position.trade_id,
        status="closed",
        realized_pnl=round(pnl, 2),
        closed_at=datetime.utcnow()
    )

    balance = bybit_client.get_balance()

    await ws_manager.send_position_update({
        "id": position.id,
        "status": "closed",
        "realized_pnl": round(pnl, 2)
    })

    return {
        "status": "closed",
        "position_id": position.id,
        "close_price": close_price,
        "pnl": round(pnl, 2),
        "new_balance": balance
    }


@router.get("/test/price")
async def get_current_price():
    """Get current simulated price"""
    from ..main import bybit_client
    if bybit_client:
        ticker = bybit_client.get_ticker()
        if ticker:
            return ticker
    return {"last_price": 65000.0, "symbol": "BTCUSDT"}
