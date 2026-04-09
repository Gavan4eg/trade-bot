import logging
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Query, Request
from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.db import get_db
from ..database.repositories import AlertRepository
from ..core.alert_processor import AlertProcessor
from ..services.websocket_manager import ws_manager
from ..config import settings

logger = logging.getLogger(__name__)
webhook_logger = logging.getLogger("webhook")  # Separate logger for webhook.log
router = APIRouter(prefix="/webhook", tags=["webhooks"])

# Global alert processor
alert_processor = AlertProcessor()

# Webhook secret token from settings
WEBHOOK_TOKEN = settings.webhook_token

# Trading components — injected from main.py after startup
_trade_executor = None
_risk_manager = None
_bybit_client = None
_trading_engine = None
_position_manager = None


def setup_trading(bybit_client, trade_executor, risk_manager, trading_engine=None, position_manager=None):
    """Called from main.py after all components are initialized."""
    global _bybit_client, _trade_executor, _risk_manager, _trading_engine, _position_manager
    _bybit_client = bybit_client
    _trade_executor = trade_executor
    _risk_manager = risk_manager
    _trading_engine = trading_engine
    _position_manager = position_manager
    logger.info("Trading components wired to webhook handler (engine=%s)", trading_engine is not None)


class WebhookPayload(BaseModel):
    """Incoming webhook payload from trdr.io"""
    type: Optional[str] = None
    symbol: Optional[str] = "BTCUSDT"
    price: Optional[float] = None
    levels: Optional[List[float]] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None
    # Allow any extra fields from trdr.io
    class Config:
        extra = "allow"


@router.api_route("/trdr", methods=["GET", "POST"])
async def receive_trdr_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    token: Optional[str] = Query(None),
    # GET parameters from trdr.io
    type: Optional[str] = Query(None, alias="type"),
    alert_type: Optional[str] = Query(None),
    price: Optional[float] = Query(None),
    symbol: Optional[str] = Query(None),
    message: Optional[str] = Query(None)
):
    """
    Receive webhook from trdr.io

    Expected payload:
    {
        "type": "BTC Diamond" | "BTC Double Diamond" | "Diamond Top Levels" | "Aggregated Liquidation",
        "symbol": "BTCUSDT",
        "price": 65000.00,
        "levels": [64500, 65500],
        "message": "Alert message",
        "timestamp": "2024-01-15T12:00:00Z"
    }
    """
    # Check token if configured
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        logger.warning(f"Invalid webhook token from {request.client.host}")
        raise HTTPException(status_code=403, detail="Invalid token")

    # Handle both GET and POST
    import json

    if request.method == "GET":
        # Build data from query parameters
        raw_data = {
            "type": type or alert_type,
            "price": price,
            "symbol": symbol or "BTCUSDT",
            "message": message
        }
        # Remove None values
        raw_data = {k: v for k, v in raw_data.items() if v is not None}
    else:
        # Get raw body for POST
        raw_body = await request.body()
        try:
            raw_data = json.loads(raw_body) if raw_body else {}
        except:
            raw_data = {"message": raw_body.decode()} if raw_body else {}

    logger.info(f"=== WEBHOOK RECEIVED ===")
    logger.info(f"From: {request.client.host}")
    logger.info(f"Raw data: {raw_data}")

    # Also log to webhook.log file
    webhook_logger.info(f"=== WEBHOOK [{request.method}] ===")
    webhook_logger.info(f"From: {request.client.host}")
    webhook_logger.info(f"Headers: {dict(request.headers)}")
    webhook_logger.info(f"Raw data: {raw_data}")

    # Broadcast raw data to UI for debugging
    await ws_manager.broadcast({
        "type": "webhook_debug",
        "data": {
            "raw": str(raw_data)[:500],
            "from": request.client.host,
            "timestamp": datetime.utcnow().isoformat()
        }
    })
    await ws_manager.send_log(
        f"📨 Webhook received from {request.client.host} | type={raw_data.get('type') or raw_data.get('name','?')} price={raw_data.get('price','?')}",
        level="info", source="webhook"
    )

    # Parse trdr.io format — pass all raw fields directly and let the model handle it
    parsed_data = {}

    if isinstance(raw_data, dict):
        # Copy all raw fields so AlertWebhook model can use them
        parsed_data = dict(raw_data)

        # Normalize: trdr.io uses 'name' for alert type
        if not parsed_data.get("type") and parsed_data.get("name"):
            parsed_data["type"] = parsed_data["name"]

        # Normalize: trdr.io uses 'time' for timestamp
        if not parsed_data.get("timestamp") and parsed_data.get("time"):
            parsed_data["timestamp"] = parsed_data["time"]

        # Normalize: get price from markets array if present (old format)
        markets = parsed_data.get("markets", [])
        if markets and len(markets) > 0:
            parsed_data.setdefault("price", markets[0].get("price"))
            parsed_data.setdefault("symbol", markets[0].get("ticker", "BTCUSDT"))

        webhook_logger.info(
            f"Parsed trdr.io format: type={parsed_data.get('type')}, "
            f"price={parsed_data.get('price')}, side={parsed_data.get('side')}, "
            f"ticker={parsed_data.get('ticker')}"
        )
    else:
        parsed_data = {"message": str(raw_data)}

    # Try to parse payload
    try:
        payload = WebhookPayload(**parsed_data)
    except Exception as e:
        logger.error(f"Failed to parse payload: {e}")
        payload = WebhookPayload(**parsed_data)

    # Try to extract alert type from message if type is missing
    alert_type = payload.type
    if not alert_type and payload.message:
        msg = payload.message.lower()
        if "double diamond" in msg:
            alert_type = "BTC Double Diamond"
        elif "diamond top" in msg:
            alert_type = "Diamond Top Levels"
        elif "diamond" in msg:
            alert_type = "BTC Diamond"
        elif "liquidation" in msg:
            alert_type = "Aggregated Liquidation"
        if alert_type:
            payload.type = alert_type

    logger.info(f"Parsed: type={payload.type}, price={payload.price}, side={parsed_data.get('side')}")

    # Parse webhook
    alert = alert_processor.parse_webhook(payload.model_dump())

    if not alert:
        await ws_manager.send_log(f"❌ Invalid alert type: {payload.type}", level="error", source="webhook")
        raise HTTPException(status_code=400, detail="Invalid alert type")

    # Check if should process
    if not alert_processor.should_process(alert):
        await ws_manager.send_log(f"⏭ Alert skipped (cooldown or priority): {alert.alert_type.value}", level="warning", source="webhook")
        return {
            "status": "skipped",
            "reason": "Alert filtered (cooldown or priority)"
        }

    # Save to database
    repo = AlertRepository(db)
    db_alert = await repo.create({
        "alert_type": alert.alert_type.value,
        "timestamp": alert.timestamp,
        "price": alert.price,
        "levels": alert.levels,
        "status": alert.status.value,
        "priority": alert.priority,
        "raw_data": alert.raw_data
    })

    alert.id = db_alert.id

    # Register alert
    alert_processor.register_alert(alert)

    # Broadcast to WebSocket clients
    await ws_manager.send_alert({
        "id": alert.id,
        "type": alert.alert_type.value,
        "price": alert.price,
        "levels": alert.levels,
        "status": alert.status.value,
        "priority": alert.priority,
        "timestamp": alert.timestamp.isoformat()
    })

    await ws_manager.send_log(
        f"✅ Alert #{alert.id} saved | {alert.alert_type.value} @ ${alert.price:,.0f}",
        level="success", source="webhook"
    )

    # Process alert in background (pass parsed_data so price/type are normalized)
    background_tasks.add_task(process_alert_background, alert, parsed_data)

    return {
        "status": "received",
        "alert_id": alert.id,
        "type": alert.alert_type.value,
        "price": alert.price
    }


async def process_alert_background(alert, alert_raw: dict):
    """
    Background task — routes alert to the correct handler:

    • Aggregated Liquidation →
        Инжектируется как подтверждающий фактор в активный Diamond алерт.
        Если нет активного Diamond — игнорируется (не торгуем без Diamond сигнала).

    • BTC Diamond / BTC Double Diamond / Diamond Top Levels →
        Запуск полного пайплайна в TradingEngine:
        range detection → liquidity sweep → confirmation → trade.
        Liquidation усиливает confirmation если приходит во время пайплайна.
    """
    from ..database.db import AsyncSessionLocal
    from ..database.repositories import TradeRepository, PositionRepository, AlertRepository

    alert_id = alert.id
    alert_type = alert_raw.get("type") or alert_raw.get("name", "")

    webhook_logger.info(
        f"[BG] Alert {alert_id} | type={alert_type} | "
        f"side={alert_raw.get('side')} | price={alert_raw.get('price')}"
    )

    if _trade_executor is None or _bybit_client is None or _risk_manager is None:
        logger.warning(f"Alert {alert_id}: trading components not wired, skipping execution")
        return

    # ── Aggregated Liquidation: подтверждающий фактор для Diamond ──────────
    if "Aggregated Liquidation" in alert_type:
        side = (alert_raw.get("side") or "").lower()

        webhook_logger.info(
            f"[LIQUIDATION] Alert {alert_id} | side={side} | price={alert_raw.get('price')} | "
            f"Ищем активный Diamond алерт в пайплайне..."
        )

        if _trading_engine is None:
            logger.warning(f"Alert {alert_id}: TradingEngine не инициализирован")
            return

        injected = _trading_engine.inject_liquidation(alert_raw)

        if injected:
            logger.info(
                f"Alert {alert_id} [LIQUIDATION]: "
                f"инжектирован как подтверждение в активный Diamond алерт"
            )
            webhook_logger.info(
                f"[LIQUIDATION → CONFIRMATION] Alert {alert_id} | "
                f"side={side} → добавлен как confirming factor"
            )
            await ws_manager.send_log(
                f"💧 Liquidation #{alert_id} → injected as confirmation factor (side={side})",
                level="success", source="pipeline"
            )
        else:
            logger.info(
                f"Alert {alert_id} [LIQUIDATION]: нет активного Diamond алерта — игнорируем. "
                f"(Liquidation без Diamond не торгуем)"
            )
            webhook_logger.info(
                f"[LIQUIDATION IGNORED] Alert {alert_id} | нет Diamond в пайплайне"
            )
            await ws_manager.send_log(
                f"💧 Liquidation #{alert_id} ignored — no active Diamond in pipeline",
                level="warning", source="pipeline"
            )

    # ── Diamond сигналы: полный пайплайн ────────────────────────────────────
    elif any(t in alert_type for t in ("BTC Diamond", "BTC Double Diamond", "Diamond Top Levels")):
        if _trading_engine is None:
            logger.warning(f"Alert {alert_id}: TradingEngine не инициализирован")
            return

        if not _trading_engine.is_running:
            logger.warning(f"Alert {alert_id}: TradingEngine остановлен, запустите бот")
            return

        logger.info(
            f"Alert {alert_id} [{alert_type}]: передаю в пайплайн "
            f"(range → sweep → confirmation → trade)"
        )
        webhook_logger.info(
            f"[PIPELINE] Alert {alert_id} | type={alert_type} | запуск пайплайна"
        )

        await ws_manager.send_log(
            f"🔷 Diamond #{alert_id} [{alert_type}] → starting pipeline (range → sweep → confirm → trade)",
            level="info", source="pipeline"
        )
        success = await _trading_engine.process_alert_direct(alert)

        if success:
            logger.info(f"Alert {alert_id}: пайплайн запущен успешно")
            webhook_logger.info(f"[PIPELINE OK] Alert {alert_id} | пайплайн активен")
            await ws_manager.send_log(
                f"🔷 Diamond #{alert_id} pipeline started successfully",
                level="success", source="pipeline"
            )
        else:
            logger.warning(f"Alert {alert_id}: пайплайн не запущен (движок занят или остановлен)")
            webhook_logger.warning(f"[PIPELINE SKIP] Alert {alert_id}")
            await ws_manager.send_log(
                f"⚠️ Diamond #{alert_id} pipeline skipped (engine busy or stopped)",
                level="warning", source="pipeline"
            )

    else:
        logger.info(f"Alert {alert_id}: тип '{alert_type}' не требует торговли")


@router.get("/test")
async def test_webhook():
    """Test endpoint to verify webhook is working"""
    return {"status": "ok", "message": "Webhook endpoint is active"}


@router.post("/test-set-price")
async def set_test_price(price: float):
    """Установить симулированную цену (только paper trading)."""
    if _bybit_client is None:
        return {"error": "bybit_client not initialized"}
    if not _bybit_client.paper_trading:
        return {"error": "Доступно только в paper trading режиме"}
    _bybit_client.set_simulated_price(price)
    return {"status": "ok", "simulated_price": price}


@router.get("/test-positions")
async def get_test_positions():
    """
    Вернуть in-memory позиции из PositionManager.
    Актуальнее чем /api/positions (которые из БД) во время теста.
    """
    if _position_manager is None:
        return []
    result = []
    for pos in _position_manager.get_active_positions():
        result.append({
            "trade_id": pos.trade_id,
            "direction": pos.direction.value,
            "entry_price": pos.entry_price,
            "current_price": pos.current_price,
            "stop_loss": pos.stop_loss,
            "take_profit_1": pos.take_profit_1,
            "take_profit_2": pos.take_profit_2,
            "trailing_stop": pos.trailing_stop,
            "status": pos.status.value,
            "tp1_filled": pos.tp1_filled,
            "tp2_filled": pos.tp2_filled,
            "quantity": pos.current_quantity,
            "realized_pnl": pos.realized_pnl,
            "unrealized_pnl": pos.unrealized_pnl,
        })
    return result


@router.get("/test-engine-state")
async def get_engine_state():
    """
    Вернуть состояние Diamond пайплайна (активные алерты, range, sweep).
    """
    if _trading_engine is None:
        return {"error": "TradingEngine не инициализирован"}

    states = []
    for alert_id, state in _trading_engine.active_states.items():
        states.append({
            "alert_id": alert_id,
            "alert_type": state.alert.alert_type.value,
            "alert_status": state.alert.status.value,
            "alert_price": state.alert.price,
            "last_price": state.last_price,
            "range": {
                "local_high": state.range.local_high,
                "local_low": state.range.local_low,
                "width_percent": state.range.width_percent,
            } if state.range else None,
            "sweep": {
                "direction": state.sweep.direction.value,
                "sweep_price": state.sweep.sweep_price,
            } if state.sweep else None,
            "has_trade": state.trade is not None,
        })

    ticker = _bybit_client.get_ticker() if _bybit_client else None
    return {
        "is_running": _trading_engine.is_running,
        "active_states": states,
        "current_price": ticker["last_price"] if ticker else None,
    }


@router.post("/test-force-sweep")
async def force_sweep(alert_id: int = 0):
    """
    Принудительно инжектировать sweep в активный алерт (только для тестов).
    Пропускает ожидание реального движения цены.
    """
    if _trading_engine is None:
        return {"error": "TradingEngine не инициализирован"}

    from ..models.sweep import SweepEvent, SweepDirection
    from ..models.alert import AlertStatus

    try:
        # Найти нужный state
        state = None
        if alert_id and alert_id in _trading_engine.active_states:
            state = _trading_engine.active_states[alert_id]
        elif _trading_engine.active_states:
            state = next(iter(_trading_engine.active_states.values()))

        if not state:
            return {"error": "Нет активных алертов в пайплайне"}

        # Создаём sweep event (LOW = sweep ниже range → LONG)
        ticker = _bybit_client.get_ticker() if _bybit_client else None
        current_price = ticker["last_price"] if ticker else (state.last_price or state.alert.price)
        level_swept = state.range.local_low if state.range else current_price * 0.995
        sweep_price = level_swept * 0.995  # чуть ниже low

        sweep = SweepEvent(
            alert_id=state.alert.id or 0,
            range_id=0,
            direction=SweepDirection.LOW,
            sweep_price=sweep_price,
            level_swept=level_swept,
            timestamp=datetime.utcnow()
        )

        state.sweep = sweep
        state.alert.status = AlertStatus.SWEEP_DETECTED

        logger.info(f"[TEST] Force sweep injected for alert {state.alert.id}: LOW at {sweep_price:.2f}")

        # Триггерим confirmation
        await _trading_engine._check_for_confirmation(state, current_price)

        return {
            "status": "sweep_injected",
            "alert_id": state.alert.id,
            "alert_status": state.alert.status.value,
            "sweep_price": sweep_price,
            "current_price": current_price
        }
    except Exception as e:
        logger.exception(f"[TEST] force_sweep error: {e}")
        return {"error": str(e)}


@router.post("/test-alert")
async def send_test_alert(
    alert_type: str = "BTC Diamond",
    price: float = 65000.0
):
    """Send a test alert for development"""
    test_payload = WebhookPayload(
        type=alert_type,
        symbol="BTCUSDT",
        price=price,
        levels=[price * 0.99, price * 1.01],
        message="Test alert",
        timestamp=datetime.utcnow().isoformat()
    )

    # Broadcast test alert to WebSocket
    await ws_manager.send_alert({
        "id": 0,
        "type": alert_type,
        "price": price,
        "levels": test_payload.levels,
        "status": "test",
        "priority": 2,
        "timestamp": test_payload.timestamp
    })

    return {
        "status": "test_sent",
        "type": alert_type,
        "price": price
    }
