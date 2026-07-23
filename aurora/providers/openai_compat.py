"""OpenAI-compatible servers: llama.cpp (--jinja), OpenRouter, LM Studio, …
Streaming, tool calls, usage. Malformed tool-call responses raise
MalformedToolCall so agent.py can retry-then-degrade (R5)."""

import ipaddress
import json
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from .base import (MalformedToolCall, Provider, ProviderError, ToolCall,
                   TurnResult, cancellable_sse)
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


# R99: exponential, not the connection-retry's flat 0.3*(attempt+1) — a
# shared free-tier limit clears on the order of seconds, a stale pooled
# connection resets instantly. One entry per retry (len == _ATTEMPTS - 1).
_RATE_LIMIT_BACKOFF = (1.0, 3.0)


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
        entries = json.loads(_REMOTE_CONTEXT_LIMITS_PATH.read_text())
        return {e["model"]: e for e in entries}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}


REMOTE_CONTEXT_LIMITS = _load_remote_context_limits()


def fetch_openrouter_model_info(model_id: str) -> tuple[dict | None, bool]:
    """Look a model up in OpenRouter's public catalog (`/api/v1/models`, no
    key needed). Returns (info, catalog_ok): info is
    {context_size, price_in_per_mtok, price_out_per_mtok, description}, or
    None when the model ISN'T in the catalog; catalog_ok is False when the
    catalog itself couldn't be fetched (offline/API error) — so the caller
    can tell "no such model" (refuse) apart from "can't verify" (proceed,
    warn). Prices are the API's listed route price ($/token, converted to
    $/Mtok) — NOT the usage-weighted average the hand-maintained table
    entries use (close enough for a fresh add; edit
    remote_context_limits.json to refine)."""
    def _mtok(v):
        try:
            return round(float(v) * 1_000_000, 3)
        except (TypeError, ValueError):
            return None
    try:
        r = httpx.get("https://openrouter.ai/api/v1/models", timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception:
        return None, False
    for m in data:
        if m.get("id") == model_id:
            pricing = m.get("pricing") or {}
            return {"context_size": m.get("context_length"),
                    "price_in_per_mtok": _mtok(pricing.get("prompt")),
                    "price_out_per_mtok": _mtok(pricing.get("completion")),
                    "description": (m.get("description") or "").strip()}, True
    return None, True


def save_remote_model_info(model_id: str, info: dict) -> None:
    """Add/refresh one model's entry in remote_context_limits.json AND the
    in-memory table, so the footer's ctx gauge and $ badge (R71/R73) work
    for a just-added model without a restart. Only known fields are set."""
    entry = dict(REMOTE_CONTEXT_LIMITS.get(model_id) or
                 {"model": model_id, "provider": "openrouter",
                  "code": model_id.rsplit("/", 1)[-1],
                  "pricing_url": f"https://openrouter.ai/{model_id}#pricing"})
    if info.get("context_size"):
        entry["context_size"] = int(info["context_size"])
    for k in ("price_in_per_mtok", "price_out_per_mtok", "description"):
        if info.get(k):
            entry[k] = info[k]
    REMOTE_CONTEXT_LIMITS[model_id] = entry
    try:
        entries = json.loads(_REMOTE_CONTEXT_LIMITS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        entries = []
    entries = [e for e in entries if e.get("model") != model_id] + [entry]
    _REMOTE_CONTEXT_LIMITS_PATH.write_text(json.dumps(entries, indent=2) + "\n")


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

    def _probe(self, url: str) -> bool:
        """Quick connectivity test for a local endpoint. Public URLs are
        assumed reachable and not probed; local endpoints are checked with a
        short /props request so we can fail over fast instead of waiting for
        the main request to time out."""
        if not _is_lan_host(url):
            return True
        base = url.removesuffix("/v1")
        try:
            h = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            # R95h: reuse this endpoint's pooled client. A bare `httpx.get`
            # built a fresh client — and so a fresh TCP+TLS handshake — for
            # every probe, which is most of what a probe costs.
            self._client_for(url).get(f"{base}/props", headers=h,
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
        for url in urls:
            if self._probe(url):
                self._working_url = url
                self._working_url_at = time.time()
                self.base_url = url
                return url
        # nothing reachable — pin first URL and let the next request fail
        self._working_url = urls[0]
        self._working_url_at = 0.0
        self.base_url = urls[0]
        return urls[0]

    def live_context_limit(self, model: str = "local") -> int | None:
        """llama.cpp exposes the real loaded -c via /props (R13)."""
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
        self.pick_endpoint(cache_ok=True)
        if not _is_lan_host(self.base_url):
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
        """llama.cpp exposes the real loaded model's basename via /props."""
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
        for them would misleadingly imply Aurora knows it's free)."""
        entry = REMOTE_CONTEXT_LIMITS.get(model, {})
        return "price_in_per_mtok" in entry and "price_out_per_mtok" in entry

    def cost(self, model: str, inp: int, out: int) -> float:
        entry = REMOTE_CONTEXT_LIMITS.get(model, {})
        price_in = entry.get("price_in_per_mtok")
        price_out = entry.get("price_out_per_mtok")
        if price_in is None or price_out is None:
            return 0.0
        return (inp * price_in + out * price_out) / 1_000_000

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
                for kind, a, _b in cancellable_sse(
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
                                raise _RateLimited(body)
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
                    if rc and self.on_think:
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
                        import time
                        time.sleep(_RATE_LIMIT_BACKOFF[_attempt])
                        continue
                    raise ProviderError(
                        f"{self.name} rate-limited (429): {e}") from e
                # nothing streamed yet: a transient connection failure (stale
                # pooled keep-alive reset after idle) is safe to retry fresh
                if _attempt + 1 < _ATTEMPTS and isinstance(e, _RETRIABLE):
                    import time
                    time.sleep(0.3 * (_attempt + 1))
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
