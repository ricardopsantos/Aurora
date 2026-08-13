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

from . import (
    agent,
    compact,
    config,
    extensions,
    keystore,
    rewind,
    tokens,
    tools,
)
from . import secrets as secretscan
from .config import load_config, persist_runtime_value
from .frontend import Frontend
from .providers import make_provider
from .providers.base import ProviderError
from .session import Session


# R156: how much of the window a compaction's INPUT may occupy. The fold has
# to leave room for the ask's own wrapper, the summary coming back, and the
# recent tail being kept — half the window is the conservative split that
# holds on a 65k local context as well as a 200k remote one.
_COMPACT_INPUT_FRACTION = 0.5
# Used only while the real limit is still unknown (`_context_limit_nonblocking`
# returns 0 until /props answers). Sized for the smallest context Aurora
# realistically runs against, since guessing high here is what breaks.
_COMPACT_INPUT_TOKENS_FALLBACK = 16_000
# R158: room reserved for the reply the summarization request generates and
# for the ask's own wrapper text — the budget below sizes the summary INPUT,
# and the output has to land somewhere too.
_COMPACT_HEADROOM_TOKENS = 4_000
# A floor, so a session whose kept tail nearly fills the window still gets a
# usable summary rather than a one-line stub. Deliberately small: at this
# point the tail is the context that matters, and the alternative to a short
# summary is no fold at all.
_COMPACT_MIN_SUMMARY_TOKENS = 2_000


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
    compactions: int = 0       # R159: folds so far THIS session, including
    # ones replayed from a resumed log — the status bar reads it live
    cost_known: bool = False   # only render the $ badge when this is True —
    # an unpriced model (local, or a remote model missing from
    # remote_context_limits.json) always has cost_usd == 0.0, which is
    # indistinguishable from "genuinely priced, $0 spent so far" without
    # this flag
    local_model: str | None = None   # what the "local" sentinel is actually
    # serving right now (live `/props` name, cached) — the status bar renders
    # `local: <name>`, since "local" alone doesn't say which model is loaded.
    # None whenever the answer isn't known yet or the model isn't "local".

    @property
    def pct(self) -> float:
        return (self.used / self.limit * 100) if self.limit else 0.0


class Engine:
    def __init__(self, config_path: str, session_id: str | None = None):
        self.cfg = load_config(config_path)
        self.session = Session(session_id)
        # R150e: coerce a YAML null. A bare `models:` line parses as None, and
        # `load_config`'s `cfg.setdefault("models", [])` does NOT replace it —
        # the key exists, so setdefault is a no-op. `_default_model`'s
        # `next(m for m in self.models …)` then raised TypeError during
        # construction: a crash at startup, on a hand-edited config.
        # `remove_model_entries` already guarded this exact None case, on one
        # side only.
        #
        # Also assigns THROUGH cfg rather than from a `.get()` default, so
        # `self.models` provably aliases `cfg["models"]` — the invariant
        # `add_model` depends on, since `config.persist_model_entry` appends
        # via `cfg.setdefault("models", []).append(...)`. That aliasing is not
        # currently breakable (load_config guarantees the key exists), so this
        # half is an invariant made explicit, not a bug being fixed.
        if not isinstance(self.cfg.get("models"), list):
            self.cfg["models"] = []
        self.models: list[dict] = self.cfg["models"]
        self.runtime = self.cfg.get("runtime", {})
        self.max_iterations = int(self.runtime.get("max_iterations", 5))
        self.web = bool(self.runtime.get("web_search", True))
        self.timeout = float(self.runtime.get("timeout", 300))
        tools.set_command_timeout(self.timeout)  # run_command honours it too (R90g)
        tools.set_parallel_tools(self.runtime.get("parallel_tools", True))  # R94
        # R119: extensions (bundled — e.g. MCP — + user's ~/.aurora/extensions/).
        # Loaded once per Engine lifetime: an MCP extension's register() may
        # spawn child server processes, which must not happen on every turn.
        self._mcp_manager = None
        ext_specs, ext_runners, ext_warnings = extensions.discover(self)
        # R187d: discover() guards import and register(), but this merge used
        # to run bare — so anything it raised escaped Engine construction and
        # Aurora refused to start over one bad extension file. set_extensions
        # now shape-checks its input; this is the outer net for the same
        # invariant (a broken extension costs its own tools, never the
        # session), since a future edit there shouldn't be able to break
        # startup either.
        try:
            ext_warnings += tools.set_extensions(ext_specs, ext_runners)
        except Exception as e:
            ext_warnings.append(
                f"extension tools could not be installed "
                f"({e.__class__.__name__}: {e}) — continuing without them")
            tools.set_extensions([], {})
        if self._mcp_manager is not None:
            ext_warnings += [f"mcp: {e}" for e in self._mcp_manager.errors]
        self.extension_warnings = ext_warnings
        self.redact_secrets = bool(self.runtime.get("redact_secrets", True))
        self.prompt_cache = bool(self.runtime.get("prompt_cache", True))  # R91
        # R118: silently fold OLDER history once context usage gets close to
        # the limit, instead of only ever showing the manual ">80%" hint and
        # letting a long session run into a hard context error.
        #
        # R154 turned this ON by default and moved the threshold to 80%. It
        # was opt-in ("it changes what the model can see without being
        # asked") and end-of-turn only, which is precisely the shape that
        # cannot save a long tool-heavy turn: the turn that overflows dies
        # BEFORE reaching the end-of-turn check, so the safety net only ever
        # ran on turns that didn't need it. A tool result the user never sees
        # accounted for is a worse surprise than a fold.
        self.auto_compact = bool(self.runtime.get("auto_compact", True))
        # R202: same invariant R200 put on the SETTERS, applied to the LOAD
        # path — which a hand edit reaches directly, and config.yaml is a file
        # Aurora explicitly invites the user to edit.
        self.auto_compact_threshold_pct = self._runtime_number(
            "auto_compact_threshold_pct", 80.0, float,
            lambda v: 0 < v <= 100, ">0 and <=100")
        self.compact_keep_recent_tokens = self._runtime_number(
            "compact_keep_recent_tokens", 20_000, int,
            lambda v: v > 0, "a positive number of tokens")
        # R58: hashes of confirmed false positives (secrets.hash_value) —
        # never the raw values, so config.yaml stays safe to commit/share
        self.secret_allowlist: set[str] = set(self.runtime.get("secret_allowlist", []))
        self.multiline = bool(self.runtime.get("multiline", False))
        # R162: on a hard provider failure, retry the SAME turn against the
        # next configured model with a usable key before giving up — off by
        # default, since silently switching models mid-session is a real
        # behavior change some setups don't want (e.g. a fixed-model CI job).
        self.model_fallback = bool(self.runtime.get("model_fallback", False))
        self.system = _base_system()
        self.messages: list[dict] = []
        self._used = 0
        self.compactions = 0   # R159
        # /diff: the shadow-repo checkpoint HEAD as of the START of the last
        # turn (None if no checkpoint existed yet) — set in `send()` before
        # the turn runs, so /diff can show exactly what THAT turn's approved
        # mutations changed, not the whole project's checkpoint history.
        self.last_turn_diff_base: str | None = None
        self._cost = 0.0
        # R171/I8: whether ANY of the accrued `_cost` came from a round with
        # real pricing — `context_stats`'s `cost_known` used to ask only
        # whether the CURRENT model is priced, so a real accrued $ figure
        # from an earlier model went invisible (badge just disappears) the
        # moment `/model` switched to an unpriced one mid-session, reading
        # as "this session cost $0" instead of "the current model's rounds
        # are unpriced, on top of $X already spent".
        self._cost_priced = False
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
        # R170j: same nonblocking-cache shape as _limit_cache/_limit_pending
        # above, for live_model_name() instead of live_context_limit() — the
        # /model picker used to call live_model_name() directly and
        # synchronously to label the "local" entry, which is a live /props
        # probe behind an endpoint pick; a dead/unreachable local server
        # froze picker construction for up to 4s. See
        # _live_model_name_nonblocking.
        self._live_name_cache: dict = {}
        self._live_name_pending: set = set()
        self._live_name_pending_lock = threading.Lock()
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
        or (for a local model with no config entry of its own, e.g. reloaded
        outside Aurora) the last provider's entry re-labelled. None when
        unknown or the key is gone."""
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
            # R145b: the provider being replaced owns a dict of pooled
            # httpx.Clients, one per endpoint, each holding live keep-alive
            # sockets. Dropping the reference never closed them. R96j already
            # closes the losing racer one level down in `_client_for`, for the
            # same reason — the whole provider needs it too, or every
            # cross-provider `/model` switch strands a pool of open fds.
            _close_provider(self._provider)
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

    def _runtime_number(self, key: str, default, cast, valid, hint: str):
        """R202: read one numeric `runtime:` setting, falling back to the
        default when it is unusable.

        R200 range-checked the setters, so `/autocompact 0` is refused — but
        `config.yaml` is hand-editable and its values are read straight into
        attributes here, so a hand edit walked right past that check and
        reinstated the same broken states (a 0 threshold auto-compacts every
        turn; one above 100 disables it while the UI reports it ON).

        Never raises. A non-numeric value used to crash `Engine.__init__`
        through the bare `float()`/`int()`, i.e. a typo in an editable config
        made Aurora unstartable — and an unstartable app is a worse answer to
        a bad setting than a corrected one. Reports through
        `extension_warnings`, the surface both frontends already print at
        startup, rather than inventing a second one."""
        raw = self.runtime.get(key, default)
        try:
            val = cast(raw)
        except (TypeError, ValueError):
            self.extension_warnings.append(
                f"config runtime.{key}: {raw!r} is not a number — "
                f"using {default}")
            return default
        if not valid(val):
            self.extension_warnings.append(
                f"config runtime.{key}: {val:g} must be {hint} — "
                f"using {default}")
            return default
        return val

    def set_prompt_cache(self, on: bool) -> None:
        self.prompt_cache = on
        persist_runtime_value(self.cfg, "prompt_cache", on)

    def set_auto_compact(self, on: bool) -> None:
        self.auto_compact = on
        persist_runtime_value(self.cfg, "auto_compact", on)

    def set_auto_compact_threshold_pct(self, pct: float) -> None:
        """R200: range-checked. R189 made this settable from a running
        session but accepted any float and PERSISTED it, so one typo broke
        auto-compact until the user hand-edited config.yaml back:

        - `0` or negative — the gate is `stats.pct < threshold`, so it never
          returns early and auto-compact folds history on EVERY turn, even at
          5% context, burning a summarization request each time.
        - anything above 100 — `pct` can't reach it, so auto-compact never
          fires again while the UI keeps reporting it ON. A safety mechanism
          silently off is worse than one that's visibly off.

        Raised as ValueError because `/autocompact`'s handler already catches
        it for a non-numeric argument; this just gives it a real reason."""
        if not 0 < pct <= 100:
            raise ValueError(
                f"threshold must be >0 and <=100 percent (got {pct:g})")
        self.auto_compact_threshold_pct = pct
        persist_runtime_value(self.cfg, "auto_compact_threshold_pct", pct)

    def set_compact_keep_recent_tokens(self, n: int) -> None:
        """R200: must be positive. `compact_history` treats
        `keep_recent_tokens=0` as the MANUAL `/compact` sentinel meaning
        "fold the entire history" — as a persisted AUTO value that means
        every auto-compact throws away the current turn too, which is the
        opposite of what the setting is for."""
        if n <= 0:
            raise ValueError(
                f"keep must be a positive number of tokens (got {n})")
        self.compact_keep_recent_tokens = n
        persist_runtime_value(self.cfg, "compact_keep_recent_tokens", n)

    def set_model_fallback(self, on: bool) -> None:
        self.model_fallback = on
        persist_runtime_value(self.cfg, "model_fallback", on)

    def _fallback_models(self) -> list[dict]:
        """Other configured models with a usable key, in `config.yaml`'s
        `models:` order — same list `/model`'s picker walks — excluding
        whichever one is current."""
        cur = self.current.get("model")
        return [m for m in self.valid_models() if m.get("model") != cur]

    def valid_models(self) -> list[dict]:
        """Configured models whose provider has a key available (no prompt)."""
        return [m for m in self.models if self._has_key(m.get("provider"))]

    # ── the turn ────────────────────────────────────────────────────────
    def _run_turn_with_fallback(self, provider, model, cb, tools_enabled, fe):
        """R162: run the turn; on a hard provider failure and `model_fallback`
        is on, retry the SAME messages against the next configured model with
        a usable key instead of failing the turn outright. Two failure shapes
        both count: a `ProviderError` that escapes `run_turn` (R143a's
        corrective-retry path), and the more common case where `run_turn`
        catches it internally, notifies, and returns a turn that produced
        NOTHING (`len(self.messages)` unchanged — the first request never
        got a reply). A failure part-way through a multi-round turn is left
        alone: messages already grew, so the turn made real progress on the
        current model and switching underneath it would be the surprising
        move, not the safe one.

        `self.current`/`self.system` stay on the model that actually
        answered — a silent switch, same as any other `/model` pick, logged
        so `/context` shows where the turn really ran."""
        candidates = [None] + (self._fallback_models() if self.model_fallback
                               else [])
        before = len(self.messages)
        last_error: BaseException | None = None
        turn = None
        for i, entry in enumerate(candidates):
            if entry is not None:
                fe.notify(f"· {model} failed"
                         + (f" ({last_error.__class__.__name__}: {last_error})"
                            if last_error else "")
                         + f" — falling back to {entry.get('model')}")
                self.switch_model(entry)
                provider = self._provider_for(self.current, interactive=True)
                provider.extra_body = self.current.get("extra_body") or {}
                provider.on_think = getattr(fe, "on_think", None)
                provider.notify = fe.notify
                provider.cache_prompt = self.cache_enabled()
                model = self.current.get("model", "")
            last = (i == len(candidates) - 1)
            try:
                turn = agent.run_turn(provider, model, self.messages,
                                      self.system, cb, self.max_iterations,
                                      tools_enabled)
            except ProviderError as e:
                last_error = e
                if last:
                    raise
                continue
            if turn.cancelled:
                # R171: a user cancel (Ctrl+C / Esc) also leaves `messages`
                # unchanged, same shape as a dead provider — but it means
                # "stop", not "retry elsewhere". Falling back here would fire
                # a brand-new request against another model behind the
                # user's back, the opposite of what cancel means.
                return turn
            if len(self.messages) > before:
                return turn   # made progress — stop here, even if imperfect
            if last:
                return turn   # nothing left to fall back to
            last_error = ProviderError(
                (turn.events[-1].get("error") if turn.events else None)
                or "no reply")
        return turn

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
        provider.notify = fe.notify
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
        # /diff: capture the checkpoint HEAD as it stood BEFORE this turn's
        # first mutation — must happen here, before run_turn, not after
        self.last_turn_diff_base = rewind.head(".")

        cb = agent.AgentCallbacks(
            on_text=fe.on_text,
            on_tool_start=fe.on_tool_start,
            # R133b: `output` stays truncated (a 60KB result must not be
            # written twice), so `chars` carries the REAL size — without it a
            # capped result and a 4000-char one are indistinguishable on disk.
            on_tool_result=lambda n, o: (
                fe.on_tool_result(n, o),
                self.session.log("tool", name=n, output=o[:4000],
                                 chars=len(o),
                                 status=tools.result_status(o)),
                # R154: the result is in history now — count it now too
                self._count_tool_output(fe, len(o))),
            approve=fe.approve,
            ask_continue=fe.ask_continue,
            notify=fe.notify,
            cancelled=fe.cancelled,
            # R154: routed through the engine so the context gauge tracks the
            # live number mid-turn, not just at the end; still forwards to
            # the frontend's own on_usage exactly as before
            on_usage=lambda i, o, c=0: self._live_usage(fe, i, o, c),
            maybe_compact=lambda: self._maybe_auto_compact_mid_turn(fe),
            # R133c: session.py has always claimed approvals were logged; they
            # never were. Every gate outcome now lands in the JSONL, including
            # the allowlisted ones that are never asked about.
            on_approval=lambda tool, decision, detail: self.session.log(
                "approval", tool=tool, decision=decision,
                **({"detail": detail} if detail else {})),
            # R47: label each pre-mutation snapshot with the causing prompt.
            # R181: ALSO snapshot the single target file (write_file/
            # edit_file/apply_patch all take `path`) regardless of whether
            # it's inside this checkpointed tree — see rewind.
            # snapshot_before_write's docstring for why the whole-tree
            # checkpoint alone isn't enough.
            checkpoint=lambda tool, args: (
                rewind.checkpoint(f"[{tool}] {user_text}"),
                rewind.snapshot_before_write(str(args["path"]))
                if args.get("path") else None),
            on_request=getattr(fe, "on_request", None),
            # R58: None (feature off) short-circuits scanning in the agent loop
            secret_challenge=(lambda ctx, m, source_text="":
                              self._secret_challenge(fe, ctx, m, source_text=source_text))
                              if self.redact_secrets else None,
            secret_allowlist=self.secret_allowlist,
        )
        before = len(self.messages)
        try:
            turn = self._run_turn_with_fallback(
                provider, model, cb, tools_enabled, fe)
        except BaseException:
            # R143a: `run_turn` deliberately lets some exceptions out — a
            # ProviderError raised by the corrective retry INSIDE `except
            # MalformedToolCall` (the outer handler can't see it), and the
            # bare re-raise when tools are already degraded. Those skipped
            # the dangling-user-message cleanup below, leaving `messages`
            # ending on a `user` entry; the next send appended a second one,
            # and two consecutive user turns is exactly what R44/R95e/R128
            # all exist to prevent — most APIs reject the request outright,
            # one turn away from the cause.
            if self.messages and self.messages[-1] is user_msg:
                self.messages.pop()
            raise
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
        # R163: cost is now accrued LIVE, per round, inside `_live_usage` —
        # no end-of-turn addition here (it would double-count what streaming
        # already added; see `_live_usage`'s docstring for why the two are
        # mathematically the same total, not just close).
        # capture the final assistant text for /copy + session log
        if not produced:
            return   # nothing to log — see R95e above
        # R143b: on every early-return path (`_flush()` then `return turn` —
        # iteration cap, "stop" at the approval gate, interrupt, secret-stop)
        # the LAST message is a `tool` result, not the reply. `_assistant_text`
        # happily returns its string content, so the session recorded
        # `assistant text="[skipped: user stopped the turn]"` — the skip
        # marker shown as the model's answer, in the markdown export and,
        # worse, replayed as a real assistant message by `--continue`.
        # Take the last actual ASSISTANT message instead (on a normal turn
        # that IS `messages[-1]`, so nothing changes); the record is still
        # written, because the tokens were really spent and /cost reads it.
        last = next((m for m in reversed(self.messages)
                     if m.get("role") == "assistant"), {})
        text = _assistant_text(last)
        self.session.log("assistant", text=text, model=model,
                         input_tokens=turn.input_tokens,
                         output_tokens=turn.output_tokens,
                         # what /cost reads (R92): billed_input is the real
                         # cost basis for a multi-iteration turn, input_tokens
                         # alone is only the last round's prompt
                         billed_input=turn.billed_input,
                         cached_input=turn.cached_input,
                         # R133a: a SUBSET of output_tokens, never additive.
                         # reasoning_chars is the streamed fallback for
                         # backends that report no reasoning_tokens.
                         reasoning_tokens=turn.reasoning_tokens,
                         reasoning_chars=turn.reasoning_chars,
                         # /model picker (feature request, 2026-07-27): the
                         # last successful request's wall time this turn —
                         # session.last_latency_by_model() reads it back so
                         # the picker can show "how fast is this model right
                         # now" without a live probe.
                         latency_s=round(turn.last_request_latency, 3),
                         degraded=turn.degraded,
                         # R171: `provider.extra_body` is a plain mutable
                         # attribute on a shared provider object — a
                         # `tools.set_extensions` runner holding a reference
                         # to `engine` can mutate it for a LATER turn with no
                         # trace anywhere. Logging the keys (not values, which
                         # could carry secrets) at least makes an
                         # extension-injected payload visible after the fact.
                         extra_body_keys=sorted(self._provider.extra_body)
                         if getattr(self._provider, "extra_body", None)
                         else [])
        self._maybe_auto_compact(fe)

    def _live_usage(self, fe: Frontend, input_tokens: int,
                    output_tokens: int, cached_tokens: int = 0) -> None:
        """R154 (task 1): move the context gauge to the REAL number as each
        round reports it, instead of once when the whole turn is over.

        The gauge read `self._used`, which was assigned in exactly one place
        — after `run_turn` returned. So for the entire duration of a
        tool-heavy turn the status bar showed the size of the PREVIOUS turn's
        prompt while the live context climbed past it, and the first sign of
        trouble was the provider rejecting the request. Same basis as the
        end-of-turn assignment (this round's prompt + this round's reply, not
        the summed output — each earlier reply is already inside the next
        round's prompt, R90d), so the number the user watches during a turn
        and the number left behind after it agree.

        The `or` guard matters: a provider that reports no usage for a round
        must not blank a gauge that was correct — keep the last real value.

        R163: `self._cost` is accrued HERE too, per round, instead of once
        at the end of `send()` — the same move R154 made for `self._used`,
        for the same reason (a tool-heavy turn ran the whole request before
        the user saw a dollar figure move). Summing `provider.cost(round_in,
        round_out)` across every round equals `provider.cost(sum_in,
        sum_out)` — pricing is linear in tokens (`openai_compat.py`'s
        `cost()`) — so this is the exact same total the old end-of-turn line
        computed via `turn.billed_input`/`turn.output_tokens`, just visible
        as it happens instead of only after.

        R192 kept that invariant: cache reads add a THIRD linear term
        (`turn.cached_input` sums the same per-round numbers passed here), so
        per-round summing still equals the whole-turn figure. It matters that
        the split is applied per round rather than to the turn total — the
        first round of a turn is usually a cache MISS and later ones hits, and
        only the per-round numbers know which was which."""
        if input_tokens or output_tokens:
            self._used = input_tokens + output_tokens
            if hasattr(self._provider, "cost"):
                model = self.current.get("model", "")
                self._cost += self._provider.cost(
                    model, input_tokens, output_tokens, cached_tokens)
                if (getattr(self._provider, "has_pricing", None)
                        and self._provider.has_pricing(model)):
                    self._cost_priced = True
        on_usage = getattr(fe, "on_usage", None)
        if on_usage is not None:
            on_usage(input_tokens, output_tokens)

    def _count_tool_output(self, fe: Frontend, chars: int) -> None:
        """R154 (task 1, second half): a tool result enters history NOW but
        is only *measured* by the provider on the next round's prompt. A
        single 60KB result (`tools.TOOL_OUTPUT_LIMIT`) is ~15k tokens that
        the gauge, and therefore the auto-compact check, could not see until
        after the request carrying it had already been built and rejected.

        Estimated, not authoritative — it's replaced by the provider's real
        count on the next `_live_usage`. Erring high is the useful direction:
        it makes the fold happen a round early rather than a round late."""
        self._used += tokens.estimate_tokens_from_chars(chars)
        invalidate = getattr(fe, "invalidate_status", None)
        if invalidate is not None:
            invalidate()

    def _maybe_auto_compact_mid_turn(self, fe: Frontend) -> None:
        """R154 (task 2): the same fold as `_maybe_auto_compact`, but at a
        round boundary inside a running turn — called by `agent.run_turn` via
        `AgentCallbacks.maybe_compact` before it builds each request after
        the first.

        Why this is where it belongs: the failure being fixed is a turn that
        overflows the window BEFORE it finishes, so nothing that runs after
        the turn can help. The end-of-turn check (R118) stays as-is for the
        session that creeps up over many small turns.

        Self-limiting by construction, which is why there's no attempt
        counter: after a successful fold, everything foldable has become the
        summary merged into `messages[0]`, so `compact.cut_index` finds no
        older-than-the-tail region and `compact_history` returns 0 — BEFORE
        spending a summarization request. A turn whose own recent tail is
        what blew the window therefore pays for one summarization at most,
        not one per round."""
        if not self.auto_compact:
            return
        stats = self.context_stats()
        if not stats.limit or stats.pct < self.auto_compact_threshold_pct:
            return
        folded = self.compact_history(
            keep_recent_tokens=self.compact_keep_recent_tokens, notify=fe.notify)
        if folded:
            fe.notify(f"auto-compact: context was at {stats.pct:.0f}% mid-task "
                     f"— folded {folded} older message(s) and continued")

    def _maybe_auto_compact(self, fe: Frontend) -> None:
        """R118: fold older history once usage crosses
        `auto_compact_threshold_pct` — deliberately above the manual ">80%"
        hint's threshold, so a user who acts on that hint never even notices
        this firing; it's the safety net for the session that keeps going
        past it. `context_stats()` never blocks (R95i): `limit == 0` just
        means the real limit isn't known yet, so this quietly no-ops rather
        than guessing."""
        if not self.auto_compact:
            return
        stats = self.context_stats()
        if not stats.limit or stats.pct < self.auto_compact_threshold_pct:
            return
        folded = self.compact_history(
            keep_recent_tokens=self.compact_keep_recent_tokens, notify=fe.notify)
        if folded:
            fe.notify(f"auto-compact: folded {folded} older message(s) — "
                     f"context was at {stats.pct:.0f}%")

    def last_response(self) -> str:
        for m in reversed(self.messages):
            if m.get("role") == "assistant":
                return _assistant_text(m)
        return ""

    def last_prompt(self) -> str:
        """The user text that started the last turn — R124's `/copy-last`
        includes it alongside thinking + the response. A "user" message's
        `content` is always a plain string here (never the tool_calls shape
        assistant messages can have), so no `_assistant_text`-style
        unwrapping is needed."""
        for m in reversed(self.messages):
            if m.get("role") == "user":
                content = m.get("content")
                return content if isinstance(content, str) else ""
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
            return ContextStats("", self._used, 0, self._cost, self.session.id,
                                compactions=self.compactions)
        provider = self._provider_for(self.current)
        limit = self._context_limit_nonblocking(provider, model)
        known = (bool(getattr(provider, "has_pricing", None))
                and provider.has_pricing(model)) or self._cost_priced
        # nonblocking by construction (see `_live_model_name_nonblocking`) —
        # this runs on the render path, once per status repaint
        live = (self._live_model_name_nonblocking(provider)
                if model == "local" else None)
        return ContextStats(model, self._used, limit, self._cost,
                            self.session.id, compactions=self.compactions,
                            cost_known=known, local_model=live)

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
        The 120s TTL is unchanged (the server can be reloaded with a
        different ctx outside Aurora, so a live n_ctx must not be cached
        forever) — only the waiting moved off the render path. A failed
        refresh caches the static fallback, so a down backend backs off for
        the TTL instead of spawning a probe per render.
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

    def _live_model_name_nonblocking(self, provider) -> str | None:
        """R170j: same nonblocking-cache shape as `_context_limit_nonblocking`
        above, for `live_model_name()` — the `/model` picker used to call it
        directly and synchronously to label the "local" entry, which is a
        live `/props` call behind an endpoint pick. A dead/unreachable local
        server made every `/props` GET wait out its own 4s timeout, freezing
        picker construction before the menu could even render. Serves the
        cache immediately (`None` on the very first call, same as before —
        the picker just shows "local" with no live name that one time) and
        refreshes on a daemon thread; TTL matches `_limit_cache`'s 120s
        since both track the same "is the local server up, and what's
        loaded" fact."""
        import threading
        import time as _time
        if not hasattr(provider, "live_model_name"):
            return None
        key = self._provider_key
        cached = self._live_name_cache.get(key)
        if cached and _time.time() - cached[1] < 120:
            return cached[0]

        with self._live_name_pending_lock:
            already_pending = key in self._live_name_pending
            if not already_pending:
                self._live_name_pending.add(key)

        if not already_pending:

            def _refresh() -> None:
                try:
                    name = provider.live_model_name()
                except Exception:
                    name = None
                self._live_name_cache[key] = (name, _time.time())
                self._live_name_pending.discard(key)

            threading.Thread(target=_refresh, daemon=True).start()

        return cached[0] if cached else None

    def clear(self) -> None:
        self.messages = []
        self._used = 0
        self.session.log("clear")

    def reset(self, cwd: str = ".") -> None:
        """/reset: wipe history and restore the bare base system prompt.
        Any project knowledge (e.g. a bootstrap prompt) must be reintroduced
        explicitly — the UI offers to re-run /bootstrap after."""
        self.clear()
        self.system = _base_system()
        self.session.log("reset")

    def compact_history(self, keep_recent_tokens: int = 0, notify=None) -> int:
        """/compact (R14) and auto-compact (R118): summarize OLDER history
        with the CURRENT model and carry only the summary — a flatten barely
        shrinks anything, so it is the fallback, not the mechanism.

        `notify`, if given, is wired onto the summarization request's
        provider (R171m) so a connection retry during this request is
        visible the same way a normal turn's retry is — this call site was
        missed when `provider.notify` was introduced, so a retry here
        stayed silent. Every caller now passes `fe.notify`; `None` stays a
        safe default since the provider's own default is `None`.

        `keep_recent_tokens=0` (the manual `/compact` default, unchanged
        behavior): fold the ENTIRE history — a deliberate "start fresh from
        a summary" action, not a trim.

        `keep_recent_tokens > 0` (auto-compact): fold only messages old
        enough to fall outside that budget, via `compact.cut_index` — auto-
        compact fires mid-session without being asked, so losing fidelity on
        the turns you're most likely still actively using would defeat the
        point; only the recent-tail turns are excluded from what's already
        the fallback path.

        Returns messages folded away (0 if nothing needed folding)."""
        n = len(self.messages)
        if not n:
            return 0
        cut = compact.cut_index(self.messages, keep_recent_tokens) \
            if keep_recent_tokens else n
        older, recent = self.messages[:cut], self.messages[cut:]
        if not older:
            return 0
        transcript = compact.flatten_history(older)
        # R156: both the request below and the fallback under it have to FIT.
        # The case auto-compact exists for is a history already bigger than
        # the window, so an unclipped summarization request is rejected with
        # the very `exceed_context_size_error` that triggered the fold, and
        # the unclipped fallback carries back exactly what it removed — a
        # "fold" that frees nothing. Budget is a fraction of the real window
        # so the ask, the reply and the kept tail all still fit; when the
        # limit isn't known yet (`_context_limit_nonblocking` → 0) fall back
        # to a figure safe on the smallest local context Aurora targets.
        # ONE `_provider_for` for both the budget and the request: it is the
        # call that can raise (no key, no model configured) and the call a
        # turn is charged for, so asking twice would both double-build the
        # provider and misreport how many summarizations a fold costs.
        budget = _COMPACT_INPUT_TOKENS_FALLBACK
        summary = ""
        try:
            provider = self._provider_for(self.current)
            provider.notify = notify
            limit = self._context_limit_nonblocking(
                provider, self.current.get("model", ""))
            if limit:
                # R158: budget against what the NEXT request actually carries,
                # not against the window alone. A flat fraction ignored the two
                # things sitting beside the summary — the system prompt (~12k
                # on a bootstrapped session) and the raw tail being kept — so
                # "half the window" could still add up to more than the window.
                # Size the summary to the room genuinely left over, and keep
                # the fraction as a ceiling so a huge window doesn't produce an
                # absurdly long summary.
                room = (limit
                        - tokens.estimate_tokens(self.system or "")
                        - sum(tokens.estimate_tokens(str(m.get("content", "")))
                              for m in recent)
                        - _COMPACT_HEADROOM_TOKENS)
                budget = max(_COMPACT_MIN_SUMMARY_TOKENS,
                             min(int(limit * _COMPACT_INPUT_FRACTION), room))
            ask = ("Summarize this conversation for your own continued use. "
                   "Keep: decisions made, exact file paths and commands, open "
                   "tasks, constraints the user stated. Drop: pleasantries, "
                   "superseded attempts, full file dumps. Reply with ONLY the "
                   "summary.\n\n" + compact.clip_transcript(transcript, budget))
            msg = [{"role": "user", "content": ask}]
            result = provider.turn(self.current.get("model", ""), msg,
                                   "", None, lambda _s: None, lambda: False)
            summary = (result.text or "").strip()
        except Exception:
            pass  # model unreachable → clipped flatten below
        if summary:
            body = "[Summary of the earlier conversation:]\n\n" + summary
        else:
            # clipped, NOT verbatim: this path runs precisely when the model
            # couldn't be reached, which includes "the window is already
            # blown", so it is the one that most needs to actually shrink.
            body = compact.clip_transcript(
                compact.flattened_as_user_message(older)["content"], budget)
        if recent and recent[0].get("role") == "user":
            # bug fix: when recent[0] is a "user" message, prepending a
            # separate {"role": "user", ...} summary message in front of it
            # produced two consecutive user turns, which providers that
            # enforce strict user/assistant alternation (Anthropic-family
            # models via OpenRouter) reject outright on the next request.
            # Folding the summary into recent[0]'s own content instead keeps
            # the sequence strictly alternating.
            merged_first = dict(recent[0])
            merged_first["content"] = body + "\n\n" + merged_first.get("content", "")
            # R154: IN PLACE (`[:]`), not a rebind. `agent.run_turn` is handed
            # `self.messages` and mutates that same list object for the whole
            # turn, so once compaction can happen mid-turn, rebinding here
            # would silently split them: the engine would hold the folded
            # history while the loop kept appending to — and sending — the
            # unfolded one, i.e. the compaction would have no effect on the
            # very requests it exists to shrink.
            self.messages[:] = [merged_first] + recent[1:]
        elif recent:
            # R156: mid-turn, `cut_index` may legally cut at an assistant
            # message that OPENS a tool round (`is_cut_boundary`). The
            # summary must NOT be merged into that message's content — it
            # would put the summary in the assistant's own mouth and sit
            # beside the `tool_calls` its `tool` results answer. A separate
            # user message in front of it is both correct and still strictly
            # alternating (user -> assistant -> tool), which is exactly the
            # shape the alternation fix above exists to protect.
            self.messages[:] = [{"role": "user", "content": body}] + recent
        else:
            self.messages[:] = [{"role": "user", "content": body}]
        # the gauge reflects the LAST turn's billed prompt size, which no
        # longer exists once history is folded — re-estimate from what
        # actually survives so it (and the >80% /compact hint) drop with it
        before = self._used
        # R158: the system prompt counts. `_used` is otherwise set from the
        # provider's own input_tokens (`_live_usage`), which BILLS the system
        # prompt — so re-estimating from messages alone silently dropped it
        # here, and a long `/bootstrap` prompt plus its own tool-driven
        # project orientation can easily run several thousand tokens. The
        # gauge then read low, `_maybe_auto_compact_mid_turn` saw headroom
        # that did not exist, and the next request went out over the limit.
        # Counting it here keeps the two sources of `_used` measuring the
        # same thing.
        self._used = (tokens.estimate_tokens(self.system or "")
                      + sum(tokens.estimate_tokens(str(m.get("content", "")))
                            for m in self.messages))
        # R134a: the whole point of a fold is the drop, and until now the only
        # record of it was a message COUNT. `/context`'s spine is context
        # size, so a fold logged without its before/after renders as a gap in
        # the one place a reader is looking for the descent.
        # R159: incremented HERE, beside the log record it must agree with —
        # this is the one point that knows a fold actually happened. Every
        # early `return 0` above (nothing foldable) correctly skips it.
        self.compactions += 1
        self.session.log("compact", folded=len(older), summarized=bool(summary),
                         kept_recent=len(recent),
                         used_before=before, used_after=self._used)
        return len(older)

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
        # R215: `_provider_for` returns None when no model is configured —
        # reachable via `/model remove` of the last entry (R81), or a
        # `models: []` config. `context_stats` already guards this exact
        # case; this path did not, so the startup health probe died with an
        # AttributeError on its own daemon thread and dumped a traceback to
        # stderr before the banner. Nothing caught it because nothing was
        # meant to: the probe is fire-and-forget.
        if provider is None:
            # R216: two DIFFERENT causes reach here, and R215 reported both as
            # "no model configured" — which the banner rendered beside the
            # model's own name as `model v/m  ✘ no model configured`, a line
            # that contradicts itself. `make_provider` also returns None when a
            # model names a `provider:` the config doesn't define.
            if not self.current.get("model"):
                return {"ok": False, "detail": "no model configured"}
            missing = self.current.get("provider")
            return {"ok": False,
                    "detail": f"provider {missing!r} not in config.yaml"
                              if missing else "model has no provider set"}
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
                    "detail": "remote API"}
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
            # (see documents/CHANGELOG_TECHNICAL.md "Upgrade surfaces")
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

    # ── resume ────────────────────────────────────────────────────────────
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
            role = ev if ev == "user" else "assistant"
            # R128: never restore two consecutive messages of the same role.
            # `send()` logs its `user` event unconditionally, but R95e
            # deliberately skips the `assistant` log when a turn produced
            # nothing (provider error, interrupt, cancel) — so a failed turn
            # followed by a retry leaves two adjacent `user` records in the
            # log, and replaying them verbatim rebuilds exactly the invalid
            # sequence R44/R95e/R125 each prevent on the LIVE path. Providers
            # that enforce strict role alternation (Anthropic-family models
            # via OpenRouter) reject it on the first send after --continue.
            # Merge rather than drop: the earlier prompt is real context the
            # user typed, and losing it silently would be its own surprise.
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n\n" + text
                continue
            msgs.append({"role": role, "content": text})
            # R171: count TURNS (one per user message), not messages — the
            # caller (`__main__`'s "(N turns)" print) means a user/assistant
            # exchange, and counting both roles doubled it.
            if role == "user":
                restored += 1
        # R170c: a session that crashed mid-turn (provider error, kill -9,
        # power loss) logs its `user` record and never reaches the
        # `assistant` reply, so the log's last record is a dangling `user`.
        # R128 above only merges CONSECUTIVE same-role records within this
        # replay — it has nothing to merge here, since there's no second
        # `user` record yet. Left alone, `send()` appends its own new `user`
        # message unconditionally, producing two adjacent `user` turns that
        # strict-alternation providers (Anthropic-family, via OpenRouter)
        # reject on the very first request after `--continue`. Drop it
        # before restoring — the prompt is still in the log for `/context`
        # or a human reading the JSONL, just not replayed into live history
        # where it would never get an answer anyway.
        had_any = restored > 0
        if msgs and msgs[-1]["role"] == "user":
            msgs.pop()
            restored -= 1
        if had_any:
            # still resume the SESSION (keep appending to the same log) even
            # if the only record was the dangling prompt just dropped above
            # — restarting into a brand-new session id would silently fork
            # the log the user thinks they're continuing.
            self.messages = msgs
            self.session = past
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
            # R159: a resumed session keeps appending to the SAME log, so its
            # earlier folds are part of this session's count — a `--continue`
            # that reported 0 after ten folds would make the counter mean
            # "since this process started", which is not what the bar claims.
            # Counted from the log rather than carried in state because the
            # log is the only thing that survives the restart.
            self.compactions = sum(1 for r in past.iter_records()
                                   if r.get("event") == "compact")
            # R171: seed the live $ gauge from the resumed session's own
            # logged usage — otherwise `self._cost` (constructor default 0.0)
            # only counts spend from turns run AFTER --continue, while
            # `/context <id>` (which reads the same log fresh each time)
            # shows the session's true total. Same pricing basis as
            # `/context`'s per-model breakdown, so the two never disagree.
            from .ctxtree import model_breakdown_lines
            from .providers.openai_compat import price_for
            from .session import usage_by_model
            usage = usage_by_model(session_id)
            _, self._cost = model_breakdown_lines(usage)
            self._cost_priced = any(price_for(m) for m in usage)
        return restored


def _close_provider(provider) -> None:
    """Release a discarded provider's pooled HTTP connections (R145b). Must
    never raise: this runs on the `/model` switch path, and failing to tidy
    up an old provider is not a reason to fail building the new one."""
    for client in getattr(provider, "_http", {}).values():
        try:
            client.close()
        except Exception:
            pass


def _assistant_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if b.get("type") == "text")
    return ""
