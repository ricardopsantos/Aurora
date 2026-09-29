"""web_search (DuckDuckGo via ddgs, no API key) and web_fetch (httpx + crude
html→text). Both read-only — no approval (R6).

R157: this was `aurora/websearch.py`, wired into the engine by name. It ships
with Aurora exactly as before — living in `aurora/extensions_bundled/` is the
only thing that makes it "built in" rather than user-installed, same as the
MCP extension — but it is no longer special-cased anywhere in `tools.py`.
Nothing in the engine depended on it beyond the `runtime.web_search` toggle,
which `register()` below now honours; that made it the one built-in tool
module that was already extension-shaped.

Dynamic `register(engine)` rather than a static `SPEC`/`RUNNERS` pair
precisely because of that toggle — a static extension loads unconditionally,
which would turn `web_search: false` into a no-op.
"""

import re

import httpx

_SPEC = [
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


def _strip_script_style(html: str) -> str:
    """R282: drop <script>/<style> blocks in ONE forward pass.

    Was `re.sub(r"<(script|style)[\\s\\S]*?</\\1>", ...)`: on an opener
    with no closer, the lazy scan runs to the end of the text, and it is
    retried from every later opener — quadratic. Measured: 224KB of
    unclosed `<script` took 33.7s, against a 2MB fetch cap, on an ungated
    tool Esc could not interrupt. Here every search starts where the last
    one ended. An unclosed block swallows the rest of the page, which is
    what a browser does with it too."""
    low = html.lower()
    out, pos = [], 0
    while True:
        i_s, i_t = low.find("<script", pos), low.find("<style", pos)
        starts = [i for i in (i_s, i_t) if i != -1]
        if not starts:
            out.append(html[pos:])
            break
        i = min(starts)
        out.append(html[pos:i])
        tag = "script" if i == i_s else "style"
        close = low.find(f"</{tag}", i)
        if close == -1:
            break                         # unclosed: the rest is script/style
        end = low.find(">", close)
        pos = len(html) if end == -1 else end + 1
    return "".join(out)


_MAX_REDIRECTS = 5


def _peer_is_private(r) -> bool:
    import ipaddress
    try:
        stream = r.extensions.get("network_stream")
        addr = stream.get_extra_info("server_addr") if stream is not None else None
        ip = ipaddress.ip_address(addr[0].split("%")[0]) if addr else None
    except Exception:
        return False
    return ip is not None and not ip.is_global


def web_fetch(url: str, **_) -> str:
    from urllib.parse import urljoin, urlsplit

    from aurora import tools
    # R283: redirects are followed HERE, one hop at a time, so a public URL
    # (no approval asked) cannot bounce the request to a private host —
    # 169.254.169.254, a LAN admin page. A URL that was itself gated (the
    # user approved a private/query fetch) may redirect anywhere.
    # R301: only a URL whose OWN target is private counts as "approved to
    # reach a private host". It used to be "any gated URL", so approving
    # a harmless `https://example.com/search?q=x` let its 302 go to
    # 169.254.169.254 or a LAN admin page.
    approved_private = tools._private_host(urlsplit(url).hostname or "")
    try:
        for _hop in range(_MAX_REDIRECTS + 1):
            # stream with a byte cap — a plain .get() would download an
            # arbitrarily large body into memory before we ever truncate
            with httpx.stream("GET", url, timeout=20, follow_redirects=False,
                              headers={"User-Agent": "Aurora/0.1"}) as r:
                # R301: DNS rebinding — the name may have resolved public for
                # the check and private for the connect. Refuse to read
                # anything from a private peer the user didn't approve.
                if not approved_private and _peer_is_private(r):
                    return ("[web_fetch blocked: the server resolved to a "
                            "private/local address — fetch it directly to get "
                            "an approval prompt]")
                if getattr(r, "is_redirect", False):
                    nxt = urljoin(url, r.headers.get("location", ""))
                    if not approved_private and \
                            tools._private_host(urlsplit(nxt).hostname or ""):
                        return ("[web_fetch blocked: redirect to a private/local "
                                f"address ({urlsplit(nxt).hostname}) — fetch that "
                                "URL directly to get an approval prompt]")
                    url = nxt
                    continue
                r.raise_for_status()
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) >= _FETCH_CAP:
                        break
                break
        else:
            return f"[web_fetch error: more than {_MAX_REDIRECTS} redirects]"
        html = bytes(buf[:_FETCH_CAP]).decode(
            r.encoding or "utf-8", errors="replace")
    except Exception as e:
        return f"[web_fetch error: {e}]"
    html = _strip_script_style(html)
    text = _TAG.sub("", html)
    text = _WS.sub("\n\n", text).strip()
    return text[:20_000] + ("\n[truncated]" if len(text) > 20_000 else "")


_RUNNERS = {"web_search": web_search, "web_fetch": web_fetch}


def register(engine):
    """Honours `runtime.web_search` (engine.web). Off → contribute nothing,
    so the model never sees a tool it isn't allowed to call — which is what
    the flag meant when `tools.specs(include_web)` gated it engine-side."""
    if not getattr(engine, "web", True):
        return [], {}
    return _SPEC, _RUNNERS
