#!/usr/bin/env python3
"""ArXiv API browser for Atlas Research Discovery.

Fetches recent quantitative finance papers from ArXiv using the `arxiv` package,
downloads PDFs locally, and deduplicates via seen_urls.txt.

Usage:
    from research.discovery.arxiv_api import fetch_new_papers
    papers = fetch_new_papers(["momentum breakout stocks", "RSI mean reversion"])
"""

import logging
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import arxiv

logger = logging.getLogger(__name__)

# ─── Paths ───────────────────────────────────────────────────────────────────

_DISCOVERY_DIR = Path(__file__).resolve().parent
_PAPERS_DIR = _DISCOVERY_DIR / "papers"
_SEEN_URLS_FILE = _DISCOVERY_DIR / "seen_urls.txt"

_PAPERS_DIR.mkdir(parents=True, exist_ok=True)


# ─── Dedup helpers ────────────────────────────────────────────────────────────

def _load_seen_urls() -> set:
    """Load the set of already-processed URLs from seen_urls.txt."""
    if not _SEEN_URLS_FILE.exists():
        return set()
    return {line.strip() for line in _SEEN_URLS_FILE.read_text().splitlines() if line.strip()}


def _is_url_seen(url: str) -> bool:
    """Return True if url already appears in seen_urls.txt."""
    if not _SEEN_URLS_FILE.exists():
        return False
    for line in _SEEN_URLS_FILE.read_text().splitlines():
        if line.strip() == url:
            return True
    return False


def _mark_url_seen(url: str) -> None:
    """Append url to seen_urls.txt."""
    with _SEEN_URLS_FILE.open("a") as f:
        f.write(url + "\n")


# ─── PDF download ─────────────────────────────────────────────────────────────

def _download_pdf(pdf_url: str, paper_id: str) -> str | None:
    """Download a PDF to the papers/ directory. Returns local path or None on error."""
    # Sanitise paper_id for use as filename
    safe_id = paper_id.replace("/", "_").replace(":", "_")
    dest = _PAPERS_DIR / f"{safe_id}.pdf"
    if dest.exists():
        logger.debug("PDF already cached: %s", dest)
        return str(dest)
    try:
        logger.info("Downloading PDF: %s → %s", pdf_url, dest)
        urllib.request.urlretrieve(pdf_url, dest)
        return str(dest)
    except Exception as exc:
        logger.warning("Failed to download PDF %s: %s", pdf_url, exc)
        return None


# ─── Main API ─────────────────────────────────────────────────────────────────

def fetch_new_papers(
    queries: list,
    max_results: int = 20,
    since_days: int = 7,
) -> list:
    """Search ArXiv q-fin category for recent papers matching the given queries.

    Args:
        queries:     List of search query strings.
        max_results: Maximum results to fetch per query.
        since_days:  Only return papers published within the last N days.

    Returns:
        List of dicts with keys:
            url, title, authors, abstract, pdf_url, published, source, local_pdf
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=since_days)
    seen_urls = _load_seen_urls()
    results: list = []
    seen_this_run: set = set()

    client = arxiv.Client()

    for query in queries:
        # Scope to quantitative finance category
        scoped_query = f"cat:q-fin.* {query}"
        logger.info("ArXiv query: %s", scoped_query)

        search = arxiv.Search(
            query=scoped_query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )

        try:
            for paper in client.results(search):
                url = paper.entry_id  # canonical URL e.g. https://arxiv.org/abs/2401.12345

                # Date filter
                published_dt = paper.published
                if published_dt.tzinfo is None:
                    published_dt = published_dt.replace(tzinfo=timezone.utc)
                if published_dt < cutoff:
                    logger.debug("Skipping old paper: %s (%s)", url, published_dt.date())
                    continue

                # Dedup
                if url in seen_urls or url in seen_this_run:
                    logger.debug("Skipping already-seen: %s", url)
                    continue

                seen_this_run.add(url)

                # Download PDF
                pdf_url = paper.pdf_url
                paper_id = paper.get_short_id()
                local_pdf = _download_pdf(pdf_url, paper_id)

                # Build result record
                record = {
                    "url": url,
                    "title": paper.title,
                    "authors": ", ".join(str(a) for a in paper.authors),
                    "abstract": paper.summary.replace("\n", " ").strip(),
                    "pdf_url": pdf_url,
                    "published": published_dt.strftime("%Y-%m-%d"),
                    "source": "arxiv",
                    "local_pdf": local_pdf,
                }
                results.append(record)
                logger.info("Found: %s — %s", record["published"], paper.title[:80])

        except Exception as exc:
            logger.error("ArXiv query failed (%s): %s", query, exc)
            continue

    # Mark all newly-seen URLs in batch
    if seen_this_run:
        with _SEEN_URLS_FILE.open("a") as f:
            for url in seen_this_run:
                f.write(url + "\n")

    logger.info("fetch_new_papers: %d new papers found across %d queries", len(results), len(queries))
    return results


# ─── CLI convenience ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    papers = fetch_new_papers(
        ["momentum breakout individual stocks", "mean reversion RSI daily"],
        max_results=5,
        since_days=30,
    )
    print(json.dumps(papers, indent=2, default=str))
