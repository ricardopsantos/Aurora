"""OpenAI-compatible servers: llama.cpp (--jinja), OpenRouter, LM Studio,
Ollama (R223), …. Streaming, tool calls, usage. Malformed tool-call
responses raise MalformedToolCall so agent.py can retry-then-degrade (R5)."""

import ipaddress
import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .base import (
    MalformedToolCall,
    Provider,
    ProviderError,
    ToolCall,
    TurnResult,
    cancellable_sse,
)
from .happy_eyeballs import HappyEyeballsTransport

_REMOTE_CONTEXT_LIMITS_PATH = Path(__file__).parent / "remote_context_limits.json"


class _RateLimited(Exception):
    """R99: internal control-flow only, never raised past `turn()` — a 429
    status remembered just long enough to retry the same request with
    backoff before it ever becomes a ProviderError the agent loop has to
    give up the turn on. A free-tier model's shared rate limit is routinely
    a few seconds of real waiting, not a fatal error; today's code prompted
    the user to just try again themselves. Deliberately its own exception
    (not reusing ProviderError) so the retry logic in `turn()` can tell a
    429 apart from a generic 4xx/5xx without parsing the message text."""

    def __init__(self, body: str, retry_after: float | None = None):
        super().__init__(body)
        self.retry_after = retry_after


# R99: exponential, not the connection-retry's flat 0.3*(attempt+1) — a
# shared free-tier limit clears on the order of seconds, a stale pooled
# connection resets instantly. One entry per retry (len == _ATTEMPTS - 1).
# Used only when the server doesn't send its own `Retry-After` (R125c).
_RATE_LIMIT_BACKOFF = (1.0, 3.0)
# A server-provided Retry-After is honoured up to this ceiling — long
# enough to respect a real cooldown, short enough that Aurora doesn't sit
# silently for minutes on a turn the user is watching.
_RATE_LIMIT_BACKOFF_CAP = 30.0

# R290: how long a probe failure keeps an endpoint out of rotation.
_DEAD_URL_BACKOFF_S = 120.0

# same cadence cancellable_sse polls at — short enough that Esc feels
# immediate, long enough not to spin
_CANCEL_POLL_S = 0.15


def _sleep_unless_cancelled(wait: float, cancel) -> bool:
    """Sleep `wait` seconds in short hops, returning True as soon as `cancel()`
    goes true (R147). A rate-limit backoff can legitimately be 30 seconds; a
    single `time.sleep(30)` makes the app unresponsive to Esc for all of it."""
    import time
    deadline = time.monotonic() + wait
    while True:
        if cancel():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_CANCEL_POLL_S, remaining))


def _parse_retry_after(headers) -> float | None:
    """`Retry-After` is either a delay in seconds or an HTTP-date (RFC
    9110 §10.2.3) — only the seconds form is worth honouring here; an
    HTTP-date is rare from these providers and parsing it adds a timezone
    edge case for little benefit, so it's treated as absent."""
    raw = headers.get("retry-after") if headers else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _load_remote_context_limits() -> dict[str, dict]:
    """Known per-model info for remote (non-"local") models, editable
    without a code change. The JSON file is a LIST of model entries (so it
    reads naturally and stays
    diff-friendly to append to); each entry is a dict (not a bare int) so
    future params (pricing, aliases, …) can land here without another
    schema change. Indexed here by "model" for an O(1) lookup. Only
    consulted for a model that isn't the "local" sentinel (see
    context_limit()); anything not listed here falls back to config.yaml's
    provider-level `context_limit` (or 128k)."""
    try:
        entries = json.loads(_REMOTE_CONTEXT_LIMITS_PATH.read_text(encoding="utf-8"))
        return {e["model"]: e for e in entries}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}


REMOTE_CONTEXT_LIMITS = _load_remote_context_limits()


# R192: what a cache read costs when the catalog didn't say — a fraction OF
# the input rate, not an absolute $. 0.1 is the rate every provider Aurora
# talks to actually charges (Moonshot, OpenAI, Anthropic all bill cache reads
# at 10% of input), and it is the RIGHT direction to be wrong in: assuming no
# discount is what produced the 4x overstatement this constant exists to fix.
_CACHE_READ_FALLBACK = 0.1


def _mtok(v):
    try:
        return round(float(v) * 1_000_000, 3)
    except (TypeError, ValueError):
        return None


def _model_info_from_catalog_entry(m: dict) -> dict:
    pricing = m.get("pricing") or {}
    return {"context_size": m.get("context_length"),
            "price_in_per_mtok": _mtok(pricing.get("prompt")),
            "price_out_per_mtok": _mtok(pricing.get("completion")),
            # R192: a cache HIT is billed at a fraction of the input rate
            # (OpenRouter's `input_cache_read`), and on an agentic loop the
            # resent prefix is nearly all of the prompt — so leaving this out
            # of the table is what made the $ badge read several times the
            # real bill. None when the catalog lists no cache rate; `cost()`
            # falls back rather than treating that as "no discount".
            "price_cache_read_per_mtok": _mtok(pricing.get("input_cache_read")),
            "description": (m.get("description") or "").strip()}


def _fetch_openrouter_catalog() -> tuple[list[dict] | None, bool]:
    """The raw `/api/v1/models` list (no key needed), or (None, False) if it
    couldn't be fetched (offline/API error). Split out of
    `fetch_openrouter_model_info` so a batch refresh (R136) costs one HTTP
    call for N models instead of N."""
    try:
        r = httpx.get("https://openrouter.ai/api/v1/models", timeout=10)
        r.raise_for_status()
        return r.json().get("data", []), True
    except Exception:
        return None, False


def fetch_openrouter_model_info(model_id: str) -> tuple[dict | None, bool]:
    """Look a model up in OpenRouter's public catalog. Returns (info,
    catalog_ok): info is {context_size, price_in_per_mtok,
    price_out_per_mtok, description}, or None when the model ISN'T in the
    catalog; catalog_ok is False when the catalog itself couldn't be fetched
    (offline/API error) — so the caller can tell "no such model" (refuse)
    apart from "can't verify" (proceed, warn). Prices are the API's listed
    route price ($/token, converted to $/Mtok) — NOT the usage-weighted
    average the hand-maintained table entries use (close enough for a fresh
    add; edit remote_context_limits.json to refine)."""
    data, catalog_ok = _fetch_openrouter_catalog()
    if not catalog_ok:
        return None, False
    for m in data:
        if m.get("id") == model_id:
            return _model_info_from_catalog_entry(m), True
    return None, True


def refresh_prices_for(model_ids: list[str]) -> tuple[dict[str, dict], bool]:
    """Batch counterpart to `fetch_openrouter_model_info` (R136): one catalog
    fetch, matched against every id in `model_ids` — used by the
    price-refresh extension to refresh all of config.yaml's configured
    OpenRouter models without one HTTP round-trip per model. Returns ({model_id:
    info, ...} for ids found in the catalog, catalog_ok); an id missing from
    the returned dict was either not found or duplicated, not an error."""
    data, catalog_ok = _fetch_openrouter_catalog()
    if not catalog_ok:
        return {}, False
    by_id = {m.get("id"): m for m in data}
    return ({mid: _model_info_from_catalog_entry(by_id[mid])
             for mid in model_ids if mid in by_id}, True)


# feature request, 2026-08-03: opening `/model` refreshes the picker's prices
# in the background, so a listed price that changed on OpenRouter's side stops
# needing a manual `refresh_model_prices` call. Rate-limited by a per-entry
# `refreshed_at` stamp: the catalog is only re-fetched when some configured
# model's price is older than this (or was never stamped — a hand-edited entry
# or one from before this existed).
PRICE_TTL_SECONDS = 24 * 60 * 60
_refresh_lock = threading.Lock()
_refresh_in_flight = False


def prices_are_stale(model_ids: list[str], now: float | None = None) -> bool:
    """True when at least one of `model_ids` has no fresh cached price — the
    TTL gate for the background refresh. A model with no entry at all counts
    as stale (that's exactly the case worth fetching)."""
    now = time.time() if now is None else now
    for mid in model_ids:
        stamp = (REMOTE_CONTEXT_LIMITS.get(mid) or {}).get("refreshed_at")
        try:
            if now - float(stamp) < PRICE_TTL_SECONDS:
                continue
        except (TypeError, ValueError):
            pass       # never stamped, or a malformed hand-edit → refresh
        return True
    return False


def _looks_online(host: str = "openrouter.ai") -> bool:
    """Cheap "is there any point trying" probe: can we resolve the catalog's
    host? Not a guarantee (DNS can answer from cache on a dead link) — the
    fetch still has to handle its own failure. It's here so the common offline
    case costs a failed resolve instead of the full 10s HTTP timeout, which
    matters because this runs while the user is looking at the `/model` menu."""
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def refresh_prices_in_background(model_ids: list[str], on_done=None) -> bool:
    """Fire-and-forget `refresh_prices_for` + `save_remote_model_infos` on a
    daemon thread, calling `on_done()` once the in-memory table has been
    updated (never on failure — a stale price is not worth a repaint). Returns
    whether a thread was actually started: no-op when there's nothing to fetch,
    when the TTL says the cache is fresh, or when a refresh is already running
    (re-opening the picker three times must not mean three catalog fetches).

    Deliberately silent about errors: this runs behind the user's back while
    they're picking a model, so being offline has to look like nothing
    happening, not like a failure."""
    global _refresh_in_flight
    model_ids = list(dict.fromkeys(mid for mid in model_ids if mid))
    if not model_ids or not prices_are_stale(model_ids):
        return False
    with _refresh_lock:
        if _refresh_in_flight:
            return False
        _refresh_in_flight = True

    def work():
        global _refresh_in_flight
        try:
            # the resolve, not just the fetch, happens off the caller's thread:
            # getaddrinfo blocks, and nothing about opening the picker should
            # wait on the network.
            if not _looks_online():
                return
            info_by_id, catalog_ok = refresh_prices_for(model_ids)
            if not catalog_ok or not info_by_id:
                return
            save_remote_model_infos(info_by_id)
            if on_done is not None:
                on_done()
        except Exception:
            pass       # background chore — must never take the session down
        finally:
            with _refresh_lock:
                _refresh_in_flight = False

    threading.Thread(target=work, name="price-refresh", daemon=True).start()
    return True


def _merge_model_entry(model_id: str, info: dict) -> dict:
    """Fold `info` (from a catalog lookup) onto the existing
    `remote_context_limits.json` entry for `model_id`, or a fresh skeleton if
    there isn't one yet. Only fields the catalog actually returned are
    touched — an unpriced-in-the-catalog model keeps whatever price it had.

    R136 review: numeric fields must use `is not None`, not truthiness — a
    genuinely FREE model (`price_in_per_mtok == 0.0`) was previously
    indistinguishable from "the catalog didn't say," so its $0 price was
    silently dropped and any stale non-zero price from before stuck around.
    `description` stays a truthiness check on purpose: the catalog's default
    for a missing description is `""`, and an empty string overwriting a
    real one would be a regression, not a fix — there's no "explicitly
    blank" case for a description the way `0.0` is a real price."""
    entry = dict(REMOTE_CONTEXT_LIMITS.get(model_id) or
                 {"model": model_id, "provider": "openrouter",
                  "code": model_id.rsplit("/", 1)[-1],
                  "pricing_url": f"https://openrouter.ai/{model_id}#pricing"})
    if info.get("context_size") is not None:
        try:
            entry["context_size"] = int(info["context_size"])
        except (TypeError, ValueError):
            pass   # malformed catalog data — don't let it crash the caller
    for k in ("price_in_per_mtok", "price_out_per_mtok",
              "price_cache_read_per_mtok"):
        if info.get(k) is not None:
            entry[k] = info[k]
    if info.get("description"):
        entry["description"] = info["description"]
    # When this entry came from a catalog lookup, stamp it — that's what
    # `prices_are_stale` reads to decide whether the picker's background
    # refresh needs to hit the network at all. Stamped for a catalog hit even
    # if it carried no price: the answer "OpenRouter lists no price for this"
    # is just as fresh as a number, and re-asking hourly won't change it.
    entry["refreshed_at"] = round(time.time(), 3)
    return entry


# R193: every read-modify-write of remote_context_limits.json takes this. The
# background price refresh (R188) runs on its own thread while the main thread
# can be doing `/model add` — two unsynchronized read-modify-writes lose one
# side's edit outright, and two overlapping `write_text` calls can interleave
# into invalid JSON. A corrupt file is silent and total: the loader catches
# JSONDecodeError and returns `{}`, so EVERY model loses its context limit and
# pricing at once, and the ctx gauge quietly falls back to a 128k default.
_SAVE_LOCK = threading.Lock()


def _write_entries_atomically(entries: list[dict]) -> None:
    """R193: temp file in the same directory + `os.replace`, never a direct
    `write_text`. `write_text` truncates first and writes after, so a crash,
    a Ctrl+C or a full disk mid-write leaves a half-written file — and this
    particular file failing to parse costs every model its context limit and
    price silently. `os.replace` is atomic on POSIX: readers see either the
    old file or the new one."""
    d = _REMOTE_CONTEXT_LIMITS_PATH.parent
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".remote_context_limits.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(entries, indent=2) + "\n")
        os.replace(tmp, _REMOTE_CONTEXT_LIMITS_PATH)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_remote_model_info(model_id: str, info: dict) -> None:
    """Add/refresh one model's entry in remote_context_limits.json AND the
    in-memory table, so the footer's ctx gauge and $ badge (R71/R73) work
    for a just-added model without a restart."""
    entry = _merge_model_entry(model_id, info)
    REMOTE_CONTEXT_LIMITS[model_id] = entry
    with _SAVE_LOCK:
        try:
            entries = json.loads(_REMOTE_CONTEXT_LIMITS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            entries = []
        entries = [e for e in entries if e.get("model") != model_id] + [entry]
        _write_entries_atomically(entries)


def save_remote_model_infos(infos: dict[str, dict]) -> None:
    """Batch counterpart to `save_remote_model_info` (R136 review): one read
    + one write for N models, not N of each. `refresh_model_prices` used to
    call the single-model function in a loop — N configured models meant N
    full-file read-modify-writes of remote_context_limits.json, and a crash
    or Ctrl+C between any two of them left the file (and every OTHER
    already-refreshed model's price, not just the one in flight) in a
    half-written state."""
    if not infos:
        return
    with _SAVE_LOCK:
        _save_remote_model_infos_locked(infos)


def _save_remote_model_infos_locked(infos: dict[str, dict]) -> None:
    try:
        entries = json.loads(_REMOTE_CONTEXT_LIMITS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        entries = []
    by_model = {e.get("model"): e for e in entries}
    for model_id, info in infos.items():
        entry = _merge_model_entry(model_id, info)
        REMOTE_CONTEXT_LIMITS[model_id] = entry
        by_model[model_id] = entry
    _write_entries_atomically(list(by_model.values()))


def cost_for(model: str, inp: int, out: int, cached: int = 0) -> float | None:
    """R203: THE pricing rule. `None` when the model has no listed price.

    R192 taught `OpenAICompatProvider.cost` about cache reads but left this
    module's other consumers — `/context`'s per-turn badge, its session total,
    the per-model breakdown, and the cost a RESUMED session is seeded with —
    computing their own `billed * price_in + out * price_out` from
    `price_for()`. So one live session reported two different figures for
    itself: on the real 2026-08-09 kimi-k3 session, $32.50 in `/context`
    against $4.74 in the status bar, where $4.74 is what OpenRouter charged.

    Having one function rather than four copies of the arithmetic is the
    actual fix; the cache term is just what those copies were missing."""
    entry = REMOTE_CONTEXT_LIMITS.get(model, {})
    price_in = entry.get("price_in_per_mtok")
    price_out = entry.get("price_out_per_mtok")
    if price_in is None or price_out is None:
        return None
    cached = max(0, min(cached, inp))
    price_cache = entry.get("price_cache_read_per_mtok")
    if price_cache is None:
        price_cache = price_in * _CACHE_READ_FALLBACK
    return ((inp - cached) * price_in
            + cached * price_cache
            + out * price_out) / 1_000_000


def price_for(model: str) -> tuple[float, float] | None:
    """($/Mtok in, $/Mtok out) for a model, or None when unpriced — the one
    place the per-model price table is read outside a live provider
    instance, so `/cost` (R92) prices a past session's models without
    building a provider for each one."""
    entry = REMOTE_CONTEXT_LIMITS.get(model, {})
    pin, pout = entry.get("price_in_per_mtok"), entry.get("price_out_per_mtok")
    return None if pin is None or pout is None else (pin, pout)


def _is_bare_ip(base_url: str) -> bool:
    """True when the endpoint is a literal private/loopback IP rather than a
    hostname. A LAN reverse proxy (e.g. Caddy) commonly serves a cert issued
    for its Tailscale/hostname name only — hitting it by bare IP always
    mismatches, so we skip TLS verification for that case specifically
    (hostnames, including .ts.net, keep full verification)."""
    host = (urlparse(base_url).hostname or "").lower()
    try:
        return ipaddress.ip_address(host).is_private or \
               ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_lan_host(base_url: str) -> bool:
    """True for a self-hosted server on the local machine / LAN / tailnet —
    somewhere a connect SHOULD fail fast when it's off. A public API
    (openrouter.ai, …) is not: its TLS handshake can legitimately be slow, so
    it gets a longer connect budget (see `_client`)."""
    host = (urlparse(base_url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".ts.net")):
        return True
    try:
        return ipaddress.ip_address(host).is_private or \
               ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False   # a public DNS name → remote


# R91: a cache breakpoint only pays off on a big, byte-identical prefix.
# Anthropic-family models (the ones that need the EXPLICIT cache_control
# marker, via OpenRouter) won't cache under ~1024 tokens at all, and a write
# costs more than a plain read — so below this we send the plain string and
# skip the whole mechanism. ~4 chars/token, matching tokens.estimate_tokens.
_CACHE_MIN_CHARS = 4 * 1024


def _system_message(system: str, cache: bool) -> dict:
    """The system message, as a cacheable content block when it's worth it.

    OpenAI-compatible caching splits in two: OpenAI/DeepSeek-style backends
    cache long prefixes automatically and ignore the marker, while
    Anthropic-family models routed through OpenRouter cache ONLY at an
    explicit `cache_control` breakpoint. Marking the system prompt covers
    both — it's the one part of every request that is byte-identical across
    a whole session (base preamble + AGENTS.md + the three indexes + every
    [CORE] doc), and it is re-sent on every tool iteration, not just every
    turn (R37 bills each one)."""
    if not cache or len(system) < _CACHE_MIN_CHARS:
        return {"role": "system", "content": system}
    return {"role": "system",
            "content": [{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}]}


def _to_openai_tools(specs: list[dict]) -> list[dict]:
    return [{"type": "function",
             "function": {"name": s["name"], "description": s["description"],
                          "parameters": s["parameters"]}} for s in specs]


class OpenAICompatProvider(Provider):
    def __init__(self, name: str, config: dict, timeout: float = 300):
        super().__init__(name, config, timeout)
        # one connection pool per active endpoint so fail-over doesn't keep
        # paying TLS/TCP setup and a dead pooled connection is never reused.
        self._http: dict[str, httpx.Client] = {}
        import threading
        self._http_lock = threading.Lock()  # pool dict is touched by BOTH the
        # worker (turn) and the UI thread (status-render /props probes)
        self._working_url: str | None = None
        self._working_url_at: float = 0.0
        # R290: url -> time.time() until which a failed probe is trusted
        self._dead_until: dict[str, float] = {}

    @property
    def _client(self) -> httpx.Client:
        return self._client_for(self.base_url)

    def _client_for(self, base_url: str) -> httpx.Client:
        """One persistent connection pool per endpoint — a multi-tool turn
        makes many requests, and per-request TLS/TCP setup adds up (more so
        against OpenRouter/remote than localhost)."""
        with self._http_lock:
            cached = self._http.get(base_url)
        if cached is not None:
            return cached
        # connect (TCP + TLS handshake) is bounded separately from the
        # long read timeout. A self-hosted/LAN server that's off must fail
        # in seconds (OS default ~2min looked like a hang off-grid); but a
        # PUBLIC API's TLS handshake can be slow over a poor link, and a 5s
        # budget there causes false "unreachable"/handshake-timeout, so
        # remote gets more room.
        connect = 5 if _is_lan_host(base_url) else 20
        # Happy Eyeballs (RFC 8305): race IPv4/IPv6 and use whichever
        # connects first, so a dead public-IPv6 route (Tailscale up →
        # blackhole) doesn't stall every handshake for the full timeout
        # (17s vs 0.15s). `retries=2` also retries a transient connect/TLS
        # failure on the winning family — httpcore retries only
        # ConnectError/ConnectTimeout, before the request is sent, so no
        # duplicate request and no duplicated streamed text.
        new_client = httpx.Client(
            transport=HappyEyeballsTransport(
                retries=2, verify=not _is_bare_ip(base_url)),
            timeout=httpx.Timeout(self.timeout, connect=connect))
        with self._http_lock:
            client = self._http.setdefault(base_url, new_client)
        if client is not new_client:
            # R96j: lost the race — another thread (worker vs. UI-thread
            # /props probe, both call this) built and installed a client for
            # the same endpoint first. `setdefault` correctly returns THEIRS,
            # but the one we just built here is now unreachable from
            # anywhere except this local — close it explicitly or its
            # connection pool (sockets, not just Python memory) leaks for
            # the life of the process.
            new_client.close()
        return client

    def _is_ollama(self) -> bool:
        """R223: an explicit opt-in via config.yaml's `type: ollama` — the
        same field `Engine.provider_kind()` already reads, previously only
        for a display label. Ollama's server has no /props (llama.cpp-only)
        and no reliable auto-detectable signature worth guessing at, so this
        stays config-driven rather than magic port/route sniffing."""
        return self.config.get("type") == "ollama"

    def _probe(self, url: str) -> bool:
        """Quick connectivity test for a local endpoint. Public URLs are
        assumed reachable and not probed; local endpoints are checked with a
        short request so we can fail over fast instead of waiting for the
        main request to time out. llama.cpp answers /props; Ollama has no
        such route but answers a plain GET on its root ("Ollama is
        running") — R223."""
        if not _is_lan_host(url):
            return True
        base = url.removesuffix("/v1")
        path = "/" if self._is_ollama() else "/props"
        try:
            # R245: `_auth_headers`, not a hand-rolled header — it is the one
            # place that knows `api_key: none` (the keyless-server convention
            # this config invites) means NO key, not a literal bearer token
            # called "none". Probing with `Bearer none` gets a 401 from any
            # server that validates the token, so `pick_endpoint` marks a
            # perfectly healthy endpoint dead and fails over off it.
            h = self._auth_headers()
            # R95h: reuse this endpoint's pooled client. A bare `httpx.get`
            # built a fresh client — and so a fresh TCP+TLS handshake — for
            # every probe, which is most of what a probe costs.
            self._client_for(url).get(f"{base}{path}", headers=h,
                                      timeout=2).raise_for_status()
            return True
        except Exception:
            return False

    def pick_endpoint(self, cache_ok: bool = True) -> str:
        """Return the first configured endpoint that answers our probe.
        If none answer, fall back to the first URL so error messages point
        at a real address. Caches the choice briefly so footer /status calls
        don't probe on every render."""
        import time
        urls = self._base_urls
        if not urls:
            return self.base_url
        if (cache_ok and self._working_url
                and time.time() - self._working_url_at < 10):
            self.base_url = self._working_url
            return self._working_url
        # R290: skip an endpoint that failed its probe recently, while any
        # other is still a candidate. The list is re-probed IN ORDER every
        # time the 10s cache expires — i.e. before nearly every agent round —
        # and a dead first URL (off-LAN, Tailscale down) costs its whole
        # connect budget each time: measured 6.5s per probe (2s × the
        # transport's retries), so a 10-round turn paid ~65s just probing.
        now = time.time()
        candidates = [u for u in urls if self._dead_until.get(u, 0.0) <= now] \
            or urls                   # all marked dead: try them all anyway
        for url in candidates:
            if self._probe(url):
                self._dead_until.pop(url, None)
                self._working_url = url
                self._working_url_at = time.time()
                self.base_url = url
                return url
            self._dead_until[url] = time.time() + _DEAD_URL_BACKOFF_S
        # nothing reachable — pin first URL and let the next request fail
        self._working_url = urls[0]
        self._working_url_at = 0.0
        self.base_url = urls[0]
        return urls[0]

    def _ollama_live_context_limit(self, model: str) -> int | None:
        """R223: Ollama's /api/show reports a model's real trained context
        window by NAME (`POST {"name": model}`), for any installed model —
        unlike llama.cpp's /props, which only ever describes whatever is
        CURRENTLY loaded and needs the "model: local" sentinel to be
        trustworthy. The context-length field is keyed per model FAMILY
        (`llama.context_length`, `qwen2.context_length`,
        `gemma2.context_length`, …), not one fixed name — `model_info`'s own
        `general.architecture` field names which family key to read."""
        try:
            r = self._client.post(
                f"{self.base_url.removesuffix('/v1')}/api/show",
                json={"name": model}, timeout=4, headers=self._auth_headers())
            info = r.json().get("model_info", {})
            arch = info.get("general.architecture")
            n = info.get(f"{arch}.context_length") if arch else None
            return int(n) if n else None
        except Exception:
            return None

    def live_context_limit(self, model: str = "local") -> int | None:
        """llama.cpp exposes the real loaded -c via /props (R13); Ollama via
        /api/show (R223)."""
        self.pick_endpoint(cache_ok=True)
        if not _is_lan_host(self.base_url):
            return None
        if self._is_ollama():
            return self._ollama_live_context_limit(model)
        # /props is a llama.cpp endpoint; a PUBLIC API (OpenRouter, …) has no
        # such route, so probing it just wastes a slow request (~6s) on the
        # first status render — and it's on the UI thread, so it freezes the
        # whole app at startup. Only a local/tailnet llama.cpp server has it
        # — but a gateway that unifies local + remote behind ONE LAN base_url
        # (aurora-gateway) means `_is_lan_host` alone no longer implies "the
        # SELECTED model is the local one": a remote model routed through
        # that same gateway would otherwise get /props's answer for whatever
        # is actually loaded locally, not its own real context window. The
        # "local" sentinel (config's `model: local` entry) is what actually
        # identifies the local model — check that too.
        if model != "local":
            return None
        try:
            r = self._client.get(f"{self.base_url.removesuffix('/v1')}/props",
                                 timeout=4,
                                 headers=self._auth_headers())
            n = r.json().get("default_generation_settings", {}).get("n_ctx")
            return int(n) if n else None
        except Exception:
            return None

    def live_model_name(self) -> str | None:
        """llama.cpp exposes the real loaded model's basename via /props.
        R223: Ollama can have several models resident at once (`GET
        /api/ps`), which doesn't map onto this method's single "the loaded
        model" concept — left returning None for Ollama, same as any remote
        provider gets today, rather than guessing which one "the" model is."""
        if self._is_ollama():
            return None
        self.pick_endpoint(cache_ok=True)
        if not _is_lan_host(self.base_url):
            return None
        try:
            r = self._client.get(f"{self.base_url.removesuffix('/v1')}/props",
                                 timeout=4,
                                 headers=self._auth_headers())
            path = r.json().get("model_path")
            return path.rsplit("/", 1)[-1] if path else None
        except Exception:
            return None

    def model_health(self, model: str) -> dict | None:
        """R254: per-model check for the `/model` picker. Only local/LAN
        backends get one — a remote paid API's bad-model-id only surfaces as
        a 4xx on first real use, and probing every entry there on every
        picker open would mean a real billed-provider request per row.

        Ollama can have a config entry for a model that was never actually
        `ollama pull`ed (or was later removed) — `/api/tags`, not `/api/show`,
        is the check: `/api/show` 404s the same way for "server unreachable"
        and "model not installed", which would mislabel a live server's
        missing model as a connectivity problem. llama.cpp (the `local`
        sentinel) has no such per-model concept — whatever's loaded IS
        "local" — so the only meaningful check there is reachability."""
        if not _is_lan_host(self.base_url) and not self._is_ollama():
            return None
        url = self.pick_endpoint(cache_ok=True)
        if not self._probe(url):
            return {"ok": False, "detail": "unreachable"}
        if not self._is_ollama():
            return {"ok": True, "detail": ""}
        try:
            r = self._client_for(url).get(
                f"{url.removesuffix('/v1')}/api/tags", timeout=4,
                headers=self._auth_headers())
            r.raise_for_status()
            names = {m.get("name") for m in r.json().get("models", [])}
        except Exception:
            return {"ok": False, "detail": "unreachable"}
        if model not in names:
            return {"ok": False, "detail": "not pulled"}
        return {"ok": True, "detail": ""}

    def context_limit(self, model: str) -> int:
        listed = REMOTE_CONTEXT_LIMITS.get(model, {}).get("context_size")
        return self.live_context_limit(model) or listed or super().context_limit(model)

    def static_context_limit(self, model: str) -> int:
        """R95i: same answer minus the live /props call — no network."""
        listed = REMOTE_CONTEXT_LIMITS.get(model, {}).get("context_size")
        return listed or super().static_context_limit(model)

    def has_pricing(self, model: str) -> bool:
        """Whether a real cost estimate is possible — only when the model is
        listed in remote_context_limits.json WITH price fields (local/
        unlisted models have no known $/token, and showing a "$0.00" badge
        for them would misleadingly imply Aurora knows it's free).

        R247: asks `price_for`, which is what `cost_for` actually prices
        with, instead of testing that the two KEYS exist. A hand-edited
        entry carrying `"price_in_per_mtok": null` satisfied key-presence
        while `cost_for` returned None for it — so the badge claimed a real
        figure and rendered the `0.0` fallback, i.e. "$0.00", for a model
        whose price is simply unknown. One predicate, one answer."""
        return price_for(model) is not None

    def cost(self, model: str, inp: int, out: int, cached: int = 0) -> float:
        """R192: `cached` is the part of `inp` the provider served from its
        prompt cache, and it is NOT billed at the input rate. Pricing every
        prompt token as fresh overstated a long agentic turn several-fold —
        each iteration resends the whole prefix, so on a real session the
        cache hit rate runs above 90% and the error scales with turn length,
        not with anything the user can see.

        `cached` is clamped to `inp` because the two numbers come from
        different fields of the same usage block and a provider that reports
        them inconsistently must not produce a NEGATIVE fresh-token count.

        R203: the arithmetic moved to the module-level `cost_for` so the
        display paths share it instead of each keeping a copy. `0.0` here
        rather than `None` because a live turn's accumulator adds this every
        round; "unpriced" is reported separately, via `has_pricing`."""
        usd = cost_for(model, inp, out, cached)
        return 0.0 if usd is None else usd

    def _auth_headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "none":
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def turn(self, model, messages, system, tools, on_text, cancel) -> TurnResult:
        # Pick a working endpoint so we fail over when the LAN/Tailscale path
        # changes between messages. PIN it in a local: `self.base_url` is also
        # flipped by the UI thread's status-render probes (live_context_limit
        # → pick_endpoint), and a mid-turn flip must not redirect this request
        # or its retries.
        #
        # R95h: honour the short TTL cache instead of forcing a probe. `turn()`
        # is called once per agent ITERATION, not once per user message, so
        # cache_ok=False meant a 10-iteration turn paid 10 extra probe round
        # trips — negligible on localhost, real over a tailnet. The 10s TTL
        # still re-probes between messages (a human turnaround is longer than
        # that), and a connection failure below explicitly expires the cache
        # (`_working_url_at = 0.0`), so failover is unchanged where it counts.
        base = self.pick_endpoint(cache_ok=True)
        client = self._client_for(base)
        msgs = ([_system_message(system, self.cache_prompt)]
                if system else []) + messages
        payload = {"model": model, "messages": msgs, "stream": True,
                   "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = _to_openai_tools(tools)
        if self.extra_body:
            payload.update(self.extra_body)

        # A stale pooled keep-alive connection — the server/proxy closed it
        # while the app sat idle — resets on reuse ("Connection reset by peer",
        # RemoteProtocolError "Server disconnected"). httpcore's connect-retry
        # doesn't cover a failure DURING the request, so retry here — but only
        # while nothing has streamed yet (a mid-stream drop keeps its partial).
        _RETRIABLE = (httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                      httpx.RemoteProtocolError, httpx.PoolTimeout)
        _ATTEMPTS = 3
        for _attempt in range(_ATTEMPTS):
            result = TurnResult()
            pending: dict[int, dict] = {}   # index -> {id, name, args-fragments}
            try:
                for kind, a, _b, _headers in cancellable_sse(
                        lambda: client.stream(
                            "POST", f"{base}/chat/completions",
                            headers=self._auth_headers(), json=payload),
                        cancel):
                    if kind == "status":
                        if a >= 400:
                            body = _b or ""
                            # R99: a 429 gets its own retry path below, with
                            # backoff — a shared free-tier limit routinely
                            # clears in a few seconds, so failing the whole
                            # turn immediately just pushes the same "try
                            # again" onto the user that this loop can do
                            # itself.
                            if a == 429:
                                raise _RateLimited(
                                    body, retry_after=_parse_retry_after(_headers))
                            # llama.cpp surfaces template/parse failures as
                            # 500s with "Failed to parse" — gpt-oss mode
                            if "parse" in body.lower():
                                raise MalformedToolCall(body)
                            raise ProviderError(f"{self.name} HTTP {a}: {body}")
                        continue
                    line = a
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue   # one garbled SSE line must not kill the stream
                    usage = chunk.get("usage")
                    if usage:
                        result.input_tokens = usage.get("prompt_tokens", 0)
                        result.output_tokens = usage.get("completion_tokens", 0)
                        # R91: how much of that prompt was a cache HIT. Not
                        # every backend reports it; 0 means "not reported",
                        # never "definitely no hit".
                        details = usage.get("prompt_tokens_details") or {}
                        result.cached_input_tokens = details.get(
                            "cached_tokens", 0) or 0
                        # R133a: reasoning is a SUBSET of completion_tokens —
                        # never add the two. Same "0 means unreported" rule as
                        # cached_tokens above.
                        out_details = usage.get("completion_tokens_details") or {}
                        result.reasoning_tokens = out_details.get(
                            "reasoning_tokens", 0) or 0
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    ch = choices[0]
                    if ch.get("finish_reason"):
                        result.stop_reason = ch["finish_reason"]
                    delta = ch.get("delta", {})
                    # thinking models (Qwen3.x) stream reasoning separately —
                    # route it to on_think (UI decides how to show it); it never
                    # enters the stored/copyable text
                    rc = delta.get("reasoning_content")
                    if rc:
                        # R133a: measured even with no on_think listener —
                        # the count is accounting, not display.
                        result.reasoning_chars += len(rc)
                        if self.on_think:
                            self.on_think(rc)
                    if delta.get("content"):
                        result.text += delta["content"]
                        on_text(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        slot = pending.setdefault(i, {"id": "", "name": "", "args": []})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["args"].append(fn["arguments"])
            except (httpx.HTTPError, _RateLimited) as e:
                # mid-stream drop (read timeout on a slow long generation,
                # server restart): the user already WATCHED the partial text
                # stream — keep it in history instead of discarding the turn.
                # A 429 can never reach this with result.text set — it's the
                # status line, always the first event — but the check stays
                # generic rather than gated on exception type.
                if result.text:
                    note = (f"\n[stream interrupted: {e.__class__.__name__} — "
                            f"partial answer kept]")
                    try:
                        on_text(note)
                    except Exception:
                        pass
                    result.text += note
                    result.stop_reason = "interrupted"
                    pending.clear()   # half-received tool calls are unusable
                    return result
                # R99: a rate limit gets its OWN retry schedule (backoff,
                # not the connection-retry's flat delay) — distinct because
                # the right wait time for "server briefly hiccuped" and "a
                # shared quota needs seconds to free up" aren't the same.
                if isinstance(e, _RateLimited):
                    if _attempt + 1 < _ATTEMPTS:
                        wait = _RATE_LIMIT_BACKOFF[_attempt]
                        if e.retry_after is not None:
                            wait = min(e.retry_after, _RATE_LIMIT_BACKOFF_CAP)
                        # R147: a plain sleep(wait) here was the ONE blocking
                        # point in this file that ignored `cancel` — up to 30s
                        # (the cap) during which Esc-Esc did nothing while the
                        # spinner kept animating, which reads as a hang.
                        # `cancellable_sse` polls every 0.15s precisely so the
                        # caller always unblocks promptly; this now matches.
                        if _sleep_unless_cancelled(wait, cancel):
                            result.stop_reason = "cancelled"
                            return result
                        continue
                    raise ProviderError(
                        f"{self.name} rate-limited (429): {e}") from e
                # nothing streamed yet: a transient connection failure (stale
                # pooled keep-alive reset after idle) is safe to retry fresh
                if _attempt + 1 < _ATTEMPTS and isinstance(e, _RETRIABLE):
                    import random
                    import time
                    # R171/P1: each attempt re-sends the whole prompt — a
                    # real cost on a remote model — with nothing telling the
                    # user why the request appears to restart. A flat
                    # `0.3 * attempt` delay also had no jitter, so a
                    # provider-side outage hitting many Aurora instances at
                    # once would have them all retry in lockstep.
                    if self.notify:
                        try:
                            self.notify(
                                f"· {e.__class__.__name__} — retrying "
                                f"(attempt {_attempt + 2}/{_ATTEMPTS})")
                        except Exception:
                            pass
                    delay = 0.3 * (_attempt + 1) + random.uniform(0, 0.2)
                    time.sleep(delay)
                    continue
                # the cached "working" URL may have gone down mid-turn — force
                # a re-probe on the next send so failover can try other URLs.
                if isinstance(e, _RETRIABLE):
                    self._working_url_at = 0.0
                raise ProviderError(f"{self.name} request failed: {e}") from e
            break   # streamed to completion — stop retrying

        if cancel():  # watcher aborted the stream — never act on partials
            result.stop_reason = "cancelled"
            pending.clear()
            return result

        for i in sorted(pending):
            slot = pending[i]
            raw = "".join(slot["args"]) or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError as e:
                raise MalformedToolCall(
                    f"unparseable tool arguments for {slot['name'] or '?'}: {raw[:200]}") from e
            if not slot["name"]:
                raise MalformedToolCall(f"tool call with no name: {raw[:200]}")
            # R268: valid JSON is not enough — every consumer (approval,
            # deny/allow matching, loop detection) treats arguments as a
            # mapping. A list/string/number here used to raise AttributeError
            # AFTER the assistant tool_calls message was appended, leaving an
            # orphaned tool_call that poisoned every later request.
            if not isinstance(args, dict):
                raise MalformedToolCall(
                    f"tool arguments for {slot['name']} are not a JSON object: "
                    f"{raw[:200]}")
            result.tool_calls.append(
                ToolCall(slot["id"] or f"call_{i}", slot["name"], args))
        return result

    def assistant_message(self, result: TurnResult) -> dict:
        msg: dict = {"role": "assistant", "content": result.text or None}
        if result.tool_calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name,
                                               "arguments": json.dumps(c.arguments)}}
                                 for c in result.tool_calls]
        return msg

    def tool_result_message(self, call: ToolCall, output: str) -> dict:
        return {"role": "tool", "tool_call_id": call.id, "content": output}
