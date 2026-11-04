# Aurora architecture

How the pieces fit together: the module map, the data flow, the boundaries,
and the mechanisms worth understanding before you change them. The numbered
requirements (what and why) are in `CHANGELOG_TECHNICAL.md` (R1 to R293) and
in `REQUIREMENTS.md` with `requirements/` (the newer ones). Update this file
whenever a change alters one of these shapes, not only when you add a
requirement.

## 1. The two halves: engine and front ends

Aurora is built around one hard boundary, enforced by a test
(`tests/test_architecture.py` checks the imports):

```
front end (tui.py / ui.py / headless.py / web/)  ──drives──>  Engine (engine.py)  ──drives──>  Provider (providers/*)
      ^                                                            |
      └──────────────────── Frontend protocol ─────────────────────┘
                     (the engine calls back into the front end)
```

- `engine.py` owns all conversation state: the provider and model, the
  `messages` history, the session, the config, and cost and token counts. It
  never imports a UI toolkit (no `prompt_toolkit`, no `print`, no `input`) and
  never touches the terminal. Anything the engine needs from a person goes
  through a `Frontend`.
- `frontend.py` defines the `Frontend` protocol, the only vocabulary the two
  halves share: `on_text`, `on_think`, `on_tool_start`, `on_tool_result`,
  `on_usage`, `notify`, `invalidate_status`, `approve`, `ask_continue`,
  `ask_secret`, `secret_challenge` and `cancelled`. If the engine needs a new
  kind of interaction, add it here first and then implement it in every front
  end.
- There are four front ends:
  - `tui.py`, the full-screen `prompt_toolkit` application.
  - `ui.py`, the classic inline REPL, used for `--classic` and when there is
    no terminal (pipes, CI). `tui.TuiFrontend` subclasses
    `ui.TerminalFrontend`, so most of the interaction logic (the text and
    options for approvals, continue questions and secret challenges) lives
    once in `ui.py`. The TUI overrides only what it draws differently
    (see §4).
  - `headless.py`, for `aurora -t` one-shot runs. It never asks: allowlisted
    tools run and the rest are denied, unless `--yes` is given (R311, R337).
  - `aurora/web/`, Aurora Lite (`aurora --web`, R353). `server.py` is the
    HTTP side, standard library only, serving the pages and the `/api/*`
    routes behind a bearer token. `core.py` holds the web `Frontend` and the
    one shared session: one engine, one turn at a time, with later messages
    queued in order (R373), and the events kept in memory so a page that
    reconnects can replay what it missed. Notes, attachments, chat exports,
    folders and shares each have their own module (`notes.py`, `attach.py`,
    `export.py`, `folders.py`, `shares.py`). The pages are static files
    (`index_v2.html` for `/`, `index_paper.html` for `/paper`, `index.html`
    for `/v1`) sharing one `app.js`, with `sw.js` and a manifest for offline
    use and the home screen.

The point of the boundary: a new front end means writing a new `Frontend`
implementation, not changing the engine, and the web version was built that
way. If something seems to need `print()` or a widget inside `engine.py` or
`agent.py`, the abstraction is leaking; add a `Frontend` method instead.

The import check is only as good as its parsing (R90a). A relative import
such as `from .ui import estimate_tokens` inside `engine.py` parses as
`module="ui", level=1`. The original check, `node.module.endswith(".ui")`,
never saw it, and that exact import sat in the engine for weeks while the
tests stayed green. The check now rebuilds the dotted name from `node.level`
and also looks at plain `import` statements. When both halves need a helper,
put it in a neutral module on the engine side and never import it from the UI
half. `tokens.py` is that module for `estimate_tokens` and `fmt_token_count`,
which `ui` re-exports for its existing callers.

### The front-end contract: `aurora/service.py` (R394)

A front end gets two things from the core. One is the engine object, passed
in and driven through the `Frontend` protocol above. The other is
`aurora.service`, a small facade for everything else a UI needs:

- `version()` and `aurora_home()`;
- sessions: `session_ids`, `session_exists`, `list_sessions`,
  `search_sessions`, `session_mtime`, `count_prompts`, `export_markdown`,
  `usage_by_model`, `new_session`, and `session_records` with
  `format_tool_call` for the HTML export (R421);
- the `models:` list in `config.yaml`, edited as text: `model_entries`,
  `model_add`, `model_update`, `model_delete`, `load_models`,
  `write_text_atomic` and `ModelEditError` (R423);
- remembered state: `load_state` and `save_state`;
- the key store's prompt: `set_secret_prompter`;
- checkpoints and undo: `checkpoints`, `diff_since`, `undo_preview`,
  `undo_diff` and `undo`;
- `ProviderError`.

`aurora/web/` may import nothing else from the core, so `session`, `rewind`,
`config`, `keystore` and `paths` can change freely as long as the facade keeps
its signatures. `tests/test_architecture.py::test_web_imports_core_only_via_service`
enforces this, resolving relative imports as described for R90a. Each facade
function looks up its target when called, so tests that patch
`aurora.session` still work. A new front end (or a daemon or API mode) should
use the same facade. If it needs more, add a function to the facade instead
of importing a core module.

`session_exists` checks every session id that comes from a page (read,
resume, delete, rename, move). It accepts exact ids only and costs one
`stat`, so call it freely (R395). The listing functions behind the facade
scan the sessions folder once per call; don't add per-session checks back
into them.

## 2. The agent loop (`agent.py`)

`run_turn(provider, model, messages, system, cb: AgentCallbacks, ...)` is the
loop between the model and the tools. It's driven entirely by callbacks
(`AgentCallbacks`), so it knows nothing about the UI and can be tested
directly with a fake provider and fake callbacks (see
`tests/test_core.py::FakeProvider` and `_cb`). One call is one user turn, a
`while True` loop that:

1. Calls `provider.turn(...)`, which streams text through `cb.on_text`.
2. If the response has tool calls, for each one: asks for approval if needed
   (`tools.NEEDS_APPROVAL`), runs the R58 secret scan (see §7), runs the
   tool, scans the output, and adds the (call, output) pair to `round_out`.
3. Writes `round_out` to the history with `_flush()`, one message per call
   through `tool_result_message()`. A provider may implement the optional
   `tool_results_messages()` hook instead, to write a whole round as one
   message if its API needs that for parallel tool results. No current
   provider does.
4. Repeats until the model stops calling tools (the final answer) or
   something stops it: a cancel, the step limit with the user choosing to
   stop, an approval answered "stop", or a secret challenge answered "stop".

The loop changes `messages` in place. `Engine.send` passes its own
`self.messages` list, so the history carries over between turns without the
loop knowing anything about the session or the engine.

In place is a contract (R154). `run_turn` holds that one list object for the
whole turn, so anything that rewrites the history during a turn must change
the same object. That's why `Engine.compact_history` does
`self.messages[:] = ...` instead of assigning a new list. A new list would
split the two: the engine would hold the new list while the loop kept adding
to and sending the old one, and nothing would look wrong.

Step 0 (R154) is `cb.maybe_compact()`, between rounds only. Before each
request after the first, the loop asks the engine whether the history needs
folding (auto-compact at 80%, see `_maybe_auto_compact_mid_turn`). The gap
between rounds is the only safe point, because there every assistant
`tool_calls` entry already has its matching `tool` results, so the list can
be rewritten without leaving an invalid sequence. The loop knows when it's
safe to ask; the engine owns the policy (the threshold, whether it's on, and
what a fold does).

Step 2 runs read-only calls at the same time (R94), and nothing visible
changes. Before the loop that handles calls one by one, every call whose name
is in `tools.PARALLEL_SAFE` is started at once through
`tools.run_tools_parallel`, and the loop then takes their results in the
model's original order. Keep this rule when you change it: only the waiting
is parallel. Tool starts, approvals, secret challenges, the transcript and the
history all stay in order. The user must never get two questions at once,
and `messages` must never depend on which read finished first.
`PARALLEL_SAFE` is an explicit list, not "every tool that needs no approval",
because the test is "read-only and no shared state". A tool that needs no
approval but keeps state is left out; `todo_write` (R93) was such a tool
before it was removed.

The fallback paths are deliberate. A malformed tool call gets one corrective
retry; after that the model falls back to plain chat for the rest of the
session (`turn.degraded`). A `ProviderError` is checked for known phrases
("timed out", "context" with "exceed", and so on) to give specific advice
instead of a raw exception. When you change one of these, add a case rather
than replacing the pattern. The per-requirement entries say what each one
guards against.

## 3. Providers (`providers/`)

`base.Provider` is the abstract contract. `turn()` returns a `TurnResult`
(text, tool calls, stop reason, token counts), and `assistant_message()` and
`tool_result_message()` turn Aurora's internal shapes into the API's format.
There is one implementation:

- `openai_compat.py` serves three kinds of backend: OpenRouter, a local
  llama.cpp server, and any other OpenAI-compatible server (LM Studio and
  others). Because of this, general fixes such as connect timeouts and
  retries live in one file but have to handle very different networks, from
  a local server reached over a VPN to a public API.

`make_provider(name, cfg, timeout)` in `providers/__init__.py` builds the
right provider from the `type:` field of a config entry.

Per-turn settings are attributes, not `turn()` parameters. `extra_body`,
`on_think` and `cache_prompt` (R91) are set on the provider by `Engine.send`
just before the call. Every provider implements `turn()`'s signature and a
dozen tests fake it, so adding a parameter for each new per-turn option would
change all of them for something only one implementation reads. New per-turn
options should work the same way.

### Prompt caching (R91)

`_system_message(system, cache)` returns either a plain string or a content
block with `cache_control: {"type": "ephemeral"}`. One marker covers both
kinds of OpenAI-compatible backend: OpenAI- and DeepSeek-style backends cache
long prefixes automatically and ignore the marker, while Anthropic models on
OpenRouter cache only at an explicit marker. Below `_CACHE_MIN_CHARS` nothing
is marked, because writing to the cache costs more than a plain read and
Anthropic won't cache a short prefix anyway. The system prompt is the only
sensible place for the marker: it's the one part of a request that stays the
same for a whole session, and it's sent again, and billed again, on every
tool step (R37).

### Network handling (all in `openai_compat.py`; read this before changing connection code)

Each of these exists because of a specific failure in real use:

- `_is_lan_host(base_url)` sorts a host into local or VPN (loopback, a
  private IP, `.local`, `.ts.net`) or a public API. The connect timeout is
  5 seconds for local hosts, to fail fast when a local server is off, and 20
  seconds for remote ones, because a public API's TLS handshake can be slow.
- `_is_bare_ip(base_url)` (R64) is the narrower check used to skip TLS
  verification. It's true only when the host is a literal private or loopback
  IP, never for a hostname (including `.ts.net`). A reverse proxy in front of
  llama.cpp usually has a certificate for one hostname, so reaching the same
  server by its LAN IP always fails verification even though the connection
  is trusted. Pass `verify=not _is_bare_ip(url)` to
  `HappyEyeballsTransport(...)` itself, not to `httpx.Client(verify=...)`:
  httpx ignores the client's `verify=` and `cert=` once you give it a custom
  `transport=`. Every place that dials a bare IP needs this: the pooled client
  in `openai_compat.py`, its `_probe()`, and the separate `/props` call in
  `engine.provider_health()`, which is easy to miss because it's outside
  `openai_compat.py`.
- `live_context_limit()`, the llama.cpp `/props` lookup, runs only for local
  hosts. On a remote provider it would waste a doomed request of about 6
  seconds on the UI thread at every status redraw and freeze startup.
- `_probe()` in `pick_endpoint()` calls `{base}/props` (llama.cpp's own route,
  with `/v1` removed) to choose quickly between the URLs in a `base_url` list,
  instead of waiting for a real request to time out. Any OpenAI-compatible
  endpoint placed in front of a local server, such as a gateway that combines
  a local llama.cpp server and OpenRouter behind one provider entry, must pass
  `/props` through. Otherwise every request looks unreachable and Aurora never
  gets past the probe.
- `happy_eyeballs.py` is a custom httpcore network backend that tries IPv4 and
  IPv6 at the same time and uses whichever connects first (RFC 8305). Without
  it, a host with both A and AAAA records, on a machine whose public IPv6
  route is dead (common with a VPN), waits out the whole connect timeout on
  IPv6 before trying IPv4. Against OpenRouter this went from 17 seconds to
  0.56. It isn't the same as forcing IPv4; the race also works on IPv4-only
  and IPv6-only networks.
- `turn()` retries a failed connection before streaming starts (its
  `_ATTEMPTS` loop). A pooled keep-alive connection that went stale while
  Aurora was idle resets when reused ("Connection reset by peer"). httpcore's
  own `retries=` covers only the connect phase, not a failure during the
  request, so `turn()` retries a temporary connection error itself, but only
  while `result.text` is still empty. A drop in the middle of the stream
  keeps what arrived, so nothing the user has already seen is repeated.
- Connection errors shown to the user never repeat the config word for word
  (`_provider_label` and `_connectivity_hint` in `agent.py`). A
  `ProviderError` message includes `provider.name`, the config key, which the
  user may have named after their own machine, and a local `base_url` may
  contain a personal hostname such as a VPN name. Aurora prints neither. The
  message says "local backend" for a local host, and the hostname only for a
  remote provider, since public domains such as openrouter.ai aren't
  personal and showing them helps. The suggested `curl` command follows the
  same rule. Config is user data, and Aurora doesn't echo it back.

### What a self-hosted server must provide

These are the calls Aurora makes to `providers.local.base_url` (llama.cpp's
OpenAI-compatible server). None of it is Aurora's own protocol; it's the
subset of llama.cpp's HTTP API that Aurora uses, so a server written from
scratch only needs to match this.

Required, for a llama.cpp server (`type: openai`):

- `POST {base_url}/chat/completions`: OpenAI-compatible chat completions with
  streaming (`"stream": true`, SSE `data:` lines ending with `data: [DONE]`).
  Aurora sends `messages`, `model`, `stream_options: {"include_usage": true}`
  and, if the model entry has `tools:`, `tools` in the OpenAI tool-call
  schema. It reads `choices[0].delta.content`,
  `choices[0].delta.tool_calls[].function.{name,arguments}` (both may arrive
  in pieces across chunks; Aurora joins them by index),
  `choices[0].delta.reasoning_content` (optional, for thinking models such as
  Qwen3; shown in the dim reasoning stream and never stored in the history),
  and the last chunk's `usage.{prompt_tokens,completion_tokens}`. The last
  choice's `finish_reason` becomes `TurnResult.stop_reason`. (See
  `openai_compat.py:turn()`.)
- Auth: `Authorization: Bearer <LLAMA_API_KEY>` when the provider has
  `api_key_env`. With no key configured the header is left out, which
  llama-server treats as open.

Optional, but used whenever the server is llama.cpp itself (not a generic
OpenAI-compatible backend such as OpenRouter):

- `GET {base_url without /v1}/props`, used for three things:
  1. Fast health checks (`_probe()`): a 2-second check per configured
     endpoint, so `pick_endpoint()` can switch between a LAN and a VPN
     `base_url` in seconds instead of waiting for the full request timeout.
  2. The live context limit (`live_context_limit()`): it reads
     `default_generation_settings.n_ctx`, so the status bar and `/status`
     show the real loaded context instead of a config default.
  3. Health in `/status` and the startup banner
     (`Engine.provider_health()`): it reads `model_path` (the file name is
     shown as "ready") and `n_ctx`. A reply missing either is reported as
     "reachable but the response changed", loudly, rather than shown blank.

  Without `/props` everything still works. The context limit falls back to
  `context_limit` in `config.yaml` (128k by default), and `/status` shows
  "remote API (no health endpoint)" instead of an error. Aurora handles
  OpenRouter and LM Studio this way, so it's a supported setup.

## 4. The TUI's three areas (`tui.py`, R53)

The full-screen layout is an `HSplit` of exactly three areas. Treat this as a
rule for new features too: put each one in the area that matches its role.

1. Chat and scrollback (`_ChatControl` in a `Window`). This is the only area
   that scrolls, and the mouse wheel, PgUp and drag-select work here.
   Fragments are cached per entry (`_cache`), because a long session adds
   thousands of chunks and parsing the whole ANSI stream on every keystroke
   would be O(n²).
   - It's drawn from an incremental line cache (R289). `_ChatControl`
     overrides `create_content` and serves `Tui._render_lines()` through a
     lazy `UIContent.get_line`, so a frame reads only the rows the window
     asks for, which are the visible ones. The stock `FormattedTextControl`
     split and hashed the whole transcript about 2.6 times per frame (63 ms
     at 9,600 lines, 7.6 ms after the change). `_rebuild_locked` records the
     first line a trim or append can affect (`_split_line`), and only lines
     from there are split again.
   - It's capped at `_SCROLLBACK_MAX_LINES` (R152). Aurora's own flattening
     doesn't grow with session length (R96b), but the fragment list goes
     whole to prompt_toolkit's `create_content`, which is linear and runs
     before prompt_toolkit's own cache. The only way to speed that up is
     fewer fragments. Trimming clears any selection instead of moving it,
     because `_sel` and `_sel_frozen` are absolute line positions.
2. The input area: `self.input` (a `TextArea`) and, above it, a separate
   window for `select()` menus. Two mechanisms live here and are easy to mix
   up:
   - `ask()` (free text: passphrases, comments) uses the `TextArea`'s own
     `prompt=` (a `BeforeInput` processor), which is fine for one line.
   - `select_menu()` (multiple choice: approvals, confirmations, R58
     challenges) draws in its own `Window(FormattedTextControl(...))`, not in
     the input prompt. A multi-line prompt drawn with `BeforeInput` shows
     each newline as a literal `^J`, a staircase of garbage. That bug shipped
     once; don't bring it back by moving the menu into the input prompt.
   - A menu label can contain raw ANSI colour (R59). The `[$]` and `[free]`
     tags in `/model` use the same `GREEN` and `YELLOW` constants the classic
     REPL prints. `_menu_fragments` parses each label with
     `ANSI(label).__pt_formatted_text__()` instead of treating it as plain
     text, which would show the escape bytes as garbage. Each parsed piece's
     colour is layered onto the row's base style (selected or not), so a
     plain label (approval and confirm menus have none) looks the same as
     before.
3. The status bar (two lines, `FormattedTextControl`). It's read-only and
   never scrolls or takes input. Line 1 shows identity only: model, context,
   session id, and the mode (`prompt mode` or `bash mode`). Line 2 shows
   key hints by default, replaced entirely by whichever temporary status is
   live. The order is exit question, waiting for an answer, copy notice,
   busy spinner, then key hints; the first that applies wins, and they never
   appear together.

The TUI replaces `builtins.input` while it runs (`Tui.run()`). `input()`
becomes `self.ask()` and `ui.select` becomes `self.select_menu()`. That's how
the interaction code in `ui.py`, written for plain `input()` and `select()`,
draws differently in the TUI without being duplicated. Both are restored on
exit in a `finally` block.

Mouse handlers can crash the whole app. prompt_toolkit's own
`CompletionsMenuControl.mouse_handler` asserts that a `complete_state` is
active. A click after the completion menu closed (for example when you come
back to the terminal window) raises, and any exception in a handler ends
`app.run()`. `_SafeCompletionsMenuControl` shows how to guard a library
handler we don't own: check the precondition and ignore the event if it's
missing. Any new float or control built on a library class needs the same
care.

## 5. Storage: several separate stores, on purpose

| File | Lives in | What | Written by |
|---|---|---|---|
| `config.yaml` | Your checkout (only `config.yaml.example` is in the repo). Some setups commit and share it. | Providers, models and `runtime.*` defaults, including `secret_allowlist` (SHA-256 hashes only, see §7) | `persist_runtime_value()`, used by `/redact`, `/redact allowlist [clear]` and `/multiline`; the web page's model editor (R423, R427) |
| `AURORA_HOME/config.local.yaml` | This machine | Settings for this machine only, merged over `config.yaml` (R316) | You, or the web page's YAML editor (R427) |
| `AURORA_HOME/state.yaml` | This machine | `last_model` and `last_provider` (R51), and the web page's own `web_last_model`, `web_last_provider` and `web_auto_approve` | `config.save_state_values()` |
| `AURORA_HOME/allowlist.yaml` | This machine | "Always allow" approval rules. (Not related to the secret allowlist above; same word, two features.) | `approve.add_rule()` |
| `AURORA_HOME/denylist.yaml` | This machine | "Always DENY" approval rules. They're never asked again and no allow rule overrides them. | The approval prompt's "Always DENY" |
| `AURORA_HOME/sessions/<id>.jsonl` | This machine | Every turn, tool call and approval, append-only. Past `session.SESSION_LOG_MAX_BYTES` (5 MB) a session continues in `<id>.2.jsonl`, `<id>.3.jsonl` and so on, so one session id maps to a sequence of parts, oldest first. Every reader in `session.py` walks that sequence through `_parts_for()`. Nothing is deleted; only the file a record goes to changes. | `Session.log()` |
| `AURORA_HOME/keys.enc` and `keys.salt` | This machine | The optional Fernet-encrypted key store and its PBKDF2 salt (see the keyring note below) | `keystore._encfile_save()` through `paths.write_bytes_atomic` (mode `0600`) |
| `AURORA_HOME/bootstrap.md` | This machine | The saved global bootstrap prompt; a project's `.aurora/bootstrap.md` takes precedence | `/bootstrap set` |
| `AURORA_HOME/trusted_bootstraps.json` | This machine | Project bootstrap prompts approved to run, as path to SHA-256 of the approved content; editing the prompt makes Aurora ask again (R286) | `bootstrap.trust()` |
| `AURORA_HOME/checkpoints/<hash>/` | This machine | Shadow git repo with snapshots taken before each change (R47), capped at `rewind.RETENTION` (R151) | `rewind.checkpoint()` |
| `AURORA_HOME/spend.json` | This machine | Daily spending totals for `/budget day`, 60 days kept (R340) | The engine |
| `AURORA_HOME/try/<timestamp>/` | This machine | Throwaway worktrees for `/try` (R342) | `trybox.py` |
| `AURORA_HOME/web-notes/`, `web-chats.json`, `web-shares.json` | This machine | The web page's notes (one `.txt` per note plus `index.json`), chat folders and share links | `web/notes.py`, `web/folders.py`, `web/shares.py` |
| `AURORA_HOME/workspace/input/` and `workspace/outputs/` | This machine | Files attached in the web page, and files Aurora writes for you (R413, R432) | `web/attach.py` |

Every one of these files is written atomically: `paths.write_text_atomic`
for text (R146a) and `paths.write_bytes_atomic` for the key store (R201).
Both write a temporary file next to the target, `fsync` it, `os.replace` it,
then `fsync` the folder with `_fsync_dir` (R171/I6, R234). A plain
`write_text()` over the live file empties it first, so a crash in the middle
used to leave `config.yaml` empty and `load_config` failing on the next
start. Without the folder `fsync`, a power cut can lose the rename itself and
bring back the old contents, which for `keys.enc` means every stored key. The
two writers share `_fsync_dir` so they can't drift apart; they had, and the
key store's writer was the one missing it. Any new stored file gets the same
treatment.

Only the checkpoints folder has a retention policy (R151): `RETENTION`
snapshots per project and `UNDO_TAG_RETENTION` undo tags, pruned on a
background thread every `_PRUNE_EVERY` checkpoints. It's the one store that
grows with every change rather than every setting. A delete loop wasn't
enough, because old commits can't be removed while they're ancestors of
HEAD. `prune()` marks the Nth commit as a shallow root (the mechanism behind
`git fetch --depth`) instead of rewriting history, which would change every
hash, including ones `/rewind` has already shown you. The `undo-<hash>` tags
mattered even more than the chain: each one kept an orphaned commit
reachable, so without pruning the growth could never be reduced. Session
logs still grow forever, by design (R20).

The split between `config.yaml` and `state.yaml` is real. `config.yaml` can
be shared between machines (providers, the model list, feature defaults).
`state.yaml` is runtime state for one machine that would be wrong to share,
such as which model you last used on this machine. A new stored setting has
to pick one; ask "does this make sense on every machine?".

The allowlist matches normalized command words (`approve._norm_command`,
which splits with `shlex` and expands `~`), not raw strings. `bash ~/x.sh`,
`bash "/home/me/x.sh"` and `bash /home/me/x.sh` must all match one saved
rule. This was a real bug: each spelling was a different string, so "always
allow" never fired for the next spelling the model happened to use.

`SAFE_COMMANDS` (R67) lets read-only commands match any arguments. The
default rule stores the first two words of a command (`find /path/A`) and
matches that prefix exactly, which is right for anything that can write,
delete or run code, since a bare `rm` must never approve `rm -rf /`. For a
chosen set of read-only commands (`find`, `ls`, `tree`, `grep`, `cat` and
others) that was too strict: "always allow" on one path never covered the
same command on a different path in another project, which looked like "the
allowlist doesn't persist" even though the file was fine. For a
`SAFE_COMMANDS` entry, `add_rule()` stores just the command name and
`is_allowed()` matches it whatever the arguments, unless the call uses one of
that command's writing flags (see FEATURES.md).

The OS keyring lives outside `AURORA_HOME` (R60). It's the macOS Keychain or
SecretService, used through `keyring.{get,set,delete}_password` under the
service name `"aurora-agent"`, a store of its own rather than a file. This is
true of the keyring only: the optional fallback, `keys.enc`, is a file under
`AURORA_HOME`, which is why `aurora wipe` removes it with everything else. It
also explains the order in `aurora wipe`. It first clears the keyring entry
for every `api_key_env` and `token_env` the config uses (`keystore.clear_key`
over `_known_key_names()`, not a fixed list), and only then deletes
`AURORA_HOME`. Deleting the folder alone would leave stored keys behind and
quietly keep the user logged in. `aurora key clear <VAR>` uses the same logic
for one key. When you test delete or clear logic, mock both the keyring
module and `AURORA_HOME` in the same run. Mocking only one of them has
deleted real Keychain entries before.

## 6. Threading model

- The main (UI) thread runs the `prompt_toolkit` application event loop
  (`self.app.run()`). It must never block: no network calls, no `input()`.
  Any blocking question (`ask()` or `select_menu()`) raises `RuntimeError`
  when called from this thread, so it fails loudly instead of deadlocking,
  because this is the thread that would have to deliver the answer.
- The worker thread (`Tui._worker`) runs one loop that takes submitted lines
  from `self._inbox` and calls `ui._send_turn` or `_handle_command` (R96f;
  `_send_turn` directly, not through a per-turn wrapper thread, see below).
  `Engine.send()` and the whole agent loop run here. This thread may block on
  the network or on `ask()` and `select_menu()`, which hand the question to
  the UI thread through a `queue.Queue`.
- The side worker thread (`Tui._side_worker`, R245, R253) is a second
  consumer. `_route_submitted_line` feeds it, and only while the main worker
  is busy. It runs what shouldn't wait for a turn: a `!` bash command, a
  read-only `/command`, `/model`, or a prompt answered as a side question.
  - What it may do is defined by what it must not do: it never writes
    `engine.messages` or the session, so the history keeps a single writer.
    `Engine.quick_ask` is what makes a side question possible without a
    second writer: it reads a snapshot of the history and writes nothing
    back, instead of becoming a second turn.
  - Once there's a second thread, more state turns out to belong to the
    turn. A provider carries `on_think`, `extra_body`, `cache_prompt` and
    `notify`, all set by `send()` for the turn in progress, and `fe.on_text`
    is that turn's renderer. So a side request builds its own provider
    (`_side_provider`, cached per provider like the turn's) and uses neither.
  - The general rule: anything a turn sets by assignment, or works out from
    its own output, isn't safe to borrow from the side channel. R254 found
    four cases: the provider's per-turn callbacks, the front end's renderer,
    `append()` merging into the previous entry, and `append()` closing the
    thinking row on the assumption that output means the request has
    stopped thinking.
  - R255 found three more on a path with no second thread at all. The
    one-off side requests (the approval prompt's Explain, the commit draft,
    the compaction summary) each borrowed `provider.on_think`, which `send()`
    sets per turn, so their reasoning showed up in the running turn's
    thinking row. Being on the same thread isn't the same as sharing the
    turn's context: the rule is about what the turn owns, not about
    concurrency. `providers.base.side_completion` is the one correct way to
    make such a request.
  - The same goes for checks, the other way round: the side path must make
    every check the main path makes. The R58 secret scan was skipped there
    and leaked a token to the provider and the session log (R254g). It now
    refuses instead, because the challenge is interactive and the side path
    may not own the input line.
- There's no per-turn wrapper thread in the TUI (R96f). `ui._run_turn` runs a
  turn in its own thread so the main thread can catch `KeyboardInterrupt`
  while `input()` blocks. That only makes sense in the classic REPL, where the
  turn runs on the main thread. The TUI's `_worker` is already a background
  thread, and SIGINT only ever reaches the main thread (prompt_toolkit also
  keeps the terminal in raw mode, so `^C` never becomes a signal; the TUI
  cancels with `fe.cancel_event.set()` from the Esc menu). So the TUI calls
  `ui._send_turn`, the body of `_run_turn`, directly on the worker thread.
  That cuts the depth of "which thread is a mid-turn question on" from four
  levels to three.
- The ticker thread is cosmetic. It redraws the app every 0.5 seconds while
  busy, to animate the spinner and the elapsed time.
- The startup health check (`Tui._banner` and `ui._banner`, both calling
  `Engine.provider_health()`) also runs on the main thread, but before
  `app.run()` starts the event loop. Nothing is on screen yet, so a stuck
  call would make the whole app look dead at launch (R66).
  `provider_health()` runs the actual check on a throwaway daemon thread and
  waits at most `timeout` (4 seconds by default) with `thread.join(timeout)`.
  If the check hasn't answered by then, startup goes on with an "unknown /
  timed out" result. A health check must never block the thing it's
  checking.
- The context limit refresh (`Engine._context_limit_nonblocking`, R95i)
  applies the same rule to normal redraws. `status()` calls
  `context_stats()` on the UI thread for every frame, and for the `local`
  model the limit is a live `/props` lookup behind an endpoint check. When
  the backend was down, the app froze for about 6 seconds every time the
  120-second cache expired. The lookup now runs on a daemon thread and the
  redraw gets the cached value (or `Provider.static_context_limit()`, the
  offline answer, before the first lookup finishes). A stale value is better
  than a frozen screen.

  Use this shape for anything the status bar shows: if getting the answer
  can touch a socket, the redraw gets the last known value and a refresh
  runs in the background. The redraw never makes the call itself.

A blocking question is a hand-off through a queue. The worker thread calls
`ask()` or `select_menu()`, which sets state (`self._question` or
`self._menu_*`), asks the UI thread to redraw so the question appears, and
then waits on `self._answers.get()`. The UI thread's key handlers `put()` the
answer when Enter or a menu choice fires. This is the only synchronization
between the two threads for questions. Only the worker thread touches the
session and message state, so that state needs no lock of its own.

The input line does need one (R253). `ask()` and `select_menu()` write single
slots (`_question`, `_menu_prompt` and `_menu_options`, `_saved_draft`) and
wait on the one `_answers` queue. Once a second thread could ask, two
questions could overwrite each other and neither would ever be answered. The
Esc confirmation had already hit this once (R142a). `_prompt_lock`, an
`RLock` because one command may ask more than once, is held for as long as a
command is asking. The main worker waits for it, because whoever holds it is
showing the user something. The side worker marks its thread
`_prompt_nowait` and gets an `InputLineBusy` refusal instead, because waiting
there would defeat a channel that exists so questions never wait. The lock
protects the terminal's one input line, not engine state, which still has
exactly one writer.

## 7. Secret redaction (R58): the engine decides, the front end draws

This feature touches every layer above, so it's a good template for the
next similar one.

- `secrets.py` only detects and redacts; it has no UI and no I/O:
  `scan(text) -> list[Match]`, `redact(text, matches) -> str` and
  `preview(matches) -> str`. The preview summarizes the kinds and counts and
  never shows the secret itself, not even in the challenge. Detection has
  two passes:
  - Known vendor shapes, by regex: `AKIA…`, `ghp_…`, `.env`-style
    assignments, and a GUID/UUID pattern, because some systems use those as
    API keys or session tokens.
  - An entropy check for tokens with no recognizable prefix, such as an
    internal tool's random token, which no shape regex would match. It only
    looks at text the first pass didn't already claim, so a UUID counts
    once, as `"GUID/UUID"`. It skips hex-only strings (git SHAs, MD5 and SHA
    digests) on purpose, because they're a common false positive.
  - Source code and word-shaped names are skipped (R328).
- There are three main scan points, chosen so the session log always matches
  what was sent, with no separate check for the log:
  - `Engine.send()` scans `user_text` before it enters `messages` and the
    log.
  - The tool loop in `agent.py` scans each tool's raw output before
    `_finish()`, which is before `on_tool_result` and before it becomes a
    `tool_result_message`. This covers every tool the same way, including
    read-only ones such as `read_file` and `grep` that never go through the
    approval prompt.
  - `run_command`'s own command string is scanned just before it runs. This
    one is deliberately different: it only calls `notify()`. It never
    challenges, blocks or redacts, because a command usually needs its real
    argument (a real key in a curl header) to work, so changing it would
    break it, and blocking would repeat the approval the call already
    passed. No other tool's arguments are scanned before running; a
    `write_file`'s content is checked when it's later read back with
    `read_file`, through the normal output scan.
- A fourth scan point is the `/commit` draft (R284). The staged diff is new
  content from disk sent to the provider, so `ui._commit_cmd` scans exactly
  what `draft_message` would send and runs the same challenge. Stop means no
  draft; redact sends the redacted diff.
- The approval prompt shows control characters literally (R287).
  `colors.visible()` turns every C0 and C1 control character, DEL, and every
  bidirectional or zero-width character into a visible escape (`\x1b`,
  `‮`) in everything the prompt prints. SGR colour codes are otherwise
  kept, and SGR 8 (conceal) could hide the end of a command being approved.
- Turning the feature on or off changes what the engine passes in, not a
  branch inside `agent.py`. `Engine` passes
  `AgentCallbacks.secret_challenge=fe.secret_challenge` only when
  `self.redact_secrets` is true and `None` otherwise, and the loop's
  `if cb.secret_challenge:` check skips scanning entirely. When the feature
  is off it costs nothing, and `agent.py` never needs to know about the
  config.
- There's one challenge per block, not per match. A `.env` file with ten
  keys produces one `secret_challenge(context, matches)` call with all of
  them, and the answer (keep, redact, stop, always) applies to every match in
  that block.
- `Frontend.secret_challenge` is implemented once in `ui.TerminalFrontend`,
  as a numbered menu through the shared `select()`, and the TUI inherits it,
  as with `approve` and `ask_continue`.
- Confirmed false positives can be allowed for good. A fourth answer,
  `"always"`, stops a repeated false positive (a test UUID, an internal
  tool's token) from being flagged again. Only `Engine` knows what "always"
  means. `Engine._secret_challenge()` wraps `fe.secret_challenge()`. On
  `"always"` it hashes every matched value with `secrets.hash_value()`
  (SHA-256; the raw value is never stored), adds the hashes to
  `self.secret_allowlist`, saves them with
  `persist_runtime_value(cfg, "secret_allowlist", …)`, and returns `"keep"`.
  `send()` and the agent loop only ever see the original three answers.
  `Engine.__init__` loads the saved hashes into `self.secret_allowlist` and
  passes them to every `secrets.scan(text, allowlist)` call (directly in
  `send()`, and through `AgentCallbacks.secret_allowlist` in the agent loop).
  `scan()` drops any match whose hash is in the set before it's shown.
  Manage the list with `/redact allowlist [clear]`.

## 8. The agent loop's one limit: steps (R6, R9)

`agent.run_turn()` checks once per round, while handling tool calls:
`if iteration >= checkpoint: cb.ask_continue(iteration)`. It only fires when
the model keeps calling tools. After each check the limit is set again
(`checkpoint = iteration + max_iterations`), so continuing gives one more
full interval before the next question, rather than asking every round.

This is the only limit on looping. A time-based limit (`max_wait` and
`ask_wait`, R61) once existed alongside it, but it fired on elapsed time even
when the model wasn't looping, so one long but normal reply (a big local
model, a slow network) got a "still working, continue?" question. It was
removed at the user's request; see `CHANGELOG_TECHNICAL.md` R61 for the call
sites that went with it.

## 9. Esc, pressed twice (R62), and why it needed a non-blocking menu

Three states share one Esc gesture: busy, bash mode, and idle with an empty
prompt. The first press arms the action and shows a status-bar hint. A second
press within 2 seconds opens an arrow-key Yes/No question, and nothing happens
until the user picks an answer. `Tui._on_escape(app_exit)` is a plain method,
not a closure over the key binding's `event`, so tests can call it directly.
Sending real Esc bytes through a pipe runs into prompt_toolkit's delay for
telling a lone Esc from an escape sequence, which made timing-based tests
flaky.

It can't simply call `select_menu()` (the blocking mechanism from §4 and §6).
Esc fires as a key binding, which runs on the UI thread. `select_menu()` waits
on `self._answers.get()`, and the answer comes from another key binding
(Enter or a digit), also on the UI thread. Calling `select_menu()` from a key
binding would make the UI thread wait for an answer only the UI thread can
deliver. That's the deadlock `ask()` and `select_menu()` already prevent by
raising `RuntimeError` on that thread (§6).

So there's a second, non-blocking menu, `Tui._open_ui_menu()`. It reuses all
of `select_menu()`'s drawing (`_menu_fragments` and the arrow-key and digit
bindings, all keyed off `self._menu_options is not None`; Esc does nothing
while a menu is open, so the pick must be explicit), but it resolves
differently:

- `select_menu()` (callers on the worker thread: approvals, R58, `/model`):
  `_resolve_menu()` puts the chosen key on the `self._answers` queue.
- `_open_ui_menu()` (callers on the UI thread: the Esc confirmations): sets
  `self._menu_on_select = on_select`. `_resolve_menu()` checks this first
  and, if it's set, clears the menu and calls `on_select(key)` directly
  without touching the queue, so no thread ever waits.

There's one resolver per state: `_resolve_cancel_menu` sets
`fe.cancel_event`, `_resolve_bash_leave_menu` sets `self._bash_mode = False`,
and `_resolve_quit_menu` calls the injected `app_exit()`. All three require
an explicit choice rather than treating two presses as a yes.

A single Esc must act quickly (R264). The Alt bindings that start with Esc
(`escape+enter`, `escape+m`, `escape+end`) make a lone Esc the start of a key
sequence, which prompt_toolkit holds for `Application.timeoutlen` (1 second
by default), not only for `ttimeoutlen`, the byte-level wait. Both are
lowered (`ttimeoutlen=0.001`, `timeoutlen=0.05`). An Alt chord arrives from
the terminal as one burst, so it still works. Adding another binding that
starts with `escape` is fine; raising `timeoutlen` brings back the 1-second
lag on every Esc.

The 2-second window is a real timer. It's stored as
`(self._esc_armed: str | None, self._esc_armed_at: float)`: which action is
armed and when. A second Esc after the window, or while a different state is
active, counts as a new first press: it arms again and never confirms. This
matters because the state can change between the two presses some other way
(for example leaving bash mode with Backspace on an empty line), and an old
arm must never fire on an unrelated later Esc.

## Where to look next

- Requirements: `CHANGELOG_TECHNICAL.md` for R1 to R293, and
  `REQUIREMENTS.md` with `requirements/` for the newer ones.
- What users see: `README.md` and `documents/FEATURES.md`; the Esc rule is in
  `documents/COMMANDS.md`.
