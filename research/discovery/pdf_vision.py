"""PDF figure extraction via pdftoppm → call_pi_vision.

Pre-pass before _extract_specs in research/discovery/discovery.py.
Converts each PDF page to a 2576px-wide PNG, then asks Claude Opus 4.7
vision to extract structured insights from each page's figures
(equity curves, Sharpe tables, heatmaps, strategy flow diagrams).

Returns a string blob of extracted figure descriptions, suitable for
appending to the text-extract prompt. Returns empty string and logs
on any failure — never raises (defensive: must not break _extract_specs).

Requires:
    - pdftoppm in PATH (apt: poppler-utils)
    - utils.pi_subprocess.call_pi_vision (Claude Max OAuth)
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Page-render resolution: 2576px wide is the resolution Claude Opus 4.7 vision
# handles best for chart/figure extraction (per /tmp/atlas-batch spec).
# At default 72dpi, US Letter = 612pt = ~612px. To get 2576px wide we need
# ~300dpi (300 * 8.5 = 2550px). Use -r 300.
_RENDER_DPI = 300
_MAX_PAGES_DEFAULT = 8  # cap per paper — full backtests rarely have >8 figure pages

_FIGURE_EXTRACTION_PROMPT = """\
You are inspecting a single page from a quantitative-finance research paper.

If this page contains FIGURES, TABLES, or CHARTS (equity curves, Sharpe-ratio
tables, drawdown plots, heatmaps, strategy flow diagrams, parameter sweeps,
backtest performance tables), describe them concisely. Focus on:
  \u2022 Key numeric values (Sharpe, CAGR, max drawdown, win rate, holding period)
  \u2022 Strategy parameters visible in tables (lookback, threshold, weight)
  \u2022 Direction of equity curves (monotonic up, volatile, drawdown-heavy)
  \u2022 Asset universes shown (sp500, sectors, individual tickers)

If the page is pure prose with no figures/tables/charts, respond with the
single token: NO_FIGURES

Reply in JSON, no markdown:
{"has_figures": true|false, "summary": "concise factual description", "metrics": {"sharpe": 1.5, "cagr_pct": 12.3, ...} | null}
"""


def pdftoppm_available() -> bool:
    return shutil.which("pdftoppm") is not None


def render_pdf_pages(pdf_path: Path, out_dir: Path, dpi: int = _RENDER_DPI,
                     max_pages: int = _MAX_PAGES_DEFAULT) -> list[Path]:
    """Render PDF pages → PNGs (one per page). Returns sorted page paths."""
    if not pdftoppm_available():
        logger.warning("pdftoppm not in PATH — skipping PDF render for %s", pdf_path.name)
        return []
    if not pdf_path.exists():
        logger.warning("PDF not found: %s", pdf_path)
        return []
    out_prefix = out_dir / pdf_path.stem
    try:
        # pdftoppm: -png, -r dpi, -f 1 -l max_pages limits page range
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), "-f", "1", "-l", str(max_pages),
             str(pdf_path), str(out_prefix)],
            check=True, capture_output=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.warning("pdftoppm timeout on %s", pdf_path.name)
        return []
    except subprocess.CalledProcessError as exc:
        logger.warning("pdftoppm failed on %s: %s", pdf_path.name, exc.stderr.decode()[:200])
        return []

    # pdftoppm outputs <prefix>-NN.png (zero-padded by default since pdftoppm 0.66)
    pngs = sorted(out_dir.glob(f"{pdf_path.stem}-*.png"))
    logger.info("Rendered %d page(s) from %s", len(pngs), pdf_path.name)
    return pngs


def extract_figures_from_pdf(pdf_path: Path, max_pages: int = _MAX_PAGES_DEFAULT) -> str:
    """Render PDF pages → call Claude vision per page → merge results.

    Returns a multi-line string blob (page-by-page summary) suitable for
    inclusion in the downstream extract.md prompt as context. Returns
    empty string on any failure (defensive — never raises).
    """
    if not pdf_path.exists():
        return ""

    try:
        from utils.pi_subprocess import call_pi_vision
    except ImportError:
        logger.warning("call_pi_vision not available — vision pre-pass skipped")
        return ""

    with tempfile.TemporaryDirectory(prefix="pdf_vision_") as tmpdir:
        tmp_path = Path(tmpdir)
        pngs = render_pdf_pages(pdf_path, tmp_path, max_pages=max_pages)
        if not pngs:
            return ""

        summaries: list[str] = []
        for i, png in enumerate(pngs, 1):
            try:
                raw = call_pi_vision(
                    prompt=_FIGURE_EXTRACTION_PROMPT,
                    image_paths=[png],
                    model="claude-opus-4-7",
                    timeout=180,
                    mode="json",
                )
                raw = raw.strip()
                if not raw or "NO_FIGURES" in raw[:64]:
                    continue
                summaries.append(f"  [page {i}] {raw[:1500]}")
            except Exception as exc:
                logger.warning("vision call failed for %s page %d: %s",
                               pdf_path.name, i, exc)
                continue

        if not summaries:
            return ""
        header = f"### Vision-extracted figure summaries for {pdf_path.name}"
        return header + "\n" + "\n".join(summaries)


def enrich_papers_with_vision(papers: list[dict], max_pages: int = _MAX_PAGES_DEFAULT) -> list[dict]:
    """For each paper with a local_pdf, attach vision_summary to dict.

    Returns the same papers list with added/updated key:
      paper["vision_summary"]: str (may be empty)

    Mutates input list in-place. Defensive — never raises.
    """
    if not pdftoppm_available():
        logger.info("pdftoppm unavailable — vision enrichment skipped for %d paper(s)",
                    len(papers))
        return papers

    for p in papers:
        local = p.get("local_pdf")
        if not local:
            continue
        try:
            summary = extract_figures_from_pdf(Path(local), max_pages=max_pages)
            p["vision_summary"] = summary
        except Exception as exc:
            logger.warning("vision enrichment failed for %s: %s",
                           p.get("title", "<untitled>")[:60], exc)
            p["vision_summary"] = ""
    return papers
