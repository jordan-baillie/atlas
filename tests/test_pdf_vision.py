"""Unit tests for research.discovery.pdf_vision.

Mocks pdftoppm and call_pi_vision — no real PDFs/network needed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from research.discovery import pdf_vision


class TestPdftoppmAvailable:
    def test_returns_bool(self):
        result = pdf_vision.pdftoppm_available()
        assert isinstance(result, bool)


class TestRenderPdfPages:
    def test_missing_pdf_returns_empty(self, tmp_path):
        result = pdf_vision.render_pdf_pages(
            tmp_path / "missing.pdf", tmp_path
        )
        assert result == []

    def test_no_pdftoppm_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pdf_vision, "pdftoppm_available", lambda: False)
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-fake")
        result = pdf_vision.render_pdf_pages(pdf, tmp_path)
        assert result == []

    def test_subprocess_timeout_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pdf_vision, "pdftoppm_available", lambda: True)
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-fake")
        def boom(*a, **kw):
            raise subprocess.TimeoutExpired("pdftoppm", 120)
        monkeypatch.setattr(subprocess, "run", boom)
        result = pdf_vision.render_pdf_pages(pdf, tmp_path)
        assert result == []


class TestExtractFiguresFromPdf:
    def test_missing_pdf_returns_empty_string(self, tmp_path):
        result = pdf_vision.extract_figures_from_pdf(tmp_path / "missing.pdf")
        assert result == ""

    def test_no_pages_rendered_returns_empty(self, tmp_path, monkeypatch):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-fake")
        monkeypatch.setattr(pdf_vision, "render_pdf_pages", lambda *a, **k: [])
        result = pdf_vision.extract_figures_from_pdf(pdf)
        assert result == ""


class TestEnrichPapersWithVision:
    def test_no_pdftoppm_returns_papers_unchanged(self, monkeypatch):
        monkeypatch.setattr(pdf_vision, "pdftoppm_available", lambda: False)
        papers = [{"title": "X", "local_pdf": "/nope/x.pdf"}]
        result = pdf_vision.enrich_papers_with_vision(papers)
        # vision_summary should NOT be added when pdftoppm is unavailable
        assert "vision_summary" not in papers[0]
        assert result is papers

    def test_paper_without_local_pdf_skipped(self, monkeypatch):
        monkeypatch.setattr(pdf_vision, "pdftoppm_available", lambda: True)
        papers = [{"title": "no-pdf"}]
        result = pdf_vision.enrich_papers_with_vision(papers)
        assert "vision_summary" not in papers[0]

    def test_extraction_exception_logged_and_continues(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pdf_vision, "pdftoppm_available", lambda: True)
        def boom(*a, **kw):
            raise RuntimeError("vision crashed")
        monkeypatch.setattr(pdf_vision, "extract_figures_from_pdf", boom)
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-fake")
        papers = [{"title": "X", "local_pdf": str(pdf)}]
        # Should NOT raise
        pdf_vision.enrich_papers_with_vision(papers)
        assert papers[0]["vision_summary"] == ""
