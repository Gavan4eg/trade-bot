import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, JSON, Enum as SQLEnum
from datetime import datetime
from ..config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


class AlertDB(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    alert_type = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    price = Column(Float, nullable=False)
    levels = Column(JSON, default=list)
    status = Column(String, default="pending")
    priority = Column(Integer, default=4)
    raw_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RangeDB(Base):
    __tablename__ = "ranges"

    id = Column(Integer, primary_key=True, index=True)
    alert_id = Column(Integer, nullable=False)
    local_high = Column(Float, nullable=False)
    local_low = Column(Float, nullable=False)
    mid_range = Column(Float)
    width_percent = Column(Float)
    timeframe = Column(String, default="1h")
    candles_used = Column(Integer)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    detected_at = Column(DateTime, default=datetime.utcnow)
    is_valid = Column(Boolean, default=True)


class SweepDB(Base):
    __tablename__ = "sweeps"

    id = Column(Integer, primary_key=True, index=True)
    range_id = Column(Integer, nullable=False)
    alert_id = Column(Integer, nullable=False)
    direction = Column(String, nullable=False)
    sweep_price = Column(Float, nullable=False)
    level_swept = Column(Float, nullable=False)
    wick_size = Column(Float)
    wick_percent = Column(Float)
    volume_spike = Column(Boolean, default=False)
    quick_reversal = Column(Boolean, default=False)
    price_back_in_range = Column(Boolean, default=False)
    confirmation_count = Column(Integer, default=0)
    sweep_time = Column(DateTime, default=datetime.utcnow)
    confirmation_time = Column(DateTime, nullable=True)


class TradeDB(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    alert_id = Column(Integer, nullable=False)
    direction = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit_1 = Column(Float, nullable=False)
    take_profit_2 = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    status = Column(String, default="pending")
    order_id = Column(String, nullable=True)
    executed_price = Column(Float, nullable=True)
    executed_quantity = Column(Float, nullable=True)
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    risk_reward = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)


class PositionDB(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, index=True)
    trade_id = Column(Integer, nullable=False)
    direction = Column(String, nullable=False)
    initial_quantity = Column(Float, nullable=False)
    current_quantity = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, default=0.0)
    stop_loss = Column(Float, nullable=False)
    take_profit_1 = Column(Float, nullable=False)
    take_profit_2 = Column(Float, nullable=False)
    trailing_stop = Column(Float, nullable=True)
    status = Column(String, default="open")
    tp1_filled = Column(Boolean, default=False)
    tp2_filled = Column(Boolean, default=False)
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)


class SettingsDB(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


async def init_db():
    """Initialize database tables"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")


async def get_db():
    """Get database session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
