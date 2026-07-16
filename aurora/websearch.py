"""web_search (DuckDuckGo via ddgs, no API key) and web_fetch (httpx + crude
html→text). Both read-only — no approval (R6)."""

import re

import httpx

SPEC = [
    {"name": "web_search", "description": "Search the web; returns top result titles, URLs, snippets.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "max_results": {"type": "integer", "description": "default 5"}},
         "required": ["query"]}},
    {"name": "web_fetch", "description": "Fetch a URL and return its readable text.",
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string"}}, "required": ["url"]}},
]


def web_search(query: str, max_results: int = 5, **_) -> str:
    try:
        from ddgs import DDGS
    except ImportError:
        return "[web_search unavailable: ddgs not installed]"
    try:
        with DDGS() as d:
            hits = list(d.text(query, max_results=max_results))
    except Exception as e:
        return f"[web_search error: {e}]"
    if not hits:
        return "[no results]"
    return "\n\n".join(
        f"{h.get('title', '')}\n{h.get('href', '')}\n{h.get('body', '')}" for h in hits)


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n\s*\n\s*\n+")


_FETCH_CAP = 2_000_000  # bytes — a page this big is never useful past the cap


def web_fetch(url: str, **_) -> str:
    try:
        # stream with a byte cap — a plain .get() would download an
        # arbitrarily large body into memory before we ever truncate
        with httpx.stream("GET", url, timeout=20, follow_redirects=True,
                          headers={"User-Agent": "Aurora/0.1"}) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) >= _FETCH_CAP:
                    break
        html = bytes(buf[:_FETCH_CAP]).decode(
            r.encoding or "utf-8", errors="replace")
    except Exception as e:
        return f"[web_fetch error: {e}]"
    html = re.sub(r"<(script|style)[\s\S]*?</\1>", "", html, flags=re.I)
    text = _TAG.sub("", html)
    text = _WS.sub("\n\n", text).strip()
    return text[:20_000] + ("\n[truncated]" if len(text) > 20_000 else "")


RUNNERS = {"web_search": web_search, "web_fetch": web_fetch}
