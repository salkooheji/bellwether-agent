"""Public information search tool, backed by Tavily.

Results are truncated snippets with URLs, because the memo layer
requires every claim to carry a named source, and the URL is the name.
"""

from __future__ import annotations

from tavily import TavilyClient

SNIPPET_CHARS = 400


def search_news(client: TavilyClient, query: str, max_results: int = 5) -> dict:
    """Web search for public information; returns titles, URLs, snippets."""
    try:
        if not query or not query.strip():
            return {"ok": False, "error": "empty search query"}
        resp = client.search(query=query.strip(), max_results=max_results)
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": (r.get("content") or "")[:SNIPPET_CHARS],
            }
            for r in resp.get("results", [])
        ]
        if not results:
            return {"ok": False,
                    "error": f"no search results for '{query}'; try different "
                             "or broader keywords, such as the company name "
                             "plus the quarter"}
        return {"ok": True, "query": query, "results": results}
    except Exception as e:
        return {"ok": False, "error": f"news search failed: {e}"}
