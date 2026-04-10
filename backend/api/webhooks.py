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
    _log_price = raw_data.get('price') or (raw_data.get('markets') or [{}])[0].get('price', '?')
    await ws_manager.send_log(
        f"📨 Webhook received from {request.client.host} | type={raw_data.get('type') or raw_data.get('name','?')} price={_log_price}",
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

    # Aggregated Liquidation — не сохраняем в БД и не показываем в UI
    # Просто инжектируем в пайплайн как confirmation фактор
    is_liquidation = "Aggregated Liquidation" in (payload.type or "")

    if is_liquidation:
        await ws_manager.send_log(
            f"💧 Liquidation received @ ${alert.price:,.0f} — injecting into pipeline",
            level="info", source="webhook"
        )
        background_tasks.add_task(process_alert_background, alert, parsed_data)
        return {"status": "received", "type": "liquidation", "price": alert.price}

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


@router.post("/test-full-trade")
async def test_full_trade(
    direction: str = "long",
    close_after_seconds: int = 60
):
    """
    Полная имитация торгового цикла на РЕАЛЬНЫХ ключах:
    1. Создаёт Diamond алерт с текущей ценой
    2. Определяет реальный range
    3. Инжектирует sweep
    4. Форсирует confirmation
    5. Открывает РЕАЛЬНЫЙ ордер на Bybit
    6. Через close_after_seconds закрывает позицию
    """
    if _trading_engine is None or _bybit_client is None:
        return {"error": "Trading engine not initialized"}

    from ..models.alert import Alert, AlertType, AlertStatus
    from ..models.sweep import SweepEvent, SweepDirection
    from ..models.range import Range

    try:
        ticker = _bybit_client.get_ticker()
        if not ticker:
            return {"error": "Cannot get ticker"}

        current_price = ticker["last_price"]

        await ws_manager.send_log(
            f"🧪 TEST TRADE STARTED | direction={direction} | price=${current_price:,.0f} | closes in {close_after_seconds}s",
            level="warning", source="test"
        )

        # 1. Создаём фейковый Diamond алерт
        alert = Alert(
            id=99999,
            alert_type=AlertType.BTC_DIAMOND,
            price=current_price,
            status=AlertStatus.PROCESSING,
            priority=2,
            raw_data={"type": "BTC Diamond", "test": True}
        )

        # 2. Реальный range с биржи
        candles = _bybit_client.get_klines(interval="60", limit=24)
        if not candles:
            return {"error": "Cannot get candles"}

        highs = [c["high"] for c in candles]
        lows  = [c["low"]  for c in candles]
        local_high = max(highs)
        local_low  = min(lows)

        now = datetime.utcnow()
        price_range = Range(
            alert_id=99999,
            local_high=local_high,
            local_low=local_low,
            timeframe="1h",
            candles_count=24,
            start_time=now,
            end_time=now
        )

        # 3. Sweep
        sweep_dir = SweepDirection.LOW if direction == "long" else SweepDirection.HIGH
        if direction == "long":
            sweep_price = local_low * 0.998
            level_swept = local_low
        else:
            sweep_price = local_high * 1.002
            level_swept = local_high

        sweep = SweepEvent(
            alert_id=99999,
            range_id=0,
            direction=sweep_dir,
            sweep_price=sweep_price,
            level_swept=level_swept,
            timestamp=datetime.utcnow()
        )

        # 4. Форсируем confirmation напрямую
        from ..core.confirmation import ConfirmationResult
        entry_price = current_price
        if direction == "long":
            stop_loss = round(sweep_price * 0.998, 2)
        else:
            stop_loss = round(sweep_price * 1.002, 2)

        confirmation = ConfirmationResult(
            is_confirmed=True,
            confirmations_met=3,
            required_confirmations=2,
            trade_direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
        )

        # 5. Открываем реальную позицию
        from ..models.alert import AlertState
        from ..core.trading_engine import AlertState as TradingAlertState
        state = TradingAlertState(alert=alert)
        state.range = price_range
        state.sweep = sweep
        state.last_price = current_price

        await _trading_engine._execute_trade(state, confirmation)

        await ws_manager.send_log(
            f"🧪 TEST: Trade executed! Closing in {close_after_seconds}s...",
            level="success", source="test"
        )

        # 6. Закрываем через N секунд
        async def close_after_delay():
            import asyncio
            await asyncio.sleep(close_after_seconds)
            positions = _bybit_client.get_positions(symbol="BTCUSDT")
            if positions:
                pos = positions[0]
                side = "Sell" if pos["side"] == "Buy" else "Buy"
                qty = pos["size"]
                _bybit_client.place_order(side=side, qty=qty, order_type="Market", reduce_only=True)
                await ws_manager.send_log(
                    f"🧪 TEST: Position closed after {close_after_seconds}s | PnL will show on Bybit",
                    level="success", source="test"
                )
            else:
                await ws_manager.send_log(
                    f"🧪 TEST: No position found to close (may have been closed by SL/TP)",
                    level="warning", source="test"
                )

        import asyncio
        asyncio.ensure_future(close_after_delay())

        return {
            "status": "test_trade_started",
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "close_after_seconds": close_after_seconds,
            "range": {"high": local_high, "low": local_low}
        }

    except Exception as e:
        logger.exception(f"[TEST] full_trade error: {e}")
        await ws_manager.send_log(f"🧪 TEST ERROR: {e}", level="error", source="test")
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
