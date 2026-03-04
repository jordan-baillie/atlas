"""Alpaca broker package for Atlas.

Implements the BrokerAdapter interface for US equity trading
via Alpaca Markets (alpaca-py SDK).

Usage:
    from brokers.alpaca import AlpacaBroker
    broker = AlpacaBroker(config, live=False)  # paper trading
    broker.connect()

Exports:
    AlpacaBroker  — Main broker adapter class
    to_atlas      — Convert Alpaca symbol to Atlas format
    to_alpaca     — Convert Atlas ticker to Alpaca symbol

Config section (in config.yaml under 'alpaca'):
    paper: true          # true = paper, false = live real-money
    feed:  "iex"         # "iex" (free) or "sip" (paid)
    tif:   "day"         # time in force: day, gtc, ioc, fok

Credentials (NOT in config — loaded from env or ~/.atlas-secrets.json):
    ALPACA_API_KEY     — Alpaca API key
    ALPACA_SECRET_KEY  — Alpaca API secret key
"""

from brokers.alpaca.broker import AlpacaBroker
from brokers.alpaca.mapper import to_atlas, to_alpaca

__all__ = ["AlpacaBroker", "to_atlas", "to_alpaca"]
