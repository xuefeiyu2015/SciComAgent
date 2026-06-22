"""Tests for api.fetch.fetch_source — pure extraction, no network, no AI.

Web fetches are stubbed via monkeypatch; PDFs are generated in-memory with
pymupdf so the tests stay offline and deterministic.
"""

from __future__ import annotations

import httpx
import pymupdf
import pytest

from api import fetch
from api.fetch import fetch_source

# Paper-like filler: clears _MIN_TEXT_LEN and carries academic markers.
_PAPER_BODY = (
    "Abstract. We report that the treatment improved outcomes in a small "
    "preliminary cohort of mice. Introduction. Prior work motivates this study. "
    "Methods. We used a randomized in vivo mouse model. Results. A 23% reduction "
    "in tumor volume was observed (n=12). Discussion. Effects may not generalize "
    "to humans. References. doi:10.1234/example. "
) * 6


def _fake_response(content: bytes, content_type: str, url: str = "https://x.test/a"):
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", url),
    )


def _pdf_bytes(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    # insert_textbox wraps within the rect; insert_text would clip the runoff.
    page.insert_textbox(pymupdf.Rect(72, 72, 523, 770), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


# --- PDF source -------------------------------------------------------------

def test_pdf_path_returns_full_text(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_pdf_bytes(_PAPER_BODY))

    result = fetch_source(str(pdf), "pdf")

    assert result.ok
    assert result.code == "ok"
    assert "preliminary cohort of mice" in result.text


def test_pdf_without_text_layer_is_too_short(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(_pdf_bytes("too short"))  # below _MIN_TEXT_LEN

    result = fetch_source(str(pdf), "pdf")

    assert not result.ok
    assert result.code == "too_short"


# --- URL / DOI source -------------------------------------------------------

def test_url_full_text_paper_ok(monkeypatch):
    html = f"<html><body><article><h1>Study</h1><p>{_PAPER_BODY}</p></article></body></html>"
    monkeypatch.setattr(
        fetch.httpx, "get", lambda *a, **k: _fake_response(html.encode(), "text/html")
    )

    result = fetch_source("https://journal.test/full", "url")

    assert result.ok
    assert "preliminary cohort of mice" in result.text


def test_url_marketing_page_is_not_a_paper(monkeypatch):
    # Long enough to clear _MIN_TEXT_LEN, so it is rejected on structure
    # (no academic markers) rather than on length.
    marketing = (
        "Get job-ready for an in-demand career. Join 100M+ learners worldwide. "
        "Why people choose our platform: flexible schedules, affordable plans, "
        "trusted by top companies. Start your free trial today and build the "
        "skills employers want. Learners share their success stories every day. "
    ) * 4
    html = f"<html><body><h1>Learn without limits</h1><p>{marketing}</p></body></html>"
    monkeypatch.setattr(
        fetch.httpx, "get", lambda *a, **k: _fake_response(html.encode(), "text/html")
    )

    result = fetch_source("https://courses.test/", "url")

    assert not result.ok
    assert result.code == "not_a_paper"


def test_abstract_page_upgrades_to_citation_pdf(monkeypatch):
    """An abstract page advertising citation_pdf_url is upgraded to the PDF."""
    pdf_url = "https://journal.test/article.pdf"
    abstract_html = (
        f'<html><head><meta name="citation_pdf_url" content="{pdf_url}">'
        "</head><body><p>Abstract only. Short landing page.</p></body></html>"
    )
    pdf = _pdf_bytes(_PAPER_BODY)

    def dispatch(url, *a, **k):
        if url == pdf_url:
            return _fake_response(pdf, "application/pdf", url=pdf_url)
        return _fake_response(abstract_html.encode(), "text/html", url=str(url))

    monkeypatch.setattr(fetch.httpx, "get", dispatch)

    result = fetch_source("https://journal.test/abstract", "url")

    assert result.ok
    assert "preliminary cohort of mice" in result.text  # came from the PDF
    assert result.source_url == pdf_url


def test_arxiv_abs_rewritten_to_pdf(monkeypatch):
    seen = {}
    pdf = _pdf_bytes(_PAPER_BODY)

    def dispatch(url, *a, **k):
        seen["url"] = url
        return _fake_response(pdf, "application/pdf", url=str(url))

    monkeypatch.setattr(fetch.httpx, "get", dispatch)

    result = fetch_source("https://arxiv.org/abs/1706.03762", "url")

    assert result.ok
    assert seen["url"] == "https://arxiv.org/pdf/1706.03762"  # rewritten before GET


def test_doi_redirecting_to_pdf_is_extracted(monkeypatch):
    pdf = _pdf_bytes(_PAPER_BODY)
    monkeypatch.setattr(
        fetch.httpx, "get", lambda *a, **k: _fake_response(pdf, "application/pdf")
    )

    result = fetch_source("10.1234/example", "doi")

    assert result.ok
    assert "preliminary cohort of mice" in result.text


def test_http_error_is_fetch_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(fetch.httpx, "get", boom)

    result = fetch_source("https://down.test/x", "url")

    assert not result.ok
    assert result.code == "fetch_error"


def test_unknown_source_type_raises():
    with pytest.raises(ValueError):
        fetch_source("whatever", "ftp")
