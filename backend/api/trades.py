import logging
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.db import get_db
from ..database.repositories import TradeRepository, PositionRepository, AlertRepository
from ..services.websocket_manager import ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["trades"])


class TradeResponse(BaseModel):
    id: int
    alert_id: int
    direction: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    quantity: float
    status: str
    realized_pnl: float
    unrealized_pnl: float
    risk_reward: float
    created_at: datetime
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]


class PositionResponse(BaseModel):
    id: int
    trade_id: int
    direction: str
    initial_quantity: float
    current_quantity: float
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    trailing_stop: Optional[float]
    status: str
    tp1_filled: bool
    tp2_filled: bool
    realized_pnl: float
    unrealized_pnl: float
    opened_at: datetime
    closed_at: Optional[datetime]


class StatsResponse(BaseModel):
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    # Дополнительно — чтобы фронтенд мог показать детали
    class Config:
        extra = "allow"


@router.get("/positions", response_model=List[PositionResponse])
async def get_positions(db: AsyncSession = Depends(get_db)):
    """Get all active positions"""
    repo = PositionRepository(db)
    positions = await repo.get_active()
    return positions


@router.get("/positions/{position_id}", response_model=PositionResponse)
async def get_position(position_id: int, db: AsyncSession = Depends(get_db)):
    """Get position by ID"""
    repo = PositionRepository(db)
    position = await repo.get_by_id(position_id)
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")
    return position


@router.post("/positions/{position_id}/close")
async def close_position(
    position_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Close position manually"""
    repo = PositionRepository(db)
    position = await repo.get_by_id(position_id)

    if not position:
        raise HTTPException(status_code=404, detail="Position not found")

    if position.status in ["closed", "stopped"]:
        raise HTTPException(status_code=400, detail="Position already closed")

    # Close position (in real implementation, this would execute on exchange)
    await repo.close_position(position_id, position.unrealized_pnl, "closed")

    # Broadcast update
    await ws_manager.send_position_update({
        "id": position_id,
        "status": "closed",
        "realized_pnl": position.unrealized_pnl
    })

    return {"status": "closed", "position_id": position_id}


@router.get("/trades", response_model=List[TradeResponse])
async def get_trades(
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Get trade history"""
    repo = TradeRepository(db)
    trades = await repo.get_history(limit)
    return trades


@router.get("/trades/open", response_model=List[TradeResponse])
async def get_open_trades(db: AsyncSession = Depends(get_db)):
    """Get all open trades"""
    repo = TradeRepository(db)
    trades = await repo.get_open_trades()
    return trades


@router.get("/trades/{trade_id}", response_model=TradeResponse)
async def get_trade(trade_id: int, db: AsyncSession = Depends(get_db)):
    """Get trade by ID"""
    repo = TradeRepository(db)
    trade = await repo.get_by_id(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade


@router.post("/trades/{trade_id}/close")
async def close_trade(
    trade_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Close trade manually"""
    repo = TradeRepository(db)
    trade = await repo.get_by_id(trade_id)

    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    if trade.status in ["closed", "cancelled"]:
        raise HTTPException(status_code=400, detail="Trade already closed")

    # Update trade status
    await repo.update(trade_id, status="closed", closed_at=datetime.utcnow())

    # Broadcast update
    await ws_manager.send_trade_update({
        "id": trade_id,
        "status": "closed"
    })

    return {"status": "closed", "trade_id": trade_id}


@router.get("/stats", response_model=StatsResponse)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Get trading statistics"""
    repo = TradeRepository(db)
    stats = await repo.get_stats()
    return stats


@router.get("/alerts")
async def get_alerts(
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Get alert history"""
    repo = AlertRepository(db)
    alerts = await repo.get_recent(limit)
    return [
        {
            "id": a.id,
            "type": a.alert_type,
            "price": a.price,
            "levels": a.levels,
            "status": a.status,
            "priority": a.priority,
            "timestamp": a.timestamp.isoformat()
        }
        for a in alerts
    ]


@router.get("/alerts/active")
async def get_active_alerts(db: AsyncSession = Depends(get_db)):
    """Get active alerts"""
    repo = AlertRepository(db)
    alerts = await repo.get_active()
    return [
        {
            "id": a.id,
            "type": a.alert_type,
            "price": a.price,
            "levels": a.levels,
            "status": a.status,
            "priority": a.priority,
            "timestamp": a.timestamp.isoformat()
        }
        for a in alerts
    ]
