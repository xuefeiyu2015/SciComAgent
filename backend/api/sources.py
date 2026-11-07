"""Background path — external search clients. Pure code, no AI (like fetch.py).

One registry of small search clients, each `(query, limit) -> list[Hit]`:

    arxiv            arxiv.org API (vendored `arxiv` package)   keyless
    semantic_scholar Semantic Scholar Graph API                 keyless (S2_API_KEY boosts rate limit)
    pubmed           NCBI E-utilities esearch + esummary        keyless (NCBI_API_KEY boosts rate limit)
    crossref         api.crossref.org/works                     keyless
    wikipedia        Wikipedia REST search                      keyless
    ddgs             DuckDuckGo via the `ddgs` package          keyless
    tavily           api.tavily.com                             requires TAVILY_API_KEY (byo_key, env only)

Which clients run is config-driven: `search.sources` in config/config.yaml
(env: SEARCH_SOURCES, comma-separated) — see config.example.yaml. Results are
BACKGROUND for the drafter (framing only, never facts), so this module is
deliberately forgiving: a failing client contributes nothing rather than
raising, and `search_all` never raises past itself.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import arxiv
import httpx
from ddgs import DDGS

from api.config_loader import resolve_setting
from api.schema import SourceKind

_HTTP_TIMEOUT = 15.0
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; SciCommAgent/0.1; +https://turingplanet.example)"
    )
}

# Keyless clients enabled when config/env say nothing (mirror config.example.yaml).
DEFAULT_SOURCES = "arxiv,semantic_scholar,pubmed,crossref,wikipedia,ddgs"

# Keep snippets short — they are search-result context, not source text.
_MAX_SNIPPET = 500

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Hit:
    """One raw search result offered to the background sub-agent."""

    title: str
    url: str
    snippet: str
    kind: SourceKind


def search_all(queries: list[str], max_per_source: int = 3) -> list[Hit]:
    """Run every enabled client for every query; never raises.

    Clients run concurrently (they are all network-bound). A failing client
    or query contributes `[]`. Hits are deduplicated by normalized URL,
    preserving (query, registry) order.
    """
    names = [n for n in enabled_sources() if n in _CLIENTS]
    if not queries or not names:
        return []

    jobs = [(name, query) for query in queries for name in names]
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        batches = pool.map(
            lambda job: _search_one(job[0], job[1], max_per_source), jobs
        )

    hits: list[Hit] = []
    seen: set[str] = set()
    for batch in batches:
        for hit in batch:
            key = hit.url.strip().rstrip("/").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            hits.append(hit)
    return hits


def enabled_sources() -> list[str]:
    """Client names to run, from config/env (see module docstring)."""
    raw = resolve_setting(("search", "sources"), "SEARCH_SOURCES", DEFAULT_SOURCES)
    return [name.strip().lower() for name in raw.split(",") if name.strip()]


def _search_one(name: str, query: str, limit: int) -> list[Hit]:
    """One client, one query — any failure means no hits, never an error."""
    try:
        return _CLIENTS[name](query, limit)
    except Exception:
        return []


# --- clients ------------------------------------------------------------------

def _arxiv_search(query: str, limit: int) -> list[Hit]:
    search = arxiv.Search(query=query, max_results=limit)
    return [
        _hit(r.title, r.entry_id, r.summary, SourceKind.arxiv)
        for r in arxiv.Client().results(search)
    ]


def _semantic_scholar_search(query: str, limit: int) -> list[Hit]:
    headers = {}
    if key := os.environ.get("S2_API_KEY"):  # optional rate-limit boost
        headers["x-api-key"] = key
    data = _get_json(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={
            "query": query,
            "limit": limit,
            "fields": "title,abstract,url,year",
        },
        headers=headers,
    )
    return [
        _hit(
            paper.get("title", ""),
            paper.get("url") or "",
            paper.get("abstract") or "",
            SourceKind.semantic_scholar,
        )
        for paper in data.get("data") or []
    ]


def _pubmed_search(query: str, limit: int) -> list[Hit]:
    eutils = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    extra = {}
    if key := os.environ.get("NCBI_API_KEY"):  # optional rate-limit boost
        extra["api_key"] = key
    found = _get_json(
        f"{eutils}/esearch.fcgi",
        params={"db": "pubmed", "term": query, "retmode": "json", "retmax": limit}
        | extra,
    )
    ids = found.get("esearchresult", {}).get("idlist") or []
    if not ids:
        return []
    summaries = _get_json(
        f"{eutils}/esummary.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"} | extra,
    ).get("result", {})
    hits = []
    for pmid in ids:
        entry = summaries.get(pmid) or {}
        blurb = ", ".join(
            part
            for part in (entry.get("fulljournalname", ""), entry.get("pubdate", ""))
            if part
        )
        hits.append(
            _hit(
                entry.get("title", ""),
                f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                blurb,
                SourceKind.pubmed,
            )
        )
    return hits


def _crossref_search(query: str, limit: int) -> list[Hit]:
    data = _get_json(
        "https://api.crossref.org/works",
        params={
            "query": query,
            "rows": limit,
            "select": "title,abstract,URL,DOI",
        },
    )
    hits = []
    for work in data.get("message", {}).get("items") or []:
        titles = work.get("title") or [""]
        hits.append(
            _hit(titles[0], work.get("URL", ""), work.get("abstract", ""), SourceKind.crossref)
        )
    return hits


def _wikipedia_search(query: str, limit: int) -> list[Hit]:
    data = _get_json(
        "https://en.wikipedia.org/w/rest.php/v1/search/page",
        params={"q": query, "limit": limit},
    )
    return [
        _hit(
            page.get("title", ""),
            f"https://en.wikipedia.org/wiki/{page.get('key', '')}",
            page.get("description") or page.get("excerpt") or "",
            SourceKind.web,
        )
        for page in data.get("pages") or []
    ]


def _ddgs_search(query: str, limit: int) -> list[Hit]:
    results = DDGS().text(query, max_results=limit) or []
    return [
        _hit(r.get("title", ""), r.get("href", ""), r.get("body", ""), SourceKind.web)
        for r in results
    ]


def _tavily_search(query: str, limit: int) -> list[Hit]:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:  # opt-in client: no key -> quietly no hits (byo_key)
        return []
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "max_results": limit},
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        _hit(r.get("title", ""), r.get("url", ""), r.get("content", ""), SourceKind.web)
        for r in resp.json().get("results") or []
    ]


_CLIENTS: dict[str, Callable[[str, int], list[Hit]]] = {
    "arxiv": _arxiv_search,
    "semantic_scholar": _semantic_scholar_search,
    "pubmed": _pubmed_search,
    "crossref": _crossref_search,
    "wikipedia": _wikipedia_search,
    "ddgs": _ddgs_search,
    "tavily": _tavily_search,
}


# --- shared helpers -----------------------------------------------------------

def _get_json(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    """GET a JSON endpoint; raises httpx.HTTPError (callers absorb via _search_one)."""
    resp = httpx.get(
        url,
        params=params,
        headers=_HEADERS | (headers or {}),
        follow_redirects=True,
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _hit(title: str, url: str, snippet: str, kind: SourceKind) -> Hit:
    """Build a Hit with whitespace/markup-cleaned, length-capped text."""
    return Hit(
        title=_clean(title),
        url=str(url).strip(),
        snippet=_clean(snippet)[:_MAX_SNIPPET],
        kind=kind,
    )


def _clean(text: str) -> str:
    """Strip markup tags (JATS/HTML in abstracts) and collapse whitespace."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", str(text))).strip()
