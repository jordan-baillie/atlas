"""
overlay.sources.alt_data — Alt data scraper for OpenInsider and Finviz.

Scrapes insider trading data and market screener data for current holdings
and watchlist tickers. Results written to news_intel table via atlas_db.record_news().

Runs weekly: Sunday 09:00 AEST via cron.

Usage
-----
    python3 -m overlay.sources.alt_data                      # full run, writes to DB
    python3 -m overlay.sources.alt_data --dry-run             # scrape but don't write
    python3 -m overlay.sources.alt_data --tickers AAPL,MSFT   # specific tickers
    python3 -m overlay.sources.alt_data --dry-run --tickers AMT,MRVL,NFLX
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_REQUEST_DELAY = 2          # seconds between HTTP requests
_MAX_RETRIES = 3            # retries on 403
_RETRY_BACKOFF = 2          # exponential backoff base (2, 4, 8 sec)

_INSIDER_BUY_THRESHOLD = 100_000   # $100K minimum buy to be "notable"
_INSIDER_SELL_THRESHOLD = 500_000  # $500K minimum sell to be "notable"


# ── HTTP helper ──────────────────────────────────────────────────────────────

def _get_with_retry(url: str, timeout: int = 20) -> Optional[requests.Response]:
    """
    GET *url* with retry on 403.  Returns Response or None on failure.
    Warns about potential Playwright fallback need on persistent 403.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            logger.warning("alt_data: request error for %s — %s", url, exc)
            return None

        if resp.status_code == 200:
            return resp

        if resp.status_code == 403:
            wait = _RETRY_BACKOFF ** (attempt + 1)  # 2, 4, 8
            logger.warning(
                "alt_data: 403 from %s (attempt %d/%d) — sleeping %ds. "
                "If this persists, a Playwright/headless-browser fallback may be needed.",
                url, attempt + 1, _MAX_RETRIES, wait,
            )
            time.sleep(wait)
            continue

        logger.warning("alt_data: unexpected HTTP %d from %s", resp.status_code, url)
        return None

    logger.warning(
        "alt_data: %d consecutive 403s from %s — giving up. "
        "Consider adding a Playwright fallback for this source.",
        _MAX_RETRIES, url,
    )
    return None


# ── Class 1: OpenInsider scraper ─────────────────────────────────────────────

class OpenInsiderScraper:
    """
    Scrapes insider-trade data from openinsider.com for a given ticker.

    Significant trades (large buys ≥$100K, large sells ≥$500K) are returned as
    news_intel-compatible dicts ready for atlas_db.record_news().
    """

    _BASE_URL = "http://openinsider.com/{ticker}"

    def scrape(self, ticker: str) -> List[Dict[str, Any]]:
        """
        Scrape insider trades for *ticker*.

        Returns a list of record dicts (each suitable for atlas_db.record_news(**rec)).
        Returns empty list on any error.
        """
        url = self._BASE_URL.format(ticker=ticker)
        records: List[Dict[str, Any]] = []

        resp = _get_with_retry(url)
        if resp is None:
            logger.warning("alt_data: OpenInsider — no response for %s", ticker)
            return records

        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table", class_="tinytable")
            if table is None:
                logger.info("alt_data: OpenInsider — no tinytable found for %s", ticker)
                return records

            rows = table.find_all("tr")
            if len(rows) < 2:
                logger.info("alt_data: OpenInsider — no data rows for %s", ticker)
                return records

            # Parse header row to map column indices
            header_row = rows[0]
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

            col_map = self._build_col_map(headers)

            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells:
                    continue
                record = self._parse_row(cells, col_map, ticker, url)
                if record is not None:
                    records.append(record)

        except Exception as exc:
            logger.warning("alt_data: OpenInsider parse error for %s — %s", ticker, exc)

        logger.info("alt_data: OpenInsider — %d notable trade(s) for %s", len(records), ticker)
        return records

    # ── internal helpers ────────────────────────────────────────────────────

    def _build_col_map(self, headers: List[str]) -> Dict[str, int]:
        """Map column name variants to zero-based column indices."""
        lookup = {h.lower().replace("\xa0", "").replace(" ", "").replace("-", ""): i for i, h in enumerate(headers)}
        mapping = {}
        # Try various header name patterns seen on openinsider
        for alias, canonical in [
            ("filingdate", "filing_date"),
            ("tradedate", "trade_date"),
            ("ticker", "ticker"),
            ("insidername", "insider_name"),
            ("title", "title"),
            ("tradetype", "trade_type"),
            ("price", "price"),
            ("qty", "qty"),
            ("owned", "owned"),
            ("deltaown", "delta_own"),
            ("value", "value"),
        ]:
            if alias in lookup:
                mapping[canonical] = lookup[alias]
        return mapping

    def _parse_row(
        self,
        cells: List[Any],
        col_map: Dict[str, int],
        ticker: str,
        url: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Parse a single data row.  Returns a record dict or None if not notable.
        """

        def _cell(key: str) -> str:
            idx = col_map.get(key)
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].get_text(strip=True)

        trade_type = _cell("trade_type")
        insider_name = _cell("insider_name")
        title = _cell("title")
        price_str = _cell("price")
        qty_str = _cell("qty")
        value_str = _cell("value")
        filing_date = _cell("filing_date")
        trade_date = _cell("trade_date")

        # Clean numeric strings: remove $, commas, +, %
        def _num(s: str) -> float:
            cleaned = s.replace("$", "").replace(",", "").replace("+", "").replace("%", "").strip()
            try:
                return float(cleaned)
            except (ValueError, TypeError):
                return 0.0

        value = _num(value_str)
        qty = _num(qty_str)
        price = _num(price_str)

        # Determine if trade is a purchase or sale
        is_buy = any(kw in trade_type.upper() for kw in ("P -", "PURCHASE", "BUY", "P-"))
        is_sell = any(kw in trade_type.upper() for kw in ("S -", "SALE", "SELL", "S-"))

        # Filter: only keep trades above significance thresholds
        if is_buy and abs(value) < _INSIDER_BUY_THRESHOLD:
            return None
        if is_sell and abs(value) < _INSIDER_SELL_THRESHOLD:
            return None
        if not is_buy and not is_sell:
            # Could be an option exercise or other transaction — include if value is large
            if abs(value) < _INSIDER_SELL_THRESHOLD:
                return None

        action = "bought" if is_buy else "sold"
        value_fmt = f"${abs(value):,.0f}" if abs(value) >= 1_000 else f"${abs(value):.2f}"

        headline = (
            f"Insider {'Buy' if is_buy else 'Sale'}: "
            f"{insider_name or 'Unknown'} ({title or 'Unknown'}) "
            f"{action} {qty:,.0f} shares of {ticker} ({value_fmt})"
        )

        summary = json.dumps(
            {
                "trade_type": trade_type,
                "insider_name": insider_name,
                "title": title,
                "value": value,
                "qty": qty,
                "price": price,
                "filing_date": filing_date,
                "trade_date": trade_date,
            },
            default=str,
        )

        relevance_score = 0.8 if is_buy else 0.6

        return {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "source": "openinsider",
            "headline": headline,
            "url": url,
            "relevance_score": relevance_score,
            "category": "insider_trade",
            "summary": summary,
        }


# ── Class 2: Finviz scraper ──────────────────────────────────────────────────

class FinvizScraper:
    """
    Scrapes fundamental snapshot and recent news from finviz.com for a ticker.

    Returns news_intel-compatible dicts for both the snapshot record and any
    notable news headlines found on the quote page.
    """

    _BASE_URL = "https://finviz.com/quote.ashx?t={ticker}"

    # Fields to extract from the snapshot table (display label → dict key)
    _SNAPSHOT_FIELDS = [
        ("Market Cap", "market_cap"),
        ("P/E", "pe"),
        ("EPS (ttm)", "eps_ttm"),
        ("Insider Own", "insider_own"),
        ("Inst Own", "inst_own"),
        ("Short Float", "short_float"),
        ("Perf Week", "perf_week"),
        ("Perf Month", "perf_month"),
        ("Perf Quarter", "perf_quarter"),
        ("Volume", "volume"),
        ("Avg Volume", "avg_volume"),
        ("Rel Volume", "rel_volume"),
        ("Earnings", "earnings_date"),
    ]

    def scrape(self, ticker: str) -> List[Dict[str, Any]]:
        """
        Scrape fundamental snapshot + news for *ticker*.

        Returns a list of record dicts (snapshot + news items).
        Returns empty list on any error.
        """
        url = self._BASE_URL.format(ticker=ticker)
        records: List[Dict[str, Any]] = []

        resp = _get_with_retry(url)
        if resp is None:
            logger.warning("alt_data: Finviz — no response for %s", ticker)
            return records

        try:
            soup = BeautifulSoup(resp.text, "html.parser")

            # ── Fundamental snapshot ────────────────────────────────────────
            snapshot = self._parse_snapshot(soup)
            if snapshot:
                pe = snapshot.get("pe", "N/A")
                short_float = snapshot.get("short_float", "N/A")
                rel_vol = snapshot.get("rel_volume", "N/A")

                headline = (
                    f"{ticker} Snapshot: P/E {pe}, "
                    f"Short Float {short_float}, "
                    f"Rel Volume {rel_vol}x"
                )
                records.append(
                    {
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "source": "finviz",
                        "headline": headline,
                        "url": url,
                        "relevance_score": 0.5,
                        "category": "fundamentals",
                        "summary": json.dumps(snapshot, default=str),
                    }
                )

            # ── News headlines ──────────────────────────────────────────────
            news_items = self._parse_news(soup)
            for item in news_items:
                headline_text = item.get("headline", "")
                is_earnings = any(
                    kw in headline_text.lower()
                    for kw in ("earnings", "eps", "revenue", "beats", "misses", "guidance")
                )
                records.append(
                    {
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "source": "finviz_news",
                        "headline": headline_text,
                        "url": item.get("url"),
                        "relevance_score": 0.7 if is_earnings else 0.5,
                        "category": "earnings" if is_earnings else "news",
                        "summary": None,
                    }
                )

        except Exception as exc:
            logger.warning("alt_data: Finviz parse error for %s — %s", ticker, exc)

        logger.info("alt_data: Finviz — %d record(s) for %s", len(records), ticker)
        return records

    # ── internal helpers ─────────────────────────────────────────────────────

    def _parse_snapshot(self, soup: BeautifulSoup) -> Dict[str, str]:
        """
        Parse the Finviz snapshot table.

        Finviz renders fundamentals in a <table class="snapshot-table2"> or
        similar.  We look for all label→value pairs matching our field list.
        """
        snapshot: Dict[str, str] = {}

        # Finviz uses tables with class containing "snapshot" for the quote grid
        # Each label is in a <td class="snapshot-td2-cp"> and value follows
        # Try multiple known class patterns
        target_classes = [
            "snapshot-table2",
            "t-ct",            # older finviz layout
        ]

        table = None
        for cls in target_classes:
            table = soup.find("table", class_=cls)
            if table:
                break

        if table is None:
            # Fall back: search all tables for known field names
            for tbl in soup.find_all("table"):
                text = tbl.get_text()
                if "P/E" in text and "Short Float" in text:
                    table = tbl
                    break

        if table is None:
            logger.info("alt_data: Finviz — snapshot table not found in page")
            return snapshot

        # Build a flat list of (label, value) pairs from all td elements
        cells = table.find_all("td")
        # Finviz alternates label/value cells
        for i in range(0, len(cells) - 1, 2):
            label = cells[i].get_text(strip=True)
            value = cells[i + 1].get_text(strip=True) if (i + 1) < len(cells) else ""
            for display_label, dict_key in self._SNAPSHOT_FIELDS:
                if display_label.lower() == label.lower():
                    snapshot[dict_key] = value
                    break

        return snapshot

    def _parse_news(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        """
        Parse the news table from a Finviz quote page.

        Returns a list of {headline, url} dicts.
        """
        items: List[Dict[str, str]] = []

        # Finviz news rows are in a table with id="news-table"
        news_table = soup.find("table", id="news-table")
        if news_table is None:
            logger.info("alt_data: Finviz — news-table not found")
            return items

        seen_headlines: set[str] = set()
        for row in news_table.find_all("tr"):
            link = row.find("a", class_="tab-link-news")
            if link is None:
                # Older Finviz uses plain <a> inside news rows
                link = row.find("a")
            if link is None:
                continue

            headline = link.get_text(strip=True)
            href = link.get("href", "")

            if not headline or headline in seen_headlines:
                continue
            seen_headlines.add(headline)
            items.append({"headline": headline, "url": href or None})

        return items


# ── Class 3: Orchestrator ────────────────────────────────────────────────────



# ── Config helper ─────────────────────────────────────────────────────────────


def _load_alt_data_config() -> Dict[str, Any]:
    """Load the alt_data section from config/active/sp500.json.

    Returns an empty dict on any error so callers can fall back gracefully.
    """
    try:
        config_path = _PROJECT_ROOT / "config" / "active" / "sp500.json"
        with open(config_path) as _f:
            _cfg = json.load(_f)
        return _cfg.get("alt_data", {})
    except Exception as exc:
        logger.warning("alt_data: could not load config — %s", exc)
        return {}


class AltDataCollector:
    """
    Orchestrates OpenInsider + Finviz scraping for Atlas portfolio tickers.

    Args:
        dry_run:  If True, print results to stdout instead of writing to DB.
        tickers:  Optional explicit ticker list (overrides auto-detection).
    """

    def __init__(
        self,
        dry_run: bool = False,
        tickers: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.dry_run = dry_run
        self._ticker_override = tickers  # set via constructor; also overridable in run()
        self._config_override = config   # override for tests; None → load from file

    # ── ticker resolution ────────────────────────────────────────────────────

    def get_tickers(self) -> List[str]:
        """
        Return the list of tickers to scrape.

        Priority:
          1. Constructor-supplied override
          2. atlas_db.get_open_positions()
          3. Fallback: ['SPY', 'QQQ', 'IWM']
        """
        if self._ticker_override:
            return list(self._ticker_override)

        try:
            from db.atlas_db import get_open_positions

            positions = get_open_positions()
            tickers = list({p["ticker"] for p in positions if p.get("ticker")})
            if tickers:
                logger.info("alt_data: detected %d open-position ticker(s)", len(tickers))
                return sorted(tickers)
        except Exception as exc:
            logger.warning("alt_data: could not load open positions — %s", exc)

        logger.info("alt_data: falling back to default market-index tickers")
        return ["SPY", "QQQ", "IWM"]

    # ── main entry point ─────────────────────────────────────────────────────

    def run(self, tickers: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Scrape OpenInsider and Finviz for the given (or auto-detected) tickers.

        Args:
            tickers: Optional override; if None, uses get_tickers().

        Returns:
            Summary dict: {tickers, openinsider_records, finviz_records, errors}
        """
        # ── Load config + validate mode ──────────────────────────────────────
        alt_cfg = (
            self._config_override
            if self._config_override is not None
            else _load_alt_data_config()
        )
        _mode = alt_cfg.get("mode", "observe")
        _max_per_ticker_cfg = int(alt_cfg.get("max_per_ticker", 3))

        if _mode not in ("observe", "active"):
            logger.warning(
                "[alt_data] unknown mode=%r — expected 'observe' or 'active'; "
                "aborting run to avoid unintended side-effects",
                _mode,
            )
            return {
                "tickers": [],
                "openinsider_records": 0,
                "finviz_records": 0,
                "errors": [f"unknown mode: {_mode!r}"],
            }

        if tickers:
            self._ticker_override = tickers

        # If no explicit CLI/constructor override but config supplies tickers, use them.
        # This covers: (a) tests passing a config dict; (b) production cron reading
        # config/active/sp500.json which now lists 199 watchlist tickers.
        if not self._ticker_override and alt_cfg.get("tickers"):
            self._ticker_override = list(alt_cfg["tickers"])

        active_tickers = self.get_tickers()

        # Structured startup observation log (greppable)
        logger.info(
            "[alt_data] mode=%s tickers=%d max_per_ticker=%d",
            _mode, len(active_tickers), _max_per_ticker_cfg,
        )
        logger.info(
            "alt_data: starting run for %d ticker(s) (dry_run=%s)",
            len(active_tickers), self.dry_run,
        )
        _run_start = time.monotonic()

        openinsider_scraper = OpenInsiderScraper()
        finviz_scraper = FinvizScraper()

        all_records: List[Dict[str, Any]] = []
        openinsider_count = 0
        finviz_count = 0
        errors: List[str] = []

        for ticker in active_tickers:
            # ── OpenInsider ─────────────────────────────────────────────────
            try:
                oi_records = openinsider_scraper.scrape(ticker)
                openinsider_count += len(oi_records)
                all_records.extend(oi_records)
            except Exception as exc:
                msg = f"OpenInsider/{ticker}: {exc}"
                logger.error("alt_data: %s", msg)
                errors.append(msg)

            time.sleep(_REQUEST_DELAY)

            # ── Finviz ───────────────────────────────────────────────────────
            try:
                fv_records = finviz_scraper.scrape(ticker)
                finviz_count += len(fv_records)
                all_records.extend(fv_records)
            except Exception as exc:
                msg = f"Finviz/{ticker}: {exc}"
                logger.error("alt_data: %s", msg)
                errors.append(msg)

            time.sleep(_REQUEST_DELAY)

        # ── Write or dry-run ─────────────────────────────────────────────────
        if self.dry_run:
            print(f"\n=== DRY RUN: {len(all_records)} record(s) would be written ===\n")
            for rec in all_records:
                print(json.dumps(rec, indent=2))
                print()
        else:
            self._write_records(all_records, errors)

        _elapsed_ms = int((time.monotonic() - _run_start) * 1000)
        _total_hits = openinsider_count + finviz_count

        result = {
            "tickers": active_tickers,
            "openinsider_records": openinsider_count,
            "finviz_records": finviz_count,
            "errors": errors,
        }
        logger.info("alt_data: run complete — %s", result)
        # Structured end-of-batch observation log (greppable)
        logger.info(
            "[alt_data] mode=%s tickers=%d hits=%d latency_ms=%d",
            _mode, len(active_tickers), _total_hits, _elapsed_ms,
        )
        return result

    # ── DB write ─────────────────────────────────────────────────────────────

    def _write_records(
        self, records: List[Dict[str, Any]], errors: List[str]
    ) -> None:
        """Write each record to news_intel via atlas_db.record_news()."""
        try:
            from db.atlas_db import record_news
        except ImportError as exc:
            msg = f"atlas_db import failed — {exc}"
            logger.error("alt_data: %s", msg)
            errors.append(msg)
            return

        for rec in records:
            try:
                record_news(
                    timestamp=rec["timestamp"],
                    source=rec.get("source"),
                    headline=rec.get("headline"),
                    url=rec.get("url"),
                    relevance_score=rec.get("relevance_score"),
                    category=rec.get("category"),
                    summary=rec.get("summary"),
                )
            except Exception as exc:
                msg = f"record_news failed for '{rec.get('headline', '')[:60]}': {exc}"
                logger.error("alt_data: %s", msg)
                errors.append(msg)

        logger.info("alt_data: wrote %d record(s) to news_intel", len(records))




# ── Public summary helper ─────────────────────────────────────────────────────


def get_alt_data_summary(tickers: list[str] | None = None, max_per_ticker: int = 3) -> str:
    """Build a string summary of recent insider/screener signals for `tickers`.

    Returns empty string if scraper unavailable, no tickers given, or no signals found.
    Output format (one line per signal):
        AAPL: Insider Buy — Tim Cook (CEO) bought 10,000 shares ($2,000,000)
        MSFT: Insider Sale — Satya Nadella (CEO) sold 5,000 shares ($1,500,000)
    Maximum `max_per_ticker` signals per ticker, max 30 lines total.
    """
    if not tickers:
        return ""
    try:
        lines: list[str] = []
        scraper = OpenInsiderScraper()
        for ticker in tickers[:30]:  # cap to avoid runaway scrape time
            try:
                records = scraper.scrape(ticker)
            except Exception as exc:
                logger.debug("alt_data summary: skip %s: %s", ticker, exc)
                continue
            # Pick the top `max_per_ticker` by relevance_score
            top = sorted(records, key=lambda r: r.get("relevance_score", 0), reverse=True)[:max_per_ticker]
            for rec in top:
                headline = rec.get("headline", "")
                if headline:
                    lines.append(f"{ticker}: {headline}")
                if len(lines) >= 30:
                    break
            if len(lines) >= 30:
                break
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("get_alt_data_summary failed: %s", exc)
        return ""

# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Alt data scraper")
    parser.add_argument(
        "--dry-run", action="store_true", help="Scrape but don't write to DB"
    )
    parser.add_argument(
        "--tickers",
        type=str,
        help="Comma-separated tickers (e.g. AAPL,MSFT)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    collector = AltDataCollector(dry_run=args.dry_run)
    result = collector.run(tickers=tickers)
    print(json.dumps(result, indent=2))
