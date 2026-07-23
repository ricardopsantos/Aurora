"""Engine facade — the stable API a front end drives (R: clean UI/engine split).

The UI holds an Engine and a Frontend. It calls Engine methods (send, switch
model, compact, context stats) and passes its Frontend so the engine can stream
and prompt. The engine owns ALL conversation state (provider, model, messages,
session, config); the UI owns none of it. Neither imports the other's internals
— the only shared vocabulary is `frontend.Frontend` and the small dataclasses.

Swap the UI → write a new Frontend, reuse this Engine unchanged.
Improve the engine → keep these method signatures, the UI is untouched.
"""

import os
import platform
from dataclasses import dataclass
from pathlib import Path

from . import (agent, compact, config, context, keystore, rewind, todo,
               tokens, tools)
from . import secrets as secretscan
from .config import load_config, persist_runtime_value
from .frontend import Frontend
from .providers import make_provider
from .session import Session


def _base_system() -> str:
    """Always-on preamble: who Aurora is + real environment facts, so models
    don't guess relative paths or hallucinate a sandbox."""
    return (
        "You are Aurora, a terminal coding agent running directly on the "
        "user's machine.\n"
        f"Environment: {platform.system()} ({platform.machine()}), "
        f"cwd: {os.getcwd()}, home: {Path.home()}.\n"
        "Your tools operate on the REAL filesystem — you are NOT sandboxed. "
        "You may read any file or directory the user can (reads need no "
        "approval); writes, edits and shell commands ask the user first. "
        "Use absolute paths or ~ (e.g. ~/Desktop), not guesses relative to "
        "the cwd. If a path doesn't exist, list its parent instead of "
        "concluding you lack access. In shell commands, ALWAYS double-quote "
        "file paths — they often contain spaces or parentheses.")


@dataclass
class ContextStats:
    model: str
    used: int
    limit: int
    cost_usd: float
    session_id: str
    cost_known: bool = False   # only render the $ badge when this is True —
    # an unpriced model (local, or a remote model missing from
    # remote_context_limits.json) always has cost_usd == 0.0, which is
    # indistinguishable from "genuinely priced, $0 spent so far" without
    # this flag

    @property
    def pct(self) -> float:
        return (self.used / self.limit * 100) if self.limit else 0.0


class Engine:
    def __init__(self, config_path: str, session_id: str | None = None):
        self.cfg = load_config(config_path)
        self.session = Session(session_id)
        self.models: list[dict] = self.cfg.get("models", [])
        self.runtime = self.cfg.get("runtime", {})
        self.max_iterations = int(self.runtime.get("max_iterations", 5))
        self.web = bool(self.runtime.get("web_search", True))
        self.timeout = float(self.runtime.get("timeout", 300))
        tools.set_command_timeout(self.timeout)  # run_command honours it too (R90g)
        tools.set_todo_enabled(self.runtime.get("todo_tool", True))       # R93
        tools.set_parallel_tools(self.runtime.get("parallel_tools", True))  # R94
        self.redact_secrets = bool(self.runtime.get("redact_secrets", True))
        self.prompt_cache = bool(self.runtime.get("prompt_cache", True))  # R91
        # R58: hashes of confirmed false positives (secrets.hash_value) —
        # never the raw values, so config.yaml stays safe to commit/share
        self.secret_allowlist: set[str] = set(self.runtime.get("secret_allowlist", []))
        self.multiline = bool(self.runtime.get("multiline", False))
        self.system = _base_system()  # + context bootstrap when present
        self.messages: list[dict] = []
        self._used = 0
        self._cost = 0.0
        self._key_ok: dict[str, bool] = {}  # provider → key available (/model picker)
        self._provider = None
        self._provider_key = None
        self._limit_cache: dict = {}
        # R95i: keys whose limit refresh is already in flight, so a burst of
        # renders spawns ONE probe thread, not one per frame.
        self._limit_pending: set = set()
        # R96i: guards the check-then-add below. `.add()`/`.discard()` alone
        # ARE atomic under the GIL — but "if key not in pending: pending.add
        # (key)" is two operations, and two near-simultaneous callers (a UI
        # render + the classic footer, say) can both observe "not pending"
        # before either adds it, spawning two probe threads for one key. The
        # lock only wraps that check-then-add, never the network call
        # itself (which stays on the background thread, unguarded) — this
        # must never become something the UI thread can block on.
        import threading
        self._limit_pending_lock = threading.Lock()
        # starting model: last one used on this machine, else the first
        # configured model that already has a usable key (never nags for a
        # key on a model nobody selected — see _default_model)
        self.current = self._restore_last_model() or self._default_model()

    def _default_model(self) -> dict:
        """First-ever boot (no state.yaml yet): prefer a model whose key is
        ALREADY available, so Aurora doesn't default onto e.g. a local
        server needing a key nobody's set, just because it's models[0] — the
        interactive key-prompt on send() would fire every message otherwise.
        Only falls back to the literal first entry if NONE have a key yet
        (then something has to ask, and it should be the intended default)."""
        return next((m for m in self.models if self._has_key(m.get("provider"))),
                   self.models[0] if self.models else {})

    def _restore_last_model(self) -> dict | None:
        """The entry matching state.yaml's last_model — an exact config match,
        or (for a LlamaDesk library model that has no config entry) the last
        provider's entry re-labelled. None when unknown or the key is gone."""
        st = config.load_state()
        name, pkey = st.get("last_model"), st.get("last_provider")
        if not name:
            return None
        entry = next((m for m in self.models if m.get("model") == name), None)
        if entry is None and pkey:
            base = next((m for m in self.models
                         if m.get("provider") == pkey), None)
            entry = dict(base, model=name) if base else None
        if entry is not None and not self._has_key(entry.get("provider")):
            return None
        return entry

    # ── model / provider ────────────────────────────────────────────────
    def _provider_for(self, model_entry: dict, interactive: bool = False):
        """Build/reuse the provider. Key resolution only prompts the human on
        the send path (interactive=True) — never from the footer/stats path.
        A provider built keyless gets rebuilt once a send can prompt."""
        pkey = model_entry.get("provider")
        rebuild = (pkey != self._provider_key
                   or (interactive and self._provider is not None
                       and not self._provider.api_key
                       and self.cfg["providers"].get(pkey, {}).get("api_key_env")))
        if rebuild:
            pcfg = dict(self.cfg["providers"].get(pkey, {}))
            env = pcfg.get("api_key_env")
            if env and not pcfg.get("api_key"):
                pcfg["api_key"] = keystore.get_key(env, interactive=interactive) or ""
            self._provider = make_provider(pkey, pcfg, self.timeout)
            self._provider_key = pkey
        return self._provider

    def provider_kind(self, model_entry: dict | None = None) -> str:
        e = model_entry or self.current
        return self.cfg["providers"].get(e.get("provider"), {}).get("type", "openai")

    def list_models(self) -> list[dict]:
        return self.models

    def switch_model(self, model_entry: dict) -> None:
        """Switch model."""
        self.current = model_entry
        self._limit_cache = {}  # a switch can change the live n_ctx
        self.session.log("model_switch", model=model_entry.get("model"))
        # remember across restarts (per-machine state, not config.yaml)
        try:
            config.save_state_values(last_model=model_entry.get("model"),
                                     last_provider=model_entry.get("provider"))
        except Exception:
            pass  # persistence must never break a switch

    def _has_key(self, pkey: str | None) -> bool:
        pcfg = self.cfg["providers"].get(pkey, {})
        if pcfg.get("api_key"):
            return True
        env = pcfg.get("api_key_env")
        if not env:  # keyless provider (e.g. a local server) is always usable
            return True
        if pkey not in self._key_ok:
            self._key_ok[pkey] = bool(keystore.get_key(env, interactive=False))
        return self._key_ok[pkey]

    def has_key(self, pkey: str | None) -> bool:
        """Public: does this provider have a usable key right now (never
        prompts)? For the /model picker to flag an entry that WOULD trigger
        an interactive key prompt if selected."""
        return self._has_key(pkey)

    def forget_key_check(self, pkey: str | None) -> None:
        """Clear the cached has_key()/_has_key() result for one provider —
        call after storing a new key so the NEXT check picks it up instead
        of the stale cached miss (the cache exists so repeated footer/picker
        renders don't re-hit the keystore every time; storing a key is the
        one moment that cache must be invalidated)."""
        self._key_ok.pop(pkey, None)

    def add_model(self, model_id: str,
                  provider: str = "openrouter") -> tuple[dict, bool]:
        """/model add (R80): append an OpenRouter model entry to config.yaml
        and the live model list. Returns (entry, created) — created is False
        when the exact (provider, model) pair was already configured, in
        which case nothing is written."""
        existing = next((m for m in self.models
                         if m.get("model") == model_id
                         and m.get("provider") == provider), None)
        if existing is not None:
            return existing, False
        entry = {"provider": provider, "model": model_id, "tools": True}
        config.persist_model_entry(self.cfg, entry)  # self.models aliases cfg["models"]
        self.session.log("model_add", model=model_id, provider=provider)
        return entry, True

    def remove_model(self, model_id: str) -> tuple[int, dict | None]:
        """/model remove (R81): drop every config entry matching model_id
        (any provider). If the CURRENT model was removed, fall back to the
        first remaining model with a usable key (same rule as first boot).
        Returns (removed_count, new_current) — new_current is None when the
        selection didn't change, {} when nothing is left to switch to.
        Cached info in remote_context_limits.json is deliberately kept
        (harmless; a re-add gets its ctx/pricing instantly)."""
        removed = config.remove_model_entries(self.cfg, model_id)
        if not removed:
            return 0, None
        self.session.log("model_remove", model=model_id)
        if self.current.get("model") != model_id:
            return removed, None
        new = self._default_model()
        if new:
            self.switch_model(new)
        else:
            self.current = {}
        return removed, new

    def cache_enabled(self, model_entry: dict | None = None) -> bool:
        """R91: should the system prompt carry a cache breakpoint for this
        model? `runtime.prompt_cache` is the global switch; a model entry may
        opt out (or in) with its own `cache:` flag, exactly like `tools:`.

        Default per model: ON for a remote model, OFF for the `local`
        sentinel — llama.cpp keeps its own KV-cache prefix locally, there is
        nothing to bill and nothing to mark, and a structured (list-of-blocks)
        system message is a needless compatibility risk against whatever
        server happens to be loaded."""
        e = model_entry or self.current
        if not self.prompt_cache:
            return False
        if "cache" in e:
            return bool(e["cache"])
        return e.get("model") != "local"

    def set_prompt_cache(self, on: bool) -> None:
        self.prompt_cache = on
        persist_runtime_value(self.cfg, "prompt_cache", on)

    def valid_models(self) -> list[dict]:
        """Configured models whose provider has a key available (no prompt)."""
        return [m for m in self.models if self._has_key(m.get("provider"))]

    # ── the turn ────────────────────────────────────────────────────────
    def send(self, user_text: str, fe: Frontend, *, bootstrap: bool = False) -> None:
        """Run one user turn against the current model, streaming/prompting
        through the front end. Mutates conversation state; logs everything.
        bootstrap=True tags the logged user event so session listings can
        skip the boilerplate prompt when picking a preview line."""
        if not self.current.get("model"):
            # possible since /model remove (R81) can empty the config
            fe.notify("no model selected — /model to pick one, or "
                      "/model add <url> to add one")
            return
        provider = self._provider_for(self.current, interactive=True)
        provider.extra_body = self.current.get("extra_body") or {}
        provider.on_think = getattr(fe, "on_think", None)
        provider.cache_prompt = self.cache_enabled()
        model = self.current.get("model", "")
        tools_enabled = self.current.get("tools", True)

        # R58: scan the prompt BEFORE it enters history/log — both record
        # exactly what gets decided here, so no separate log-side check.
        if self.redact_secrets:
            matches = secretscan.scan(user_text, self.secret_allowlist)
            if matches:
                decision = self._secret_challenge(fe, "prompt", matches,
                                                  source_text=user_text)
                if decision == "stop":
                    fe.notify("stopped: secret detected — prompt not sent")
                    return
                elif decision == "redact":
                    user_text = secretscan.redact(user_text, matches)

        user_msg = {"role": "user", "content": user_text}
        self.messages.append(user_msg)
        self.session.log("user", text=user_text, model=model,
                         **({"bootstrap": True} if bootstrap else {}))

        cb = agent.AgentCallbacks(
            on_text=fe.on_text,
            on_tool_start=fe.on_tool_start,
            on_tool_result=lambda n, o: (fe.on_tool_result(n, o),
                                         self.session.log("tool", name=n, output=o[:4000])),
            approve=fe.approve,
            ask_continue=fe.ask_continue,
            notify=fe.notify,
            cancelled=fe.cancelled,
            on_usage=getattr(fe, "on_usage", None),
            # R47: label each pre-mutation snapshot with the causing prompt
            checkpoint=lambda tool: rewind.checkpoint(f"[{tool}] {user_text}"),
            on_request=getattr(fe, "on_request", None),
            # R58: None (feature off) short-circuits scanning in the agent loop
            secret_challenge=(lambda ctx, m, source_text="":
                              self._secret_challenge(fe, ctx, m, source_text=source_text))
                              if self.redact_secrets else None,
            secret_allowlist=self.secret_allowlist,
        )
        before = len(self.messages)
        turn = agent.run_turn(provider, model, self.messages, self.system, cb,
                              self.max_iterations, tools_enabled, self.web)
        # R95e: did this turn actually produce anything? The user message is
        # popped below when it didn't, which leaves messages[-1] pointing at
        # the PREVIOUS turn's assistant reply — logging that as a fresh
        # `assistant` event re-records an old answer, inflating /cost's turn
        # count (R92) and duplicating it in the markdown export.
        produced = len(self.messages) > before

        # a turn that produced NOTHING (provider error / interrupt before any
        # assistant output) leaves the user message dangling — the next send
        # would then stack two consecutive user turns, which most chat APIs
        # reject. The prompt stays in the session log; retyping re-sends it
        # cleanly.
        if self.messages and self.messages[-1] is user_msg:
            self.messages.pop()

        # token/cost accounting for the footer. A turn that errored before
        # any request completed reports 0/0 — keep the previous gauge value
        # rather than showing "ctx 0" while the (popped-user-msg) history
        # still holds the earlier conversation
        # ...and it is the LAST request's prompt + the LAST reply, never the
        # summed output: each earlier round's reply is already inside the
        # next round's prompt, so summing them overstates the gauge on every
        # multi-tool turn (R90d). turn.output_tokens (the sum) stays the
        # basis for COST below — that really is billed per round.
        if turn.input_tokens or turn.output_tokens:
            self._used = turn.input_tokens + turn.last_output_tokens
        if hasattr(provider, "cost"):
            # billed_input sums EVERY iteration's prompt — a multi-tool turn
            # pays for the context on each round, not just the last one
            self._cost += provider.cost(model, turn.billed_input, turn.output_tokens)
        # capture the final assistant text for /copy + session log
        if not produced:
            return   # nothing to log — see R95e above
        last = self.messages[-1] if self.messages else {}
        text = _assistant_text(last)
        self.session.log("assistant", text=text, model=model,
                         input_tokens=turn.input_tokens,
                         output_tokens=turn.output_tokens,
                         # what /cost reads (R92): billed_input is the real
                         # cost basis for a multi-iteration turn, input_tokens
                         # alone is only the last round's prompt
                         billed_input=turn.billed_input,
                         cached_input=turn.cached_input,
                         degraded=turn.degraded)

    def last_response(self) -> str:
        for m in reversed(self.messages):
            if m.get("role") == "assistant":
                return _assistant_text(m)
        return ""

    def nth_response(self, n: int) -> str:
        seen = [_assistant_text(m) for m in self.messages if m.get("role") == "assistant"]
        return seen[-n] if 0 < n <= len(seen) else ""

    # ── context management ──────────────────────────────────────────────
    def context_stats(self) -> ContextStats:
        model = self.current.get("model", "")
        if not model:
            # no model configured (possible after /model remove of the last
            # entry, R81) — don't build a keyless, URL-less provider on every
            # status render just to ask it for a limit it can't know (R90g)
            return ContextStats("", self._used, 0, self._cost, self.session.id)
        provider = self._provider_for(self.current)
        limit = self._context_limit_nonblocking(provider, model)
        known = bool(getattr(provider, "has_pricing", None)) and provider.has_pricing(model)
        return ContextStats(model, self._used, limit, self._cost,
                            self.session.id, cost_known=known)

    def _context_limit_nonblocking(self, provider, model: str) -> int:
        """The context limit for the gauge, WITHOUT ever blocking the caller
        (R95i).

        `context_stats()` is called by the TUI's `status()` — i.e. on the UI
        event-loop thread, on every render. For the `local` model the limit
        lookup is a live `/props` call behind an endpoint probe, so a backend
        that is down froze the whole app for ~6s each time the cache expired.
        `live_context_limit` already carried a comment about this class of
        freeze; only the remote half had been fixed.

        So: serve the cache immediately and refresh it on a daemon thread.
        The 120s TTL is unchanged (LlamaDesk can reload the same model at a
        different ctx, so a live n_ctx must not be cached forever) — only the
        waiting moved off the render path. A failed refresh caches the static
        fallback, so a down backend backs off for the TTL instead of spawning
        a probe per render.
        """
        import threading
        import time as _time
        key = (self._provider_key, model)
        cached = self._limit_cache.get(key)
        if cached and _time.time() - cached[1] < 120:
            return cached[0]

        with self._limit_pending_lock:
            already_pending = key in self._limit_pending
            if not already_pending:
                self._limit_pending.add(key)

        if not already_pending:

            def _refresh() -> None:
                try:
                    live = provider.context_limit(model)
                except Exception:
                    live = 0
                try:
                    fallback = provider.static_context_limit(model)
                except Exception:
                    fallback = 0
                self._limit_cache[key] = (live or fallback or 128_000,
                                          _time.time())
                self._limit_pending.discard(key)

            threading.Thread(target=_refresh, daemon=True).start()

        if cached:
            return cached[0]   # stale beats blocking
        try:
            return provider.static_context_limit(model)
        except Exception:
            return 128_000

    def clear(self) -> None:
        self.messages = []
        self._used = 0
        todo.clear()   # R93: the task list belongs to the conversation
        self.session.log("clear")

    def reset(self, cwd: str = ".") -> None:
        """/reset: wipe history and restore the bare base system prompt.
        Any project knowledge (e.g. a bootstrap prompt) must be reintroduced
        explicitly — the UI offers to re-run /bootstrap after."""
        self.clear()
        self.system = _base_system()
        self.session.log("reset")

    def compact_history(self) -> int:
        """/compact (R14): summarize the conversation with the CURRENT model
        and carry only the summary — a flatten barely shrinks anything, so it
        is the fallback, not the mechanism. Returns messages folded away."""
        n = len(self.messages)
        if not n:
            return 0
        transcript = compact.flatten_history(self.messages)
        summary = ""
        try:
            provider = self._provider_for(self.current)
            ask = ("Summarize this conversation for your own continued use. "
                   "Keep: decisions made, exact file paths and commands, open "
                   "tasks, constraints the user stated. Drop: pleasantries, "
                   "superseded attempts, full file dumps. Reply with ONLY the "
                   "summary.\n\n" + transcript)
            msg = [{"role": "user", "content": ask}]
            result = provider.turn(self.current.get("model", ""), msg,
                                   "", None, lambda _s: None, lambda: False)
            summary = (result.text or "").strip()
        except Exception:
            pass  # model unreachable → plain flatten below
        if summary:
            body = "[Summary of the earlier conversation:]\n\n" + summary
            self.messages = [{"role": "user", "content": body}]
        else:
            self.messages = [compact.flattened_as_user_message(self.messages)]
        # the gauge reflects the LAST turn's billed prompt size, which no
        # longer exists once history is folded — re-estimate from what
        # actually survives so it (and the >80% /compact hint) drop with it
        self._used = tokens.estimate_tokens(
            str(self.messages[0].get("content", "")))
        self.session.log("compact", folded=n, summarized=bool(summary))
        return n

    def provider_health(self, timeout: float = 4.0) -> dict:
        """Live health of the current model's backend, hard-bounded to
        `timeout` seconds. Both TUI and classic UI call this synchronously
        at startup, before anything is on screen — a socket/DNS stall deep
        in httpx (seen with certain LAN+VPN routing combos) can hang past
        its own per-request timeouts, and that must never freeze the whole
        app before it's even rendered once. The probing thread is abandoned
        (daemon) if it doesn't return in time; startup proceeds regardless."""
        import threading
        result: dict = {}

        def _run():
            result["h"] = self._provider_health_uncached()

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout)
        if "h" in result:
            return result["h"]
        return {"ok": False,
                "detail": f"health check timed out after {timeout:.0f}s "
                          "(startup not blocked)"}

    def _provider_health_uncached(self) -> dict:
        provider = self._provider_for(self.current)
        # openai-compat providers may have several endpoints configured; make
        # sure we test the one we would actually use for a request.
        if callable(getattr(provider, "pick_endpoint", None)):
            provider.pick_endpoint(cache_ok=False)
        # /props is llama.cpp-only and reports whatever's actually loaded on
        # the LOCAL backend — meaningless for a real remote model. A gateway
        # that unifies local + remote behind one LAN base_url (aurora-
        # gateway) means the base_url alone can't tell them apart anymore;
        # only the "local" sentinel (config's `model: local` entry) can, so
        # any other selected model must skip the /props probe entirely
        # instead of silently reporting the wrong (locally-loaded) model.
        if self.current.get("model") != "local":
            return {"ok": bool(provider.api_key),
                    "detail": "remote API (no health endpoint)"}
        try:
            import httpx
            from .providers.openai_compat import _is_bare_ip
            base = provider.base_url.removesuffix("/v1")
            headers = {"Authorization": f"Bearer {provider.api_key}"} \
                if provider.api_key else {}
            r = httpx.get(f"{base}/props", headers=headers, timeout=5,
                         verify=not _is_bare_ip(provider.base_url))
            r.raise_for_status()
            props = r.json()
            n_ctx = props.get("default_generation_settings", {}).get("n_ctx")
            model = (props.get("model_path") or "?").rsplit("/", 1)[-1]
            # degrade LOUDLY: a llama.cpp upgrade that moves n_ctx/model_path
            # in /props must read as "schema changed", never as a blank field
            # (see AURORA.md "Upgrade surfaces")
            if n_ctx is None or model == "?":
                return {"ok": True,
                        "detail": (f"{model} ready, ctx unknown — /props "
                                   "schema changed? (llama.cpp upgrade)")}
            return {"ok": True, "detail": f"{model} ready, ctx {n_ctx}"}
        except Exception as e:
            return {"ok": False, "detail": f"unreachable: {e}"}

    def set_redact_secrets(self, on: bool) -> None:
        self.redact_secrets = on
        persist_runtime_value(self.cfg, "redact_secrets", on)

    def _secret_challenge(self, fe: Frontend, context: str, matches: list,
                          source_text: str = "") -> str:
        """Wraps fe.secret_challenge to handle 'always': allowlist every
        matched value in this challenge, persist it, then treat the turn as
        'keep' — the caller (send()/agent loop) never sees a 4th outcome."""
        decision = fe.secret_challenge(context, matches, source_text=source_text)
        if decision == "always":
            self.add_secret_allowlist_entries(m.text for m in matches)
            fe.notify(f"allowlisted {len(matches)} value(s) — "
                     f"won't be flagged again")
            return "keep"
        return decision

    def add_secret_allowlist_entries(self, values) -> None:
        """Persist confirmed false positives as hashes (never raw values)."""
        self.secret_allowlist.update(secretscan.hash_value(v) for v in values)
        persist_runtime_value(self.cfg, "secret_allowlist",
                              sorted(self.secret_allowlist))

    def clear_secret_allowlist(self) -> None:
        self.secret_allowlist = set()
        persist_runtime_value(self.cfg, "secret_allowlist", [])

    def set_multiline(self, on: bool) -> None:
        self.multiline = on
        persist_runtime_value(self.cfg, "multiline", on)

    # ── context bootstrap + resume ──────────────────────────────────────
    def bootstrap_context(self, cwd: str = ".") -> bool:
        """agentic_context injection (R12). NOT called automatically — the
        agent knows nothing about .agentic_context unless the user's
        /bootstrap prompt (or an explicit caller) introduces it. Returns True
        when active; the system prompt is resent every turn (cached, R15)."""
        prompt = context.bootstrap(cwd)
        self.system = _base_system() + ("\n\n---\n\n" + prompt if prompt else "")
        if prompt:
            self.session.log("context_bootstrap", chars=len(prompt))
        return bool(prompt)

    def resume_from(self, session_id: str) -> int:
        """Rebuild plain-text history from a past session's JSONL (R20).
        Rebuilt as flat text (tool blocks aren't replayed), shaped for the
        current provider. Returns the number of turns restored."""
        past = Session(session_id)
        restored = 0
        msgs: list[dict] = []
        for r in past.iter_records():
            ev, text = r.get("event"), r.get("text", "")
            if ev not in ("user", "assistant") or not text:
                continue
            msgs.append({"role": ev if ev == "user" else "assistant",
                         "content": text})
            restored += 1
        if restored:
            self.messages = msgs
            self.session = past  # keep appending to the same log
            # the gauge would otherwise read 0 on a resumed session until the
            # first new turn, while a full history is already loaded (R90g).
            # Estimated, not exact: the real count only comes back with the
            # next provider response.
            # R96k: sum the lengths instead of materializing the whole
            # history as one joined string just to take len(...) // 4 — same
            # answer (a plain "".join adds no separator chars), without the
            # transient full-history copy on a long resumed session.
            total_chars = sum(len(str(m.get("content", ""))) for m in msgs)
            self._used = total_chars // 4
        return restored


def _assistant_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if b.get("type") == "text")
    return ""
