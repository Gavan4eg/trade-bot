import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from ..models.alert import Alert, AlertType, AlertStatus, AlertWebhook, ALERT_PRIORITY

logger = logging.getLogger(__name__)


class AlertProcessor:
    """Process incoming alerts from trdr.io webhook"""

    VALID_ALERT_TYPES = {
        "BTC Double Diamond": AlertType.BTC_DOUBLE_DIAMOND,
        "BTC Diamond": AlertType.BTC_DIAMOND,
        "Diamond Top Levels": AlertType.DIAMOND_TOP_LEVELS,
        "Aggregated Liquidation": AlertType.AGGREGATED_LIQUIDATION,
    }

    # Cooldown between same type alerts (minutes)
    COOLDOWN_MINUTES = 30

    def __init__(self):
        self.recent_alerts: Dict[AlertType, datetime] = {}
        self.active_alerts: List[Alert] = []

    def parse_webhook(self, data: dict) -> Optional[Alert]:
        """Parse incoming webhook data and create Alert object"""
        try:
            webhook = AlertWebhook(**data)

            # Resolve alert type from 'type' or 'name' field
            type_str = webhook.effective_type()
            if type_str not in self.VALID_ALERT_TYPES:
                logger.warning(f"Unknown alert type: {type_str}")
                return None

            # Validate symbol — accept BTC/USD, BTCUSDT, BTCUSD_PERP, etc.
            symbol = webhook.ticker or webhook.symbol or ""
            if symbol and "BTC" not in symbol.upper() and webhook.base != "BTC":
                logger.info(f"Ignoring non-BTC alert: {symbol}")
                return None

            alert_type = self.VALID_ALERT_TYPES[type_str]

            # Parse timestamp — accept 'timestamp' or 'time' field
            timestamp = datetime.utcnow()
            ts_str = webhook.effective_timestamp()
            if ts_str:
                try:
                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    timestamp = timestamp.replace(tzinfo=None)  # store as naive UTC
                except ValueError:
                    pass

            # Extract price
            price = webhook.price or 0.0

            # Extract levels
            levels = webhook.levels or []

            alert = Alert(
                alert_type=alert_type,
                timestamp=timestamp,
                price=price,
                levels=levels,
                priority=ALERT_PRIORITY.get(alert_type, 4),
                raw_data=data
            )

            logger.info(f"Parsed alert: {alert_type.value} at ${price}, side={webhook.side}")
            return alert

        except Exception as e:
            logger.error(f"Failed to parse webhook: {e}")
            return None

    def validate_alert(self, alert: Alert) -> bool:
        """Validate if alert should be processed"""
        if not alert:
            return False

        # Check if alert type is valid
        if alert.alert_type not in self.VALID_ALERT_TYPES.values():
            return False

        # Check if price is valid
        if alert.price <= 0:
            logger.warning(f"Alert with invalid price: {alert.price}")
            return False

        return True

    def should_process(self, alert: Alert) -> bool:
        """Check if alert should be processed (cooldown, duplicates)"""
        if not self.validate_alert(alert):
            return False

        # Check cooldown for same type alerts
        if alert.alert_type in self.recent_alerts:
            last_alert_time = self.recent_alerts[alert.alert_type]
            # Use cooldown from webhook payload if present, else default
            cooldown_seconds = (alert.raw_data or {}).get("cooldown")
            if cooldown_seconds:
                cooldown_end = last_alert_time + timedelta(seconds=int(cooldown_seconds))
            else:
                cooldown_end = last_alert_time + timedelta(minutes=self.COOLDOWN_MINUTES)

            if datetime.utcnow() < cooldown_end:
                remaining = int((cooldown_end - datetime.utcnow()).total_seconds())
                logger.info(
                    f"Alert {alert.alert_type.value} in cooldown, {remaining}s remaining"
                )
                return False

        # Check for active alerts of same or higher priority
        for active in self.active_alerts:
            if active.status not in [AlertStatus.TRADED, AlertStatus.EXPIRED, AlertStatus.REJECTED]:
                if active.priority <= alert.priority:
                    logger.info(
                        f"Higher priority alert already active: {active.alert_type.value}"
                    )
                    # Allow if new alert has higher priority
                    if alert.priority < active.priority:
                        continue
                    return False

        return True

    def register_alert(self, alert: Alert) -> None:
        """Register alert as processed"""
        self.recent_alerts[alert.alert_type] = datetime.utcnow()
        self.active_alerts.append(alert)
        logger.info(f"Registered alert: {alert.alert_type.value}")

    def get_active_alerts(self) -> List[Alert]:
        """Get list of active alerts"""
        return [
            a for a in self.active_alerts
            if a.status not in [AlertStatus.TRADED, AlertStatus.EXPIRED, AlertStatus.REJECTED]
        ]

    def update_alert_status(self, alert: Alert, status: AlertStatus) -> None:
        """Update alert status"""
        alert.status = status
        logger.info(f"Alert {alert.id} status updated to {status.value}")

    def expire_old_alerts(self, max_age_hours: int = 4) -> int:
        """Expire alerts older than max_age_hours"""
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        expired_count = 0

        for alert in self.active_alerts:
            if alert.timestamp < cutoff and alert.status not in [
                AlertStatus.TRADED,
                AlertStatus.EXPIRED,
                AlertStatus.REJECTED
            ]:
                alert.status = AlertStatus.EXPIRED
                expired_count += 1

        if expired_count > 0:
            logger.info(f"Expired {expired_count} old alerts")

        return expired_count

    def extract_levels(self, alert: Alert) -> List[float]:
        """Extract significant price levels from alert"""
        levels = alert.levels.copy() if alert.levels else []

        # Add alert price as a level if not already present
        if alert.price > 0 and alert.price not in levels:
            levels.append(alert.price)

        # Sort levels
        levels.sort()

        return levels

    def get_highest_priority_alert(self) -> Optional[Alert]:
        """Get the highest priority active alert"""
        active = self.get_active_alerts()
        if not active:
            return None

        return min(active, key=lambda a: a.priority)

    def clear_alert(self, alert_id: int) -> bool:
        """Remove alert from active list"""
        for i, alert in enumerate(self.active_alerts):
            if alert.id == alert_id:
                self.active_alerts.pop(i)
                return True
        return False
