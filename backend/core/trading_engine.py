import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass, field

from ..models.alert import Alert, AlertStatus, AlertType
from ..models.range import Range
from ..models.sweep import SweepEvent, SweepDirection
from ..models.trade import Trade, TradeDirection
from .alert_processor import AlertProcessor
from .range_detector import RangeDetector
from .liquidity_tracker import LiquidityTracker
from .confirmation import ConfirmationEngine, ConfirmationResult
from ..trading.bybit_client import BybitClient
from ..trading.trade_executor import TradeExecutor
from ..trading.position_manager import PositionManager
from ..trading.risk_manager import RiskManager
from ..services.websocket_manager import ws_manager
from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class AlertState:
    """Track state of an alert through the trading pipeline"""
    alert: Alert
    range: Optional[Range] = None
    sweep: Optional[SweepEvent] = None
    confirmation: Optional[ConfirmationResult] = None
    trade: Optional[Trade] = None
    last_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    range_detected_at: Optional[datetime] = None  # когда range был обнаружен
    # Последняя ликвидация — для ConfirmationEngine
    pending_liquidation: Optional[dict] = None
    # Кластер всех ликвидаций — для расчёта стопа за зону дисбаланса
    liquidation_cluster: List[dict] = field(default_factory=list)


class TradingEngine:
    """
    Main trading engine that orchestrates the full trading pipeline:
    1. Receive alert from trdr.io
    2. Detect local range
    3. Wait for liquidity sweep
    4. Confirm entry
    5. Execute trade
    6. Manage position
    """

    def __init__(
        self,
        bybit_client: BybitClient,
        trade_executor: TradeExecutor,
        position_manager: PositionManager,
        risk_manager: RiskManager
    ):
        self.client = bybit_client
        self.executor = trade_executor
        self.position_manager = position_manager
        self.risk_manager = risk_manager

        # Core components
        self.alert_processor = AlertProcessor()
        self.range_detector = RangeDetector(
            timeframe=settings.range_timeframe,
            candles=settings.range_candles,
            max_width_percent=settings.range_max_width_percent
        )
        self.liquidity_tracker = LiquidityTracker(
            sweep_threshold_percent=settings.sweep_threshold_percent
        )
        self.confirmation_engine = ConfirmationEngine(
            min_confirmations=settings.min_confirmations
        )

        # State tracking
        self.active_states: Dict[int, AlertState] = {}
        self.is_running = False

        # Timing
        self.range_wait_hours = 4  # Max time to wait for range formation
        self.sweep_wait_hours = 4  # Max time to wait for sweep after range

        # Klines cache to avoid rate limit (cache for 60 seconds)
        self._klines_cache: Optional[list] = None
        self._klines_cache_time: Optional[datetime] = None
        self._klines_cache_ttl = 5  # seconds

    async def start(self):
        """Start the trading engine"""
        self.is_running = True
        logger.info("Trading engine started")

    async def stop(self):
        """Stop the trading engine"""
        self.is_running = False
        logger.info("Trading engine stopped")

    def inject_liquidation(self, liquidation_raw: dict) -> bool:
        """
        Инжектировать данные ликвидации в активный Diamond алерт.

        Вызывается когда приходит Aggregated Liquidation вебхук.
        Если есть активный Diamond алерт в пайплайне — добавляем ликвидацию
        как подтверждающий фактор и немедленно триггерим проверку confirmation.

        Returns True если был активный алерт, False если некуда применить.
        """
        if not self.active_states:
            return False

        # Берём Diamond алерт с наивысшим приоритетом
        best_state = min(
            self.active_states.values(),
            key=lambda s: s.alert.priority
        )

        # Принимаем ликвидацию только если алерт уже прошёл range detection
        if best_state.alert.status not in [
            "waiting_sweep", "sweep_detected", "confirmed",
            "range_detected"
        ]:
            logger.info(
                f"Liquidation injection ignored — alert {best_state.alert.id} "
                f"status={best_state.alert.status.value} (pipeline not ready)"
            )
            return False

        # Извлекаем данные ликвидаций из вебхука
        side = (liquidation_raw.get("side") or "").lower()
        price = liquidation_raw.get("price") or best_state.last_price
        message = liquidation_raw.get("message", "")

        # Парсим объём ликвидаций из message (формат: "Shorts: 5336600 > 2000000")
        liq_volume = 0.0
        import re
        match = re.search(r'(\d+)\s*>', message)
        if match:
            liq_volume = float(match.group(1))

        liquidation_data = {
            "side": side,
            "price": price,
            "volume": liq_volume,
            # Для ConfirmationEngine
            "long_liquidations": liq_volume if side == "long" else 0,
            "short_liquidations": liq_volume if side == "short" else 0,
            "avg_liquidations": 2_000_000,  # порог из алерта trdr.io
            "price_at_spike": price,
        }

        # Обновляем текущую ликвидацию (для ConfirmationEngine)
        best_state.pending_liquidation = liquidation_data
        # Накапливаем кластер (для расчёта стопа за зону дисбаланса)
        best_state.liquidation_cluster.append(liquidation_data)

        cluster_size = len(best_state.liquidation_cluster)
        logger.info(
            f"Liquidation injected into alert {best_state.alert.id} "
            f"({best_state.alert.alert_type.value}): "
            f"side={side}, volume={liq_volume:,.0f}, price=${price:,.2f} "
            f"[cluster size: {cluster_size}]"
        )
        return True

    def get_highest_priority_active_state(self):
        """Вернуть алерт с наивысшим приоритетом из активных."""
        if not self.active_states:
            return None
        return min(self.active_states.values(), key=lambda s: s.alert.priority)

    async def process_alert(self, alert: Alert) -> bool:
        """
        Process new alert from trdr.io

        Returns True if alert was accepted for processing
        """
        if not self.is_running:
            logger.warning("Trading engine is not running")
            return False

        # Validate and check if should process
        if not self.alert_processor.should_process(alert):
            logger.info(f"Alert {alert.id} filtered out")
            return False

        # Register alert
        self.alert_processor.register_alert(alert)

        # Create state tracking
        state = AlertState(alert=alert)
        self.active_states[alert.id] = state

        # Update alert status
        alert.status = AlertStatus.PROCESSING
        await self._broadcast_alert_update(alert)

        logger.info(f"Alert {alert.id} ({alert.alert_type.value}) accepted for processing")

        # Start range detection
        asyncio.create_task(self._detect_range_for_alert(alert.id))

        return True

    async def process_alert_direct(self, alert: Alert) -> bool:
        """
        Process a pre-validated alert — skips duplicate validation/registration
        since the webhook handler already did that.

        Used for Diamond / Double Diamond / Diamond Top Levels signals.
        """
        if not self.is_running:
            logger.warning("Trading engine is not running, cannot process alert")
            return False

        if alert.id in self.active_states:
            logger.info(f"Alert {alert.id} already in pipeline, skipping")
            return False

        state = AlertState(alert=alert)
        self.active_states[alert.id] = state

        alert.status = AlertStatus.PROCESSING
        await self._broadcast_alert_update(alert)

        logger.info(
            f"Alert {alert.id} ({alert.alert_type.value}) entered pipeline "
            f"(range → sweep → confirmation → trade)"
        )

        asyncio.create_task(self._detect_range_for_alert(alert.id))
        return True

    async def _detect_range_for_alert(self, alert_id: int):
        """Detect local range after alert"""
        state = self.active_states.get(alert_id)
        if not state:
            return

        logger.info(f"Starting range detection for alert {alert_id}")

        # Wait for range formation (check periodically)
        start_time = datetime.utcnow()
        max_wait = timedelta(hours=self.range_wait_hours)

        while datetime.utcnow() - start_time < max_wait:
            if not self.is_running:
                break

            # Fetch candle data
            candles = self.client.get_klines(
                interval=self._timeframe_to_interval(settings.range_timeframe),
                limit=settings.range_candles
            )

            if candles:
                # Detect range
                detected_range = self.range_detector.detect_range(
                    state.alert, candles
                )

                if detected_range and detected_range.is_valid:
                    state.range = detected_range
                    state.range_detected_at = datetime.utcnow()
                    state.alert.status = AlertStatus.RANGE_DETECTED

                    await self._broadcast_range_update(detected_range)
                    await self._broadcast_alert_update(state.alert)

                    logger.info(
                        f"Range detected for alert {alert_id}: "
                        f"H={detected_range.local_high}, L={detected_range.local_low}, "
                        f"Width={detected_range.width_percent}%"
                    )
                    await ws_manager.send_log(
                        f"📐 Range detected for alert #{alert_id}: "
                        f"H=${detected_range.local_high:,.0f} L=${detected_range.local_low:,.0f} "
                        f"({detected_range.width_percent:.1f}%) → waiting for sweep",
                        level="info", source="pipeline"
                    )

                    # Move to sweep detection
                    state.alert.status = AlertStatus.WAITING_SWEEP
                    await self._broadcast_alert_update(state.alert)
                    return

            # Wait before next check
            await asyncio.sleep(60)  # Check every minute

        # Timeout - expire alert
        logger.warning(f"Range detection timed out for alert {alert_id}")
        await ws_manager.send_log(
            f"⌛ Alert #{alert_id} expired — range not detected in {self.range_wait_hours}h",
            level="warning", source="pipeline"
        )
        state.alert.status = AlertStatus.EXPIRED
        await self._broadcast_alert_update(state.alert)
        self._cleanup_state(alert_id)

    async def on_price_update(self, price: float):
        """Called on each price update to check for sweeps and confirmations"""
        if not self.is_running:
            return

        for alert_id, state in list(self.active_states.items()):
            state.last_price = price

            # Check sweep timeout
            if state.alert.status == AlertStatus.WAITING_SWEEP and state.range:
                detected_at = state.range_detected_at or state.created_at
                sweep_age = datetime.utcnow() - detected_at
                if sweep_age > timedelta(hours=self.sweep_wait_hours):
                    logger.info(f"Alert {alert_id} sweep timeout after {self.sweep_wait_hours}h — cleaning up")
                    await ws_manager.send_log(
                        f"⌛ Alert #{alert_id} expired — sweep not detected in {self.sweep_wait_hours}h",
                        level="warning", source="pipeline"
                    )
                    state.alert.status = AlertStatus.REJECTED
                    await self._broadcast_alert_update(state.alert)
                    self._cleanup_state(alert_id)
                    continue
                await self._check_for_sweep(state, price)

            # Check for confirmation if sweep detected
            elif state.alert.status == AlertStatus.SWEEP_DETECTED and state.sweep:
                await self._check_for_confirmation(state, price)

        # Update active positions
        for position in self.position_manager.get_active_positions():
            self.position_manager.update_position(position, price)

    async def _check_for_sweep(self, state: AlertState, price: float):
        """Check if price has swept liquidity"""
        sweep = self.liquidity_tracker.detect_sweep(
            current_price=price,
            price_range=state.range
        )

        if sweep and self.liquidity_tracker.is_valid_sweep(sweep):
            state.sweep = sweep
            state.alert.status = AlertStatus.SWEEP_DETECTED

            self.liquidity_tracker.register_sweep(sweep)

            await self._broadcast_sweep_update(sweep)
            await self._broadcast_alert_update(state.alert)

            logger.info(
                f"Sweep detected for alert {state.alert.id}: "
                f"{sweep.direction.value} sweep at {sweep.sweep_price}"
            )
            await ws_manager.send_log(
                f"⚡ Sweep detected for alert #{state.alert.id}: "
                f"{sweep.direction.value.upper()} @ ${sweep.sweep_price:,.0f} → checking confirmation",
                level="success", source="pipeline"
            )

    def _get_cached_klines(self) -> list:
        """Get klines with 60s cache to avoid rate limit"""
        now = datetime.utcnow()
        if (self._klines_cache is not None and
                self._klines_cache_time is not None and
                (now - self._klines_cache_time).total_seconds() < self._klines_cache_ttl):
            return self._klines_cache
        candles = self.client.get_klines(interval="5", limit=12)
        if candles:
            self._klines_cache = candles
            self._klines_cache_time = now
        return candles or []

    async def _check_for_confirmation(self, state: AlertState, price: float):
        """Check if entry is confirmed after sweep"""
        # Require price to return back inside range after sweep (confirms reversal)
        if not self.liquidity_tracker.check_price_back_in_range(price, state.range, state.sweep):
            logger.debug(
                f"Alert #{state.alert.id}: price ${price:,.0f} not yet back in range "
                f"[{state.range.local_low:,.0f}–{state.range.local_high:,.0f}] — waiting for reversal"
            )
            return

        candles = self._get_cached_klines()  # cached, max 1 request per 60s

        result = self.confirmation_engine.check_confirmation(
            sweep=state.sweep,
            price_range=state.range,
            current_price=price,
            recent_candles=candles,
            liquidation_data=state.pending_liquidation  # подтверждение от Liquidation
        )

        if result.is_confirmed:
            state.confirmation = result
            state.alert.status = AlertStatus.CONFIRMED

            await self._broadcast_alert_update(state.alert)

            logger.info(
                f"Entry confirmed for alert {state.alert.id}: "
                f"direction={result.trade_direction}, confirmations={result.confirmations_met}"
            )
            await ws_manager.send_log(
                f"✅ Entry confirmed for alert #{state.alert.id}: "
                f"{result.trade_direction.upper()} entry=${result.entry_price:,.0f} "
                f"SL=${result.stop_loss:,.0f} ({result.confirmations_met} confirmations) → executing trade",
                level="success", source="pipeline"
            )

            # Execute trade
            await self._execute_trade(state, result)

    async def _execute_trade(self, state: AlertState, confirmation: ConfirmationResult):
        """Execute trade based on confirmed signal"""
        direction = TradeDirection.LONG if confirmation.trade_direction == "long" else TradeDirection.SHORT

        if not self.risk_manager.can_open_position(direction.value):
            logger.warning(f"Cannot open {direction.value} position - risk limits")
            await ws_manager.send_log(
                f"🚫 Alert #{state.alert.id} rejected — risk limits exceeded (max positions or daily loss)",
                level="error", source="pipeline"
            )
            state.alert.status = AlertStatus.REJECTED
            await self._broadcast_alert_update(state.alert)
            self._cleanup_state(state.alert.id)
            return

        # Если есть кластер ликвидаций — стоп за зону дисбаланса
        stop_loss = confirmation.stop_loss
        if state.liquidation_cluster:
            cluster_copy = list(state.liquidation_cluster)  # защита от race condition
            prices = [
                liq.get("price_at_spike") or liq.get("price")
                for liq in cluster_copy
                if liq.get("price_at_spike") or liq.get("price")
            ]
            if prices:
                buffer_pct = settings.liquidation_buffer_percent / 100
                if direction == TradeDirection.LONG:
                    # Стоп за нижнюю границу кластера ликвидаций
                    cluster_extreme = min(prices)
                    imbalance_stop = round(cluster_extreme * (1 - buffer_pct), 2)
                    stop_loss = min(imbalance_stop, confirmation.stop_loss)
                else:
                    # Стоп за верхнюю границу кластера ликвидаций
                    cluster_extreme = max(prices)
                    imbalance_stop = round(cluster_extreme * (1 + buffer_pct), 2)
                    stop_loss = max(imbalance_stop, confirmation.stop_loss)

                logger.info(
                    f"Alert {state.alert.id}: стоп за кластер ликвидаций "
                    f"({len(prices)} спайков, экстремум=${cluster_extreme:,.2f}) "
                    f"→ SL=${stop_loss:,.2f} (буфер {settings.liquidation_buffer_percent}%)"
                )

        trade = self.executor.open_position(
            direction=direction,
            entry_price=confirmation.entry_price,
            stop_loss=stop_loss,
            alert_id=state.alert.id
        )

        if trade:
            state.trade = trade
            state.alert.status = AlertStatus.TRADED

            # Register with risk manager
            self.risk_manager.register_trade_open(direction.value)

            # Set SL on exchange after order fills (retry up to 5x with 1s delay)
            if not self.client.paper_trading:
                asyncio.ensure_future(
                    self._set_sl_after_fill(trade.stop_loss, direction)
                )

            # Create position tracking (in-memory)
            position = self.position_manager.create_position(trade)

            # Persist trade and position to DB
            try:
                from ..database.db import AsyncSessionLocal
                from ..database.repositories import TradeRepository, PositionRepository, AlertRepository
                async with AsyncSessionLocal() as session:
                    trade_repo = TradeRepository(session)
                    db_trade = await trade_repo.create({
                        "alert_id": state.alert.id,
                        "direction": trade.direction.value,
                        "entry_price": trade.entry_price,
                        "stop_loss": trade.stop_loss,
                        "take_profit_1": trade.take_profit_1,
                        "take_profit_2": trade.take_profit_2,
                        "quantity": trade.quantity,
                        "status": trade.status.value,
                        "order_id": trade.order_id,
                        "executed_price": trade.executed_price,
                        "executed_quantity": trade.executed_quantity or trade.quantity,
                        "realized_pnl": 0.0,
                        "risk_reward": trade.risk_reward,
                        "opened_at": trade.opened_at,
                    })
                    trade.id = db_trade.id

                    pos_repo = PositionRepository(session)
                    db_pos = await pos_repo.create({
                        "trade_id": db_trade.id,
                        "direction": position.direction.value,
                        "initial_quantity": position.initial_quantity,
                        "current_quantity": position.current_quantity,
                        "entry_price": position.entry_price,
                        "current_price": position.entry_price,
                        "stop_loss": position.stop_loss,
                        "take_profit_1": position.take_profit_1,
                        "take_profit_2": position.take_profit_2,
                        "status": position.status.value,
                        "realized_pnl": 0.0,
                        "unrealized_pnl": 0.0,
                    })
                    position.id = db_pos.id

                    alert_repo = AlertRepository(session)
                    await alert_repo.update_status(state.alert.id, "traded")

                    # Update trade_id reference in position_manager
                    self.position_manager._trades[db_trade.id] = trade
                    self.position_manager.positions[db_trade.id] = position
                    if trade.id != state.alert.id:
                        self.position_manager.positions.pop(trade.alert_id, None)

            except Exception as e:
                logger.error(f"Failed to persist trade/position to DB: {e}")

            await ws_manager.send_trade_update({
                "id": trade.id,
                "direction": trade.direction.value,
                "entry_price": trade.entry_price,
                "stop_loss": trade.stop_loss,
                "take_profit_1": trade.take_profit_1,
                "take_profit_2": trade.take_profit_2,
                "quantity": trade.quantity,
                "status": trade.status.value
            })

            await self._broadcast_alert_update(state.alert)

            logger.info(
                f"Trade executed for alert {state.alert.id}: "
                f"{direction.value} {trade.quantity} BTC @ {trade.entry_price}"
            )
            await ws_manager.send_log(
                f"💹 Trade opened for alert #{state.alert.id}: "
                f"{direction.value.upper()} {trade.quantity} BTC @ ${trade.entry_price:,.0f} "
                f"SL=${trade.stop_loss:,.0f} TP1=${trade.take_profit_1:,.0f}",
                level="success", source="trade"
            )

            # Cleanup state (trade is now managed by position manager)
            self._cleanup_state(state.alert.id)

            # Reject all other pending alerts — position already open
            for other_id in list(self.active_states.keys()):
                if other_id == state.alert.id:
                    continue
                other_state = self.active_states.get(other_id)
                if other_state:
                    other_state.alert.status = AlertStatus.REJECTED
                    await self._broadcast_alert_update(other_state.alert)
                    logger.info(f"Alert {other_id} rejected — position already opened by alert {state.alert.id}")
                    self._cleanup_state(other_id)
        else:
            logger.error(f"Failed to execute trade for alert {state.alert.id}")
            await ws_manager.send_log(
                f"❌ Trade FAILED for alert #{state.alert.id} — order rejected by exchange",
                level="error", source="trade"
            )
            state.alert.status = AlertStatus.REJECTED
            await self._broadcast_alert_update(state.alert)
            self._cleanup_state(state.alert.id)

    async def _set_sl_after_fill(self, stop_loss: float, direction: TradeDirection):
        """Wait for order to fill, then set SL on the position (retry up to 5x)."""
        for attempt in range(5):
            await asyncio.sleep(2)
            try:
                positions = self.client.get_positions(symbol="BTCUSDT")
                if positions and float(positions[0].get("size", 0)) > 0:
                    ok = self.client.set_trading_stop(
                        symbol="BTCUSDT",
                        stop_loss=round(stop_loss, 2)
                    )
                    if ok:
                        logger.info(f"SL set on position after fill: {stop_loss:.2f}")
                        return
                    else:
                        logger.warning(f"SL set failed (attempt {attempt+1}), retrying...")
                else:
                    logger.info(f"Position not filled yet (attempt {attempt+1}), waiting...")
            except Exception as e:
                logger.error(f"Error setting SL (attempt {attempt+1}): {e}")
        logger.error("Failed to set SL after 5 attempts — set manually on exchange!")

    def _cleanup_state(self, alert_id: int):
        """Remove alert state from tracking"""
        if alert_id in self.active_states:
            del self.active_states[alert_id]
            self.alert_processor.clear_alert(alert_id)

    def _timeframe_to_interval(self, timeframe: str) -> str:
        """Convert timeframe string to Bybit interval"""
        mapping = {
            "1m": "1",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "4h": "240",
            "1d": "D"
        }
        return mapping.get(timeframe, "60")

    async def _broadcast_alert_update(self, alert: Alert):
        """Broadcast alert status update"""
        await ws_manager.send_alert({
            "id": alert.id,
            "type": alert.alert_type.value,
            "price": alert.price,
            "status": alert.status.value,
            "timestamp": alert.timestamp.isoformat()
        })

    async def _broadcast_range_update(self, range: Range):
        """Broadcast range detection update"""
        await ws_manager.send_range_update({
            "alert_id": range.alert_id,
            "local_high": range.local_high,
            "local_low": range.local_low,
            "mid_range": range.mid_range,
            "width_percent": range.width_percent
        })

    async def _broadcast_sweep_update(self, sweep: SweepEvent):
        """Broadcast sweep detection update"""
        await ws_manager.send_sweep_update({
            "alert_id": sweep.alert_id,
            "direction": sweep.direction.value,
            "sweep_price": sweep.sweep_price,
            "level_swept": sweep.level_swept,
            "wick_percent": sweep.wick_percent
        })

    def get_active_alerts_count(self) -> int:
        """Get number of alerts being processed"""
        return len(self.active_states)

    def get_engine_status(self) -> dict:
        """Get engine status"""
        return {
            "is_running": self.is_running,
            "active_alerts": self.get_active_alerts_count(),
            "active_positions": len(self.position_manager.get_active_positions()),
            "daily_stats": self.risk_manager.get_daily_stats()
        }
