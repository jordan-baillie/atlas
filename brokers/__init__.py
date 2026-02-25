"""Atlas Broker Layer.

Provides broker adapters for executing trades across different markets.
Each market can have its own broker implementation.

Usage:
    from brokers import get_broker
    from brokers.base import BrokerAdapter, OrderResult, OrderStatus

    broker = get_broker("asx", config)
"""

from brokers.base import (
    BrokerAdapter, OrderResult, PositionInfo, AccountInfo, DealInfo,
    OrderStatus, OrderSide, OrderType,
)
from brokers.registry import get_broker

__all__ = [
    "BrokerAdapter", "OrderResult", "PositionInfo", "AccountInfo",
    "DealInfo", "OrderStatus", "OrderSide", "OrderType", "get_broker",
]
