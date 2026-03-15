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
import time
from datetime import date as _date_type
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger("atlas.broker.alpaca.data")

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import (
        StockBarsRequest,
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

    # ── Bulk Historical Download ───────────────────────────────

    def download_universe_bars(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        timeframe: str = "1Day",
    ) -> Dict[str, pd.DataFrame]:
        """Download daily OHLCV bars for multiple tickers via Alpaca's multi-bar endpoint.

        Returns DataFrames with columns: open, high, low, close, volume
        (matching yfinance output schema for downstream compatibility).

        Handles pagination (Alpaca limits bars per request) and rate limiting
        (200 requests/minute for free tier).  Tickers are processed in batches
        of 50 to stay within Alpaca's multi-symbol limits; a short sleep is
        inserted between batches to respect the free-tier rate limit.

        Args:
            tickers:    List of Atlas-format US tickers (e.g. ['AAPL', 'MSFT']).
            start_date: Start date 'YYYY-MM-DD' (inclusive).
            end_date:   End date   'YYYY-MM-DD' (inclusive).
            timeframe:  Bar timeframe — "1Day" (default), "1Hour", "1Min".

        Returns:
            Dict of ticker -> DataFrame with columns [open, high, low, close, volume, ticker]
            and tz-naive DatetimeIndex named 'date'.  Empty dict if client unavailable.
        """
        if not self.is_available or not tickers:
            return {}

        if not ALPACA_AVAILABLE:
            return {}

        try:
            from alpaca.data.timeframe import TimeFrame
            from alpaca.data.enums import Adjustment
            from brokers.alpaca.mapper import to_alpaca_list, to_atlas
        except ImportError as e:
            logger.warning("download_universe_bars: import error: %s", e)
            return {}

        _tf_map: Dict[str, object] = {
            "1Day":    TimeFrame.Day,
            "1Hour":   TimeFrame.Hour,
            "1Min":    TimeFrame.Minute,
            "1Minute": TimeFrame.Minute,
        }
        tf = _tf_map.get(timeframe, TimeFrame.Day)

        BATCH_SIZE = 50
        batches = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
        result: Dict[str, pd.DataFrame] = {}

        for batch_idx, batch in enumerate(batches):
            if batch_idx > 0:
                time.sleep(0.3)  # rate-limit: 200 requests/minute for free tier

            processed = batch_idx * BATCH_SIZE
            logger.info(
                "download_universe_bars: batch %d/%d (%d/%d tickers processed)",
                batch_idx + 1, len(batches), processed, len(tickers),
            )

            alpaca_symbols = to_alpaca_list(batch)

            # Accumulate bars; inner loop handles next_page_token if present.
            all_bars: Dict[str, list] = {sym: [] for sym in alpaca_symbols}
            page_token: Optional[str] = None

            while True:
                try:
                    req_kwargs: dict = dict(
                        symbol_or_symbols=alpaca_symbols,
                        timeframe=tf,
                        start=start_date,
                        end=end_date,
                        adjustment=Adjustment.ALL,
                        feed=self._feed,
                    )
                    if page_token:
                        req_kwargs["page_token"] = page_token

                    req = StockBarsRequest(**req_kwargs)
                    barset = self._client.get_stock_bars(req)

                    barset_data = (
                        getattr(barset, "data", None)
                        or (barset if isinstance(barset, dict) else {})
                    )
                    for symbol, bars in barset_data.items():
                        if symbol in all_bars:
                            all_bars[symbol].extend(bars or [])

                    # Pagination: continue if Alpaca signals more pages
                    page_token = getattr(barset, "next_page_token", None)
                    if not page_token:
                        break

                except Exception as e:
                    logger.warning(
                        "download_universe_bars: batch %d fetch error: %s",
                        batch_idx + 1, e,
                    )
                    break

            # Convert accumulated bars to DataFrames
            for symbol, bars in all_bars.items():
                atlas_ticker = to_atlas(symbol)
                if not bars:
                    continue
                rows = []
                for bar in bars:
                    ts = getattr(bar, "timestamp", None)
                    if ts is None:
                        continue
                    rows.append({
                        "date":   pd.Timestamp(ts).normalize(),
                        "open":   float(getattr(bar, "open",   0) or 0),
                        "high":   float(getattr(bar, "high",   0) or 0),
                        "low":    float(getattr(bar, "low",    0) or 0),
                        "close":  float(getattr(bar, "close",  0) or 0),
                        "volume": int(getattr(bar,   "volume", 0) or 0),
                        "ticker": atlas_ticker,
                    })
                if not rows:
                    continue
                df = pd.DataFrame(rows).set_index("date")
                df.index.name = "date"
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                # Canonical column order matching yfinance output schema
                df = df[["open", "high", "low", "close", "volume", "ticker"]]
                result[atlas_ticker] = df

        logger.info(
            "download_universe_bars: completed %d/%d tickers",
            len(result), len(tickers),
        )
        return result


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


# ─────────────────────────────────────────────────────────────────
# Module-level singleton convenience functions
# ─────────────────────────────────────────────────────────────────

_singleton: Optional[AlpacaMarketData] = None
_trade_singleton = None  # Optional[TradingClient] — lazy import


def get_alpaca_data_client() -> Optional[AlpacaMarketData]:
    """Get or create singleton AlpacaMarketData client.

    Reads credentials from ~/.atlas-secrets.json or environment.
    Returns None if alpaca-py not installed or no credentials.
    """
    global _singleton
    if _singleton is not None:
        return _singleton if _singleton.is_available else None
    try:
        from brokers.secrets import get_secret
        key = get_secret("ALPACA_API_KEY")
        secret = get_secret("ALPACA_SECRET_KEY")
        if not key or not secret:
            return None
        _singleton = AlpacaMarketData(api_key=key, api_secret=secret)
        return _singleton if _singleton.is_available else None
    except Exception:
        return None


def _get_trade_client():
    """Get or create singleton TradingClient for corporate actions and trading data.

    Returns None if alpaca-py not installed or no credentials.
    """
    global _trade_singleton
    if _trade_singleton is not None:
        return _trade_singleton
    if not ALPACA_AVAILABLE:
        return None
    try:
        from alpaca.trading.client import TradingClient
        from brokers.secrets import get_secret
        key = get_secret("ALPACA_API_KEY")
        secret = get_secret("ALPACA_SECRET_KEY")
        if not key or not secret:
            return None
        # paper=False: corporate announcements live on the non-paper endpoint
        _trade_singleton = TradingClient(
            api_key=key,
            secret_key=secret,
            paper=False,
        )
        return _trade_singleton
    except Exception:
        return None


def get_historical_bars(
    symbols,
    start,
    end,
    timeframe: str = "1Day",
    adjustment: str = "all",
) -> dict:
    """Fetch historical OHLCV bars via Alpaca, returning Atlas-format DataFrames.

    Args:
        symbols: Ticker or list of Atlas-format US tickers.
        start:   Start date/datetime (inclusive). Accepts str, date, or datetime.
        end:     End date/datetime (inclusive). Accepts str, date, or datetime.
        timeframe: Bar timeframe — "1Day" (default), "1Hour", "1Min".
        adjustment: Price adjustment — "all" (default), "split", "dividend", "raw".

    Returns:
        {ticker: DataFrame} with columns [open, high, low, close, adj_close, volume, ticker]
        and DatetimeIndex named 'date'. Empty dict if Alpaca unavailable.
    """
    client = get_alpaca_data_client()
    if not client or not client.is_available:
        return {}
    if not ALPACA_AVAILABLE:
        return {}

    if isinstance(symbols, str):
        symbols = [symbols]

    try:
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import Adjustment
        from brokers.alpaca.mapper import to_alpaca_list, to_atlas

        _tf_map = {
            "1Day":    TimeFrame.Day,
            "1Hour":   TimeFrame.Hour,
            "1Min":    TimeFrame.Minute,
            "1Minute": TimeFrame.Minute,
        }
        tf = _tf_map.get(timeframe, TimeFrame.Day)

        _adj_map = {
            "all":      Adjustment.ALL,
            "split":    Adjustment.SPLIT,
            "dividend": Adjustment.DIVIDEND,
            "raw":      Adjustment.RAW,
        }
        adj = _adj_map.get(str(adjustment).lower(), Adjustment.ALL)

        alpaca_symbols = to_alpaca_list(symbols)
        req = StockBarsRequest(
            symbol_or_symbols=alpaca_symbols,
            timeframe=tf,
            start=start,
            end=end,
            adjustment=adj,
            feed=client._feed,
        )
        barset = client._client.get_stock_bars(req)
    except Exception as e:
        logger.error("get_historical_bars failed: %s", e, exc_info=True)
        return {}

    result = {}
    try:
        from brokers.alpaca.mapper import to_atlas
        # BarSet is a pydantic model with .data dict, not a dict itself
        barset_data = getattr(barset, 'data', None) or (barset if isinstance(barset, dict) else {})
        for symbol, bars in barset_data.items():
            atlas_ticker = to_atlas(symbol)
            if not bars:
                continue
            rows = []
            for bar in bars:
                ts = getattr(bar, "timestamp", None)
                if ts is None:
                    continue
                rows.append({
                    "date": pd.Timestamp(ts).normalize(),
                    "open":   float(getattr(bar, "open",   0) or 0),
                    "high":   float(getattr(bar, "high",   0) or 0),
                    "low":    float(getattr(bar, "low",    0) or 0),
                    "close":  float(getattr(bar, "close",  0) or 0),
                    "volume": int(getattr(bar,   "volume", 0) or 0),
                    "ticker": atlas_ticker,
                })
            if not rows:
                continue
            df = pd.DataFrame(rows).set_index("date")
            df.index.name = "date"
            # Atlas standard: tz-naive DatetimeIndex
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            # adj_close = close (already adjusted per Adjustment enum)
            df["adj_close"] = df["close"]
            df = df[["open", "high", "low", "close", "adj_close", "volume", "ticker"]]
            result[atlas_ticker] = df
    except Exception as e:
        logger.warning("get_historical_bars: parse error: %s", e)

    logger.debug(
        "get_historical_bars: got %d/%d symbols", len(result), len(symbols)
    )
    return result


def get_snapshot_prices(tickers) -> dict:
    """Batch fetch current prices via Alpaca snapshots.

    Args:
        tickers: List of Atlas-format US tickers (or a single string).

    Returns:
        {ticker: {price, prev_close, change, change_pct, day_high, day_low, volume, source}}
        Empty dict if Alpaca unavailable or no snapshots returned.
    """
    client = get_alpaca_data_client()
    if not client:
        return {}

    if isinstance(tickers, str):
        tickers = [tickers]

    snapshots = client.get_snapshots(list(tickers))
    result = {}
    for ticker, snap in snapshots.items():
        price = float(snap.get("price", 0) or 0)
        prev_bar = snap.get("prev_daily_bar") or {}
        prev_close = float(prev_bar.get("close", 0) or 0)
        day_bar = snap.get("daily_bar") or {}
        day_high = float(day_bar.get("high", 0) or 0)
        day_low  = float(day_bar.get("low",  0) or 0)
        volume   = int(day_bar.get("volume", 0) or 0)
        change     = round(price - prev_close, 4) if price and prev_close else 0.0
        change_pct = round(change / prev_close * 100, 4) if prev_close else 0.0
        result[ticker] = {
            "price":      price,
            "prev_close": prev_close,
            "change":     change,
            "change_pct": change_pct,
            "day_high":   day_high,
            "day_low":    day_low,
            "volume":     volume,
            "source":     "alpaca",
        }
    return result


def get_dividends(symbol: str, start, end) -> pd.Series:
    """Fetch dividend history via Alpaca Corporate Actions API.

    Uses TradingClient.get_corporate_announcements() to retrieve cash
    dividend events for a US ticker within the given date range.

    Args:
        symbol: Atlas-format US ticker (e.g. 'AAPL').
        start:  Start date (inclusive). Accepts str, date, or datetime.
        end:    End date (inclusive). Accepts str, date, or datetime.

    Returns:
        pd.Series indexed by ex-date (DatetimeIndex) with cash dividend amounts.
        Empty Series if Alpaca unavailable or no dividends found.
    """
    if not ALPACA_AVAILABLE:
        return pd.Series(dtype=float)

    def _to_date(d):
        if isinstance(d, _date_type):
            return d
        return pd.Timestamp(d).date()

    try:
        from alpaca.trading.requests import GetCorporateAnnouncementsRequest
        from alpaca.trading.enums import CorporateActionType, CorporateActionDateType
        from brokers.alpaca.mapper import to_alpaca

        tc = _get_trade_client()
        if tc is None:
            return pd.Series(dtype=float)

        alpaca_symbol = to_alpaca(symbol)
        req = GetCorporateAnnouncementsRequest(
            ca_types=[CorporateActionType.DIVIDEND],
            since=_to_date(start),
            until=_to_date(end),
            symbol=alpaca_symbol,
            date_type=CorporateActionDateType.EX_DATE,
        )
        announcements = tc.get_corporate_announcements(req)

        if not announcements:
            return pd.Series(dtype=float)

        data = {}
        for ann in announcements:
            ex_date = getattr(ann, "ex_date", None)
            cash = getattr(ann, "cash", None)
            if ex_date is None or cash is None:
                continue
            ts = pd.Timestamp(str(ex_date))
            data[ts] = float(cash)

        if not data:
            return pd.Series(dtype=float)

        series = pd.Series(data, dtype=float).sort_index()
        series.index.name = "date"
        return series

    except Exception as e:
        logger.warning("get_dividends(%s) failed: %s", symbol, e)
        return pd.Series(dtype=float)
