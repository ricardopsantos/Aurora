# Aurora — Architecture

Reference doc for how the pieces fit together. `AURORA.md` is the numbered
requirements/spec (the *what* and *why*, R1–R101+); this is the *how* — module
map, data flow, boundaries, and the mechanisms worth understanding before
touching them. Update this file whenever a change alters one of these shapes,
not just when adding a requirement.

## 1. The two halves: Engine and Frontend

Aurora is built around one hard boundary, enforced by a test
(`tests/test_architecture.py` greps imports):

```
UI (tui.py / ui.py)  ──drives──>  Engine (engine.py)  ──drives──>  Provider (providers/*)
      ^                                  |
      └──────── Frontend protocol ───────┘
         (engine calls back INTO the UI)
```

- **`engine.py`** owns ALL conversation state: provider/model selection,
  `messages` history, session, config, cost/token accounting. It **never**
  imports a UI toolkit (no `prompt_toolkit`, no `print`, no `input`) and never
  touches the terminal directly. Every side effect the engine needs from a
  human goes through a `Frontend`.
- **`frontend.py`** defines the `Frontend` Protocol — the ONLY vocabulary
  shared between the two halves: `on_text`, `on_think`, `on_tool_start`,
  `on_tool_result`, `notify`, `approve`, `ask_continue`, `ask_secret`,
  `secret_challenge`, `cancelled`. If the engine ever needs a new kind of
  human interaction, it is added here first, then every front end
  implements it.
- **Two front ends today**, both implementing `Frontend`: `tui.py` (full-screen
  `prompt_toolkit` Application) and `ui.py` (classic inline REPL, used for
  `--classic` and non-tty/pipe/CI). **`tui.TuiFrontend` subclasses
  `ui.TerminalFrontend`** — most of the interaction logic (approve/ask_continue/
  secret_challenge text and option lists) lives once in `ui.py`; the TUI only
  overrides what actually needs a different *render* (see §4).

**Why this matters**: a third front end (HTML/websocket, say) is "write a new
`Frontend` implementation," not "refactor the engine." Conversely, anything
that feels like it needs `print()` or a widget inside `engine.py` or
`agent.py` is a sign the abstraction is leaking — route it through a new
`Frontend` method instead.

**The guard is only as good as its AST check (R90a).** A *relative* import —
`from .ui import estimate_tokens` inside `engine.py` — parses as
`module="ui", level=1`, so the original `node.module.endswith(".ui")` test
never saw it, and exactly that import sat in the engine for weeks while the
suite stayed green. The check now rebuilds the dotted name from `node.level`
and also inspects plain `import` statements. The general lesson: when a
shared helper is wanted on both sides of the boundary, it goes into a
neutral engine-side module (that's what `tokens.py` is — `estimate_tokens`
/`fmt_token_count`, re-exported from `ui` for the existing call sites),
never imported *out of* the UI half.

## 2. The agent loop (`agent.py`)

`run_turn(provider, model, messages, system, cb: AgentCallbacks, ...)` is the
model⇄tool loop, driven entirely by callbacks (`AgentCallbacks`), so it's
UI-agnostic and directly unit-testable with a fake provider + fake callbacks
(see `tests/test_core.py::FakeProvider`/`_cb`). One call = one user turn = a
`while True` loop that:

1. Calls `provider.turn(...)` (streams text via `cb.on_text`).
2. If the response has tool calls: for each one — approval gate if needed
   (`tools.NEEDS_APPROVAL`), R58 secret scan (see §5), run the tool, R58 scan
   the OUTPUT, then append the (call, output) pair to `round_out`.
3. Flushes `round_out` as history messages (`_flush()` — one message per
   call via `tool_result_message()`; a provider MAY instead implement the
   optional `tool_results_messages()` hook to bulk-flush a round as one
   message, if its API needs that shape for parallel tool results — no
   current provider does).
4. Loops until the model stops calling tools (final answer) or a control
   signal fires: cancelled, iteration cap hit and user says stop, approval
   says stop, or a secret challenge says stop.

**Mutates `messages` in place** — the caller (`Engine.send`) passes its own
`self.messages` list, so history persists across turns without the agent loop
knowing anything about session/engine state.

**Step 2 runs read-only calls concurrently (R94), and that changes nothing
observable.** Before the sequential per-call loop, every call whose name is
in `tools.PARALLEL_SAFE` is dispatched at once through
`tools.run_tools_parallel`; the loop then consumes those results in the
model's original order. The invariant to preserve when touching this: only
the *waiting* is parallel. Tool starts, approvals, secret challenges, the
transcript and the history messages all stay strictly ordered — a user must
never be asked two questions at once, and `messages` must never depend on
which read finished first. `PARALLEL_SAFE` is an explicit allowlist, not
`set(all) - NEEDS_APPROVAL`: the test is "read-only AND no shared state", so
an ungated but stateful tool (`todo_write`, R93) is correctly excluded.

**Degrade paths are deliberate, not incidental**: a malformed tool call gets
one corrective retry, then the model degrades to chat-only for the rest of
the session (`turn.degraded`); a `ProviderError` is inspected for known
substrings ("timed out", "context" + "exceed", …) to give targeted advice
instead of a raw exception. Every one of these is a place a future change
should ADD a case, not replace the pattern — see AURORA.md's per-R entries
for the history of what each one is guarding against.

## 3. Providers (`providers/`)

`base.Provider` is the abstract contract: `turn()` returns a `TurnResult`
(text, tool_calls, stop_reason, token counts); `assistant_message()` /
`tool_result_message()` convert Aurora's internal shapes to the API's wire
format. One concrete implementation:

- **`openai_compat.py`** — one implementation for THREE backends: OpenRouter,
  a local llama.cpp server, and any other OpenAI-compatible server (LM
  Studio, …). This sharing is why generic-sounding fixes (connect timeouts,
  retries) live in one file but must reason about very different network
  conditions (a VPN/tailnet-only local server vs. a public API).

**`make_provider(name, cfg, timeout)`** (`providers/__init__.py`) builds the
right one from a config entry's `type:` field.

**Per-turn knobs are ATTRIBUTES, not `turn()` parameters** — `extra_body`,
`on_think` and (R91) `cache_prompt` are all set on the provider by
`Engine.send` just before the call. That's a deliberate pattern, not
laziness: `turn()`'s signature is implemented by every provider and faked by
a dozen tests, so widening it for each new per-turn option would churn all
of them for something only one implementation reads. New per-turn options
should follow the same route.

### Prompt caching (R91)
`_system_message(system, cache)` decides between a plain string and a
content block carrying `cache_control: {"type": "ephemeral"}`. The
OpenAI-compatible world splits in two here and one marker covers both:
OpenAI/DeepSeek-style backends cache long prefixes automatically and ignore
the marker; Anthropic-family models via OpenRouter cache ONLY at an explicit
breakpoint. Below `_CACHE_MIN_CHARS` nothing is marked — a cache *write*
costs more than a plain read, and Anthropic won't cache a short prefix at
all. The system prompt is the only sensible breakpoint: it's the one part of
a request that is byte-identical for a whole session, and it is re-sent on
every tool iteration, each one billed (R37).

### Networking hardening (all in `openai_compat.py` — read before touching connect logic)
These exist because of *specific* production failures, not speculative
robustness — each one is load-bearing:
- **`_is_lan_host(base_url)`** classifies a host as local/tailnet (loopback,
  private IP, `.local`, `.ts.net`) vs. a public API. Connect timeout is 5s for
  LAN (fail fast when a local server is off) vs. 20s for remote (a public
  API's TLS handshake can legitimately be slow).
- **`_is_bare_ip(base_url)`** (R64) is the NARROWER sibling check used to skip
  TLS verification: true only for a literal private/loopback IP host, never
  for a hostname (`.ts.net` included). A reverse proxy fronting llama.cpp
  commonly serves a cert issued for one hostname only — dialling the same
  listener by LAN IP always fails verification even though the connection is
  trusted. `verify=not _is_bare_ip(url)` must be passed to
  `HappyEyeballsTransport(...)` itself, NOT to `httpx.Client(verify=...)` —
  httpx silently ignores the client-level `verify=`/`cert=` kwargs once a
  custom `transport=` is supplied. Every bare-IP dial site needs this: the
  pooled client in `openai_compat.py`, its `_probe()`, and
  `engine.provider_health()`'s own separate `/props` call — a second,
  easy-to-miss call site outside `openai_compat.py` entirely.
- **`live_context_limit()`** (the `/props` llama.cpp probe) is gated behind
  `_is_lan_host` — probing it on a remote provider wastes a doomed ~6s
  request on the UI thread at every status render, freezing startup.
- **`pick_endpoint()`'s `_probe()`** hits `{base}/props` (llama.cpp-native,
  `/v1` stripped) to fail over between a `base_url` list fast instead of
  waiting for the real request to time out. Any OpenAI-compatible endpoint
  put in front of a local server — e.g. m7's `aurora-gateway.py`, which
  unifies local llama.cpp + real OpenRouter behind one provider entry (see
  AURORA.md's "As-built additions" for the deployment note) — MUST
  implement a passthrough `/props` route or every request looks
  unreachable and Aurora never gets past the probe.
- **`happy_eyeballs.py`** — a custom httpcore network backend that races
  IPv4/IPv6 and uses whichever connects first (RFC 8305). A host with both A
  and AAAA records on a machine whose public IPv6 route is dead (common with
  Tailscale up) otherwise burns the WHOLE connect timeout stalling on IPv6
  before falling back (measured 17s → 0.56s against OpenRouter). This is
  NOT the same as forcing IPv4 — the race is correct on IPv4-only and
  IPv6-only networks too.
- **Pre-stream connection retry** (`turn()`'s `_ATTEMPTS` loop) — a pooled
  keep-alive connection that went stale while Aurora sat idle resets on reuse
  ("Connection reset by peer"). httpcore's own `retries=` only covers the
  CONNECT phase, not a failure during the request, so `turn()` retries a
  transient connection error itself — but ONLY while `result.text` is still
  empty (a mid-stream drop keeps its partial instead, never duplicating
  output already shown to the user).
- **User-facing connectivity errors never echo config verbatim** (`agent.py`
  `_provider_label`/`_connectivity_hint`, 2026-07-12). A `ProviderError`'s own
  message embeds `provider.name` — the raw config-KEY string, which a user
  can name however they like (their own machine's nickname, say) — and a
  local/LAN `base_url` can bake in a personal hostname too (a Tailscale
  MagicDNS name). Neither is ever printed: the notice classifies
  `_is_lan_host(base_url)` into "local backend" (generic, no hostname) or the
  hostname itself for a remote provider (public SaaS domains — openrouter.ai
  — aren't personal, so showing them is fine and more useful). A `curl`-based
  connectivity hint follows the same rule: concrete
  for remote, host-free for local. Open-source hygiene: config is user data,
  Aurora never echoes it back.

### Backend API surface a self-hosted server must implement

Everything below is what Aurora actually calls against `providers.local
.base_url` (llama.cpp's own OpenAI-compat server) and, separately, an
optional LlamaDesk instance. Nothing here is Aurora-specific protocol —
it's the exact subset of llama.cpp's/LlamaDesk's real HTTP APIs Aurora
depends on; a from-scratch reimplementation only needs to match these.

**Required — llama.cpp server (`providers.local.base_url`, `type: openai`):**
- `POST {base_url}/chat/completions` — OpenAI-compatible chat completions,
  **streaming** (`"stream": true`, SSE `data:` lines, terminated by
  `data: [DONE]`). Aurora sends `messages`, `model`, `stream_options:
  {"include_usage": true}`, and (if the model entry has `tools:`)
  `tools` in OpenAI tool-call schema. It reads `choices[0].delta.content`,
  `choices[0].delta.tool_calls[].function.{name,arguments}` (both can arrive
  fragmented across chunks — Aurora accumulates by index), `choices[0]
  .delta.reasoning_content` (optional — thinking models only, e.g. Qwen3.x;
  routed to the UI's dim reasoning stream, never stored in history), and a
  final chunk's `usage.{prompt_tokens,completion_tokens}`. `finish_reason`
  on the last choice becomes `TurnResult.stop_reason`. (`openai_compat.py
  :turn()`)
- Auth: `Authorization: Bearer <LLAMA_API_KEY>` if `api_key_env` is set on
  the provider (omitted entirely when no key is configured — llama-server
  treats that as open).

**Optional but exercised whenever the server is llama.cpp itself (not a
generic OpenAI-compat backend like OpenRouter) — llama.cpp's own `/props`:**
- `GET {base_url_without_/v1}/props` — used for THREE separate things, so a
  server claiming llama.cpp compatibility should implement it if it wants
  full behavior rather than degraded-but-working:
  1. **Fast-fail probing** (`_probe()`) — a 2s-budget health check per
     configured endpoint, so `pick_endpoint()` can fail over between a LAN
     and Tailscale `base_url` in seconds instead of waiting out the full
     request timeout.
  2. **Live context limit** (`live_context_limit()`) — reads
     `default_generation_settings.n_ctx` so the status bar / `/status`
     shows the REAL loaded context, not a guessed config default.
  3. **`/status` / startup banner health** (`Engine.provider_health()`) —
     reads `model_path` (basename shown as "ready") and the same `n_ctx`
     field; a response that's missing either is treated as "reachable but
     schema changed" (loud, not silently blank — see R: "Upgrade surfaces").
  Missing `/props` entirely degrades gracefully: context limit falls back
  to `config.yaml`'s `context_limit` (default 128k), and `/status` shows
  "remote API (no health endpoint)" instead of erroring — this is exactly
  how a non-llama.cpp OpenAI-compat backend (OpenRouter, LM Studio) is
  already handled, so it's a supported configuration, not just tolerated.

**Optional — LlamaDesk (`llamadesk.url` in `config.yaml`), a SEPARATE
service from the llama.cpp server itself, for switching which gguf is
loaded.** Entirely optional — omit the `llamadesk:` config block and
Aurora only ever talks to whatever's already loaded on `providers.local
.base_url`. (`aurora/llamadesk.py`)
- `GET {url}/api/models` → `{"models": [...]}` or a bare list — plain gguf
  filenames. Read-only, no auth.
- `GET {url}/api/models/detail` → `{"models": [{"name", "ctx_native",
  "size_bytes"}, ...]}`. Optional refinement of `/api/models` — its
  absence (a 404, older LlamaDesk) is caught and Aurora falls back to the
  plain name list with `ctx_native: None` for every entry, which in turn
  makes R68's context picker show the unbounded ladder instead of one
  capped at the model's real max.
- `GET {url}/api/status` → `{"model": <name>|None, "ctx": <int>|None,
  "ram_used_bytes": <int>|None, "status": "online"|"offline"}` (shape-
  tolerant: `loaded_model()` also accepts a `"loaded"`/`"current"` key).
  Read-only, no auth.
- `GET {url}/api/switch/progress` → `{"running": bool}` — polled by
  `busy()` before starting a new switch, so Aurora never launches a second
  load on top of one already in flight. Read-only, no auth.
- `POST {url}/api/switch` with JSON `{"model": <name>, "ctx": <int>, "ngl":
  "auto"|<int>}` — the ONE mutating endpoint, and the only one gated
  behind a bearer token: `Authorization: Bearer <LLAMADESK_TOKEN>`
  (`token_env` in the `llamadesk:` config block) if the server requires
  one. This is a GLOBAL action — it evicts whatever's currently loaded for
  every consumer of that llama-server, which is why the UI always shows an
  explicit eviction confirm before calling it (R3). A missing/wrong token
  surfaces as `401`, matched literally in the UI's error text.
- After `switch()`, Aurora polls `GET {url}/api/status` (via `wait_ready()`,
  default 3s interval, 240s timeout) until `status.model == <requested
  name>` and `busy()` is false — there's no push/webhook mechanism, so a
  from-scratch LlamaDesk-alike just needs `/api/status` to reflect the new
  model truthfully once the load actually completes.

## 4. The TUI's three areas (`tui.py`) — R53

Full-screen layout is an `HSplit` of exactly three regions, and this is a
**discipline for future surfaces**, not just current layout — pick the one
area matching a new feature's role:

1. **Chat/scrollback** (`_ChatControl` in a `Window`) — the only area that
   scrolls; wheel/PgUp/drag-select live here. Fragment-cached per entry
   (`_cache`) since a long session appends thousands of chunks and re-parsing
   the whole ANSI stream on every keystroke would be O(n²).
2. **Input area** — `self.input` (a `TextArea`) plus, ABOVE it, a dedicated
   menu window for `select()` challenges. Two separate mechanisms live here,
   easy to conflate:
   - **`ask()`** (free-text: passphrases, comment guidance) reuses the
     `TextArea`'s own `prompt=` (a `BeforeInput` processor) — fine for a
     single line.
   - **`select_menu()`** (multi-choice: approvals, confirms, R58 challenges)
     renders in its OWN `Window(FormattedTextControl(...))`, NOT the input
     prompt — a multi-line prompt drawn via `BeforeInput` turns embedded
     newlines into literal `^J` (staircased garbage). This was a real,
     shipped bug; don't reintroduce it by "simplifying" the menu back into
     the input prompt.
   - **A menu label can carry raw ANSI colour** (R59 — `/model`'s `[$]`/
     `[free]` tags use the same `GREEN`/`YELLOW` constants the classic REPL
     prints directly). `_menu_fragments` parses each label through
     `ANSI(label).__pt_formatted_text__()` rather than treating it as
     literal text — otherwise the raw escape bytes render as garbage
     characters instead of colour. Each parsed sub-fragment's colour layers
     ONTO the row's base style (selected/option), so a plain label (the
     common case — approve/confirm menus have none) keeps its old look.
3. **Status bar** (2 lines, `FormattedTextControl`) — read-only, never
   scrolls or takes input. Line 1 is identity ONLY (model/ctx/session
   id/mode — `prompt mode` or `bash mode`); line 2 is tooltips by default,
   replaced wholesale by whichever transient
   status is live (exit-confirm → awaiting-answer → copy-notice → busy
   spinner → tooltips — first match wins, they never coexist).

**The `builtins.input` monkeypatch** (`Tui.run()`): while the TUI runs,
`input()` is replaced with `self.ask()`, and `ui.select` is replaced with
`self.select_menu()`. This is how `ui.py`'s interaction code (written against
plain `input()`/`select()`) renders differently in the TUI without
duplicating any of that logic — restored on exit via `finally`.

**Mouse handlers can crash the whole app.** prompt_toolkit's own
`CompletionsMenuControl.mouse_handler` asserts an active `complete_state`;
a stray click after the completions closed (e.g. returning to the terminal
window after switching away) raises, and any handler exception kills
`app.run()`. `_SafeCompletionsMenuControl` is the pattern for guarding a
library handler we don't own: check the precondition, swallow if absent.
Any future float/control built on a library class needs the same scrutiny.

## 5. Persistence — three DIFFERENT files, on purpose

| File | Lives in | What | Written by |
|---|---|---|---|
| `config.yaml` | repo (committed, synced) | providers, models, `runtime.*` defaults incl. `secret_allowlist` (SHA-256 hashes only — see §7) | `persist_runtime_value()` — `/redact`, `/redact allowlist [clear]`, `/multiline` (`/max` was removed, see §8) |
| `AURORA_HOME/state.yaml` | per-machine | `last_model`/`last_provider` (R51) | `config.save_state_values()` |
| `AURORA_HOME/allowlist.yaml` | per-machine | tool-approval "always" rules (unrelated to the secret allowlist above — same word, two different features) | `approve.add_rule()` |
| `AURORA_HOME/sessions/<id>.jsonl` | per-machine | every turn/tool/approval, append-only | `Session.log()` |
| `AURORA_HOME/checkpoints/<hash>/` | per-machine | shadow git repo, pre-mutation snapshots (R47) | `rewind.checkpoint()` |

**Why config.yaml vs. state.yaml is a real distinction, not a whim**:
config.yaml is meant to be committed and synced between machines (providers,
model list, feature defaults); state.yaml is per-machine runtime state that
would be actively WRONG to sync (which model you had loaded on THIS machine).
A new persisted setting has to pick the right one — "does this make sense to
share across machines?" is the test.

**The allowlist matches on NORMALIZED command tokens** (`approve._norm_command`
— `shlex` split + `~` expansion), not raw strings. `bash ~/x.sh`, `bash
"/home/me/x.sh"`, and `bash /home/me/x.sh` must all match one stored rule —
this was a real bug (each spelling was a distinct string, so "always allow"
silently never fired for the next spelling the model happened to emit).

**`SAFE_COMMANDS` (R67) generalizes read-only commands across arguments.**
The default rule stores the first TWO tokens of a command (`find /path/A`)
and matches that prefix exactly — correct for anything that can write/delete/
execute, since a bare `rm` must never auto-approve `rm -rf /`. But for a
curated set of read-only commands (`find`, `ls`, `tree`, `grep`, `cat`, …)
that's too strict: "always allow" on one path never covered the same command
against a different path in a different project, which read as "the
allowlist doesn't persist across sessions" even though the file itself was
fine. For a `SAFE_COMMANDS` entry, `add_rule()` stores just the bare command
name and `is_allowed()` prefix-matches it regardless of args.

**Stored API keys live OUTSIDE `AURORA_HOME` entirely** (R60) — the OS
keyring (macOS Keychain/SecretService via `keyring.{get,set,delete}_password`)
is a SEPARATE store, keyed by service name `"aurora-agent"`, not a file under
`AURORA_HOME`. This is why `aurora wipe` clears keyring entries FIRST
(`keystore.clear_key` for every `api_key_env`/`token_env` this config.yaml
uses — `_known_key_names()`, not a hardcoded list) and only THEN deletes the
`AURORA_HOME` directory: `rm -rf`ing the directory alone would leave stored
keys behind, silently un-logging-out the user. `aurora key clear <VAR>`
exposes the same clearing logic for a single key. Verifying delete/clear
logic MUST mock both the keyring module AND `AURORA_HOME` in the same
command — see the MEMORY finding on this (a real Keychain deletion incident
from an ad-hoc verification run that mocked only one of the two).

## 6. Threading model

- **Main/UI thread**: runs the `prompt_toolkit` `Application` event loop
  (`self.app.run()`). MUST NEVER BLOCK — no network calls, no `input()`. Any
  blocking ask (`ask()`/`select_menu()`) raises `RuntimeError` if called from
  this thread specifically, to fail loudly instead of deadlocking silently
  (that thread is the one that would have to deliver the answer).
- **Worker thread** (`Tui._worker`): runs one loop consuming submitted lines
  from `self._inbox`, calling into `ui._send_turn`/`_handle_command` (R96f —
  `_send_turn` directly, not through a per-turn wrapper thread; see below).
  This is where `Engine.send()` and the whole agent loop execute — it's fine
  for this thread to block on network I/O or on `ask()`/`select_menu()`
  (those route back to the UI thread via a `queue.Queue`).
- **No per-turn wrapper thread in the TUI (R96f).** `ui._run_turn` wraps a
  turn in its own thread so the MAIN thread can catch `KeyboardInterrupt`
  while `input()` blocks — that only makes sense in the classic REPL, where
  `run_turn` executes ON the main thread. The TUI's `_worker` is already a
  background thread, and SIGINT is only ever delivered to the process's main
  thread anyway (prompt_toolkit also runs the terminal in raw mode, so `^C`
  never becomes a signal there at all — TUI cancellation is
  `fe.cancel_event.set()` via the Esc-Esc menu, unrelated to this
  mechanism). So the TUI calls `ui._send_turn` — the turn's body factored out
  of `_run_turn` — directly on the worker thread, collapsing "which thread is
  a mid-turn key prompt on" from four levels deep to three.
- **Ticker thread**: purely cosmetic, invalidates the app every 0.5s while
  busy to animate the spinner/elapsed-time.
- **Startup banner health probe** (`Tui._banner`/`ui._banner`, both call
  `Engine.provider_health()`): this runs on the main thread too, but BEFORE
  `app.run()` even starts the event loop — nothing is on screen yet, so a
  stuck call here doesn't just block one render, it makes the whole app look
  dead at launch (R66). `provider_health()` therefore runs its actual probe
  on a throwaway daemon thread and hard-caps the wait (`timeout=4.0` default)
  via `thread.join(timeout)` — if the probe hasn't returned in time, startup
  proceeds with an "unknown/timed out" health result instead of waiting on
  it. A health CHECK must never be able to block the thing it's checking.
- **Context-limit refresh** (`Engine._context_limit_nonblocking`, R95i): the
  same rule applied to the *steady-state* render path. `context_stats()` is
  called by `status()` on the UI thread on every frame, and for the `local`
  model the limit is a live `/props` lookup behind an endpoint probe — so a
  backend that was down froze the app for ~6s each time the 120s cache
  expired. The lookup now runs on a daemon thread and the render is served
  the cached value (or `Provider.static_context_limit()`, the offline
  answer, before the first one lands). **Stale beats blocking.**

  This is the general shape for anything the status bar wants to show: if
  answering it can touch a socket, the render path gets the last known
  value and a background refresh, never the call itself.

A blocking ask/select is a **queue handoff**: the worker thread calls
`ask()`/`select_menu()`, which sets state (`self._question`/`self._menu_*`),
invalidates the app so the UI thread renders the prompt, then blocks on
`self._answers.get()`. The UI thread's key-binding handlers (running on the
main thread) `put()` the answer when Enter/a menu choice fires. This is the
one synchronization primitive between the two threads for interactive
prompts — session/message state itself is only ever touched by the worker
thread, so it doesn't need its own lock.

## 7. Secret redaction (R58) — a case study in "engine decides, frontend renders"

Added 2026-07-12; a good template for the next similar feature, since it
touches all the layers above:

- **`secrets.py`** — pure detection/redaction, zero UI, zero I/O:
  `scan(text) -> list[Match]`, `redact(text, matches) -> str`,
  `preview(matches) -> str` (kind + count summary; NEVER echoes the actual
  secret text back, even in the challenge prompt). Two passes: known vendor
  **shapes** (regex — `AKIA…`, `ghp_…`, `.env`-style assignments, a dedicated
  GUID/UUID pattern since those are sometimes used as API keys/session
  tokens, not just harmless correlation IDs), plus an **entropy fallback**
  for tokens with no recognizable prefix at all (shape regex alone misses an
  ad-hoc token from some internal tool — it's just a random string). The
  fallback only scans spans the shape pass didn't already claim (so a UUID
  is counted once, as `"GUID/UUID"`, not also as a generic high-entropy hit),
  and explicitly excludes hex-only strings (git SHAs, MD5/SHA digests) — a
  real false-positive source worth guarding deliberately rather than tuning
  away by accident. (UUIDs used to be excluded here too, until it became
  clear some systems DO use them as secrets — see AURORA.md R58.)
- **Three hook points**, chosen so the on-disk session log is automatically
  consistent with what was sent (no separate log-side check needed):
  `Engine.send()` scans `user_text` before it enters `messages`/the log;
  `agent.py`'s tool loop scans a tool's raw output before `_finish()` (i.e.
  before `on_tool_result` AND before it becomes a `tool_result_message`) —
  this covers EVERY tool uniformly, including read-only ones (`read_file`,
  `grep`) that never go through the approval gate at all. The THIRD is
  narrower and asymmetric on purpose: `run_command`'s own command STRING is
  scanned right before it runs, but only ever produces a `notify()` — never
  a challenge, never a block, never a redaction. A command usually needs its
  real argument to actually work (a real key in a curl header), so silently
  altering it would just break it, and blocking would duplicate the approval
  gate the call already passed. No other tool's arguments get this
  pre-execution scan (a `write_file`'s content isn't checked until its
  eventual `read_file` output is, through the normal hook above) — this is
  specifically about what's about to be handed to a shell.
- **The toggle is a capability, not a runtime branch inside `agent.py`**:
  `Engine` only ever passes `AgentCallbacks.secret_challenge=fe.secret_challenge`
  when `self.redact_secrets` is true; otherwise it passes `None`, and the
  agent loop's `if cb.secret_challenge:` guard skips scanning entirely — zero
  cost when the feature is off, and `agent.py` never has to know about config.
- **One challenge per block**, not per match — a `.env` file with ten keys
  produces ONE `secret_challenge(context, matches)` call (all matches
  together), not ten. The user's decision (keep/redact/stop/always) applies
  to every match in that block.
- **`Frontend.secret_challenge`** is implemented once in
  `ui.TerminalFrontend` (numbered menu via the shared `select()` primitive)
  and inherited by the TUI for free — same pattern as `approve`/`ask_continue`.
- **Allowlist for confirmed false positives (2026-07-14).** A 4th challenge
  outcome, `"always"`, lets a repeat false positive (a fixture UUID, an
  internal-tool token) stop being flagged at all. `Engine` is the only layer
  that knows what "always" means: `Engine._secret_challenge()` wraps
  `fe.secret_challenge()`, and on `"always"` it hashes every matched value
  with `secrets.hash_value()` (SHA-256 — the raw value is never persisted,
  only its hash), adds the hashes to `self.secret_allowlist`, persists via
  `persist_runtime_value(cfg, "secret_allowlist", …)`, and returns `"keep"`
  to the caller — `send()` and the agent loop only ever see the original
  three outcomes. `Engine.__init__` loads the persisted hashes into
  `self.secret_allowlist` and threads it into every `secrets.scan(text,
  allowlist)` call (directly in `send()`, via `AgentCallbacks.secret_allowlist`
  in the agent loop); `scan()` drops any match whose `hash_value()` is in the
  set before it's ever surfaced. Managed via `/redact allowlist [clear]`.

## 8. The agent loop's one cap: iterations (R6/R9)

`agent.run_turn()` checks once per round, inside the tool-handling branch:
`if iteration >= checkpoint: cb.ask_continue(iteration)` — only fires when
the model is actually looping on tool calls, using a
**checkpoint-then-re-arm** pattern (`checkpoint = iteration +
max_iterations`): continuing grants exactly one more full interval before
asking again, not "asks every single round forever."

A second, TIME-based cap (`max_wait`/`ask_wait`, R61) existed alongside this
from 2026-07-12 to 2026-07-13: it fired on wall-clock elapsed time regardless
of whether the model was looping, which meant a single long-but-normal
generation (big local model, slow network) got a "still working, continue?"
challenge unrelated to any actual runaway behavior. Removed at the user's
request — see AURORA.md R61 for the full torn-out call-site list. The
iteration cap above is the only loop-safety mechanism now.

## 9. Esc as a generic double-tap control key (R62) — and why it needed a NEW non-blocking menu

Three states — busy/working, bash mode, idle-empty-prompt — share ONE Esc
gesture: **first press arms (shows a status-bar hint), second press (within
2s) opens an explicit arrow-key Yes/No question**; nothing happens until the
user actually picks an option from it. `Tui._on_escape(app_exit)` is a plain
method (not a closure over the key-binding's `event`), specifically so it's
directly callable/testable — driving real Esc bytes through a pipe input
hits prompt_toolkit's own ESC-vs-escape-sequence disambiguation delay, which
made timing-based tests flaky; calling the method directly sidesteps that
entirely.

**Why this couldn't just call `select_menu()`** (§4/§6's blocking mechanism):
Esc fires as a key binding, which runs ON THE UI THREAD. `select_menu()`
blocks on `self._answers.get()`, and the answer is delivered by ANOTHER key
binding (Enter/digit) — also on the UI thread. Calling `select_menu()` from
inside a key binding would be the UI thread blocking on an answer only the
UI thread can deliver: the same deadlock `ask()`/`select_menu()` already
guard against by raising `RuntimeError` when called from that thread (§6).

**The fix: a second, non-blocking menu path, `Tui._open_ui_menu()`**, reusing
every piece of `select_menu()`'s RENDERING (`_menu_fragments`, the arrow-key/
digit-jump bindings — all keyed off the same `self._menu_options is not None`
check; Esc is a no-op while any menu is open, the pick must be explicit) but
resolving differently:
- `select_menu()` (worker-thread callers: approvals, R58, `/model`) →
  `_resolve_menu()` puts the chosen key on `self._answers` queue.
- `_open_ui_menu()` (UI-thread callers: Esc confirms) → sets
  `self._menu_on_select = on_select`; `_resolve_menu()` checks this FIRST and,
  if set, clears the menu state and calls `on_select(key)` directly instead
  of touching the queue — no thread ever blocks.

Three resolvers, one per confirmable state: `_resolve_cancel_menu` (→
`fe.cancel_event.set()`), `_resolve_bash_leave_menu` (→ `self._bash_mode =
False`), `_resolve_quit_menu` (→ the injected `app_exit()`). **History**:
busy-cancel briefly confirmed directly on the second press (shipped, then
revised same-day) before being brought in line with the other two, for one
reason — an explicit picked choice, not an implicit "you pressed Esc twice,
that counts."

**The 2-second window is a real timer, not "immediately after"**: tracked as
`(self._esc_armed: str | None, self._esc_armed_at: float)` — the KIND of
pending action plus when it armed. A second Esc past the window, or while a
DIFFERENT state is active, is treated as a fresh first press (re-arms,
never confirms) — this matters because state can change between the two
presses through some OTHER path (e.g. bash mode left via Backspace-on-empty,
not Esc) and a stale arm must never silently fire on an unrelated later Esc.

## Where to look next

- **Requirements** (R1–R101+, the numbered spec with dates and rationale):
  `AURORA.md`.
- **User-facing feature list**: `README.md` → "Daily use" and "Esc, the
  double-tap control key".
- **`.agentic_context/`**: this project's own cross-session memory system
  (yes, Aurora dogfoods a version of the same protocol it was built to
  assist with) — see `.agentic_context/KNOWLEDGE/project/*.md`.
