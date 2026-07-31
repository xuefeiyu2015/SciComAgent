"""Tests for api.sources — all network calls stubbed, no keys.

Verifies per-client response parsing (canned JSON payloads), the enabled-set
dispatch from config/env, the never-raise contract of search_all, URL dedup,
and the tavily no-key skip.
"""

from __future__ import annotations

from api import sources
from api.schema import SourceKind
from api.sources import Hit, _clean, enabled_sources, search_all


def _no_env(monkeypatch, *names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


# --- enabled-set dispatch -----------------------------------------------------

def test_enabled_sources_default(monkeypatch):
    _no_env(monkeypatch, "SEARCH_SOURCES")
    monkeypatch.setattr(
        sources,
        "resolve_setting",
        lambda path, env, default="": default,
    )
    assert enabled_sources() == [
        "arxiv", "semantic_scholar", "pubmed", "crossref", "wikipedia", "ddgs",
    ]


def test_enabled_sources_env_and_normalization(monkeypatch):
    monkeypatch.setattr(
        sources, "resolve_setting", lambda *a, **k: " ArXiv , pubmed ,, "
    )
    assert enabled_sources() == ["arxiv", "pubmed"]


def test_search_all_skips_unknown_source_names(monkeypatch):
    monkeypatch.setattr(sources, "enabled_sources", lambda: ["nonsense"])
    assert search_all(["q"]) == []


def test_search_all_empty_queries(monkeypatch):
    monkeypatch.setattr(sources, "enabled_sources", lambda: ["arxiv"])
    assert search_all([]) == []


# --- never-raise + dedup ------------------------------------------------------

def test_search_all_absorbs_client_errors_and_dedups(monkeypatch):
    def boom(query, limit):
        raise RuntimeError("network down")

    def ok(query, limit):
        return [
            Hit("A", "https://x.org/a/", "s", SourceKind.web),
            Hit("A again", "https://X.org/a", "s", SourceKind.web),  # dup URL
            Hit("no url", "", "s", SourceKind.web),  # dropped
        ]

    monkeypatch.setattr(sources, "enabled_sources", lambda: ["bad", "good"])
    monkeypatch.setitem(sources._CLIENTS, "bad", boom)
    monkeypatch.setitem(sources._CLIENTS, "good", ok)

    hits = search_all(["q1", "q2"])
    assert [h.title for h in hits] == ["A"]  # dedup across queries too


# --- per-client parsing (canned payloads) --------------------------------------

def test_semantic_scholar_parsing(monkeypatch):
    _no_env(monkeypatch, "S2_API_KEY")
    payload = {"data": [
        {"title": "Paper <b>one</b>", "url": "https://s2.org/p1", "abstract": "About  stuff."},
        {"title": "No url", "url": None, "abstract": None},
    ]}
    monkeypatch.setattr(sources, "_get_json", lambda *a, **k: payload)

    hits = sources._semantic_scholar_search("q", 2)
    assert hits[0] == Hit("Paper one", "https://s2.org/p1", "About stuff.", SourceKind.semantic_scholar)
    assert hits[1].url == ""  # dropped later by search_all's dedup key


def test_pubmed_parsing_two_stage(monkeypatch):
    _no_env(monkeypatch, "NCBI_API_KEY")
    calls = []

    def fake_get_json(url, params=None, headers=None):
        calls.append(url)
        if "esearch" in url:
            return {"esearchresult": {"idlist": ["11", "22"]}}
        return {"result": {
            "11": {"title": "T11", "fulljournalname": "Nature", "pubdate": "2024"},
            "22": {"title": "T22"},
        }}

    monkeypatch.setattr(sources, "_get_json", fake_get_json)
    hits = sources._pubmed_search("q", 2)

    assert len(calls) == 2  # esearch then esummary
    assert hits[0].url == "https://pubmed.ncbi.nlm.nih.gov/11/"
    assert hits[0].snippet == "Nature, 2024"
    assert hits[1].title == "T22"
    assert all(h.kind is SourceKind.pubmed for h in hits)


def test_pubmed_no_ids_short_circuits(monkeypatch):
    monkeypatch.setattr(
        sources, "_get_json", lambda *a, **k: {"esearchresult": {"idlist": []}}
    )
    assert sources._pubmed_search("q", 3) == []


def test_crossref_parsing_strips_jats(monkeypatch):
    payload = {"message": {"items": [
        {"title": ["Work one"], "URL": "https://doi.org/10.1/x",
         "abstract": "<jats:p>Cells divide.</jats:p>"},
        {"URL": "https://doi.org/10.1/y"},  # no title/abstract
    ]}}
    monkeypatch.setattr(sources, "_get_json", lambda *a, **k: payload)

    hits = sources._crossref_search("q", 2)
    assert hits[0].snippet == "Cells divide."
    assert hits[1] == Hit("", "https://doi.org/10.1/y", "", SourceKind.crossref)


def test_wikipedia_parsing(monkeypatch):
    payload = {"pages": [
        {"title": "Attention", "key": "Attention_(machine_learning)",
         "description": "ML technique"},
    ]}
    monkeypatch.setattr(sources, "_get_json", lambda *a, **k: payload)

    hits = sources._wikipedia_search("q", 1)
    assert hits == [Hit(
        "Attention",
        "https://en.wikipedia.org/wiki/Attention_(machine_learning)",
        "ML technique",
        SourceKind.web,
    )]


def test_arxiv_parsing(monkeypatch):
    class _Result:
        title = "Attention Is All You Need"
        entry_id = "http://arxiv.org/abs/1706.03762v7"
        summary = "The dominant sequence transduction models..."

    class _Client:
        def results(self, search):
            return [_Result()]

    monkeypatch.setattr(sources.arxiv, "Client", _Client)
    monkeypatch.setattr(sources.arxiv, "Search", lambda query, max_results: None)

    hits = sources._arxiv_search("q", 1)
    assert hits[0].kind is SourceKind.arxiv
    assert hits[0].url == "http://arxiv.org/abs/1706.03762v7"


def test_ddgs_parsing(monkeypatch):
    class _DDGS:
        def text(self, query, max_results):
            return [{"title": "T", "href": "https://w.org", "body": "B"}]

    monkeypatch.setattr(sources, "DDGS", _DDGS)
    hits = sources._ddgs_search("q", 1)
    assert hits == [Hit("T", "https://w.org", "B", SourceKind.web)]


def test_tavily_without_key_returns_empty(monkeypatch):
    _no_env(monkeypatch, "TAVILY_API_KEY")
    called = []
    monkeypatch.setattr(sources.httpx, "post", lambda *a, **k: called.append(a))

    assert sources._tavily_search("q", 3) == []
    assert called == []  # no request without a key


# --- helpers -------------------------------------------------------------------

def test_clean_and_snippet_cap():
    assert _clean("  a <b>b</b>\n\n c ") == "a b c"
    hit = sources._hit("t", "u", "x" * 1000, SourceKind.web)
    assert len(hit.snippet) == 500
