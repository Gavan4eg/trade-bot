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
from .trading.risk_manager import RiskManager
from .trading.trade_executor import TradeExecutor
from .trading.position_manager import PositionManager
from .core.alert_processor import AlertProcessor
from .core.range_detector import RangeDetector
from .core.liquidity_tracker import LiquidityTracker
from .core.confirmation import ConfirmationEngine
from .core.trading_engine import TradingEngine

# Configure logging to both console and file
log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(log_dir, exist_ok=True)

# Root logger
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.StreamHandler(),  # Console
        logging.FileHandler(os.path.join(log_dir, "bot.log"), encoding="utf-8"),  # File
    ]
)

# Webhook specific logger
webhook_logger = logging.getLogger("webhook")
webhook_handler = logging.FileHandler(os.path.join(log_dir, "webhook.log"), encoding="utf-8")
webhook_handler.setFormatter(logging.Formatter(log_format))
webhook_logger.addHandler(webhook_handler)

logger = logging.getLogger(__name__)

# Auth sessions (in-memory, resets on restart)
_sessions: set = set()

# Public routes — no auth required
PUBLIC_PATHS = {"/login", "/logout", "/health", "/webhook/trdr", "/webhook/test"}

def is_authenticated(request: Request) -> bool:
    token = request.cookies.get("session")
    return token in _sessions

# Global services
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

    # Initialize database
    await init_db()

    # Initialize trading components
    bybit_client = BybitClient(testnet=settings.bybit_testnet)
    risk_manager = RiskManager()
    trade_executor = TradeExecutor(bybit_client, risk_manager)
    position_manager = PositionManager(bybit_client, trade_executor, risk_manager)

    # Initialize core components
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

    # Initialize market data service
    market_data_service = MarketDataService(bybit_client)

    # Start market data polling
    await market_data_service.start(interval=2.0)

    # Register price callback for trading logic
    market_data_service.register_callback(on_price_update)

    # Initialize and start trading engine (Diamond signal pipeline)
    trading_engine = TradingEngine(
        bybit_client=bybit_client,
        trade_executor=trade_executor,
        position_manager=position_manager,
        risk_manager=risk_manager
    )
    await trading_engine.start()

    # Wire trading components + engine into the webhook handler
    setup_trading(bybit_client, trade_executor, risk_manager, trading_engine, position_manager)

    logger.info(f"Bot initialized (testnet={settings.bybit_testnet})")

    yield

    # Shutdown
    logger.info("Shutting down...")
    if trading_engine:
        await trading_engine.stop()
    await market_data_service.stop()


async def on_price_update(ticker: dict):
    """Callback for price updates — drives the Diamond signal pipeline and position management"""
    from .database.db import AsyncSessionLocal
    from .database.repositories import PositionRepository, TradeRepository

    current_price = ticker["last_price"]

    # Feed price to trading engine (handles sweep detection + confirmation for Diamond signals)
    if trading_engine and trading_engine.is_running:
        await trading_engine.on_price_update(current_price)
    else:
        for position in position_manager.get_active_positions():
            position_manager.update_position(position, current_price)

    # Sync in-memory position state → DB after every price update
    active = position_manager.get_active_positions()
    if not active:
        return

    try:
        async with AsyncSessionLocal() as session:
            pos_repo = PositionRepository(session)
            trade_repo = TradeRepository(session)

            for pos in active:
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

                # Sync trade PnL
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


# Create FastAPI app
app = FastAPI(
    title="BTC Trading Bot",
    description="Trading bot with trdr.io signals integration",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CORS middleware
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
    # Only protect the dashboard HTML page (/)
    # API, webhooks, WebSocket — all open
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

# Include routers
app.include_router(webhooks_router)
app.include_router(trades_router)
app.include_router(settings_router)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates"""
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive
            data = await websocket.receive_text()
            # Handle any incoming messages if needed
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


# Mount static files for frontend
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
