"""The standalone web-search backends (services/websearch.py).

These are what make Company Pulse work with a local model, so the normalization
into SearchResult and the error messages are the contract. No network: httpx is
stubbed at the module boundary.
"""

import httpx
import pytest

from app import config
from app.config import Settings
from app.services import websearch
from app.services.websearch import SearchError, SearchResult


def _stub(monkeypatch, method, payload, status=200):
    class _Resp:
        status_code = status

        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError(
                    "boom", request=httpx.Request("GET", "https://x"),
                    response=httpx.Response(status, request=httpx.Request("GET", "https://x")),
                )

        def json(self):
            return payload

    monkeypatch.setattr(httpx, method, lambda *a, **k: _Resp())


def _settings(monkeypatch, **overrides):
    monkeypatch.setattr(config, "settings", Settings(**overrides))
    monkeypatch.setattr(websearch, "settings", config.settings)


def test_tavily_results_are_normalized(monkeypatch):
    _settings(monkeypatch, web_search="tavily", tavily_api_key="tvly-x")
    _stub(monkeypatch, "post", {"results": [
        {"title": "Acme reviews", "url": "https://glassdoor.com/acme",
         "content": "Employees praise autonomy."},
    ]})
    [result] = websearch.search("acme reviews")
    assert result.title == "Acme reviews"
    assert result.url == "https://glassdoor.com/acme"
    assert result.outlet == "glassdoor.com"  # derived from the host


def test_brave_results_are_normalized(monkeypatch):
    _settings(monkeypatch, web_search="brave", brave_api_key="brv-x")
    _stub(monkeypatch, "get", {"web": {"results": [
        {"title": "Acme news", "url": "https://news.example/acme",
         "description": "Acme expands.", "profile": {"name": "Example News"}},
    ]}})
    [result] = websearch.search("acme news")
    assert result.snippet == "Acme expands."
    assert result.outlet == "Example News"


def test_searxng_results_are_normalized(monkeypatch):
    _settings(monkeypatch, web_search="searxng", searxng_url="http://localhost:8888")
    _stub(monkeypatch, "get", {"results": [
        {"title": "Acme", "url": "https://acme.example", "content": "About Acme."},
    ]})
    [result] = websearch.search("acme")
    assert result.url == "https://acme.example"


def test_a_bad_key_says_which_key(monkeypatch):
    _settings(monkeypatch, web_search="tavily", tavily_api_key="wrong")
    _stub(monkeypatch, "post", {}, status=401)
    with pytest.raises(SearchError, match="TAVILY_API_KEY"):
        websearch.search("acme")


def test_searxng_403_explains_the_json_format(monkeypatch):
    """The single most common SearXNG setup mistake gets a specific message."""
    _settings(monkeypatch, web_search="searxng", searxng_url="http://localhost:8888")
    _stub(monkeypatch, "get", {}, status=403)
    with pytest.raises(SearchError, match="JSON format"):
        websearch.search("acme")


def test_unreachable_host_is_a_search_error(monkeypatch):
    _settings(monkeypatch, web_search="searxng", searxng_url="http://localhost:8888")

    def boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(SearchError, match="Could not reach"):
        websearch.search("acme")


def test_no_backend_configured_lists_the_options(monkeypatch):
    _settings(monkeypatch, llm_provider="ollama", web_search="auto")
    assert not websearch.available()
    with pytest.raises(SearchError, match="TAVILY_API_KEY"):
        websearch.search("acme")


def test_search_many_dedupes_by_url_and_preserves_order(monkeypatch):
    _settings(monkeypatch, web_search="tavily", tavily_api_key="k")
    pages = {
        "a": [SearchResult("A", "https://x/1", "s"), SearchResult("B", "https://x/2", "s")],
        "b": [SearchResult("B again", "https://x/2", "s"), SearchResult("C", "https://x/3", "s")],
    }
    monkeypatch.setattr(websearch, "search", lambda q, n=5: pages[q])
    merged = websearch.search_many(["a", "b"])
    assert [r.url for r in merged] == ["https://x/1", "https://x/2", "https://x/3"]


def test_prompt_block_carries_the_citation_number_and_url():
    block = SearchResult("Title", "https://x/1", "Snippet", "x.com").as_prompt_block(3)
    assert block.startswith("[3] Title")
    assert "https://x/1" in block and "Snippet" in block


def test_long_snippets_are_truncated():
    """One verbose result must not crowd the others out of the prompt."""
    block = SearchResult("T", "https://x/1", "z" * 5000).as_prompt_block(1)
    assert len(block) < websearch.MAX_SNIPPET_CHARS + 200
