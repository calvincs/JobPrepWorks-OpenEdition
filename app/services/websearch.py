"""Standalone web search, for providers that can't search on their own.

Anthropic, OpenAI, and OpenRouter can run searches server-side as part of a
single model call. A local model behind Ollama or llama.cpp cannot — so if you
want Company Pulse to work there, the *app* has to do the searching and hand
the model what it found. That's this module.

Three backends, picked by ``WEB_SEARCH`` (or auto-detected from whichever key
you set):

- **tavily** — a search API built for LLM use; returns clean snippets and is
  the least work to set up (one key, generous free tier).
- **brave** — Brave's independent index; one key, a real web result shape.
- **searxng** — any SearXNG instance with the JSON format enabled, including
  one you run yourself. Nothing leaves your machines except the searches
  SearXNG forwards, which is the most private option here.

All three are normalized to :class:`SearchResult`. Failures raise
:class:`SearchError` with a message that names the backend, because "research
failed" without saying *which* piece failed is the least useful error there is.
"""

import logging
from dataclasses import dataclass

import httpx

from app.config import search_backend_name, settings

log = logging.getLogger(__name__)

TIMEOUT_S = 20.0
# Snippets longer than this are noise: the model gets many of them, and a
# single verbose result would crowd out the others.
MAX_SNIPPET_CHARS = 800


class SearchError(RuntimeError):
    """A search backend was unreachable or rejected the request."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    outlet: str = ""

    def as_prompt_block(self, index: int) -> str:
        head = f"[{index}] {self.title}".strip()
        parts = [head, f"    url: {self.url}"]
        if self.outlet:
            parts.append(f"    outlet: {self.outlet}")
        if self.snippet:
            parts.append(f"    {self.snippet[:MAX_SNIPPET_CHARS]}")
        return "\n".join(parts)


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").removeprefix("www.")


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _tavily(query: str, max_results: int) -> list[SearchResult]:
    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            json={
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": False,
            },
            headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
            timeout=TIMEOUT_S,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        raise SearchError(
            f"Tavily returned {exc.response.status_code} — check TAVILY_API_KEY."
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchError("Could not reach Tavily.") from exc
    return [
        SearchResult(
            title=_clean(r.get("title")),
            url=str(r.get("url", "")),
            snippet=_clean(r.get("content")),
            outlet=_host(str(r.get("url", ""))),
        )
        for r in data.get("results", [])
        if r.get("url")
    ]


def _brave(query: str, max_results: int) -> list[SearchResult]:
    try:
        response = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": settings.brave_api_key or "",
            },
            timeout=TIMEOUT_S,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        raise SearchError(
            f"Brave Search returned {exc.response.status_code} — check BRAVE_API_KEY."
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchError("Could not reach Brave Search.") from exc
    return [
        SearchResult(
            title=_clean(r.get("title")),
            url=str(r.get("url", "")),
            snippet=_clean(r.get("description")),
            outlet=_clean(r.get("profile", {}).get("name")) or _host(str(r.get("url", ""))),
        )
        for r in (data.get("web") or {}).get("results", [])
        if r.get("url")
    ]


def _searxng(query: str, max_results: int) -> list[SearchResult]:
    try:
        response = httpx.get(
            f"{settings.searxng_url}/search",
            params={"q": query, "format": "json"},
            timeout=TIMEOUT_S,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        detail = (
            " (that instance may not allow the JSON format — enable it in "
            "settings.yml under search.formats)"
            if exc.response.status_code in (403, 404)
            else ""
        )
        raise SearchError(
            f"SearXNG returned {exc.response.status_code}{detail}."
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchError(f"Could not reach SearXNG at {settings.searxng_url}.") from exc
    return [
        SearchResult(
            title=_clean(r.get("title")),
            url=str(r.get("url", "")),
            snippet=_clean(r.get("content")),
            outlet=_clean(r.get("engine")) or _host(str(r.get("url", ""))),
        )
        for r in data.get("results", [])[:max_results]
        if r.get("url")
    ]


_BACKENDS = {"tavily": _tavily, "brave": _brave, "searxng": _searxng}


def available() -> bool:
    """Whether a standalone search backend is configured and usable."""
    return search_backend_name() in _BACKENDS


def backend() -> str:
    return search_backend_name()


def search(query: str, max_results: int = 8) -> list[SearchResult]:
    """Run one query against the configured backend. Raises SearchError rather
    than returning [] on failure: an empty result set and a broken API key
    should not look the same to the caller."""
    name = search_backend_name()
    fn = _BACKENDS.get(name)
    if fn is None:
        raise SearchError(
            "No web search backend is configured — set TAVILY_API_KEY, "
            "BRAVE_API_KEY, or SEARXNG_URL."
        )
    results = fn(query, max_results)
    log.info("web search [%s] %r → %d results", name, query, len(results))
    return results


def search_many(queries: list[str], per_query: int = 5) -> list[SearchResult]:
    """Run several queries and merge, de-duplicating by URL and preserving
    order. One backend failure aborts (the key is wrong, or the host is down —
    the next query would fail identically); a query that simply finds nothing
    is fine and the others still count."""
    seen: set[str] = set()
    merged: list[SearchResult] = []
    for query in queries:
        for result in search(query, per_query):
            if result.url in seen:
                continue
            seen.add(result.url)
            merged.append(result)
    return merged
