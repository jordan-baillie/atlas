#!/usr/bin/env python3
"""Source rotation config for Atlas Research Discovery — Channel 4.

Maps each day of the week to a discovery source and method, plus a blog
rotation list for Saturday browser-based crawls.

Usage:
    from research.discovery.sources import get_today_source, get_queries_for_source
    source = get_today_source()
    queries = get_queries_for_source(source)
"""

import json
import datetime
from pathlib import Path

_DISCOVERY_DIR = Path(__file__).resolve().parent
_QUERIES_FILE = _DISCOVERY_DIR / "config" / "queries.json"

# ─── Daily source rotation ───────────────────────────────────────────────────
#
# Each entry maps a 3-letter weekday abbreviation to a discovery config:
#   source      — human-readable source name
#   method      — "api" | "computer_use" | "review"
#   description — what the session does
#   categories  — list of query category keys from queries.json

DAILY_ROTATION: dict = {
    "mon": {
        "source": "arxiv",
        "method": "api",
        "description": "Fetch recent q-fin papers from ArXiv API covering momentum and mean reversion",
        "categories": ["momentum", "mean_reversion"],
    },
    "tue": {
        "source": "arxiv",
        "method": "api",
        "description": "Fetch recent q-fin papers from ArXiv API covering volume and event strategies",
        "categories": ["volume", "event"],
    },
    "wed": {
        "source": "ssrn",
        "method": "computer_use",
        "description": "Browse SSRN for working papers on volatility and calendar effects via Computer Use",
        "categories": ["volatility", "calendar"],
    },
    "thu": {
        "source": "arxiv",
        "method": "api",
        "description": "Fetch recent q-fin papers from ArXiv API covering miscellaneous strategies",
        "categories": ["other"],
    },
    "fri": {
        "source": "quantpedia",
        "method": "computer_use",
        "description": "Browse Quantpedia strategy library for new algorithmic trading ideas via Computer Use",
        "categories": ["momentum", "mean_reversion", "volatility"],
    },
    "sat": {
        "source": "blog",
        "method": "computer_use",
        "description": "Browse rotating quant research blog for strategy ideas via Computer Use",
        "categories": ["other", "momentum", "mean_reversion"],
    },
    "sun": {
        "source": "review",
        "method": "review",
        "description": "Review downloaded PDFs and queue promising strategies for code generation",
        "categories": [],
    },
}

# ─── Blog rotation for Saturday ───────────────────────────────────────────────
#
# Rotates through these blogs by ISO week number (week_number % len(BLOG_ROTATION)).

BLOG_ROTATION: list = [
    {"url": "https://quantifiedstrategies.com/trading-strategies/", "name": "Quantified Strategies"},
    {"url": "https://alphaarchitect.com/blog/", "name": "Alpha Architect"},
    {"url": "https://www.factorresearch.com/research", "name": "Factor Research"},
    {"url": "https://robotwealth.com/blog/", "name": "Robot Wealth"},
    {"url": "https://blog.thinknewfound.com/", "name": "Newfound Research"},
    {"url": "https://www.portfoliovisualizer.com/blog", "name": "Portfolio Visualizer"},
    {"url": "https://systematicmoney.org/", "name": "Systematic Money"},
    {"url": "https://allocatesmartly.com/tactical-asset-allocation/", "name": "Allocate Smartly"},
]


# ─── Public API ───────────────────────────────────────────────────────────────

def get_today_source() -> dict:
    """Return the discovery source config for today's day of week.

    Returns a dict with keys: source, method, description, categories.
    """
    day_abbr = datetime.date.today().strftime("%a").lower()  # e.g. "mon"
    config = DAILY_ROTATION.get(day_abbr)
    if config is None:
        # Fallback: shouldn't happen, but default to arxiv api
        config = DAILY_ROTATION["mon"]
    return {**config, "day": day_abbr}


def get_queries_for_source(source: dict) -> list:
    """Return a flat list of search queries for the given source config.

    Args:
        source: Dict returned by get_today_source() — must have a 'categories' key.

    Returns:
        Flat list of query strings from queries.json for the given categories.
    """
    categories = source.get("categories", [])
    if not categories:
        return []

    try:
        queries_data = json.loads(_QUERIES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        import logging
        logging.getLogger(__name__).error("Failed to load queries.json: %s", exc)
        return []

    result: list = []
    for category in categories:
        category_queries = queries_data.get(category, [])
        result.extend(category_queries)
    return result


def get_blog_for_today() -> dict:
    """Return the blog to crawl today (Saturday rotation by ISO week number).

    Returns a dict with keys: url, name.
    """
    week_number = datetime.date.today().isocalendar()[1]
    idx = week_number % len(BLOG_ROTATION)
    return BLOG_ROTATION[idx]


# ─── CLI convenience ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json as _json
    source = get_today_source()
    queries = get_queries_for_source(source)
    blog = get_blog_for_today()
    print("Today's source:", _json.dumps(source, indent=2))
    print(f"Queries ({len(queries)}):", _json.dumps(queries, indent=2))
    print("Blog rotation:", _json.dumps(blog, indent=2))
