import logging
from typing import Optional, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from sqlalchemy.orm import selectinload
from .db import AlertDB, TradeDB, PositionDB, RangeDB, SweepDB

logger = logging.getLogger(__name__)


class AlertRepository:
    """Repository for Alert operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, alert_data: dict) -> AlertDB:
        """Create new alert"""
        alert = AlertDB(**alert_data)
        self.session.add(alert)
        await self.session.commit()
        await self.session.refresh(alert)
        return alert

    async def get_by_id(self, alert_id: int) -> Optional[AlertDB]:
        """Get alert by ID"""
        result = await self.session.execute(
            select(AlertDB).where(AlertDB.id == alert_id)
        )
        return result.scalar_one_or_none()

    async def get_active(self) -> List[AlertDB]:
        """Get all active alerts"""
        result = await self.session.execute(
            select(AlertDB).where(
                AlertDB.status.not_in(["traded", "expired", "rejected"])
            ).order_by(AlertDB.priority, AlertDB.timestamp.desc())
        )
        return result.scalars().all()

    async def get_recent(self, limit: int = 50) -> List[AlertDB]:
        """Get recent alerts"""
        result = await self.session.execute(
            select(AlertDB).order_by(AlertDB.timestamp.desc()).limit(limit)
        )
        return result.scalars().all()

    async def update_status(self, alert_id: int, status: str) -> bool:
        """Update alert status"""
        await self.session.execute(
            update(AlertDB).where(AlertDB.id == alert_id).values(status=status)
        )
        await self.session.commit()
        return True

    async def delete(self, alert_id: int) -> bool:
        """Delete alert"""
        await self.session.execute(
            delete(AlertDB).where(AlertDB.id == alert_id)
        )
        await self.session.commit()
        return True


class TradeRepository:
    """Repository for Trade operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, trade_data: dict) -> TradeDB:
        """Create new trade"""
        trade = TradeDB(**trade_data)
        self.session.add(trade)
        await self.session.commit()
        await self.session.refresh(trade)
        return trade

    async def get_by_id(self, trade_id: int) -> Optional[TradeDB]:
        """Get trade by ID"""
        result = await self.session.execute(
            select(TradeDB).where(TradeDB.id == trade_id)
        )
        return result.scalar_one_or_none()

    async def get_open_trades(self) -> List[TradeDB]:
        """Get all open trades"""
        result = await self.session.execute(
            select(TradeDB).where(
                TradeDB.status.in_(["open", "partial_close"])
            )
        )
        return result.scalars().all()

    async def get_history(self, limit: int = 100) -> List[TradeDB]:
        """Get trade history"""
        result = await self.session.execute(
            select(TradeDB).order_by(TradeDB.created_at.desc()).limit(limit)
        )
        return result.scalars().all()

    async def update(self, trade_id: int, **kwargs) -> bool:
        """Update trade"""
        await self.session.execute(
            update(TradeDB).where(TradeDB.id == trade_id).values(**kwargs)
        )
        await self.session.commit()
        return True

    async def get_by_alert_id(self, alert_id: int) -> Optional[TradeDB]:
        """Get trade by alert ID"""
        result = await self.session.execute(
            select(TradeDB).where(TradeDB.alert_id == alert_id)
        )
        return result.scalar_one_or_none()

    async def get_stats(self) -> dict:
        """Get trading statistics — includes all opened trades (closed + partial + open)"""
        result = await self.session.execute(
            select(TradeDB).where(
                TradeDB.status.in_(["open", "partial_close", "closed"])
            )
        )
        trades = result.scalars().all()

        # Realized PnL — из закрытых и частично закрытых
        total_realized = sum(t.realized_pnl for t in trades)
        # Unrealized — из открытых позиций
        total_unrealized = sum(t.unrealized_pnl for t in trades
                               if t.status in ("open", "partial_close"))
        total_pnl = total_realized + total_unrealized

        # Считаем победами трейды с положительным realized PnL
        closed = [t for t in trades if t.status == "closed"]
        win_count = sum(1 for t in closed if t.realized_pnl > 0)
        loss_count = sum(1 for t in closed if t.realized_pnl <= 0)
        total_count = len(trades)
        closed_count = len(closed)

        return {
            "total_trades": total_count,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round(win_count / closed_count * 100, 2) if closed_count > 0 else 0,
            "total_pnl": round(total_pnl, 2)
        }


class PositionRepository:
    """Repository for Position operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, position_data: dict) -> PositionDB:
        """Create new position"""
        position = PositionDB(**position_data)
        self.session.add(position)
        await self.session.commit()
        await self.session.refresh(position)
        return position

    async def get_by_id(self, position_id: int) -> Optional[PositionDB]:
        """Get position by ID"""
        result = await self.session.execute(
            select(PositionDB).where(PositionDB.id == position_id)
        )
        return result.scalar_one_or_none()

    async def get_active(self) -> List[PositionDB]:
        """Get all active positions"""
        result = await self.session.execute(
            select(PositionDB).where(
                PositionDB.status.not_in(["closed", "stopped"])
            )
        )
        return result.scalars().all()

    async def update(self, position_id: int, **kwargs) -> bool:
        """Update position"""
        await self.session.execute(
            update(PositionDB).where(PositionDB.id == position_id).values(**kwargs)
        )
        await self.session.commit()
        return True

    async def close_position(
        self,
        position_id: int,
        realized_pnl: float,
        status: str = "closed"
    ) -> bool:
        """Close position"""
        await self.session.execute(
            update(PositionDB).where(PositionDB.id == position_id).values(
                status=status,
                realized_pnl=realized_pnl,
                closed_at=datetime.utcnow()
            )
        )
        await self.session.commit()
        return True

    async def get_by_trade_id(self, trade_id: int) -> Optional[PositionDB]:
        """Get position by trade ID"""
        result = await self.session.execute(
            select(PositionDB).where(PositionDB.trade_id == trade_id)
        )
        return result.scalar_one_or_none()


class RangeRepository:
    """Repository for Range operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, range_data: dict) -> RangeDB:
        """Create new range"""
        range_obj = RangeDB(**range_data)
        self.session.add(range_obj)
        await self.session.commit()
        await self.session.refresh(range_obj)
        return range_obj

    async def get_by_alert_id(self, alert_id: int) -> Optional[RangeDB]:
        """Get range by alert ID"""
        result = await self.session.execute(
            select(RangeDB).where(RangeDB.alert_id == alert_id)
        )
        return result.scalar_one_or_none()


class SweepRepository:
    """Repository for Sweep operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, sweep_data: dict) -> SweepDB:
        """Create new sweep"""
        sweep = SweepDB(**sweep_data)
        self.session.add(sweep)
        await self.session.commit()
        await self.session.refresh(sweep)
        return sweep

    async def get_by_alert_id(self, alert_id: int) -> List[SweepDB]:
        """Get sweeps by alert ID"""
        result = await self.session.execute(
            select(SweepDB).where(SweepDB.alert_id == alert_id)
        )
        return result.scalars().all()
