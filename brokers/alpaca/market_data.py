"""Alpaca market data helpers for Atlas.

Provides real-time and snapshot price data via the Alpaca Data API.
Uses alpaca-py's StockHistoricalDataClient (works without a live account —
just needs API key/secret or can use unauthenticated free tier).

This module replaces Yahoo Finance for real-time price data when
Alpaca is the active broker. Use for:
    - get_latest_quotes()  — bid/ask/mid for a list of tickers
    - get_latest_bars()    — latest OHLCV bar for a list of tickers
    - get_snapshot()       — full snapshot (quote + bar + trade) for one ticker

Supports batch requests for efficiency (Alpaca handles multi-symbol natively).

All tickers use Atlas format (bare US symbols: AAPL, MSFT, etc.).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("atlas.broker.alpaca.data")

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import (
        StockLatestBarRequest,
        StockLatestQuoteRequest,
        StockSnapshotRequest,
    )
    from alpaca.data.models import Bar, Quote, Snapshot
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    logger.warning("alpaca-py not installed. Run: pip install alpaca-py")


class AlpacaMarketData:
    """Market data client wrapping Alpaca's data API.

    Instantiate once, reuse across requests. Thread-safe for reads.

    Args:
        api_key:    Alpaca API key (or None for free tier).
        api_secret: Alpaca API secret (or None for free tier).
        feed:       Data feed: "iex" (free) | "sip" (paid, full market).
                    Default "iex" works without a subscription.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        feed: str = "iex",
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._feed = feed
        self._client: Optional["StockHistoricalDataClient"] = None

        if ALPACA_AVAILABLE and api_key and api_secret:
            try:
                self._client = StockHistoricalDataClient(
                    api_key=api_key,
                    secret_key=api_secret,
                )
                logger.debug(
                    "AlpacaMarketData client created (feed=%s)",
                    feed,
                )
            except Exception as e:
                logger.warning("AlpacaMarketData client init failed: %s", e)
                self._client = None
        elif ALPACA_AVAILABLE:
            logger.debug(
                "AlpacaMarketData: no credentials provided — market data disabled"
            )

    @property
    def is_available(self) -> bool:
        """True if alpaca-py is installed and client is initialised."""
        return ALPACA_AVAILABLE and self._client is not None

    # ── Latest Quotes ──────────────────────────────────────────

    def get_latest_quotes(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch latest bid/ask quotes for a batch of tickers.

        Returns a dict of ticker → quote dict with keys:
            bid_price, bid_size, ask_price, ask_size, mid_price, timestamp

        On failure, returns an empty dict for affected tickers.

        Args:
            tickers: List of Atlas-format US tickers (e.g. ['AAPL', 'MSFT']).

        Returns:
            Dict of ticker -> quote info dict.
        """
        if not self.is_available or not tickers:
            return {}

        from brokers.alpaca.mapper import to_alpaca_list, to_atlas
        alpaca_symbols = to_alpaca_list(tickers)

        try:
            req = StockLatestQuoteRequest(
                symbol_or_symbols=alpaca_symbols,
                feed=self._feed,
            )
            raw = self._client.get_stock_latest_quote(req)
        except Exception as e:
            logger.error("get_latest_quotes failed: %s", e, exc_info=True)
            return {}

        result = {}
        for symbol, quote in (raw or {}).items():
            atlas_ticker = to_atlas(symbol)
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            mid = round((bid + ask) / 2, 4) if bid and ask else (bid or ask)
            result[atlas_ticker] = {
                "bid_price": bid,
                "bid_size": int(getattr(quote, "bid_size", 0) or 0),
                "ask_price": ask,
                "ask_size": int(getattr(quote, "ask_size", 0) or 0),
                "mid_price": mid,
                "timestamp": str(getattr(quote, "timestamp", "")),
            }

        logger.debug("get_latest_quotes: got %d/%d quotes", len(result), len(tickers))
        return result

    # ── Latest Bars ────────────────────────────────────────────

    def get_latest_bars(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch the latest 1-minute OHLCV bar for a batch of tickers.

        Returns a dict of ticker → bar dict with keys:
            open, high, low, close, volume, vwap, timestamp

        Args:
            tickers: List of Atlas-format US tickers.

        Returns:
            Dict of ticker -> bar info dict.
        """
        if not self.is_available or not tickers:
            return {}

        from brokers.alpaca.mapper import to_alpaca_list, to_atlas
        alpaca_symbols = to_alpaca_list(tickers)

        try:
            req = StockLatestBarRequest(
                symbol_or_symbols=alpaca_symbols,
                feed=self._feed,
            )
            raw = self._client.get_stock_latest_bar(req)
        except Exception as e:
            logger.error("get_latest_bars failed: %s", e, exc_info=True)
            return {}

        result = {}
        for symbol, bar in (raw or {}).items():
            atlas_ticker = to_atlas(symbol)
            result[atlas_ticker] = {
                "open": float(getattr(bar, "open", 0) or 0),
                "high": float(getattr(bar, "high", 0) or 0),
                "low": float(getattr(bar, "low", 0) or 0),
                "close": float(getattr(bar, "close", 0) or 0),
                "volume": int(getattr(bar, "volume", 0) or 0),
                "vwap": float(getattr(bar, "vwap", 0) or 0),
                "timestamp": str(getattr(bar, "timestamp", "")),
            }

        logger.debug("get_latest_bars: got %d/%d bars", len(result), len(tickers))
        return result

    # ── Snapshot ───────────────────────────────────────────────

    def get_snapshot(self, ticker: str) -> Optional[dict]:
        """Fetch a full market snapshot for a single ticker.

        Snapshot includes: latest_trade, latest_quote, minute_bar, daily_bar,
        previous_daily_bar. This is the richest single-call data source.

        Args:
            ticker: Atlas-format US ticker (e.g. 'AAPL').

        Returns:
            Snapshot dict or None on failure.
        """
        return self.get_snapshots([ticker]).get(ticker)

    def get_snapshots(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch market snapshots for a batch of tickers.

        Each snapshot contains:
            latest_trade:    {price, size, timestamp}
            latest_quote:    {bid_price, ask_price, mid_price, timestamp}
            minute_bar:      {open, high, low, close, volume, vwap}
            daily_bar:       {open, high, low, close, volume, vwap}
            prev_daily_bar:  {open, high, low, close, volume, vwap}
            price:           Best available price (trade > ask > bar close)

        Args:
            tickers: List of Atlas-format US tickers.

        Returns:
            Dict of ticker -> snapshot dict.
        """
        if not self.is_available or not tickers:
            return {}

        from brokers.alpaca.mapper import to_alpaca_list, to_atlas
        alpaca_symbols = to_alpaca_list(tickers)

        try:
            req = StockSnapshotRequest(
                symbol_or_symbols=alpaca_symbols,
                feed=self._feed,
            )
            raw = self._client.get_stock_snapshot(req)
        except Exception as e:
            logger.error("get_snapshots failed: %s", e, exc_info=True)
            return {}

        result = {}
        for symbol, snap in (raw or {}).items():
            atlas_ticker = to_atlas(symbol)
            result[atlas_ticker] = _parse_snapshot(snap)

        logger.debug("get_snapshots: got %d/%d snapshots", len(result), len(tickers))
        return result

    # ── Price convenience ──────────────────────────────────────

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Get latest mid-price for a list of tickers.

        Uses latest_quote (bid/ask mid). Falls back to latest_bar close
        if quote is unavailable. This is the primary price source for
        get_prices() in the broker adapter.

        Args:
            tickers: List of Atlas-format US tickers.

        Returns:
            Dict of ticker -> price (float). Missing tickers omitted.
        """
        if not tickers:
            return {}

        # Try snapshots first (richest data, single request)
        snapshots = self.get_snapshots(tickers)
        prices = {}
        for ticker, snap in snapshots.items():
            price = snap.get("price", 0.0)
            if price > 0:
                prices[ticker] = price

        # For any missing tickers, fall back to latest quote
        missing = [t for t in tickers if t not in prices]
        if missing:
            quotes = self.get_latest_quotes(missing)
            for ticker, q in quotes.items():
                mid = q.get("mid_price", 0.0)
                if mid > 0:
                    prices[ticker] = mid

        return prices


# ─────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────

def _parse_bar(bar) -> dict:
    """Parse an Alpaca Bar object to a plain dict."""
    if bar is None:
        return {}
    return {
        "open": float(getattr(bar, "open", 0) or 0),
        "high": float(getattr(bar, "high", 0) or 0),
        "low": float(getattr(bar, "low", 0) or 0),
        "close": float(getattr(bar, "close", 0) or 0),
        "volume": int(getattr(bar, "volume", 0) or 0),
        "vwap": float(getattr(bar, "vwap", 0) or 0),
        "timestamp": str(getattr(bar, "timestamp", "")),
    }


def _parse_quote(quote) -> dict:
    """Parse an Alpaca Quote object to a plain dict."""
    if quote is None:
        return {}
    bid = float(getattr(quote, "bid_price", 0) or 0)
    ask = float(getattr(quote, "ask_price", 0) or 0)
    mid = round((bid + ask) / 2, 4) if bid and ask else (bid or ask)
    return {
        "bid_price": bid,
        "bid_size": int(getattr(quote, "bid_size", 0) or 0),
        "ask_price": ask,
        "ask_size": int(getattr(quote, "ask_size", 0) or 0),
        "mid_price": mid,
        "timestamp": str(getattr(quote, "timestamp", "")),
    }


def _parse_trade(trade) -> dict:
    """Parse an Alpaca Trade object to a plain dict."""
    if trade is None:
        return {}
    return {
        "price": float(getattr(trade, "price", 0) or 0),
        "size": int(getattr(trade, "size", 0) or 0),
        "timestamp": str(getattr(trade, "timestamp", "")),
    }


def _parse_snapshot(snap) -> dict:
    """Parse an Alpaca Snapshot object to a rich plain dict.

    The 'price' key gives the best available current price in order:
        1. latest trade price
        2. ask price (approx current market)
        3. daily bar close
        4. minute bar close
    """
    latest_trade = _parse_trade(getattr(snap, "latest_trade", None))
    latest_quote = _parse_quote(getattr(snap, "latest_quote", None))
    minute_bar = _parse_bar(getattr(snap, "minute_bar", None))
    daily_bar = _parse_bar(getattr(snap, "daily_bar", None))
    prev_daily_bar = _parse_bar(getattr(snap, "previous_daily_bar", None))

    # Best-effort price: trade → ask → daily close → minute close
    price = (
        latest_trade.get("price", 0)
        or latest_quote.get("ask_price", 0)
        or daily_bar.get("close", 0)
        or minute_bar.get("close", 0)
    )

    return {
        "price": float(price),
        "latest_trade": latest_trade,
        "latest_quote": latest_quote,
        "minute_bar": minute_bar,
        "daily_bar": daily_bar,
        "prev_daily_bar": prev_daily_bar,
    }
