import logging
import asyncio
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
import os

from .config import settings
from .database.db import init_db
from .api.webhooks import router as webhooks_router, setup_trading
from .api.trades import router as trades_router
from .api.settings import router as settings_router
from .services.websocket_manager import ws_manager
from .services.market_data import MarketDataService
from .trading.bybit_client import BybitClient
from .trading.binance_client import BinanceClient


def create_exchange_client():
    """Фабрика: выбирает биржу через ENV EXCHANGE=bybit|binance"""
    exchange = settings.exchange.lower()
    if exchange == "binance":
        logger.info("Exchange: Binance Futures")
        return BinanceClient(testnet=settings.binance_testnet)
    else:
        logger.info("Exchange: Bybit")
        return BybitClient(testnet=settings.bybit_testnet)
from .trading.risk_manager import RiskManager
from .models.position import PositionStatus
from .trading.trade_executor import TradeExecutor
from .trading.position_manager import PositionManager
from .trading.multi_exchange_executor import MultiExchangeExecutor, MultiPositionManager
from .core.alert_processor import AlertProcessor
from .core.range_detector import RangeDetector
from .core.liquidity_tracker import LiquidityTracker
from .core.confirmation import ConfirmationEngine
from .core.trading_engine import TradingEngine

log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(log_dir, exist_ok=True)

# Root logger
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(log_dir, "bot.log"), encoding="utf-8"),
    ]
)

webhook_logger = logging.getLogger("webhook")
webhook_handler = logging.FileHandler(os.path.join(log_dir, "webhook.log"), encoding="utf-8")
webhook_handler.setFormatter(logging.Formatter(log_format))
webhook_logger.addHandler(webhook_handler)

logger = logging.getLogger(__name__)

_sessions: set = set()  # in-memory, resets on restart

PUBLIC_PATHS = {"/login", "/logout", "/health", "/webhook/trdr", "/webhook/test"}

def is_authenticated(request: Request) -> bool:
    token = request.cookies.get("session")
    return token in _sessions

bybit_client: BybitClient = None
market_data_service: MarketDataService = None
risk_manager: RiskManager = None
trade_executor: TradeExecutor = None
position_manager: PositionManager = None
alert_processor: AlertProcessor = None
range_detector: RangeDetector = None
liquidity_tracker: LiquidityTracker = None
confirmation_engine: ConfirmationEngine = None
trading_engine: TradingEngine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    global bybit_client, market_data_service, risk_manager
    global trade_executor, position_manager
    global alert_processor, range_detector, liquidity_tracker, confirmation_engine
    global trading_engine

    logger.info("Starting BTC Trading Bot...")

    await init_db()

    # Initialize trading components — multi-exchange aware
    risk_manager = RiskManager()
    _exchange_pairs = []  # [(name, client, TradeExecutor, PositionManager)]

    def _make_exchange_pair(name: str, client) -> tuple:
        ex = TradeExecutor(client, risk_manager)
        pm = PositionManager(client, ex, risk_manager)
        return (name, client, ex, pm)

    primary_client = create_exchange_client()
    _exchange_pairs.append(_make_exchange_pair(settings.exchange, primary_client))

    # Additional exchanges from exchanges_enabled setting
    enabled = [e.strip().lower() for e in settings.exchanges_enabled.split(",") if e.strip()]
    for exch in enabled:
        if exch == settings.exchange.lower():
            continue  # skip — already added as primary
        try:
            if exch == "binance" and settings.binance_api_key:
                from .trading.binance_client import BinanceClient
                c = BinanceClient(testnet=settings.binance_testnet)
                _exchange_pairs.append(_make_exchange_pair("binance", c))
            elif exch == "bybit" and settings.bybit_api_key:
                from .trading.bybit_client import BybitClient as BC
                c = BC(testnet=settings.bybit_testnet)
                _exchange_pairs.append(_make_exchange_pair("bybit", c))
            elif exch == "okx" and settings.okx_api_key:
                from .trading.okx_client import OKXClient
                c = OKXClient(testnet=settings.okx_testnet)
                _exchange_pairs.append(_make_exchange_pair("okx", c))
            else:
                logger.info(f"Exchange '{exch}' skipped (no API keys configured)")
        except Exception as e:
            logger.warning(f"Failed to init {exch}: {e}")

    if len(_exchange_pairs) > 1:
        multi_executor = MultiExchangeExecutor([(n, ex) for n, _, ex, _ in _exchange_pairs])
        multi_pm = MultiPositionManager([(n, pm) for n, _, _, pm in _exchange_pairs], multi_executor)
        trade_executor = multi_executor
        position_manager = multi_pm
        bybit_client = primary_client
        logger.info(f"Multi-exchange mode: {[n for n, _, _, _ in _exchange_pairs]}")
    else:
        trade_executor = _exchange_pairs[0][2]
        position_manager = _exchange_pairs[0][3]
        bybit_client = primary_client
        logger.info(f"Single exchange mode: {settings.exchange}")

    alert_processor = AlertProcessor()
    range_detector = RangeDetector(
        timeframe=settings.range_timeframe,
        candles=settings.range_candles
    )
    liquidity_tracker = LiquidityTracker(
        sweep_threshold_percent=settings.sweep_threshold_percent
    )
    confirmation_engine = ConfirmationEngine(
        min_confirmations=settings.min_confirmations
    )

    market_data_service = MarketDataService(bybit_client)
    await market_data_service.start(interval=2.0)
    market_data_service.register_callback(on_price_update)

    trading_engine = TradingEngine(
        bybit_client=bybit_client,
        trade_executor=trade_executor,
        position_manager=position_manager,
        risk_manager=risk_manager
    )
    await trading_engine.start()

    setup_trading(bybit_client, trade_executor, risk_manager, trading_engine, position_manager)

    if not settings.paper_trading:
        for name, client, _, _ in _exchange_pairs:
            try:
                ok = client.set_leverage(symbol="BTCUSDT", leverage=settings.leverage)
                if ok:
                    logger.info(f"Leverage set to {settings.leverage}x on {name}")
                else:
                    logger.warning(f"Failed to set leverage on {name} — check exchange settings manually")
            except Exception as e:
                logger.warning(f"Leverage setup error on {name}: {e}")

    is_testnet = settings.binance_testnet if settings.exchange == "binance" else settings.bybit_testnet
    logger.info(f"Bot initialized (exchange={settings.exchange}, testnet={is_testnet})")

    try:
        for name, client, _, _ in _exchange_pairs:
            real_positions = client.get_positions(symbol="BTCUSDT")
            if not real_positions:
                logger.info(f"Startup sync [{name}]: no open positions")
            else:
                logger.info(f"Startup sync [{name}]: {len(real_positions)} open position(s)")
        position_manager.positions.clear()
        position_manager._trades.clear()
        logger.info("Startup sync: memory cleared")
    except Exception as e:
        logger.warning(f"Startup sync failed: {e}")

    yield

    logger.info("Shutting down...")
    if trading_engine:
        await trading_engine.stop()
    await market_data_service.stop()


_last_exchange_sync: float = 0.0
EXCHANGE_SYNC_INTERVAL = 30  # seconds
_last_alert_cleanup: float = 0.0
ALERT_CLEANUP_INTERVAL = 300  # seconds


async def on_price_update(ticker: dict):
    """Callback for price updates — drives the Diamond signal pipeline and position management"""
    import time
    global _last_exchange_sync
    from .database.db import AsyncSessionLocal
    from .database.repositories import PositionRepository, TradeRepository

    current_price = ticker["last_price"]

    if trading_engine and trading_engine.is_running:
        await trading_engine.on_price_update(current_price)
    else:
        for position in position_manager.get_active_positions():
            position_manager.update_position(position, current_price)

    now = time.time()
    if now - _last_exchange_sync > EXCHANGE_SYNC_INTERVAL:
        _last_exchange_sync = now
        try:
            before = len(position_manager.get_active_positions())
            position_manager.sync_with_exchange()
            after = len(position_manager.get_active_positions())
            if before != after:
                logger.info(f"Sync: {before - after} position(s) closed externally on exchange")
                await ws_manager.send_log(
                    f"🔄 Sync: {before - after} position(s) closed on exchange — UI updated",
                    level="warning", source="sync"
                )
        except Exception as e:
            logger.debug(f"Exchange sync error: {e}")

    global _last_alert_cleanup
    if now - _last_alert_cleanup > ALERT_CLEANUP_INTERVAL:
        _last_alert_cleanup = now
        try:
            from .database.db import AsyncSessionLocal
            from .database.repositories import AlertRepository as AR
            expired = alert_processor.expire_old_alerts(max_age_hours=4)
            async with AsyncSessionLocal() as session:
                repo = AR(session)
                await repo.expire_old_alerts(max_age_hours=4)
            if expired:
                logger.info(f"Alert cleanup: {expired} expired alert(s) removed")
        except Exception as e:
            logger.debug(f"Alert cleanup error: {e}")

    # Include recently closed positions so UI reflects close immediately
    active = position_manager.get_active_positions()
    recently_closed = [
        p for p in position_manager.positions.values()
        if p.status == PositionStatus.CLOSED and p.closed_at is not None
    ]
    to_sync = active + [p for p in recently_closed if p not in active]
    if not to_sync:
        return

    try:
        async with AsyncSessionLocal() as session:
            pos_repo = PositionRepository(session)
            trade_repo = TradeRepository(session)

            for pos in to_sync:
                if not pos.id:
                    continue

                await pos_repo.update(pos.id,
                    current_price=pos.current_price,
                    current_quantity=pos.current_quantity,
                    status=pos.status.value,
                    tp1_filled=pos.tp1_filled,
                    tp2_filled=pos.tp2_filled,
                    trailing_stop=pos.trailing_stop,
                    stop_loss=pos.stop_loss,
                    realized_pnl=pos.realized_pnl,
                    unrealized_pnl=pos.unrealized_pnl,
                    closed_at=pos.closed_at,
                )

                trade = position_manager._trades.get(pos.trade_id)
                if trade and trade.id:
                    await trade_repo.update(trade.id,
                        status=trade.status.value,
                        realized_pnl=trade.realized_pnl,
                        unrealized_pnl=pos.unrealized_pnl,
                        executed_quantity=trade.executed_quantity,
                        closed_at=trade.closed_at,
                    )
    except Exception as e:
        logger.debug(f"DB sync error: {e}")


app = FastAPI(
    title="BTC Trading Bot",
    description="Trading bot with trdr.io signals integration",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Only the dashboard root requires auth; API/webhooks/WebSocket are open
    if path == "/" and not is_authenticated(request):
        return RedirectResponse(url="/login")
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>BTC Bot — Login</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-950 min-h-screen flex items-center justify-center">
<div class="bg-gray-900 rounded-2xl p-8 w-full max-w-sm shadow-2xl border border-gray-800">
    <div class="text-center mb-6">
        <div class="text-4xl mb-2">₿</div>
        <h1 class="text-xl font-bold text-white">BTC Trading Bot</h1>
        <p class="text-gray-500 text-sm mt-1">Dashboard Login</p>
    </div>
    {'<div class="bg-red-900/50 text-red-300 text-sm rounded-lg px-4 py-2 mb-4">Invalid username or password</div>' if error else ''}
    <form method="POST" action="/login" class="space-y-4">
        <div>
            <label class="block text-xs text-gray-400 mb-1">Username</label>
            <input name="username" type="text" autocomplete="username"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-blue-500"
                placeholder="admin"/>
        </div>
        <div>
            <label class="block text-xs text-gray-400 mb-1">Password</label>
            <input name="password" type="password" autocomplete="current-password"
                class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-blue-500"
                placeholder="••••••••"/>
        </div>
        <button type="submit"
            class="w-full bg-blue-600 hover:bg-blue-500 text-white font-semibold py-2.5 rounded-lg transition-colors">
            Login
        </button>
    </form>
</div>
</body></html>"""


@app.post("/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    if username == settings.dashboard_username and password == settings.dashboard_password:
        token = secrets.token_urlsafe(32)
        _sessions.add(token)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400*30)
        return resp
    return RedirectResponse(url="/login?error=1", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    _sessions.discard(token)
    resp = RedirectResponse(url="/login")
    resp.delete_cookie("session")
    return resp

app.include_router(webhooks_router)
app.include_router(trades_router)
app.include_router(settings_router)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates"""
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "testnet": settings.bybit_testnet,
        "ws_connections": ws_manager.get_connection_count()
    }


@app.get("/")
async def root():
    """Serve frontend"""
    frontend_path = os.path.join(
        os.path.dirname(__file__), "..", "frontend", "index.html"
    )
    if os.path.exists(frontend_path):
        return FileResponse(frontend_path)
    return {"message": "BTC Trading Bot API", "docs": "/docs"}


frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=True
    )
