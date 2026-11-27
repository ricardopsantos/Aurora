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


_WS = re.compile(r"\n\s*\n\s*\n+")


_FETCH_CAP = 2_000_000  # bytes — a page this big is never useful past the cap


from aurora.htmltext import _flatten, _strip_blocks, _strip_comments  # noqa: E402,F401


def _strip_script_style(html: str) -> str:
    return _strip_blocks(html, ("script", "style"))


# R338: page furniture that is not the content
_FURNITURE = ("script", "style", "nav", "footer", "aside", "form", "noscript",
              "svg", "template", "iframe")
def _main_region(html: str) -> str:
    """The first <main> or <article> element, when the page has one."""
    low = html.lower()
    for tag in ("main", "article"):
        i = low.find(f"<{tag}")
        if i == -1:
            continue
        j = low.find(">", i)
        k = low.find(f"</{tag}", j)
        if j != -1 and k != -1 and k - j > 200:
            return html[j + 1:k]
    return html


def html_to_text(html: str) -> str:
    """R338: readable text — navigation, footers, forms and scripts dropped,
    the <main>/<article> body preferred, block tags kept as line breaks, list
    items as `- `, entities decoded."""
    import html as htmllib
    html = _strip_blocks(_strip_comments(html), _FURNITURE)
    html = _main_region(html)
    text = htmllib.unescape(_flatten(html))
    text = "\n".join(re.sub(r"[ \t\r\f\v]+", " ", ln).strip()
                     for ln in text.split("\n"))
    return _WS.sub("\n\n", text).strip()


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


def _proxied(url: str) -> bool:
    """R512: whether httpx would send this URL through a `*_proxy` from the environment (then the peer we see is the
    proxy, not the server, and the peer check below says nothing about the target)."""
    import urllib.request
    from urllib.parse import urlsplit
    u = urlsplit(url)
    host = u.hostname or ""
    proxies = urllib.request.getproxies()
    if not (proxies.get(u.scheme) or proxies.get("all")):
        return False
    try:
        return not urllib.request.proxy_bypass(host)
    except Exception:
        return True


def _resolves_private(url: str) -> bool:
    """R512: the target's name resolved BEFORE the request is sent, so a public-looking name that points at a
    private address never receives the GET (the old check read the peer after the request had gone out)."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").strip("[]")
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False                   # unresolvable: the fetch itself fails
    for ai in infos:
        try:
            if not ipaddress.ip_address(ai[4][0].split("%")[0]).is_global:
                return True
        except ValueError:
            return True
    return False


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
    # R320: decided WITHOUT DNS, exactly as the approval gate decided it
    # (`url_needs_approval`). With DNS here, a host the gate did not recognise
    # as private was still treated as "user approved a private target", which
    # also switched off the rebinding and redirect guards below.
    approved_private = tools._private_host(urlsplit(url).hostname or "",
                                           resolve=False)
    try:
        for _hop in range(_MAX_REDIRECTS + 1):
            if not approved_private and _resolves_private(url):
                return ("[web_fetch blocked: the server resolves to a private/local address — fetch it by that "
                        "address to get an approval prompt]")
            via_proxy = _proxied(url)
            # stream with a byte cap — a plain .get() would download an
            # arbitrarily large body into memory before we ever truncate
            with httpx.stream("GET", url, timeout=20, follow_redirects=False,
                              headers={"User-Agent": "Aurora/0.1"}) as r:
                # R301: DNS rebinding — the name may have resolved public for
                # the check and private for the connect. Refuse to read
                # anything from a private peer the user didn't approve.
                if not approved_private and not via_proxy and _peer_is_private(r):
                    return ("[web_fetch blocked: the server resolved to a "
                            "private/local address]")
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
    ctype = (getattr(r, "headers", None) or {}).get("content-type", "") or ""
    ctype = ctype.split(";")[0].strip().lower()
    if ctype == "application/pdf" or buf[:5] == b"%PDF-":
        return ("[web_fetch: this URL is a PDF — download it and read it with "
                "a PDF tool; Aurora cannot extract PDF text itself]")
    text = html_to_text(html) if "html" in ctype or not ctype or "<" in html[:2000] \
        else html.strip()
    return text[:20_000] + ("\n[truncated]" if len(text) > 20_000 else "")


_RUNNERS = {"web_search": web_search, "web_fetch": web_fetch}


def register(engine):
    """Honours `runtime.web_search` (engine.web). Off → contribute nothing,
    so the model never sees a tool it isn't allowed to call — which is what
    the flag meant when `tools.specs(include_web)` gated it engine-side."""
    if not getattr(engine, "web", True):
        return [], {}
    return _SPEC, _RUNNERS
