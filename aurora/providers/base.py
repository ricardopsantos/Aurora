"""Provider interface.

A turn = one request with the running message history (+ tool definitions).
The provider streams text chunks to `on_text` as they arrive and returns a
TurnResult carrying any tool calls the model made plus token usage.
History is stored provider-natively; cross-provider switches flatten it
first (compact.flatten_history), so a provider only ever sees its own shape.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    # R91: how many of input_tokens the provider served from its prompt cache
    # (usage.prompt_tokens_details.cached_tokens). 0 when the provider doesn't
    # report it — never assume "no cache hit", only "not reported".
    cached_input_tokens: int = 0


class ProviderError(Exception):
    pass


class MalformedToolCall(ProviderError):
    """The model emitted a tool call the API/parser rejected — the local-model
    failure mode (e.g. gpt-oss harmony tokens). agent.py retries then degrades."""


def cancellable_sse(open_stream, cancel: Callable[[], bool],
                    poll: float = 0.15):
    """Run a streaming HTTP request in a reader thread and yield its events,
    honouring cancel() at every blocking point.

    The caller can block in TWO silent places: waiting for response headers
    (a proxy like Caddy may not send them until the model's first byte — a
    long prefill stalls right here) and waiting for body lines. Both happen
    in the reader thread; this generator polls a queue AND cancel(), so the
    caller always unblocks within `poll` seconds of a cancel. On cancel the
    underlying socket is shut down when reachable (best effort — private
    httpcore attr): the hard disconnect makes llama-server abort the
    generation instead of burning GPU on a dead client.

    `open_stream`: () -> httpx streaming-response context manager.
    Yields ("status", code, error_body) once headers arrive (error_body is
    the read body only when code >= 400, else None), then ("line", text)
    per SSE line. Ends silently on cancel. Reader-side exceptions re-raise
    here unless we cancelled."""
    import queue
    import socket
    import threading

    q: queue.Queue = queue.Queue()
    _END = object()
    resp_box: list = []

    def abort():
        if not resp_box:
            return  # still waiting for headers — reader owns the connection
        ns = resp_box[0].extensions.get("network_stream")
        # plain HTTP: the socket sits on the stream; TLS: one level deeper
        for obj in (ns, getattr(ns, "_stream", None)):
            sock = getattr(obj, "_sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
        try:
            resp_box[0].close()
        except Exception:
            pass

    def read():
        try:
            with open_stream() as resp:
                resp_box.append(resp)
                if resp.status_code >= 400:
                    resp.read()
                    q.put(("status", resp.status_code, resp.text[:300]))
                    q.put(_END)
                    return
                q.put(("status", resp.status_code, None))
                for line in resp.iter_lines():
                    if cancel():
                        break
                    q.put(("line", line, None))
        except Exception as e:
            q.put(e)
        q.put(_END)

    threading.Thread(target=read, daemon=True).start()
    while True:
        if cancel():
            abort()
            return
        try:
            item = q.get(timeout=poll)
        except queue.Empty:
            continue
        if item is _END:
            return
        if isinstance(item, Exception):
            if cancel():
                return
            raise item
        yield item


class Provider(ABC):
    def __init__(self, name: str, config: dict, timeout: float = 300):
        self.name = name
        self.config = config
        # Multiple endpoints can be supplied for failover; order is the
        # fallback order. A single string keeps the old behaviour.
        self._base_urls = self._urls(config.get("base_url"))
        self.base_url = self._base_urls[0] if self._base_urls else ""
        self.api_key = config.get("api_key") or ""
        self.timeout = timeout
        # per-model payload extras (e.g. chat_template_kwargs to disable a
        # thinking mode); set by the engine from the model entry before a turn
        self.extra_body: dict = {}
        # optional reasoning-stream callback (thinking models); set by the
        # engine per turn. Reasoning never enters TurnResult.text/history.
        self.on_think = None
        # R91: mark the system prompt as cacheable on this request. Set by the
        # engine per turn from the model entry's `cache:` flag — an attribute
        # rather than a turn() parameter, same as extra_body/on_think, so the
        # Provider signature (and every fake provider in the tests) is unchanged.
        self.cache_prompt: bool = False

    @staticmethod
    def _urls(raw) -> list[str]:
        if isinstance(raw, list):
            return [str(u).rstrip("/") for u in raw if u]
        return [str(raw).rstrip("/")] if raw else []

    @abstractmethod
    def turn(self, model: str, messages: list[dict], system: str | list,
             tools: list[dict] | None, on_text: Callable[[str], None],
             cancel: Callable[[], bool]) -> TurnResult:
        """Run one model turn. `tools` are Aurora-shape tool specs (see
        tools.SPEC); each provider converts to its own wire format.
        `cancel()` polled between stream events — return early when true."""

    @abstractmethod
    def tool_result_message(self, call: ToolCall, output: str) -> dict:
        """Provider-native message carrying one tool result."""

    @abstractmethod
    def assistant_message(self, result: TurnResult) -> dict:
        """Provider-native assistant message for the history (text + calls)."""

    def context_limit(self, model: str) -> int:
        return int(self.config.get("context_limit", 0)) or 128_000

    def static_context_limit(self, model: str) -> int:
        """The limit knowable WITHOUT touching the network (R95i). The status
        bar renders on the UI thread and must never block on a socket, so it
        serves this until a background refresh produces the live value.
        Subclasses that do live lookups override `context_limit`; this stays
        the offline answer."""
        return int(self.config.get("context_limit", 0)) or 128_000

    def has_pricing(self, model: str) -> bool:
        """Whether cost() returns a real $ estimate for this model — the
        footer only shows a cost badge when this is True (see
        engine.py's context_stats()). Providers with no known per-token
        price (e.g. a local model) default to False."""
        return False
