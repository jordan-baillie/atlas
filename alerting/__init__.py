"""Alerting package — centralised alert dispatch via AlertManager."""
from alerting.manager import AlertManager, get_alert_manager, AlertLevel

__all__ = ["AlertManager", "get_alert_manager", "AlertLevel"]
