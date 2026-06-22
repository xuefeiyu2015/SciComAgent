"""Pipeline step 1 — fetch + extract source text.

Turn a source (web URL / DOI / PDF) into clean, *complete* paper text for the
claim ledger. Pure code, no AI:
    - web articles -> httpx + trafilatura (main-content extraction)
    - PDFs         -> pymupdf text extraction

``fetch_source`` succeeds only when it yields useful, whole paper text suitable
for drafting, and otherwise returns a ``FetchResult`` whose ``code`` says why:

    ok          -> full text obtained
    need_pdf    -> source exists but access is blocked (HTTP 401/402/403/451,
                   e.g. a publisher paywall) -> provide the PDF
    too_short   -> reachable, but too little text (paywall/landing stub, or a
                   scanned PDF with no text layer) -> provide the PDF
    not_a_paper -> text lacks academic structure (e.g. a marketing page)
    fetch_error -> network failure / unreachable link

To get the *whole* body, an HTML abstract/landing page is auto-upgraded to its
full-text PDF via the Highwire ``citation_pdf_url`` meta tag (arXiv + most
journals expose it). The relevance heuristic here is best-effort; the
authoritative "is this really a paper" judgment lives downstream in the LLM
extractor.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import pymupdf
import trafilatura
from bs4 import BeautifulSoup

from api.schema import SourceType

# Below this many characters we don't trust the extraction (paywall/cookie
# wall stub, landing page, or image-only PDF).
_MIN_TEXT_LEN = 500

# Academic-structure markers used to tell a paper from non-paper content
# (e.g. a marketing homepage). Matched case-insensitively as substrings.
_ACADEMIC_MARKERS = (
    "abstract",
    "introduction",
    "method",
    "results",
    "discussion",
    "conclusion",
    "references",
    "doi",
)
_MIN_MARKERS = 3

_HTTP_TIMEOUT = 30.0
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; SciCommAgent/0.1; +https://turingplanet.example)"
    )
}


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a fetch. ``ok`` gates whether ``text`` is usable for drafting."""

    ok: bool
    text: str = ""
    reason: str = ""  # human-readable; empty when ok
    code: str = "ok"  # machine: ok | need_pdf | too_short | not_a_paper | fetch_error
    source_url: str = ""  # final resolved URL/path actually extracted


def fetch_source(source: str, source_type: str | SourceType) -> FetchResult:
    """Fetch a source and return complete paper text, or a typed failure.

    Args:
        source: web URL, DOI (bare or full), or PDF path/URL.
        source_type: one of "url", "doi", "pdf" (or the SourceType enum).

    Returns:
        FetchResult. On success ``ok`` is True and ``text`` holds the full body.
    """
    st = SourceType(source_type)

    if st is SourceType.pdf:
        return _pdf_source(source)

    url = source if st is SourceType.url else _doi_to_url(source)
    return _web_source(url)


def _doi_to_url(doi: str) -> str:
    """Resolve a DOI (bare or already a doi.org URL) to a resolvable URL."""
    doi = doi.strip()
    if doi.startswith(("http://", "https://")):
        return doi
    return f"https://doi.org/{doi.removeprefix('doi:').strip()}"


def _web_source(url: str) -> FetchResult:
    """Fetch an article URL, upgrading to full-text PDF when possible."""
    # arXiv fast path: the /abs/ page is only an abstract; go straight to PDF.
    if "arxiv.org/abs/" in url:
        url = url.replace("arxiv.org/abs/", "arxiv.org/pdf/")

    try:
        resp = _get(url)
    except httpx.HTTPError as exc:
        return _http_failure(exc, url)

    final_url = str(resp.url)
    if _looks_like_pdf(resp):
        return _assess(_pdf_from_bytes(resp.content), final_url)

    # HTML: prefer the full-text PDF advertised by the publisher, if any.
    pdf_url = _citation_pdf_url(resp.text, final_url)
    if pdf_url and pdf_url != final_url:
        try:
            pdf_resp = _get(pdf_url)
        except httpx.HTTPError:
            pdf_resp = None
        if pdf_resp is not None and _looks_like_pdf(pdf_resp):
            return _assess(_pdf_from_bytes(pdf_resp.content), str(pdf_resp.url))

    text = trafilatura.extract(resp.text, url=final_url) or ""
    return _assess(text.strip(), final_url)


def _pdf_source(source: str) -> FetchResult:
    """Extract text from a PDF given by local path or URL."""
    if source.startswith(("http://", "https://")):
        try:
            resp = _get(source)
        except httpx.HTTPError as exc:
            return _http_failure(exc, source)
        return _assess(_pdf_from_bytes(resp.content), str(resp.url))

    data = Path(source).read_bytes()
    return _assess(_pdf_from_bytes(data), source)


def _get(url: str) -> httpx.Response:
    """GET a URL following redirects, raising httpx.HTTPError on failure."""
    resp = httpx.get(
        url, headers=_HEADERS, follow_redirects=True, timeout=_HTTP_TIMEOUT
    )
    resp.raise_for_status()
    return resp


# Access-restriction statuses: the source exists, but we are not allowed to
# read it (auth / payment / paywall) — the human should supply the PDF.
_ACCESS_BLOCKED_STATUSES = frozenset({401, 402, 403, 451})


def _http_failure(exc: httpx.HTTPError, url: str) -> FetchResult:
    """Classify an HTTP failure as a paywall (need_pdf) vs. unreachable link."""
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in _ACCESS_BLOCKED_STATUSES
    ):
        return FetchResult(
            False,
            reason=f"access blocked (HTTP {exc.response.status_code}) — likely a "
            "publisher paywall; provide the PDF",
            code="need_pdf",
            source_url=url,
        )
    return FetchResult(
        False, reason=f"could not fetch {url}", code="fetch_error", source_url=url
    )


def _looks_like_pdf(resp: httpx.Response) -> bool:
    """True if the response is a PDF (by content-type or magic bytes)."""
    content_type = resp.headers.get("content-type", "").lower()
    return "pdf" in content_type or resp.content[:5] == b"%PDF-"


def _citation_pdf_url(html: str, base_url: str) -> str | None:
    """Return the Highwire ``citation_pdf_url`` full-text link, if present."""
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and meta.get("content"):
        return httpx.URL(base_url).join(meta["content"]).__str__()
    return None


def _pdf_from_bytes(data: bytes) -> str:
    """Extract concatenated page text from PDF bytes."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc).strip()


def _assess(text: str, source_url: str) -> FetchResult:
    """Gate extracted text on length and paper-like structure."""
    if len(text) < _MIN_TEXT_LEN:
        return FetchResult(
            False,
            reason="too little text — likely a paywall/landing stub or a PDF "
            "with no text layer; provide the PDF",
            code="too_short",
            source_url=source_url,
        )

    lowered = text.lower()
    markers = sum(marker in lowered for marker in _ACADEMIC_MARKERS)
    if markers < _MIN_MARKERS:
        return FetchResult(
            False,
            reason="content lacks academic structure — does not look like a paper",
            code="not_a_paper",
            source_url=source_url,
        )

    return FetchResult(True, text=text, source_url=source_url)
