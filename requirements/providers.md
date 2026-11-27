# Aurora — Requirements: Providers & models

Provider configuration, endpoints, keys, model picker, context limits, fallbacks, idle-unloaded servers.
Part of the index in [`../REQUIREMENTS.md`](../REQUIREMENTS.md); numbers are shared across all files.

1. **R223 — Ollama as a configured provider.** A `config.yaml` provider
   entry with `type: ollama` talks to a local/LAN Ollama server correctly:
   - Reachability (`_probe`, used by `pick_endpoint` before every turn and
     by the UI's status render) checks Ollama's own root route, never
     llama.cpp's `/props` (which Ollama doesn't implement and would always
     404).
   - `live_context_limit(model)` reports the real trained context window
     for the named model via Ollama's `/api/show`, for any installed model
     — not gated on the `model == "local"` sentinel llama.cpp's `/props`
     path requires.
   - `live_model_name()` returns `None` for an Ollama provider rather than
     attempting `/props` — left unimplemented pending a real "which
     resident model" concept (`GET /api/ps`), since Ollama can hold several
     models loaded at once.
   - No API key is required or sent when the provider config has none.
   - Chat turns (streaming text, tool calls) need no Ollama-specific
     branch in `turn()` — Ollama's OpenAI-compatible endpoint is handled by
     the existing generic OpenAI streaming/delta parsing.
   - A provider with no `type:` (or any value other than `"ollama"`)
     continues to use llama.cpp's `/props` exactly as before this change —
     no regression for existing llama.cpp/OpenRouter configs.

   **Verified live (2026-08-26)** against a real Ollama server on the server, not
   just mocked HTTP: probe, `live_context_limit` (against two different
   model families, `qwen35` and `gemma`, correctly reading the
   family-prefixed context-length key each time), a full streamed chat
   turn, and a real multi-round tool-call → tool-result → final-answer
   round trip, all through the actual `Engine`/`config.yaml` path, not just
   `OpenAICompatProvider` in isolation.

   **Caveat found during live verification, not a code defect:** Ollama's
   own `/api/show` can report `"capabilities": ["tools", ...]` for a model
   that then never actually emits a structured tool call at inference time
   — `qwen2.5-coder:3b` printed the call as plain JSON text 3/3 tries
   despite being marked tools-capable; `qwen3:1.7b` emitted correct
   structured `tool_calls` 3/3 tries on the identical prompts. Aurora has
   no way to detect this ahead of time (Ollama's own capability flag says
   the opposite), so it isn't fixable in code — recorded here so a future
   session doesn't waste time debugging "why does an Ollama model configured
   with `tools: true` never call any tools" as if it were an Aurora bug.
   Prefer the Qwen3 family for an Ollama backend Aurora will actually use
   tools with.

3. **R228 — `key_status` never reports a false "not set".** With an
   encrypted key store present and a cached passphrase that fails to
   decrypt it (stale after an external re-encryption, or another session
   sharing AURORA_HOME), `keystore.key_status` must return the same
   `"possibly set (encrypted file — enter passphrase to confirm)"` answer
   the no-cached-passphrase case gets. It used to swallow the decrypt
   failure and fall through to a flat `"not set"` — indistinguishable from
   no store existing at all, and confidently wrong with the file right
   there.

8. **R233 — the Happy Eyeballs fast path connects the address it selected.**
    When a host resolves to a single address, `happy_eyeballs_connect` must
    connect that exact `getaddrinfo` sockaddr with the family already
    chosen, via the same `_connect_one` helper each racing attempt uses. It
    previously called `socket.create_connection(sa[:2], …)`, which (a)
    truncated an IPv6 sockaddr `(addr, port, flowinfo, scope_id)` to its
    first two elements, discarding the scope id — `sa[0]` carries no
    `%iface` suffix, so the kernel rejects the result with EINVAL and a
    link-local IPv6 host was unreachable whenever it resolved to exactly one
    address, though the racing path reached it fine — and (b) re-resolved
    the hostname a second time, ignoring the family already selected. The
    two paths share one helper so they cannot diverge again.

37. **R267 — a fallback turn is attributed to the model that answered.** `send()`
   bound `model` before `_run_turn_with_fallback`; when R162 fell back to
   another model, the `assistant` record still named the original, failed
   model. `usage_by_model`, `/context`, the resume cost seed and
   `last_latency_by_model` then priced and credited the tokens to the wrong
   model. The record now reads `self.current` after the turn.

   **Test:** `tests/test_deepdive_regressions.py::test_fallback_turn_is_attributed_to_the_answering_model`.

48. **R278 — R58 detects OpenRouter keys and `export`ed credentials.**
   OpenRouter is Aurora's own remote provider, and its keys
   (`sk-or-v1-<64 hex>`) matched no pattern. The OpenAI shape stops at the
   hyphen after `sk-or`, and the entropy fallback skips all-lower-case hex.
   Verified: `scan()` returned nothing for a JSON `"api_key"`, a YAML
   `key:` and `export OPENROUTER_API_KEY=…`. That last case also showed the
   `.env` rule was anchored at the line start, so any `export X_TOKEN=…`
   line (every .bashrc/.zshrc/.envrc credential) was missed. A dedicated
   `OpenRouter key` shape (with its literal guard) and an optional
   `export ` prefix on the env rule close both. Ordinary exports
   (`PATH`, `EDITOR`) still don't match.

   **Test:** `test_openrouter_keys_and_export_lines_are_detected` (5 shapes),
   `test_export_detection_does_not_flag_ordinary_exports`; the R96g guard
   superset test gained an OpenRouter sample.

65. **R295 — an idle-unloaded local server reports "asleep", not a schema
   alarm.** the server's llama-server idle manager (and any gateway in front of it)
   answers `/props` with `{"sleeping": true, "model_path": null}` while the
   model is unloaded. The startup banner and `/status` read that as a
   broken backend: "✔ ? ready, ctx unknown — /props schema changed?
   (llama.cpp upgrade)", on every cold start. It now reports healthy and
   "asleep — the model loads on the first request (expect a slower first
   reply)", which is what actually happens. An awake server's line is
   unchanged.

   **Test:** `test_sleeping_server_reports_asleep_not_schema_change`,
   `test_awake_server_still_reports_model_and_ctx`.

67. **R297 — the `/model` picker warns about a model that will fail if
   picked, instead of only finding out mid-turn.** A config entry can name an
   Ollama model that was never `ollama pull`ed (or was later removed) — real
   case caught live: `qwen2.5:3b` was configured but not installed on the
   local Ollama server, while `qwen3:1.7b` right above it in the same picker
   was. Same failure shape for any local/LAN backend (llama.cpp `local`
   entry, or an Ollama host) that's simply unreachable — nothing in the
   picker distinguished "this will work" from "this will fail" before now.

   `Provider.model_health(model)` (`providers/base.py`) returns
   `{"ok": bool, "detail": str}` or `None` (nothing worth checking — the
   default, and what a remote paid API like OpenRouter gets: a bad model id
   there only surfaces as a 4xx on first use, and probing every entry would
   cost a real billed request per row on every picker open).
   `OpenAICompatProvider.model_health` (`providers/openai_compat.py`)
   implements it for local/LAN backends: reachability via the existing
   `_probe`, and for Ollama specifically, existence via `/api/tags` (not
   `/api/show` — that 404s identically for "server unreachable" and "model
   not installed", which would mislabel a live server's missing model as a
   connectivity problem).

   `/model` (`ui.py:_pick_model`) probes every checkable entry (in parallel,
   one thread per entry) BEFORE the menu is built, bounded to a 2.5s total
   deadline — not the price refresh's pure fire-and-forget-then-relabel
   shape, because `TerminalFrontend.update_menu_labels` (`ui.py`) is a
   documented no-op on the classic (non-TUI) frontend: "the classic REPL's
   select() already PRINTED its numbered list." A relabel-only warning would
   never reach a classic-mode user at all, and even in the TUI a fast Enter
   could beat it — so the bounded wait runs first, and only a probe still
   unresolved past the deadline falls back to a background relabel (TUI
   only, same shape R80's price refresh uses). Appends a red `⚠ <detail>`
   (e.g. `⚠ not pulled`, `⚠ unreachable`) after any entry that comes back
   `ok: False`.

   **Test:** `test_model_health_ollama_model_not_pulled`,
   `test_model_health_ollama_unreachable`,
   `test_model_health_local_llamacpp_reachable_and_unreachable`,
   `test_model_health_remote_paid_api_returns_none`,
   `test_pick_model_shows_health_warning_before_first_render`,
   `test_pick_model_slow_probe_does_not_block_past_deadline`.

69. **R307 — a closed connection pool is replaced, never retried on.** Seen
   2026-09-30: a local turn hit `ReadError — retrying (attempt 2/3)` and then
   died with `RuntimeError: Cannot send a request, as the client has been
   closed.` The turn fetched its pooled `httpx.Client` once and retried on
   that same object, so once the pool was closed under it (which is also what
   produces the ReadError) every retry was dead on arrival. Now `_client_for`
   drops a closed cached pool and builds a new one, and `turn()` fetches the
   client per attempt, so the retry runs on a live pool. What closed it in
   that session: see R308.

   **Test:** `tests/test_closed_pool_recovery.py`.

70. **R308 — the `/model` picker never touches the running turn's provider.**
   The root cause behind R307's report: `/model` opened while a turn is
   running (the bootstrap turn, here) probes every configured model for
   health/latency via `_provider_for(m)`. For an entry on a different
   provider that REBUILT `engine._provider` and closed the old one's pools —
   the pools the running turn was streaming on. Probes now go through
   `Engine.probe_provider_for`, which reuses the turn's provider for the same
   provider key and otherwise uses separate, cached, non-interactive
   instances. Opening the picker mid-turn leaves the turn's provider and its
   pools untouched.

   **Test:** `tests/test_closed_pool_recovery.py`
   `test_model_picker_probes_never_close_the_running_turns_pool`.


71. **R315 — a machine can use its own config.** The committed `config.yaml` is shared by every machine and lists
    models and servers that exist on one of them (its first entry is the default model, and `local` points at the server's
    llama-server), so a machine whose servers differ had no way to start on its own model short of passing a path
    every time. `AURORA_CONFIG=<file>` now names the config `aurora` starts with when no path argument is given; an
    explicit `aurora <config.yaml>` still wins, and a value that is not a file stops with a clear message instead of
    falling back. Unset, nothing changes. Found on a machine with its own llama-server but a shared config that opened on
    another host's first entry.

    **Test:** `tests/test_default_config.py`.


72. **R316 — per-machine overrides without touching the repo.** `$AURORA_HOME/config.local.yaml`, when it
    exists, is merged over whichever config Aurora loads: dicts merge key by key, anything else (a `base_url`
    list, `models`) is replaced whole. A machine whose llama-server differs from the committed `local` entry
    (say `http://127.0.0.1:18181/v1`, keyless) lists just that, so `config.yaml` and the source stay
    identical everywhere. A missing or empty overlay changes nothing. `/model add` and `/max` still write to the
    base config, not the overlay.

    **Test:** `tests/test_config_overlay.py`.

73. **R325 — a keyring that never answers cannot freeze Aurora.** SecretService
    waits for a GUI unlock prompt with no deadline, so on an ssh/tmux session
    with a locked keyring `/model` sat on "working…" forever (and the first
    request to a keyed provider, and the fallback test, hung the same way).
    Keyring reads and writes now run on a worker thread with a deadline
    (`AURORA_KEYRING_TIMEOUT`, default 5 s). On timeout the keyring counts as
    "no key here" for the rest of the process — the wait is paid once — and the
    env var, passphrase file and prompt paths carry on. Unlock the keyring and
    restart to use it again.

    **Test:** `tests/test_deepdive_20261022.py` (R325 group).

74. **R326 — the `/props` health probe uses the provider's pooled client.**
    `engine._provider_health_uncached` called a bare `httpx.get`, a fresh
    TCP+TLS handshake per status check that also skipped the IPv4/IPv6 race
    (a dead IPv6 route stalled it up to the 5 s timeout). It now goes through
    `provider._client_for(base_url)`, as the endpoint probe already did (R95h).

    **Test:** `tests/test_deepdive_20261022.py::test_props_probe_uses_the_pooled_client`.

75. **R341 — the `/model` picker has an "+ add a model…" row.** `/model add <url>`
    existed but nothing in the picker pointed at it. The last row asks for an
    OpenRouter URL or `org/model` and runs the same `/model add` flow
    (catalog validation, key prompt, switch). It is not offered for a deferred
    pick (`on_pick`, a `/model` opened while a turn runs), since adding switches
    the model at once.

    **Test:** `tests/test_deepdive_20261022.py` (R341 group).

76. **R343 — a sleeping local model is announced before the slow reply.** the server's
    idle manager unloads the model and answers `/props` with `sleeping: true`
    (R295); the first request then loads it, and the first reply was just slow
    with no explanation. For the `local` model only, `Engine.send` probes health
    with a 1.5 s deadline (at most every 30 s while it is awake, every message
    while it is known asleep) and notifies "the local model is asleep … waking it
    now; this first reply can take a minute". Remote models are never probed.
    (Listing or switching the server's GGUFs through the model manager's `/api/switch`
    is NOT done: it loads models on a shared GPU, a decision left to the user.)

    **Test:** `tests/test_deepdive_20261022.py` (R343 group).

77. **R344 — an unreachable backend is explained, not echoed.** The startup
    header and `/status` printed `unreachable: [Errno 111] Connection refused`.
    They now say `unreachable: connection refused — is the server running?` (or "no
    answer in time (server down?)" / "host name does not resolve"; any other
    error keeps its own text). Typical on a machine whose
    config names a server that only runs elsewhere.

    **Test:** `tests/test_deepdive_20261022.py::test_unreachable_message_is_a_sentence`.

78. **R345 — a failed backend is explained: cause and fixes.** A bare `✘` line
    left the user to guess. `aurora.diagnose` classifies a failure (`refused`,
    `timeout`, `dns`, `tls`, `auth`, `nokey`, `notfound`, `ratelimit`, `other`)
    and gives a cause plus 2–4 fixes naming only things Aurora itself supports
    (start the server, `config.yaml`'s `base_url`, `aurora key set <VAR>`,
    `ollama pull <model>`, `/model`, `/model add`). The same text appears
    (1) under the model line of the startup header in both front ends,
    (2) under `/status`, and (3) after a failed turn — there without any host,
    URL or config-key name (the R-never-echo rule). When the default model
    fails its startup check the model picker opens straight away (Esc keeps the
    model); a picker error never stops startup. `provider_health()` results now
    carry `kind` (and `url`); a remote model with no stored key reports
    `nokey` ("no API key set") instead of `✘ remote API`.

    **Test:** `tests/test_diagnose.py`.

- **R480 — The Rust OpenAI-compatible provider (owner, 2026-11-24).** One provider in `aurora-providers` serves
  OpenRouter, llama.cpp and Ollama as Python's `openai_compat` does: streamed text, `reasoning_content`, usage (with
  `stream_options.include_usage`), tool calls put together from fragments by index, and a call whose arguments are not
  a JSON object rejected as malformed (R268). Three attempts: a 429 waits 1 s then 3 s, or `Retry-After` up to 30 s;
  a dropped connection is retried, but a stream cut after text arrived keeps that text. LAN servers are probed first
  (llama.cpp `/props`, Ollama `/`), the answer cached 10 s, a dead URL skipped for 120 s, so a list of `base_url`s
  fails over. `api_key: none` sends no key; TLS checks are skipped only for a bare private IP; Esc (cancel) ends the
  stream at once. Tool-call arguments go back to the model byte for byte as Python sends them (`json.dumps` spacing and
  `\u` escapes), so both send the same history. Tests: `cargo test -p aurora-providers` (a scripted HTTP server).

- **R497 — Provider requests match Python on the wire (owner, 2026-11-26).** `tests/test_rust_providers_r497.py`
  sends the same headless task to a llama.cpp-style (R2), an Ollama-style (R223) and a remote OpenAI-style provider
  (R91) on one scripted server. It checks that the Rust and Python request bodies are equal (model, stream options,
  tools and messages; the system text is excluded because it names the cwd). Headless system prompts are under R91's
  4 KiB threshold, so there the breakpoint is never sent. The marker itself is covered by the Rust provider unit
  tests. Live endpoints are review note N2.

- **R502 — Live tests against a real local model, on both cores (review note N2) (owner, 2026-11-26).**
  `tests/test_live_r502.py` is skipped unless `AURORA_LIVE=1`. It uses only a local server named by `AURORA_LIVE_URL` (llama.cpp or Ollama; a key, if
  any, in `LLAMA_API_KEY`), never a paid key. On each core a real model streams
  a reply, makes a real `read_file` call, and the session records the call and an answer that holds what it read. A
  small model words things differently each run, so the checks are on the shape. The nightly (R503) runs it when its
  environment file turns it on.

- **R504 — A failed model request reads the same on both cores, and never shows the config-key name (R345, R455, R499) (owner, 2026-11-26).**
  `agent.provider_error_notices(provider, msg)` holds Python's wording for a failed request: context full, rate
  limited, unreachable (with the cause and fix list, never the raw error, which names the provider's config key),
  a diagnosable failure, or the plain error. Python's loop uses it, and `aurora/rustcore.py` renders the Rust core's
  `provider error: …` notice through it, dropping Rust's own follow-up lines. The Rust provider's transport error now
  carries its kind and its whole cause chain (`request failed (ConnectError): …: Connection refused`), so it
  classifies the same. Test: `tests/test_core.py::test_piped_input_actually_runs_a_turn` on both cores (the nightly runs
  the suite with `AURORA_CORE=rust`).

- **R505 — The provider bridge: Python's provider objects drive the Rust loop, so Python's whole suite tests it (owner, 2026-11-26).**
  `aurora-rs --core --provider-bridge` asks the front end to run each model request
  (`{"ev":"provider",…}`); Python runs the provider object it was given in-process (an embedding or a test's fake)
  and streams chunks back (`provider_chunk`), then answers with the result and its assistant message, or the error
  (malformed tool calls kept apart). The adapter uses the bridge whenever `Engine._provider_for` was replaced, and
  checks for a cancel before each request, as Python's loop does. Only a replaced `agent.run_turn` (a Python loop
  swapped in) still keeps a turn on Python. `AURORA_CORE=python|rust` overrides `runtime.core`. Tests: the whole
  suite with `AURORA_CORE=rust` (the nightly's second run): every engine test with a fake provider runs on the
  Rust loop. Each turn also carries the model entry's `tools` flag as Python sees it. D9 in
  `06-DELIBERATE-DIFFERENCES.md` is the one behaviour kept different.

- **R506 — The Rust core sends the model Python's system prompt (R30, R460, R499) (owner, 2026-11-26).**
  Each `turn` request carries the system prompt Python composed (environment, skills, the web page's notes, …) and
  the core uses it for that turn, so the model sees the same prompt on both cores. Found by R505: the web page's
  linked notes (R460) never reached the model on the Rust core. Test: `tests/test_web_r460.py` with `AURORA_CORE=rust`.
