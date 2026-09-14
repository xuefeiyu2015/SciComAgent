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


def test_paywall_403_requests_pdf(monkeypatch):
    """A publisher block (HTTP 403) asks the human for the PDF, not a hard error."""
    def forbidden(url, *a, **k):
        resp = _fake_response(b"<html>Access Denied</html>", "text/html", url=str(url))
        resp.status_code = 403
        return resp

    monkeypatch.setattr(fetch.httpx, "get", forbidden)

    result = fetch_source("https://www.cell.com/neuron/fulltext/S0896-6273", "url")

    assert not result.ok
    assert result.code == "need_pdf"
    assert "PDF" in result.reason


# --- rate limiting ------------------------------------------------------------

def _throttling(status=429, retry_after=None, succeed_after=None):
    """A host that returns `status` until `succeed_after` attempts have been made."""
    calls = {"n": 0}

    def get(url, *a, **k):
        calls["n"] += 1
        if succeed_after is not None and calls["n"] > succeed_after:
            page = (f"<html><body><article><h1>Study</h1><p>{_PAPER_BODY}</p>"
                    "</article></body></html>")
            return _fake_response(page.encode(), "text/html", url=str(url))
        resp = _fake_response(b"<html>Attention Required</html>", "text/html",
                              url=str(url))
        resp.status_code = status
        if retry_after is not None:
            resp.headers["retry-after"] = str(retry_after)
        return resp

    return get, calls


def test_a_rate_limit_is_waited_out_not_failed(monkeypatch):
    """HTTP 429 means "slow down", not "gone" — asking again usually works."""
    get, calls = _throttling(succeed_after=1)
    monkeypatch.setattr(fetch.httpx, "get", get)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)

    result = fetch_source("https://www.biorxiv.org/content/10.1/2026.01.01v1", "url")

    assert result.ok, result.reason
    assert calls["n"] == 2, "it should have tried again"


def test_a_persistent_rate_limit_says_the_link_is_fine(monkeypatch):
    """The reported confusion: "could not fetch" sent the human to check a URL
    that was never the problem."""
    get, calls = _throttling()
    monkeypatch.setattr(fetch.httpx, "get", get)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)

    result = fetch_source("https://www.biorxiv.org/content/10.1/2026.01.01v1", "url")

    assert not result.ok
    assert result.code == "rate_limited"
    assert "the link is fine" in result.reason
    assert "PDF" in result.reason, "the way past it must be in the message"
    assert calls["n"] == fetch._RATE_LIMIT_RETRIES + 1


def test_a_server_asking_us_to_wait_is_obeyed_within_reason(monkeypatch):
    waited = []
    get, _calls = _throttling(retry_after=5, succeed_after=1)
    monkeypatch.setattr(fetch.httpx, "get", get)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: waited.append(s))

    fetch_source("https://www.biorxiv.org/content/10.1/2026.01.01v1", "url")

    assert waited == [5.0]


def test_an_absurd_retry_after_does_not_hold_a_run_hostage(monkeypatch):
    waited = []
    get, _calls = _throttling(retry_after=3600, succeed_after=1)
    monkeypatch.setattr(fetch.httpx, "get", get)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: waited.append(s))

    fetch_source("https://www.biorxiv.org/content/10.1/2026.01.01v1", "url")

    assert waited == [fetch._RATE_LIMIT_MAX_WAIT_S]


def test_a_paywall_is_not_retried(monkeypatch):
    """403 will say 403 again; waiting only wastes the human's time."""
    get, calls = _throttling(status=403)
    monkeypatch.setattr(fetch.httpx, "get", get)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: pytest.fail("must not wait"))

    result = fetch_source("https://www.cell.com/neuron/fulltext/S0896", "url")

    assert result.code == "need_pdf"
    assert calls["n"] == 1


def test_unknown_source_type_raises():
    with pytest.raises(ValueError):
        fetch_source("whatever", "ftp")
