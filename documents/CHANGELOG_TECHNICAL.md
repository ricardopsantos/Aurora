# Aurora — micro terminal coding agent

> **Repo = source of truth.** Runs on macOS and Linux; synced between
> machines via git. This document is the full specification: agreed
> requirements, build plan, test plan, and usage — written **before** the
> first line of code, and updated as the build progresses.
>
> **STATUS (2026-07-10): v0.1.0 — all seven phases built, tested, and live.**
> Everything added beyond the original R1–R24 is specified in
> **§5 As-built additions**. Quick-start lives in README.md.

---

## 1. Requirements (agreed, final)

### Providers & models
- **R1.** *(Built pre-2026-07-10: Anthropic provider — default model
  `claude-sonnet-5`; opus/haiku in the picker. See git history for the full
  original spec.)* **Removed 2026-07-15** at the user's request: OpenRouter-
  compatible models only, for now. `providers/anthropic.py` deleted,
  `providers/__init__.py`'s factory now always returns
  `OpenAICompatProvider`, `config.yaml`'s `anthropic` provider block and
  `claude-*` model entries removed — historical numbering preserved, not
  reused for something else. See R74.
- **R1b.** OpenRouter provider — via the same `openai_compat` code
  (`https://openrouter.ai/api/v1`, `${OPENROUTER_API_KEY}` through the
  keystore). Models listed in config appear in the picker with a `$` tag;
  cost in the footer from OpenRouter's cached per-model price list; per-model
  `tools:` flag applies as everywhere.
- **R2.** Local provider — llama.cpp (or any OpenAI-compatible server) via
  its OpenAI-compatible API, optionally over TLS with a bearer key (see R23).
  Aurora consumes **whatever model is currently loaded**; it never launches
  or manages `llama-server` itself.
- **R3.** *(Removed 2026-07-23 — LlamaDesk integration torn out; the
  picker no longer has a "library" section, only configured models.)*
  ~~Optional local model **library switching via LlamaDesk** (a companion
  loading agent, `/api/models`, `/api/status`, `/api/switch`): the picker
  shows Anthropic models ($), the currently-loaded local model (free,
  ready), and the LlamaDesk library (free, needs a ~1–2 min load). Loading a
  library model requires an explicit **eviction confirm** (a switch is
  global — it unloads the model for every other consumer of that server) and
  checks for in-flight work first. Not configured? The picker just shows the
  local/Anthropic/OpenRouter models from `config.yaml` — LlamaDesk is
  entirely optional (see the commented block in `config.yaml`).~~
- **R4.** Fast switching: `/model` picker. Cross-provider switches flatten
  history to a plain-text transcript (tool blocks don't translate 1:1 between
  APIs); same-provider switches keep structured history.
- **R5.** Per-model `tools: true|false` config flag. Malformed tool calls from
  a local model: one corrective retry, then a second failure auto-degrades the
  session to chat mode with a visible notice — never a cryptic crash.

### Coding agent
- **R6.** Tool loop: `read_file`, `write_file`, `edit_file`, `run_command`,
  `list_dir`, `grep`, `open_context_doc`, `web_search`, `web_fetch`.
- **R7.** Approval gate on writes and commands: `y` / `n` / `a`(lways).
  "Always" persists to a **pattern allowlist** (`AURORA_HOME/allowlist.yaml` —
  command prefixes, path globs), surviving restarts. `/allowlist` to review.
  Reads and web tools are free.
- **R8.** Diff preview before every write/edit approval.
- **R9.** Loop cap: **max 5 tool iterations** per turn, then Aurora pauses and
  asks approval to continue. *(The original `/max N` live-change command was
  removed alongside R61's teardown — `runtime.max_iterations` in config.yaml
  is now the only way to change it; see ARCHITECTURE.md §8.)*
- **R10.** `!cmd` passthrough runs bash locally with no LLM involved.
- **R11.** Skills: `/name args` runs python/executable skills from `skills/`
  (ported from the Terminal-Agent V2 prototype); `/skills` lists them.

### Context & memory
- **R12.** *(Removed 2026-07-29 at the user's request: this built-in
  integration — `context.py`/`memory.py`, `open_context_doc`, `/remember`,
  `/agentic_report` — was replaced by the `agentic_context_mcp` MCP server,
  which exposes the same operations as regular approval-gated MCP tools
  instead of hardcoded Aurora code. See `config.yaml`'s `mcp_servers:`.)*
  ~~**agentic_context protocol** (see `~/repositories/AgenticContext`): when
  the cwd has `.agentic_context/`, Aurora bootstraps it at start —
  `AGENTS.md` (rules AND personality; the user shapes who Aurora is through
  the context system, not code), the three `INDEX.md`s, `[CORE]` docs,
  `rebuild-index.sh` self-heal. The per-task protocol (consult MEMORY before,
  write qualifying findings + rebuild after, flag `[PROMOTE?]`) is embedded in
  the system prompt so Aurora runs it herself; her writes pass the normal
  approval gate. `open_context_doc` lazy-loads docs on summary match.~~
- **R13.** Live footer, updated every turn:
  `model │ tokens used/max (%) │ $cost │ session-id`. Token counts are the
  provider's own `usage` from the response. The context LIMIT comes from
  llama.cpp's live `n_ctx` via `/props` for the `local` model, and from the
  per-model table (`providers/remote_context_limits.json`) → config's
  provider-level `context_limit` → a 128k default for any remote model
  (R71). Cost renders only when that model's per-token price is actually
  known (R73) — never a bare `$0.00` implying "free". *(This requirement
  originally described Anthropic's static limit/price tables; that provider
  was removed 2026-07-15, R74 — rewritten 2026-07-22 to the OpenAI-compat
  reality, no behaviour change.)*
- **R14.** **`/compact`** summarizes history into one message and continues;
  **`/clear`** starts fresh; automatic warning at ~80% context.
- **R15.** *(Built pre-2026-07-15: Anthropic **prompt caching** —
  `cache_control` on the system prompt, so the bootstrap docs resent every
  turn cost ~90% less. See git history for the original spec.)* **REMOVED
  2026-07-22.** It was an Anthropic-API-specific mechanism and died with the
  provider (R74a); nothing implements it today, so the requirement is
  retired rather than left standing as a phantom. The underlying cost is
  real and unaddressed: the system prompt (base + `AGENTS.md` + three
  indexes + every `[CORE]` doc) is re-sent on every request, including every
  tool iteration of a turn, and `billed_input` bills each one (R37). If this
  comes back it is a NEW requirement against OpenAI-compatible caching
  (OpenRouter `cache_control` breakpoints), not a revival of this one —
  historical numbering preserved, not reused for something else.
  **That new requirement landed the same day: see R91.**

### UX
- **R16.** Streaming output, plain text (byte-faithful for copy).
- **R17.** **Esc cancels** the current generation or tool loop and returns to
  the prompt without exiting the app. Ctrl+C no longer interrupts — it only
  clears the input line, never exits — because it kept firing by accident
  (see the no-instant-quit-keys UX principle). Esc is the single control key:
  close a menu → cancel the in-flight request → dismiss the exit prompt →
  clear the input → offer to exit.
- **R18.** **Multi-line input**: bracketed paste (pasted newlines don't
  submit) + **Ctrl+J** for deliberate multi-line composition.
- **R19.** **`/copy [N]`** puts the Nth-last full response (raw markdown) on
  the clipboard — OSC52 first (works through SSH), `pbcopy`/`wl-copy`/`xclip`
  fallback.
- **R20.** **All conversations durably logged**: every turn, tool call/result,
  approval decision, model switch, and error appended to
  `AURORA_HOME/sessions/<id>.jsonl`. Nothing auto-deleted. `/export` writes
  the conversation as readable markdown. `aurora --continue` resumes the last
  session; `/resume` picks from a list.

### Install & security
- **R21.** One-command install: `./install.sh` — prompts for the data dir
  (default `~/.aurora`, user-selectable → `AURORA_HOME`), creates a venv,
  installs, symlinks `aurora` into PATH. Pure-python deps (`httpx`,
  `PyYAML`, `prompt_toolkit`, `keyring`, `cryptography`, `ddgs`), so macOS
  and Linux behave identically. Machine sync = `git pull`; nothing
  machine-specific is committed (keys via env/keyring, endpoints in config
  with env overrides).
- **R22.** API-key storage, first hit wins: ① env var (the standard
  `/etc/environment`-style pattern for a self-hosted box) ② OS keyring
  (macOS Keychain — encrypted, zero friction) ③ opt-in Fernet-encrypted file
  with startup passphrase ④ prompt once and offer to store. Never plaintext
  on disk.
- **R23.** Requests to a local model should be encrypted **in transit** if
  the server is reachable over a network at all — a VPN/tailnet
  (e.g. Tailscale/WireGuard) end-to-end, TLS via a reverse proxy in front of
  it, and llama-server's own native `--api-key` bearer auth are the
  recommended layers. No hand-rolled crypto, ever. (How you wire this up is
  specific to your own server setup — Aurora only needs a `base_url` and,
  optionally, a bearer key via `LLAMA_API_KEY`.)
- **R24.** *(folded into R23 above — historical numbering preserved, not a
  gap.)*

### Out of scope (v1)
MCP servers · sub-agents · rich markdown rendering (plain streaming text
keeps `/copy` byte-faithful) · multi-stage pipelines from the V2 prototype
(one model with tools replaces LLM1→LLM2) · the Google provider.

---

## 2. Build plan

As built (v0.1.0). The **engine/UI split** became a hard architectural rule
during the build: engine modules never import a UI toolkit or do terminal
I/O; the ONLY surface between the halves is `frontend.py`'s `Frontend`
protocol, and `tests/test_architecture.py` enforces the boundary with AST
checks. Swap the UI (HTML, websocket) by implementing `Frontend`.

```
aurora/
  __main__.py      # entry: aurora | --continue | --man | key set | config path
  config.py        # yaml + ${ENV} expansion + /max write-back
  providers/       # base, openai_compat (streaming, tool use, thinking
                   #   channel, extra_body), happy_eyeballs (R: RFC 8305)
  engine.py        # ENGINE FACADE: all conversation state; the API a UI drives
  frontend.py      # the engine ⇄ UI Protocol (streaming, approvals, secrets)
  agent.py         # tool loop: cap w/ continue-blocks, s/c/n-reason approvals,
                   #   loop-nudge on repeated calls, Esc cancel
  tools.py         # the tools (R6) + 60k-char output cap + the R94
                   #   parallel-safe read-only set
  approve.py       # gate + persistent pattern allowlist + diff preview
  context.py       # agentic_context bootstrap + open_context_doc tool
  ui.py            # prompt_toolkit REPL: banner, streaming+markdown, footer,
                   #   picker, autocomplete, keybindings, all slash commands
  mdrender.py      # streaming line-based markdown → ANSI (display only)
  colors.py        # ANSI palette (NO_COLOR / non-tty aware) + diff colouring
  man.py           # `aurora --man` manual page
  compact.py       # history flattener (/compact + cross-provider switch)
  clipboard.py     # OSC52 + pbcopy/wl-copy/xclip
  keystore.py      # env → keyring → Fernet file → prompt (+ key_fetch, injectable prompter)
  skills.py        # /skills · /name args (repo skills/ + AURORA_HOME/skills/)
  session.py       # JSONL logging + --continue/resume/export (streamed)
  paths.py         # AURORA_HOME resolution
  tokens.py        # local token estimate/format (engine-side, R90a)
  extensions.py    # discovers AURORA_HOME/extensions/ + extensions_bundled/ (R119)
  extensions_bundled/mcp_extension.py  # bundled: MCP client as an extension (R119)
  extensions_bundled/lint_extension.py # bundled: lint_check tool (R121)
  extensions_bundled/web_extension.py  # bundled: web_search (ddgs) + web_fetch (R157)
  mcp.py           # MCP (Model Context Protocol) stdio client (R119)
install.sh         # venv + editable install + PATH symlink + data-dir marker
config.yaml        # committed: providers, models(+extra_body), runtime, key_fetch
tests/             # test_core, test_finish, test_architecture, test_memory,
                   #   test_rewind, test_secrets, test_tui, test_mcp,
                   #   test_lint_extension,
                   #   test_expand_newlines, test_bootstrap_network
```

Phases (each ended runnable) — **all seven DONE**:
1. ✅ **Scaffold + V2 port** — config, providers (chat only), session logging, REPL echo loop.
2. ✅ **Agent loop** — tools, approval+allowlist+diff, 5-iteration gate, Esc cancel. Anthropic only.
3. ✅ **Local provider** — llama.cpp tool calling, malformed-call degrade, `/props` ctx.
4. ✅ **Local-server auth wiring** (2026-07-10) — llama-server `--api-key`, a bearer-token gate added to the optional LlamaDesk integration's `/api/switch` (it had none; fine for a browser UI, not for a programmatic client that can flip the GPU's model).
5. ✅ **UI polish** — footer, `/model` picker w/ library + eviction confirm, multiline, `/copy`, `?` help overlay.
6. ✅ **Context** — bootstrap, `open_context_doc`, `/compact`, `/clear`, 80% warning, prompt caching.
7. ✅ **Keystore + install.sh + web search + `/export`/`--continue` + README.**

## 3. Test plan

Automated — **514 tests passing** (`tests/test_core.py`, `tests/test_finish.py`,
`tests/test_architecture.py`, `tests/test_memory.py`, `tests/test_rewind.py`,
`tests/test_secrets.py`, `tests/test_tui.py`, `tests/test_expand_newlines.py`,
`tests/test_mcp.py`, `tests/test_patch.py`, `tests/test_gitcommit.py`,
`tests/test_lint_extension.py`,
`tests/test_bootstrap_network.py`; pytest, no network — providers mocked, with
the single deliberate exception of test_bootstrap_network (R86), which skips
rather than fails when offline):
- config: env expansion, missing-key errors, `/max` persistence
- allowlist: pattern matching (command prefixes, path globs), persistence round-trip
- tools: read/write/edit/grep against a tmpdir; edit_file rejects non-unique anchors
- agent loop: mocked model → tool calls → iteration cap fires at 5 → continue approval
- malformed tool call: retry once → degrade to chat with notice
- flattener: structured Anthropic history → text transcript (golden file)
- tokens: usage accounting + cost table math
- session: JSONL append + resume rebuilds identical history
- keystore: resolution order with fake backends

Manual/integration — local path, switching, and security items verified
2026-07-10; the full Anthropic coding task awaits API credits:
- **Anthropic path:** real coding task (edit a file in a scratch repo, run tests) end-to-end with approvals; footer shows tokens+cost; `/compact` mid-session and continue.
- **Local path:** same task against a loaded local model (TLS+key); confirm a degrade-to-chat model (e.g. one without tool-calling support) degrades gracefully.
- **Switching:** `/model` mid-conversation; library model → eviction confirm → LlamaDesk load → conversation continues.
- **agentic_context:** start in a repo with `.agentic_context/` — verify bootstrap loads CORE docs, MEMORY consulted, finding written via approval gate, index rebuilt.
- **UX:** Esc during generation and mid-tool-loop; multi-line paste of a code block; `/copy` through SSH lands on the local clipboard; `--continue` restores yesterday's session; `/export` markdown is readable.
- **Security:** curl the local server's inference port without a key → 401; without TLS → refused; grep repo for the key → absent; LlamaDesk switch without token → 401.

## 4. Install & use

Moved to **README.md** (the quick-start is maintained there — install,
keys incl. the optional `key_fetch` flow, daily-use table, local-model notes).
`aurora --man` renders the always-current in-app manual.

---

## 5. As-built additions (v0.1.x — agreed and built after R1–R24)

### Architecture
- **R25. Engine/UI split, enforced.** All conversation state lives in
  `engine.py`; the UI drives it only through public methods and implements
  `frontend.py`'s `Frontend` protocol (streaming, tool events, approvals,
  secrets, cancellation, thinking channel). `tests/test_architecture.py`
  AST-fails any engine module importing a UI toolkit or doing terminal I/O.
  Keys are prompted through an injectable prompter (`keystore.set_prompter`).

### Approval gate (extends R7)
- **R26. Five answers**: `y` once · `n [reason]` deny with the reason fed to
  the model (`[denied by user: …]`) · `a` always-allow · `s` stop the whole
  turn (pending calls answered `[skipped]`, history stays valid) · `c [text]`
  don't run + steer: the text is injected as the tool result
  (`[not run — user guidance: …]`) so the model re-plans immediately.
  The iteration-cap prompt takes `y / N / c <guidance>` the same way, and
  each `y` grants a full `max_iterations` block (not one round).
- **R27. Loop nudge**: a tool call identical to one from the previous round
  gets `[note: you already ran this exact call…]` appended to its result.

### Model & context awareness
- **R28. Picker shows name · file size · max context** for every entry
  ($ = paid). `local` resolves to the actually-loaded gguf. Data comes from
  LlamaDesk's `/api/models/detail` (native ctx via header-only `gguf-ctx.py`
  + `size_bytes`); library loads request `min(native ctx, configured
  llamadesk.ctx)` — never the API's 8192 default, never rope-extended.
- **R29. `/status`** — live backend health: local shows the real loaded gguf
  + running `n_ctx` from `/props`; Anthropic shows key presence; OpenRouter
  reports "remote API". Startup **banner** (clears screen) shows model +
  health, cwd (+context flag), session id.
- **R30. Base system prompt** always sent: OS/arch, cwd, home, "real
  filesystem, NOT sandboxed", absolute/~ path rule, always-double-quote
  shell paths. Prevents small-model sandbox hallucinations.
- **R31. Context-full hint** on `exceed_context` provider errors
  (suggests `/compact` · `/clear`); tool outputs hard-capped at 60k chars.

### Thinking models
- **R32. Reasoning channel**: `reasoning_content` (llama.cpp) and
  `thinking_delta` (Anthropic) stream to a display-only channel — never into
  history, `/copy`, or exports. Default: dim `(thinking…)` marker; `/think`
  prints the last turn's reasoning; `/thinking` (or
  `runtime.show_thinking`) streams it live as dim text. Per-model
  `extra_body` passes payload extras — used to disable Qwen thinking by
  default (`chat_template_kwargs: {enable_thinking: false}`).

### UX
- **R33. Streaming markdown rendering** (display-only; raw bytes preserved
  for history//copy//export): bold, inline code, headers, `•` bullets, dim
  code fences, rendered per completed line; `/markdown` (or
  `runtime.render_markdown`) toggles. Colours everywhere (tool calls,
  coloured diffs at the gate, picker) via `colors.py`; `NO_COLOR`/non-tty
  falls back to plain byte-faithful text.
- **R34. Slash-command autocomplete** (built-ins + installed skills; fires
  only on a leading `/`). **`/reset`** = clear history AND re-run the
  `.agentic_context` bootstrap (vs `/clear`, history only).
  **`aurora --man`** man-style manual; `aurora set` accepted for `key set`;
  bad CLI args print usage instead of a traceback.
- **R35. `key_fetch`** (config): `aurora key set <VAR>` can offer a shown
  shell command (e.g. an `ssh` to wherever the value lives) that runs only
  on explicit approval and stores the fetched value — no copy-pasting.

### Correctness (found in the 2026-07-10 deep-dive)
- **R36. Parallel tool results**: all of a round's `tool_result` blocks go
  to Anthropic in ONE user message (separate messages 400 with "roles must
  alternate"); OpenAI-compat keeps one `role:tool` message each. Every abort
  path (deny/stop/cancel/cap) flushes the same way.
- **R37. Cost accounting** sums prompt tokens over EVERY iteration of a
  multi-tool turn (each round bills the full context), not just the last.

### Full-screen TUI (2026-07-10)
- **R38. Pinned prompt layout** (`tui.py`, default on a tty; `--classic`
  or non-tty = the inline REPL): chat is a scrollable pane (mouse wheel,
  PgUp/PgDn; follows the tail until the user scrolls up, Esc+End or
  scrolling to the bottom re-follows), with a dim rule separator (hidden
  while a challenge owns the input line, R50), the multi-line input,
  another always-visible rule, and a two-line status bar pinned below it —
  scrolling the conversation never moves the input. The input's height is
  pinned to its content (cap 8 rows) so spare screen rows can never
  stretch it, and a short transcript is bottom-anchored (top-padded) so
  the newest text — e.g. a challenge — hugs the input line instead of
  floating at the top of the pane. All REPL flows run unchanged in a
  worker session thread with stdout redirected into the chat pane and
  `input()` routed to the pinned field, so approvals, the `/model` picker,
  bootstrap asks and `!cmd` (output captured, not interactive) just work.
- **R39. Status bar** = `model │ ctx used/limit (%) │ cost │ session` +
  hint line, always visible; while the worker is busy it animates
  `⠹ thinking…/generating…/working… Ns (Esc cancels)`. The context
  limit is the LIVE server `n_ctx` (`/props`), cached 120s so a LlamaDesk
  reload of the same model at a different ctx shows up within 2 minutes.
  **`/props` is only probed for a local/tailnet llama.cpp server** — remote
  APIs (OpenRouter, …) have no such endpoint, so on those `live_context_limit`
  returns None immediately without a request. Probing it on a remote provider
  wasted a ~6s doomed request on the UI thread at the first status render,
  freezing startup (2026-07-11 bugfix; `_is_lan_host` gates it).
- **R40. Collapsed thinking blocks** (Copilot-style): reasoning streams
  into a dim clickable `▸ thinking… — click to read` header; click toggles
  the full text; `/thinking` starts blocks expanded; `/think` unchanged.
  Extended by R49: rows are timed and appear for every request.
- **R41. Input ergonomics**: mouse click positions the cursor
  (`focus_on_click`); up/down recall a persisted prompt history
  (`AURORA_HOME/input_history`) at the input's edges, navigate the
  completion menu when it's open, and move the cursor otherwise. The
  `/`-completion menu shows a short description per command/skill
  (`display_meta`; skills show their first-line `#` blurb). `--man`
  renders its markdown (bold/headers/code) on a tty, raw when piped.

### Hardening & performance (2026-07-10 deep-dive fixes)
- **R42. A raising tool never kills the turn**: `run_tool` catches all
  exceptions (e.g. the model passing wrong argument names) and feeds
  `[tool error: …]` back as the result — a missing tool result would make
  every later request invalid (tool_use/tool_result pairing).
- **R43. Allowlist scope**: an `a` answer stores the command's first TWO
  tokens (`rm -rf`, `git push`), and prefix matching is token-bounded
  (`git` matches `git status`, never `gitk`).
- **R44. No dangling user message**: a turn that produced no assistant
  output (provider error, instant interrupt) pops its user message so the
  next send can't stack two consecutive user turns (Anthropic 400s); the
  prompt survives in the session log.
- **R45. Partial streams are kept**: a mid-stream HTTP drop (read timeout
  on a long local generation) returns the already-streamed text with a
  `[stream interrupted — partial answer kept]` marker instead of
  discarding the turn; half-received tool calls are dropped.
- **R46. `/compact` really compacts**: the current model summarizes the
  transcript (decisions, paths/commands, open tasks, constraints) and only
  the summary is carried; plain flatten is the fallback when the model is
  unreachable. LlamaDesk in-flight checks use `/api/switch/progress`
  (`/api/status` has no busy flag).
- **Performance**: TUI renders from per-entry parsed-fragment caches
  (appends no longer re-parse the whole transcript — O(n²)→O(n) over a
  session); `grep` prunes `.git`/`node_modules`/venvs/build dirs and skips
  binaries (`-I`); one persistent `httpx.Client` per provider reuses
  connections across a turn's iterations.

### Post-retrospective batch (2026-07-10, R47+)
- **R47. Checkpoints + `/rewind`** (`rewind.py`): a shadow git repo
  (`AURORA_HOME/checkpoints/<cwd-hash>` as GIT_DIR, work-tree = cwd)
  snapshots the tree just before **every** approved mutation
  (write/edit/command — allowlisted ones too), labelled `[tool] <prompt>`.
  `/rewind` lists snapshots and restores one (`reset --hard` + `clean -fd`);
  the pre-rewind state is checkpointed first, so a rewind is undoable.
  The project's own `.git` and gitignored files are never touched.
  Checkpointing swallows its own failures — it must never break a turn.
- **Legacy allowlist rules demoted** (Q2 of the retrospective): single-token
  `run_command` rules saved before R43 (`rm`, `python`) now match the bare
  command **exactly only** — they no longer prefix-approve (`rm` ≠ `rm -rf /`).
  `/allowlist` marks them "(legacy single-token — consider removing)".
- **Allowlist matching normalizes command spelling (2026-07-11 bugfix).**
  "Always allow" stores/matches the first two tokens via `shlex` with `~`
  expanded, so equivalent spellings of the same command collapse — `bash
  ~/x.sh`, `bash "/home/me/x.sh"` and `bash /home/me/x.sh` all match one rule.
  Before this, each spelling was a distinct string that never matched the
  next, so an 'always allow' silently failed to catch the model's next run and
  the allowlist piled up near-duplicate entries. Matching is token-list based
  (safe with spaces in quoted paths); the single-token safety is unchanged.
- **TUI `ask()` deadlock guard** (Q1): `builtins.input` is monkeypatched to
  `ask()` while the TUI runs; a call from the UI **event-loop** thread can
  never be answered (that thread is the answerer) and now raises
  `RuntimeError` instead of blocking forever. Every other thread may ask —
  including the nested turn thread `_run_turn` spawns, where mid-turn key
  prompts arrive from. Regression-tested both ways.
- **Completion-menu mouse crash guard (2026-07-11).** prompt_toolkit's
  `CompletionsMenuControl.mouse_handler` asserts an active `complete_state` on
  MOUSE_UP; a stray click on the stale menu region (e.g. clicking after
  switching back to the terminal window) arrives with `complete_state is None`
  and the bare assert crashes the whole app. `_SafeCompletionsMenuControl`
  ignores mouse events when no completion is active (`_completions_menu()`
  swaps it into the float). Any handler raising will still kill `app.run()`,
  so library handlers on our floats need this kind of guard.

### Upgrade surfaces (things a dependency bump can silently break)
Incidental integration points that fail as *blank features, not errors* —
check these first after upgrading llama.cpp / LlamaDesk / prompt_toolkit:
- **llama-server `/props`**: Aurora (and LlamaDesk) read
  `default_generation_settings.n_ctx` + `model_path` for the footer gauge
  and `/status`. `provider_health` now degrades loudly ("/props schema
  changed?") instead of showing a blank ctx. The n_ctx cache is 120s TTL —
  LlamaDesk can reload the same gguf at a different ctx.
- **LlamaDesk `/api/requests`** parses llama-server `print_timing` log
  lines; a llama.cpp upgrade can change the format → the table goes empty.
  Re-check the regex in llamadesk `server.py` (see memory note
  `llama-server-contention-and-llamadesk-api-contract`).
- **LlamaDesk `/api/metrics`** assumes current Prometheus metric names
  (needs `--metrics` at launch).
- **prompt_toolkit**: the TUI leans on 3.x internals (fragment mouse
  handlers, cursor-anchored scrolling) — pinned `<4` in pyproject; vet any
  major bump by hand before lifting the cap.

### Drag-select → auto-copy (2026-07-10, R48)
- Full-screen mouse reporting captures the terminal's native selection, so
  the TUI provides its own: **left-drag over chat text highlights it
  (reverse video) and auto-copies on release** via clipboard.py, with a
  transient "✂ copied N chars" in the status bar. Clipboard order: local
  sessions use the OS tool first (pbcopy/wl-copy/xclip — Terminal.app
  silently drops OSC52); SSH sessions use OSC52 first (only it reaches the
  local clipboard).
  A plain click still goes to fragment handlers (thinking toggle). Columns
  are cell-based, so wide glyphs (CJK/emoji) may be off by a cell at the
  selection edges — accepted.

### Timed think rows + inline challenges (2026-07-10, R49–R50)
- **R49. Every LLM request gets a timed row in the chat**, mirroring the
  toolbar phase+elapsed: the agent loop fires `cb.on_request` before each
  provider call (each tool round, and the malformed-tool-call retry), the
  TUI opens `✻ thinking… Ns` immediately — before any tokens — and closes
  it as `thought for Ns`. If thinking tokens streamed, the row is the R40
  clickable expander (`▸ thought for Ns — click to read`); with none it is
  a plain timed row, not clickable. Live rows are never render-cached (the
  0.5s ticker drives the clock). A round that ends in tool calls with no
  text never fires `on_text`, so the first plain chat print (tool start /
  notice) also closes the live row — a leaked row would show a forever-
  running clock AND disable the render cache for the whole session (the
  `_open_think` flag gates that cache bypass at O(1)). The input field's
  height estimate is wrap-aware so a long challenge prompt can't clip at
  narrow widths.
- **R50. Challenges are answered inline, and any text is a comment.**
  During a blocking ask (approvals, `continue?`, bootstrap, key prompts)
  the question is not printed into the chat — it becomes the input line's
  prompt, so the cursor sits right after `…[c]omment: `; the rule
  separator hides so the question attaches to the approval box above, and
  the answered Q+A pair is echoed into the transcript on Enter. Answer
  parsing: `y/n/a/s/c` and the full words `yes/no/always/stop/comment`
  work as before; **any other non-empty input is taken as a `c` comment**
  (guidance to the model) instead of re-prompting. Same at the
  iteration-cap ask: free text = continue, with the text as guidance.

### Last model remembered (2026-07-10, R51)
- **R51. The selected model survives restarts.** Every `switch_model`
  (picker, LlamaDesk library load) writes `last_model`/`last_provider` to
  `AURORA_HOME/state.yaml` — per-machine state, deliberately NOT
  config.yaml, which is committed and synced between machines. On startup
  the entry is restored by exact config match, or (library model with no
  config entry) by re-labelling the provider's entry; if the provider's
  key is gone the default (first configured) model is used. State writes
  never break a switch (best-effort).

### Aurora writes its own memory (2026-07-10, R52)
- **R52.** *(Removed 2026-07-29 along with R12 — see that entry.)*
  ~~**`/remember`** (`memory.py`): the bootstrap READS `.agentic_context`;
  this closes the loop — the agent reviews the session transcript against
  MEMORY/SKILL.md's write-criteria (non-obvious + will recur + too narrow
  for KNOWLEDGE; most sessions yield 0-2 findings, none is a good answer),
  drafts finding files in the house format (timestamped
  `MEMORY/<group>/YYYYMMDD_HHMMSS_<slug>.md`, mandatory line-2
  `> summary:`, reuses existing group folders), and each proposal goes
  through the NORMAL approval challenge: `y` writes it, `n` skips, `s`
  stops, and free text redrafts that finding with the note folded in (max
  2 redrafts). After any write the context's own `rebuild-index.sh` runs
  so the INDEX never drifts. The context root is the nearest
  `.agentic_context/` (with a `MEMORY/`) walking up from cwd. memory.py is
  engine-side: all output goes through `fe.notify`, never print().~~

### Three-area TUI layout (2026-07-11, R53–R56)
- **R53. The full-screen TUI is three fixed areas, top to bottom, and each
  role belongs to exactly one area.** (1) **Chat/scrollback** — the scrolling
  transcript: LLM replies, streamed think rows, tool starts/results. Only
  this area scrolls; wheel/PgUp move it, drag selects/copies (R48). (2)
  **Input area** — where the user types, AND where every blocking challenge
  is surfaced (the `ask` question, approvals, `continue?`, the select menu):
  a challenge owns the input area so it sits directly above the status bar,
  attached to the box that raised it (R50). (3) **Status bar** — the bottom
  two rows: identity + live state (see R56). It is read-only state, never a
  place input is entered or content scrolls. The boundary is a discipline,
  not just current layout: a new surface picks the one area matching its
  role — transient state → status bar, anything the user acts on → input
  area, anything that persists in history → chat. Nothing renders across two
  areas.
- **R54. Multi-choice challenges are an arrow-key numbered menu, in the
  input area.** `ui.select(prompt, options)` is the choice primitive:
  classic REPL prints a numbered list read by number or key letter; the TUI
  monkeypatches it (same trick as `builtins.input`) to render a `❯`-pointer
  menu — ↑/↓ move, Enter confirms, digits 1–9 jump-select. **Esc is a no-op
  while a menu is open — the pick must be explicit** (revised with R62; the
  original Esc == "No"/safest-fallback shortcut was dropped so a stray Esc
  can never silently answer a challenge). While the menu owns the area it is
  a pure chooser: every non-navigation key is swallowed (a `Keys.Any`
  fallback that specific bindings still beat). Approvals (R7/R50) and the
  iteration-cap ask (R50) route through it; the `comment` choice then falls
  to a normal text `input()` for the guidance. This supersedes R50's "any
  text is a comment" parse for those two prompts: the choice is now an
  explicit menu item, not free-text disambiguation.
  - **The menu renders in its OWN multi-line window** directly above the
    input line, NOT as the input's prompt: the input's `BeforeInput`
    processor turns embedded newlines into literal `^J` (staircased, one
    line), so a multi-line menu drawn there is corrupt. Each option gets its
    own row; the selected row is marked by a `❯` pointer + a bright bold fg,
    **no background bar** (a bg quantizes to muddy grey on non-truecolor
    terminals — white text on grey). While the menu is active the input line
    collapses to height 0 so no dangling `>` prompt shows under the choices.
    The `/command` + model **completion dropdown** is likewise re-themed dark
    — prompt_toolkit's default is a light-grey bar (`bg:#aaaaaa`) that reads
    as a stray grey box against the dark UI.
  - **EVERY choice challenge is a menu — never a bare text prompt.** All
    yes/no questions route through `ui.confirm(prompt, default_yes=…)` (built
    on `select`): the bootstrap "run it?", `/reset`'s re-run, the LlamaDesk
    evict confirm, `/rewind`'s restore confirm, and the `key set` fetch
    confirm. The default option is listed first so it is highlighted and
    Enter picks it — preserving the old `[Y/n]` / `[y/N]` default-on-empty
    feel. (Sole exception: the Esc-to-quit confirm, which is an inline
    UI-thread toggle answered by the next Enter, not a worker-thread ask.)
- **R55. `aurora --debug` tints the two non-interactive areas** so their
  bounds are visible while iterating on layout: chat (area 1) and the status
  bar (area 3) both get a red tint, in distinct shades so the two stay
  distinguishable from each other; the input area (area 2) is deliberately
  left untinted. Terminals have no alpha (bg is opaque hex), so these are a
  muted-but-clearly-visible tint, not a real % opacity — pick a value dark
  enough to keep text readable but light enough to actually see.
  Dev-only visualization, no effect on behavior.
- **R56. The status bar is two lines with fixed roles.** Line 1 is identity
  only — model, context used/limit + %, cost, session id, current mode
  (`prompt mode` / `bash mode`, R57), multiline flag — and nothing
  transient ever appends to it. Line 2 shows the key-hint tooltips by
  default, but any live/transient status TAKES OVER the whole line and the
  tooltips vanish while it shows; the two never coexist. Precedence on line
  2: exit-confirm → awaiting-answer (a challenge is open) → copy notice
  (~4s) → thinking/working spinner + elapsed → tooltips. **Clicking the
  session id on line 1 copies it to the clipboard** (SSH-safe, same path as
  drag-select copy, R48); it is underlined to signal it is clickable.
  Two more clickable, underlined fragments follow the mode indicator:
  **`copy last`** copies the last turn's RAW record (R124) — the prompt
  that started it (`engine.last_prompt()`), the model's reasoning
  (`fe.think_buffer`), if any, and its final answer — unlike `/copy`, which
  copies only the answer text (`engine.last_response()`); same text as the
  `/copy-last` command (shared logic: `ui._raw_last_response_text`). **`copy all`**
  copies the whole session transcript — questions + answers, no thinking,
  same as `/export`'s output (`session.export_markdown()`) — same text as
  the `/copy-all` command (shared logic: `ui._all_chat_text`). All three
  clickable fragments (session id, copy last, copy all) share the same
  clipboard path (`aurora/clipboard.py`, SSH-safe via OSC52).

### Persistent bash mode (2026-07-11, R57)
- **R57. `!` on an empty prompt enters a persistent bash mode (TUI).** The
  input prompt `>` becomes `$`, the status bar line-2 shows the tip "Bash
  mode!", and each Enter runs the typed line as a **local shell command** (it
  is submitted with a `!` prefix through the worker's existing `!cmd` path —
  output to chat, nothing sent to the model or added to history). It STAYS in
  bash mode across commands. Exit: **Esc**, or **Backspace on an empty line**
  (the `$` reverts to `>`). A `!` anywhere other than the start of an empty
  prompt is a literal `!`. This replaces the TUI's old inline one-shot `!cmd`
  as the entry gesture (the classic REPL keeps `!cmd`; both share the worker's
  local-exec path). Bash mode is mutually exclusive with challenges — `!` is
  ignored while a menu/ask is active.

### Secret detection + redaction (2026-07-12, R58)
- **R58. Prompts and tool output are scanned for likely secrets** — two
  passes in `aurora/secrets.py`: known vendor **shapes** (AWS/GitHub/Slack/
  Stripe/OpenAI-style keys, private-key blocks, `.env`-style credential
  assignments), plus an **entropy fallback** for ad-hoc tokens with no known
  prefix (a random internal-tool token has no "shape" to match, but is
  clearly not English prose, a hex hash, or a UUID — the fallback only scans
  spans the shape patterns didn't already claim, so nothing double-counts).
  A match triggers a blocking challenge: **keep as-is**, **replace with
  `<secret>`**, or **stop**. Covers BOTH channels reaching the model/disk:
  - The **user's typed prompt** — scanned in `Engine.send()` before the text
    enters `messages` or the session log. "Stop" aborts the send entirely
    (nothing appended, nothing logged); "redact" scrubs the text used for
    both history and the log; "keep" sends/logs it unchanged.
  - **Every tool's output** — scanned in `agent.py`'s tool loop right after
    the tool runs, before `on_tool_result`/history. This applies uniformly
    to READ-ONLY tools too (`read_file`, `grep`, `list_dir`), which never
    went through the approval gate at all — a `cat`'d `.env` file or a `grep
    -r API_KEY` is caught the same as a gated `run_command`.
  - **One challenge per BLOCK, not per match** — a file with ten keys
    produces one prompt (a kind+count summary, never the raw secret text)
    and one decision that applies to every match found in that block.
  - Because both hooks run BEFORE the session log write, the on-disk JSONL
    log is automatically consistent with what was decided — no separate
    log-side redaction pass needed.
  - **`runtime.redact_secrets`** (config.yaml), default **ON**; `/redact
    on|off` toggles + persists (same `persist_runtime_value` mechanism as
    `/max`). Off means zero scanning cost: the engine passes
    `AgentCallbacks.secret_challenge=None`, and the agent loop's `if
    cb.secret_challenge:` guard skips the scan entirely rather than branching
    on config inside the loop.
  - The challenge itself is the existing `select()` numbered-menu primitive
    (R54) — `Frontend.secret_challenge(context, matches) -> 'keep'|'stop'|
    'redact'`, implemented once in `ui.TerminalFrontend`, inherited by the TUI.
  - See `ARCHITECTURE.md` §5 for the full design writeup (chosen as the
    template for the next feature that spans engine+agent+frontend).
- **Entropy fallback (2026-07-12 bugfix).** A shape-only scan misses a
  token with no known vendor prefix (some internal tool's ad-hoc key is just
  a random string, no `AKIA…`/`ghp_…` shape to match). `scan()` runs a second
  pass: any 20+ char token-like run not already claimed by a vendor pattern,
  with both letters and digits, Shannon entropy ≥ 3.6 bits/char, is flagged
  `"High-entropy token"`. Deliberately excludes hex-only strings (git SHAs,
  MD5/SHA digests) — high-entropy-LOOKING but not secrets, and the main
  false-positive risk of entropy scoring.
- **GUIDs/UUIDs are a real detected kind (2026-07-12), not excluded.**
  Sometimes used as API keys/session tokens, not just harmless correlation
  IDs — a dedicated shape pattern (`"GUID/UUID"`) catches the standard
  8-4-4-4-12 hex-dash format, claiming the span before the entropy pass runs
  (so it's counted once, as the specific kind, not the generic fallback).
  Git SHAs and MD5/SHA hex digests (no dashes) remain excluded — that guard
  was always about hashes, not UUIDs specifically.
- **`run_command`'s PARAMETERS get a notice, never the keep/redact/stop
  challenge (2026-07-12, R58 extension).** The command string is scanned
  right before it runs; a match calls `cb.notify("possible secret in this
  command: …")` and nothing else — the command still executes with its REAL
  argument (it usually needs the actual value to work, e.g. a real key in a
  curl header — silently substituting `<secret>` would just break it), and
  blocking here would duplicate the approval gate the call already passed
  through. This is deliberately narrower than the tool-OUTPUT check: only
  `run_command`'s own command string gets this notice; its output still goes
  through the full challenge like any other tool, and no other tool's
  arguments (e.g. `write_file`'s content) get scanned pre-execution by this
  path — only what's about to be PRINTED/RUN by a shell needed this
  narrower, non-blocking treatment.
  - **Secret challenges show the matched token in bold with its surrounding
  line (2026-07-12, R58 extension).** `secrets.format_matches()` prints each
  match as `kind: <before><bold>token</bold><after>` so the user can see
  exactly what text was flagged. The challenge prompt still never echoes raw
  secrets as plain text; the bold marker is rendered by ANSI in the classic
  REPL and by the TUI's fragment parser.
- **Allowlist for confirmed false positives (2026-07-14, R58 extension).**
  A recurring false positive (a fixture UUID, an internal-tool token) used to
  re-trigger the challenge on every occurrence, forever. The challenge menu
  gained a 4th option, **"always allow"**: `secrets.hash_value()` (SHA-256)
  hashes every matched value in that challenge, `Engine` adds the hashes to
  `runtime.secret_allowlist` (persisted via `persist_runtime_value`, same as
  `/max`/`/redact`), and `secrets.scan(text, allowlist)` drops any future
  match whose hash is in that set before it's ever surfaced — the raw value
  itself is never written to disk, only its hash, so `config.yaml` stays safe
  to commit/share. `/redact allowlist` shows how many values are allowlisted;
  `/redact allowlist clear` resets it. See `ARCHITECTURE.md` §7.

### `/model` picker uses the menu, marks + pre-selects the current model (2026-07-12, R59)
- **R59. `/model` is a `select()` menu**, not raw numbered print+`input()` —
  same primitive as approvals/R58 challenges, so it gets the TUI's arrow-key
  render for free. Every configured model + the LlamaDesk library (if
  configured) is listed alphabetically with its price tag (`[$]`/`[free]`)
  and info (context size /
  GB); the currently-active entry is marked `✔` and is what a blank Enter
  accepts (`select(..., default_index=...)` — see below) or, in the TUI,
  where the pointer starts.
  - **`select()`/`select_menu()` gained an opt-in `default_index` param.**
    Opt-in matters: approve/confirm-style callers that DON'T pass it keep the
    OLD "blank Enter re-prompts" behavior — an accidental bare Enter must
    never silently pick "yes" on an approval challenge. Only pickers that
    want a sensible default (like the current model) pass it explicitly.
  - **A label may carry raw ANSI colour** (the same `GREEN`/`YELLOW`
    constants the classic REPL prints directly, e.g. `/model`'s `[$]`/`[free]`
    tags). The TUI's menu window renders fragments literally (not through
    `ANSI()` parsing) — passing a raw-escape label through unparsed would show
    garbage control characters instead of colour. `_menu_fragments` now
    parses each label via `ANSI(label).__pt_formatted_text__()` and layers
    each parsed sub-fragment's own colour ONTO the row's base style
    (selected/option), so a plain label (the common case — approve/confirm
    menus have none) keeps its old look exactly, while a coloured one (the
    new case — `/model`) renders its colours instead of literal escape bytes.
  - **Current-model detection is by `(provider, model)` VALUE, not `is`
    identity** — `Engine.switch_model()` stores whatever dict it's handed,
    essentially never the same object as the matching entry in
    `engine.list_models()` (a fresh parse of config.yaml). An identity check
    silently marks/pre-selects the WRONG entry as current whenever the two
    aren't literally the same object (found via this exact bug during
    review — the original `_pick_model` had the same identity check before
    R59, just with lower consequence since it only skipped a cosmetic label).
- **`/model` must feel as instant as `/exit` (2026-07-12 bugfix).** It's a
  local menu, not an LLM call — but it PROBES LlamaDesk (`/api/status`) first
  to show its library, and an unreachable LlamaDesk previously blocked the
  whole command for its full ~5s timeout (measured), reading as an
  unexplained "thinking" delay before anything a menu button. Fixed two ways:
  a short-TTL failure cache (`_llamadesk_last_fail`, 30s) so a recently-failed
  probe is skipped entirely on the next `/model` — no network call, no wait
  — while still automatically retrying once the TTL expires in case the box
  came back.
- **Never nag for a key on a model nobody selected (2026-07-12 bugfix).**
  A fresh boot (no `state.yaml` yet) used to default to `models[0]` — if that
  entry's provider needs a key nobody's stored (e.g. a local server that
  really does require a bearer key, and the user mainly uses OpenRouter),
  `send()`'s interactive key prompt fired on every single message, forever
  (a skipped/empty prompt is never cached, and the unresolved key also kept
  `_restore_last_model()` from ever treating that entry as valid, so it
  looped back to `models[0]` every restart too). `Engine._default_model()`
  now prefers the first configured model whose provider **already has a
  usable key** — only falling back to the literal first entry if NONE do
  (then something has to be the default, and prompting is expected). Once
  the user explicitly runs `/model` and picks something, `switch_model()`'s
  existing state.yaml persistence (R51) takes over as normal.
  Complementary UX: the `/model` picker now marks any entry whose provider
  needs a key it doesn't have with `(no key set)` (`Engine.has_key()`, a
  public non-prompting wrapper around `_has_key`) — visible before you pick
  it, not discovered by getting nagged after.
- **Selecting a keyless entry offers to enter the key right there**
  (2026-07-12). Before this, `(no key set)` was informational only — you'd
  still have to separately remember `aurora key set <VAR>`. Now picking a
  "config" entry whose provider fails `has_key()` immediately runs the same
  fetch-command-then-hidden-prompt flow as `aurora key set`
  (`ui._prompt_and_store_key`), storing via the normal keystore. Skipping
  (empty input) leaves the model selected anyway and prints the manual
  `aurora key set` command as a fallback — never blocks the switch itself.
  `Engine.forget_key_check(pkey)` clears the one-shot `_has_key` cache after
  a successful store, so the picker/footer see the fresh key immediately
  instead of the stale cached miss for the rest of the session.
  - **Bugfix (same day): the inline prompt used raw `getpass.getpass()`**
    instead of the injectable, TUI-safe prompter (`keystore._prompter`, wired
    to `fe.ask_secret` via `keystore.set_prompter` at startup — see R22).
    Calling `getpass` directly reads from the real tty, bypassing the TUI's
    monkeypatched input entirely: in the alternate-screen TUI the prompt was
    invisible and the worker thread blocked forever waiting for input nobody
    could see to give — looked exactly like "/model hung on thinking with no
    key prompt ever shown." Fixed by routing through `keystore._prompter`
    like every other interactive key prompt already does.

### Max "working" time — a continue/cancel challenge (2026-07-12, R61; removed 2026-07-13)
- **R61.** *(Built 2026-07-12: a time-based twin of the iteration cap —
  `ask_wait(elapsed_seconds)`, `runtime.max_wait`/`/max-wait N`/
  `aurora --max-wait N`, re-arming, a "don't ask again this turn" option —
  see git history for the full original spec.)* **Removed 2026-07-13** at
  the user's request: unlike the iteration cap (R6/R9), which only fires
  when the model is doing something — running tools in a loop — this fired
  purely on wall-clock time, so a single long-but-normal generation (a big
  local model, a slow network) got interrupted by "still working, continue?"
  challenges for no reason related to runaway behavior. `max_iterations`
  (R6/R9's `ask_continue`) remains as the only loop-safety cap. Fully torn
  out end-to-end: `agent.py` (`AgentCallbacks.ask_wait`, the
  `wait_checkpoint` block in `run_turn`), `engine.py` (`max_wait`,
  `set_max_wait`), `frontend.py` (`Frontend.ask_wait`), `ui.py`
  (`TerminalFrontend.ask_wait`, `/max-wait`), `__main__.py`
  (`--max-wait`), `config.yaml` (`runtime.max_wait`) — historical
  numbering preserved, not reused for something else.

### Esc is a generic double-tap control key (2026-07-12, R62)
- **R62. Every state that needs confirmation before acting uses the SAME
  gesture: press Esc, then press it again within 2 seconds to open an
  explicit arrow-key Yes/No question.** Replaces the previous ad-hoc
  per-state Esc handling (immediate cancel on busy; Esc *dismissed* the exit
  question rather than confirming it; no confirmation at all on leaving bash
  mode) with one rule, `Tui._on_escape()`. All three cases now open the same
  kind of menu on the second press — none act directly anymore:
  - **Busy/working** → 1st Esc arms ("Esc again to ask!"), 2nd Esc opens
    **"Cancel this?"** (`cancel`/`continue`) — picking `cancel` calls
    `cancel_event.set()` (R17); `continue` (or Esc again) dismisses it and
    the turn keeps running.
  - **Bash mode** (R57) → 1st Esc arms ("Esc again to ask!"), 2nd Esc opens
    **"Leave bash mode?"** (`leave`/`stay`) — picking `leave` exits bash
    mode; `stay` (or Esc again) dismisses it.
  - **Idle, empty prompt** → 1st Esc arms the exit question ("Esc again to
    ask!"), 2nd Esc opens **"Quit Aurora?"** (`yes`/`no`) — picking `yes`
    calls `app.exit()`. Typing `y` + Enter at the OLD-style status-bar
    question still works too (unchanged).
  - **History**: busy-cancel originally confirmed directly on the second
    press (2026-07-12, first revision) — "the double-tap already means
    keep-working/cancel, a menu would be redundant." Revised again the same
    day to also open a menu, for consistency across all three cases: an
    explicit choice the user actively picks, not an implicit "you pressed
    Esc twice, that's confirmation enough."
  - **NOT part of this rule, deliberately**: while a menu/approval challenge
    is open, Esc is a no-op — the challenge already has its own explicit
    choice mechanism and must be answered by an actual pick (revises R54's
    original Esc-to-safest shortcut); clearing typed text on a non-empty input
    line stays single-press too — trivially reversible, unlike cancelling/
    leaving/quitting.
  - **The 2-second window is real, not just "immediately after"**: a second
    Esc more than 2s after the first is treated as a FRESH first press (it
    re-arms, it does not confirm) — a stray Esc pressed minutes apart must
    never silently cancel/quit/leave. Tracked as `(self._esc_armed: str |
    None, self._esc_armed_at: float)` — the *kind* of pending action plus
    when it was armed, reset whenever the underlying state changes through
    some OTHER path (e.g. bash mode left via Backspace-on-empty, not Esc).
  - `_on_escape()` takes `app_exit` as a parameter (not a closure over
    `event.app.exit`) specifically so it's a plain method, callable and
    testable directly without driving real prompt_toolkit key input — raw
    Esc bytes sent through a pipe hit prompt_toolkit's own ESC-vs-escape-
    sequence disambiguation delay, which made timing-based tests flaky.
  - **Why the confirm menu needed a NEW mechanism, not `select_menu()`**:
    `select_menu()` (used by approvals/R58/`/model`) BLOCKS on the answers
    queue and must be called from the worker thread — the exact same reason
    `ask()` raises if called from the UI event-loop thread (that thread is
    the one that would have to deliver its own answer; calling it from a key
    binding, which IS the UI thread, would deadlock). `Tui._open_ui_menu`
    reuses the identical rendering/navigation (`_menu_fragments`, arrow
    keys, digit-jump, Esc-cancels-to-safest via the existing top-priority
    `_menu_options is not None` branch) but resolves via a plain callback
    (`self._menu_on_select(key)`, set only for this path) instead of the
    queue — the UI thread can call that back on itself with no blocking
    involved at all.

### `/remember` temporarily hidden from discovery (2026-07-12) — superseded by R87
- **`/remember` (R52) was reworked** and is back in `/` autocomplete and the
  README's command table — see R87 below for the landed rework.

### `aurora key clear` / `aurora wipe` — logging out (2026-07-12, R60)
- **R60. `aurora key clear <VAR>` / `--all`** removes a stored key from every
  backend that can actually be cleared (`keystore.clear_key`: OS keyring,
  encrypted file) — an env var can't be unset from outside the shell, so
  that case just tells the user to do it themselves. `--all` iterates every
  ENV_VAR name THIS config.yaml actually uses (`_known_key_names()`: each
  provider's `api_key_env` plus llamadesk's `token_env`), not a hardcoded
  list, so it stays correct for whatever providers are configured.
- **`aurora wipe`** deletes `AURORA_HOME` entirely (sessions, allowlist,
  encrypted keys, bootstrap prompt, last-model state) — logging out of every
  provider AND resetting all local state in one step, e.g. before a fresh
  reinstall. Requires typing `yes` to confirm (a real `git status`-style
  destructive-action gate, not a `y/N` one-key prompt). Clears keyring
  entries FIRST (they live outside `AURORA_HOME`, so deleting the directory
  alone wouldn't touch them), then removes the directory.
  - **Safety lesson from building this (see MEMORY)**: verifying
    delete/wipe logic against the REAL OS keyring or a real `AURORA_HOME`
    even once, "just to check," can permanently delete real credentials —
    an ad-hoc verification command outside the pytest suite doesn't inherit
    the suite's `AURORA_HOME`/keyring isolation. Always mock BOTH in the
    SAME command for any check of this code.

### Standalone operation (agreed 2026-07-10)
- **Aurora MUST work with no local server/LlamaDesk reachable** (installed
  anywhere, off LAN and off any VPN/tailnet, e.g. running only OpenRouter
  models). Verified: every backend probe is bounded (health probes capped at
  ~5s, LlamaDesk client 5s) and degrades to a message, never a crash or a
  minutes-long hang; the `/model` picker lists config models without
  LlamaDesk; the unreachable-on-send notice **classifies local vs. remote
  generically, never echoing the raw config-key name or a personal
  hostname** (2026-07-12 revision) — "local backend unreachable" or
  "`<public hostname>` unreachable — check your connection, or /model to
  switch," plus a `curl`-based connectivity hint the user can run themselves
  (concrete for a public remote host, since its domain isn't personal;
  generic — no hostname — for local/LAN, since a VPN/tailnet MagicDNS name
  or a user's own provider-key label can itself be something personal, e.g.
  their machine's name). This matters for an open-source project: config is
  user data, never echoed back verbatim. A connectivity error is also never
  assumed to be the local backend specifically — it can be any provider. The
  only LlamaDesk-specific conveniences lost when it's not configured are the
  model library and live n_ctx; `key_fetch` (if configured) falls back to
  the hidden prompt.
- **Connect timeout (TCP + TLS) is provider-aware, not a flat 5s.** A
  self-hosted/LAN/tailnet server that's off must fail fast (5s, so an
  off-grid send isn't a ~2min hang), but a PUBLIC API's TLS handshake can be
  slow over a poor link — a 5s budget there causes false
  handshake-timeouts/"unreachable". Remote (non-private host) gets 20s;
  `_is_lan_host(base_url)` decides (loopback / private IP / `.local` /
  `.ts.net` → LAN). The long read timeout (`runtime.timeout`, 300s) is
  unchanged.
- **Retry a stale-connection reset before any tokens stream.** The persistent
  pool keeps keep-alive connections; after the app sits idle the server/proxy
  (OpenRouter/Cloudflare) closes one, and the next request reuses the dead
  socket → "Connection reset by peer" / RemoteProtocolError "Server
  disconnected". httpcore's `retries=` only covers the CONNECT phase, not a
  failure during the request, so `openai_compat.turn` retries (3 attempts,
  small backoff) on a transient connection error **only while `result.text` is
  still empty** — a mid-stream drop keeps its partial (would otherwise
  duplicate streamed output). Non-connection errors (HTTP 4xx/5xx,
  MalformedToolCall) are never retried.
- **Happy Eyeballs (RFC 8305) for openai-compat connects.** A host with A+AAAA
  records on a machine whose public IPv6 route is dead (common with Tailscale
  up — public IPv6 blackholes) otherwise burns the whole connect timeout
  stalling on IPv6 before falling back to IPv4 (measured 17s → 0.15s vs
  OpenRouter). `providers/happy_eyeballs.py` plugs a custom httpcore network
  backend into the httpx client that races the address families (interleaved,
  staggered ~0.25s) and uses whichever connects first. NOT force-IPv4 — that
  would break IPv6-only networks; the race is correct on IPv4-only AND
  IPv6-only. Composes with `retries=2` (retries a transient connect failure on
  the winning family, before the request is sent → no dup request/text).

### Deployment note: unified local+OpenRouter gateway (2026-07-14)
- No Aurora code changed for this — `openai_compat.py` already supported a
  `base_url` list with try-each-in-order failover for any `type: openai`
  provider (not special-cased to a "local" config key). What changed is
  **how `config.yaml` is deployed**: on m7, `~/scripts/llama/aurora-gateway.py`
  (a small Flask service, systemd unit `aurora-gateway`, run behind Caddy on
  the existing `:18182` LAN/Tailscale endpoints) now sits in front of
  llama-server. It inspects the `model` field of each `/v1/chat/completions`
  request: `"local"` (or unset) routes to llama-server; anything else (an
  OpenRouter model id, e.g. `moonshotai/kimi-k2.7-code`) is proxied to the
  real OpenRouter API, with `OPENROUTER_API_KEY` injected server-side —
  never sent by or visible to the Aurora client.
- **Result:** `config.yaml`'s `providers:` needs only one OpenAI-compatible
  entry (named `openrouter:` in this repo's committed config) whose
  `base_url` is the two m7 URLs and `api_key_env: LLAMA_API_KEY` — that
  single key authenticates every request Aurora makes, local or
  OpenRouter-routed. Trade-off, accepted deliberately: if m7 itself is
  unreachable, ALL models are unavailable (no direct-to-OpenRouter fallback
  path) — simplicity over redundancy, since m7 uptime is otherwise good.
- **Gotcha for anyone touching the gateway:** it must implement `/props`
  (see ARCHITECTURE.md §3, networking hardening) as a llama-server
  passthrough — `pick_endpoint()`'s reachability probe hits that path, and
  its absence 404s the probe and makes Aurora report the provider
  unreachable even though real requests would have worked.

### Tool-call argument display (2026-07-12, R63)
- **R63. Tool invocations show every argument in full.** `Frontend.on_tool_start`
  prints the tool name followed by each argument on its own indented line
  (`key: value`), never truncated. This applies to all tools including
  `run_command` (full shell command), `read_file` (full path), and
  `write_file`/`edit_file` (full path + a diff preview at the approval gate,
  R8). Previously long arguments were ellipsized inline; now the UI owns
  line-wrapping and nothing is hidden from the user.

### LAN TLS for a self-signed local server (2026-07-13, R64)
- **R64. A local/LAN `base_url` reachable by bare IP skips TLS verification
  for that connection only.** A reverse proxy in front of llama.cpp (e.g.
  Caddy) commonly serves a cert issued for a single hostname (its Tailscale
  MagicDNS name) — hitting the same listener by LAN IP always fails
  certificate verification (`IP address mismatch`), even though the
  connection itself is trusted (same LAN, same box). `providers/openai_compat.py`'s
  `_is_bare_ip(url)` returns true only for a literal private/loopback
  IP host — a hostname, including `.ts.net`, is never affected and keeps full
  verification. This must be threaded into the actual TLS-performing layer:
  `httpx.Client(verify=...)` is silently IGNORED once a custom `transport=`
  is supplied (see `happy_eyeballs.py` above) — `verify` has to be passed
  to the transport itself. Applied everywhere a bare-IP endpoint is dialled:
  the pooled client in `openai_compat.py`, its `_probe()` health check, and
  `engine.provider_health()`'s own separate `/props` call (a second,
  easy-to-miss call site outside `openai_compat.py` — grep for `httpx.get`/
  `httpx.Client` before adding a new one).

### `aurora key status` (2026-07-13, R65)
- **R65. `aurora key status [ENV_VAR]`** reports where a key would resolve
  from — `set (env var)` / `set (OS keyring)` / `set (encrypted file)` /
  `possibly set (encrypted file — enter passphrase to confirm)` / `not set`
  — for one key or (no arg) every `api_key_env`/`token_env` this
  `config.yaml` uses. Read-only and never prompts: `keystore.key_status()`
  checks env var → OS keyring → encrypted-file PRESENCE only (decrypting it
  needs a passphrase, which this command must never ask for just to answer
  "is something stored"). Documented in `--man`/`--help` alongside
  `key set`/`key clear`.

### Startup health probe is hard-bounded (2026-07-13, R66)
- **R66. `Engine.provider_health()` can never block app startup past a fixed
  timeout (default 4s).** Both the TUI and classic UI call it synchronously,
  on the main thread, to build the startup banner — BEFORE anything is on
  screen. A stuck DNS/socket call deep in `httpx` (observed with certain
  LAN+VPN routing combinations, past its own per-request `timeout=`) used to
  freeze the entire app with a blank screen, indistinguishable from "won't
  boot." The probe now runs on a daemon thread; `provider_health()` joins it
  with a hard timeout and, if it hasn't returned, proceeds with
  `{"ok": False, "detail": "health check timed out after Ns (startup not
  blocked)"}` — the abandoned thread is never awaited again. This is a
  correctness requirement regardless of root cause: a health CHECK must never
  be able to block the thing it's checking the health of.

### Allowlist generalizes read-only commands across arguments (2026-07-13, R67)
- **R67. A curated `SAFE_COMMANDS` set of read-only, non-destructive commands
  (`find`, `ls`, `tree`, `grep`, `cat`, `pwd`, `whoami`, `which`, `wc`,
  `head`, `tail`, `file`) generalizes its "always allow" rule across ANY
  arguments, not just the two tokens the model happened to run first.**
  Before this, `add_rule()` stored the first two tokens of a command (`find
  /path/A`) and `is_allowed()` matched that prefix exactly — hitting "always"
  on one path never covered the same read-only command against a different
  path in a different project/session, which read as "remember doesn't work
  across sessions" even though the allowlist file itself persisted correctly.
  For a `SAFE_COMMANDS` entry, `add_rule()` now stores just the bare command
  name, and `is_allowed()` prefix-matches it regardless of args. Every other
  command (`rm`, `git push`, `bash <script>`, …) keeps the original strict
  2-token exact-prefix match — this list is deliberately narrow to commands
  that cannot write, delete, or execute arbitrary code, so generalizing the
  match carries no extra risk. `legacy_rules()` (surfaced by `/allowlist` for
  pruning) excludes `SAFE_COMMANDS` single-token entries — a bare `find` is
  intentional here, not a pre-R43 leftover.

### Context-size picker on a LlamaDesk library load (2026-07-13, R68; actually implemented 2026-07-15; removed 2026-07-23)
- **R68.** *(Removed 2026-07-23 along with R3/LlamaDesk — `_pick_ctx`,
  `_CTX_OPTIONS`, and the library-load branch in `_pick_model` are gone;
  there is no longer a "load at ctx N" step to prompt for.)* ~~Spec'd
  2026-07-13 and fully tested in `test_core.py` — a 7-rung ladder, `native`
  always offered even off-ladder, a free-text `custom…` entry. That version
  was never actually wired into `ui.py`; the real code kept silently doing
  `ctx = min(config, native)` with no prompt at all until this was noticed
  and fixed 2026-07-15 — see git history for the original spec text.
  Simplified and actually implemented 2026-07-15, local models only
  (LlamaDesk library loads — remote/OpenRouter models have no "load at ctx
  N" step, their limit is fixed by the provider): loading a LlamaDesk
  library model (R3) now genuinely asks which context size to load it at,
  instead of silently picking one. `ui._pick_ctx(default_ctx, native)` —
  called from `_pick_model`'s library-load branch, after the eviction
  confirm, before `desk.switch()` — offers just **64k / 128k / 256k**
  (`_CTX_OPTIONS`), not the original 7-rung ladder: small enough to glance
  at, big enough range for daily use. Options above `native` (the gguf's
  `ctx_native`, from `LlamaDesk.models_detail()`) are dropped entirely,
  never just disabled — Aurora never rope-extends a model past what it was
  trained for. If `native` itself is under 64k (a tiny model), it's offered
  alone instead of an empty menu. No free-text custom entry — three
  options is exactly the point. Pre-selects the largest offered size
  `<= default_ctx` (`config.yaml`'s `llamadesk.ctx`); the chosen value is
  used for that one load only, never written back to config (RAM headroom
  is per-machine, per-model — not something to sync).~~

### Clickable links in chat output (2026-07-15, R69)
- **R69. Bare URLs in model/tool output render cyan+underlined and are
  clickable.** `colors.URL_RE` finds `https?://` URLs (stopping before
  trailing sentence punctuation/closing brackets); `colors.linkify()` wraps
  matches in cyan+underline SGR plus an OSC-8 hyperlink escape, and
  `mdrender.LineRenderer.render()` runs every line through it — so the
  classic REPL (`--classic`, pipes) gets terminal-native Cmd/Ctrl-clickable
  links in any OSC-8-aware terminal (iTerm2, Terminal.app, kitty, WezTerm).
  The full-screen TUI can't reuse that: prompt_toolkit's `ANSI()` parser only
  understands CSI (`\x1b[`) sequences, and an OSC-8 escape (`\x1b]8;;...`)
  fed through it renders as garbage. `colors.IN_TUI` (set/cleared around
  `Tui.run()`) makes `linkify()` a no-op there instead, and
  `tui._linkify_fragments()` re-detects URLs at the parsed-fragment level,
  restyling matches `class:link` (bright cyan + underline, defined in the
  app's `Style.from_dict`) with a mouse handler (`tui._open_url()`, `open` on
  macOS / `xdg-open` on Linux, `webbrowser` as last resort) that opens the
  URL on click — same mechanism the collapsible-thinking header already
  uses for its click-to-expand.

### Faster Esc-Esc gesture; remote context-limit fix; boot banner cleanup (2026-07-15, R70-R72)
- **R70. The TUI's `Application.ttimeoutlen` is set to 1ms (`tui.py`'s
  `_build_app`), down from prompt_toolkit's 0.5s default.** Every Escape
  press — both taps of the double-Esc cancel/quit gesture (R62), not just
  the first — waits `ttimeoutlen` before prompt_toolkit fires the plain
  `escape` binding, since it can't yet tell a lone Escape apart from the
  start of an Alt-sequence (`escape enter` = Alt+Enter submit, `escape m` =
  Alt+M multiline toggle, both bound here). At the 0.5s default this made
  the confirm menu feel sluggish on every tap. A locally-generated
  Alt-sequence arrives at the terminal driver as one byte burst, so even a
  near-zero timeout still resolves it correctly in practice.
- **R71. A remote model's context limit no longer comes from the wrong
  backend.** `OpenAICompatProvider.live_context_limit()` (`providers/
  openai_compat.py`) and `Engine._provider_health_uncached()`
  (`engine.py`) both hit llama.cpp's `/props` endpoint keyed only on
  `_is_lan_host(base_url)` — correct when a LAN host always meant "the
  local llama.cpp model", but aurora-gateway (R-unify, `b9f80d3`) now
  routes BOTH the local model and real remote models (e.g. an OpenRouter
  model like `moonshotai/kimi-k2.7-code`) through the same LAN base_url.
  Selecting a remote model was silently reporting the LOCAL model's loaded
  ctx/name from `/props` instead of the remote model's own. Both call
  sites now gate the `/props` probe on `model == "local"` (the sentinel
  config already uses elsewhere, e.g. `ui.py`'s model picker) — any other
  model skips the probe entirely. A remote model's context limit instead
  checks, in order: `REMOTE_CONTEXT_LIMITS` (a per-model JSON table,
  `providers/remote_context_limits.json` — a list of `{model, provider,
  code, context_size}` entries, dict-per-entry so future params don't need
  another schema change; loaded once into a `model → entry` dict), then
  `config.yaml`'s provider-level `context_limit`, then a 128k default.
- **R72. Boot banner: no `v` prefix on the version, and the `/help ·
  /model · ? help · --man manual` line is gone** (`ui.py` and `tui.py`'s
  `_banner()`) — that information already lives on the status bar's
  footer hints, so the banner line was pure duplication.

### Cost estimate for priced remote models (2026-07-15, R73)
- **R73. The status bar shows a running `$` cost estimate next to the model
  name, but ONLY when Aurora actually knows that model's per-token
  price.** `Provider.has_pricing(model) -> bool` (default `False`,
  `providers/base.py`) is the gate — `Engine.context_stats()` sets
  `ContextStats.cost_known` from it, and both footers (`tui.py`'s
  `status()`, `ui.py`'s `_footer()`) render `(${cost:.2f})` right after the
  model name only when `cost_known` is True; a bare `$0.00` for an unpriced
  model (local, or a remote model missing pricing) would wrongly imply
  Aurora knows it's free, so it renders nothing instead.
  `AnthropicProvider.has_pricing()` checks its hardcoded `MODELS` table (as
  before, R13). `OpenAICompatProvider.has_pricing()` / `.cost()` read
  `price_in_per_mtok` / `price_out_per_mtok` from the SAME per-model JSON
  table as R71's context limits (`providers/remote_context_limits.json`) —
  a model entry can carry `context_size` without pricing, or vice versa;
  each is optional independently. Also carries an informational
  `pricing_url` field (not read by code, just a source-of-truth pointer for
  whoever maintains the table). Seeded with `kimi-k2.7-code` ($0.253 in /
  $3.69 out per 1M) and `z-ai/glm-5.2` ($0.367 in / $3.60 out per 1M, 1M
  context) — prices are OpenRouter's usage-weighted average across
  providers, not the headline listed price, since that's closer to what
  aurora-gateway actually pays. The `/model` picker (`ui.py`'s
  `_pick_model`) shows the same per-M pricing next to context size for any
  model with known pricing — read from this same JSON table — so cost
  awareness isn't limited to the footer badge.

### Anthropic provider removed; local/OpenRouter providers split; startup logo removed; clickable model name (2026-07-15, R74)
- **R74a. OpenRouter-compatible models only, for now** (user's explicit
  call — R1 is the historical record). `providers/anthropic.py` deleted;
  `providers/__init__.py`'s `make_provider()` always returns
  `OpenAICompatProvider`. Every branch that special-cased
  `provider_kind() == "anthropic"` collapsed to its OpenAI-compat-only
  path: `engine.py` (user/assistant message shape, `/compact`'s summary +
  fallback-flatten paths, `resume_from`, `switch_model` — the cross-
  provider history-flatten-on-switch in R4 is now dead code since there's
  only one provider kind, removed), `compact.py` (`_stringify`/
  `flattened_as_user_message` — no more Anthropic content-block branch),
  `memory.py` (`_draft`'s summarization call), `agent.py`
  (`_provider_label`'s `_default_base_url` special case, removed —
  `_provider_label` now just needs `provider.base_url`),
  `providers/openai_compat.py` (`turn()`'s `isinstance(system, list)`
  flatten, dead now that `system` is always a plain string). The generic
  `tool_results_messages()` bulk-flush hook in `agent.py`'s `_flush()`
  stays — it was never Anthropic-specific, just the only thing that used
  it, and it's exercised by a provider-agnostic test.
- **R74b. `config.yaml`'s single `openrouter` provider (routing BOTH local
  and remote models through the m7 gateway) is split into two providers**:
  `local` (the m7 gateway, LAN/Tailscale — this user's own infrastructure,
  not something a fresh clone has access to) and `openrouter` (the real
  `https://openrouter.ai/api/v1`, direct, with its own `OPENROUTER_API_KEY`
  — works for anyone with their own OpenRouter key, no dependency on this
  user's server). Model entries updated to match:
  `model: local → provider: local`; `kimi-k2.7-code`/`glm-5.2` →
  `provider: openrouter`. `config.yaml.example` already had this shape
  (kept as the reference template); `config.yaml` now matches it.
- **R74c. No more startup logo.** `logo.py` deleted; `_banner()` (`tui.py`,
  `ui.py`) no longer renders one, just the plain info-line card.
  `config.yaml`'s `runtime.logo` key and the `Pillow` dependency
  (`pyproject.toml`) removed — Pillow had no other use.
- **R74d. The model name on the status bar (line 1) is clickable** — same
  effect as typing `/model` + Enter (`tui.py`'s `_open_model_picker`,
  styled `class:status.id` like the session-id/copy buttons). A second
  click while that SAME menu (`_menu_prompt == "Select model"`, so this
  never touches an unrelated open menu) is still open closes it without
  changing the model — `select_menu()` now returns `None` for an explicit
  dismiss (distinct from an actual pick), and `ui._pick_model` treats
  `None` as "no change," same spirit as blank-Enter in the classic REPL.

### Version is pinned on GitHub deploy, not computed there (2026-07-15, R75)
- **R75. `aurora.__version__` is `1.0.<commit-count>`, computed live from
  `git rev-list --count HEAD` — but that's only meaningful in GitTea, the
  dev repo the numbering was designed around.** GitHub/Aurora (the public
  mirror, see "Deployment note") is a SEPARATE git repo with its own
  unrelated commit history — computing its own commit count there (or in
  any downstream clone/production install) would silently report a
  plausible-looking but WRONG version. `aurora/__init__.py` now has a
  `_PINNED_VERSION` constant (empty in GitTea — empty means "compute
  live"); `scripts/github-deploy.sh` overwrites that exact line on every
  deploy to GitTea's real version at that moment (`sed` after the rsync),
  so GitHub/Aurora and everything downstream of it reports a frozen,
  correct version instead of a meaningless local commit count. Every
  GitHub deploy MUST re-run this step — an old pin left in place after
  GitTea moves on would report a stale version forever.

### Rate-limit errors get an actionable hint, not a raw JSON dump (2026-07-15, R76)
- **R76. A `429`/rate-limit `ProviderError` (common on a free-tier
  OpenRouter model like a `:free` variant, shared across everyone using it
  without their own key on that upstream) no longer surfaces the provider's
  raw JSON error blob.** `agent.py`'s `run_turn` ProviderError handler
  (same pattern as the existing context-full and connectivity cases — see
  §8's comment "every one of these is a place a future change should ADD a
  case, not replace the pattern") gets a new branch: `"429" in msg` or
  `"rate"+"limit"` in the message triggers a clean notice ("rate-limited by
  the provider — this model's free tier is shared; try again shortly, add
  your own provider key, or /model to switch") instead of falling through
  to the generic `provider error: {e}` dump.

### Live draft token estimate on the status bar (2026-07-15, R77)
- **R77. While typing, the status bar shows an approximate token cost for
  the UNSENT draft, next to context usage** — `ui.estimate_tokens(text)`
  (~4 chars/token, the common English-text rule of thumb; no tokenizer
  dependency, no network call, purely a local heuristic — never the real
  count, which only exists after the provider's actual response) renders
  as `(+~N draft)` between `ctx used/limit` and the `%`. Wired into both
  status bars: `tui.py`'s `status()` reads `self.input.buffer.text`
  directly (skipped in bash mode, while a secret is being entered, or
  during a blocking `ask()` — none of those are a model-bound prompt
  draft); `ui.py`'s classic-REPL `_footer()` reads the live buffer via
  `prompt_toolkit.application.get_app().current_buffer.text` (the
  `bottom_toolbar` callable has no argument carrying it). Both already
  redraw on every keystroke via prompt_toolkit's normal buffer-change
  invalidation, so the estimate updates live with no extra wiring.

### Review batch: Esc-armed hint restored, retry-nudge leak, doc reconciliation (2026-07-16, R78)
- **R78a. The R62 "armed" status-bar hint actually renders now.** The first
  Esc of the double-tap gesture was documented (R62, ARCHITECTURE §9) as
  showing a status-bar hint, but the code showed nothing (a vestigial
  ternary in `tui.status()` had two identical branches). Line 2 now shows
  `Esc again to cancel this / leave bash mode / quit` while the 2s window is
  armed, for all three states, taking precedence over the other line-2
  content; it falls back to normal when the window expires. Tested
  (`test_esc_armed_shows_*`, `test_esc_hint_expires_back_to_tooltips`).
- **R78b. A failed malformed-tool-call retry no longer leaks the corrective
  nudge into history.** `agent.run_turn`'s R5 retry appends a transient
  "your previous tool call was malformed" user message; it was popped on
  the retry's success or a second `MalformedToolCall`, but a `ProviderError`
  raised during the retry propagated with the nudge still in `messages` —
  a stray consecutive user message that poisons the next send (most chat
  APIs reject it). The pop now happens in a `finally`, covering every
  outcome. Tested (`test_malformed_retry_error_leaves_no_nudge_in_history`).
- **R78c. `/redact` restored to autocomplete** — it was listed in `/help`
  but missing from `COMMAND_INFO`, so `/`-completion never offered it.
- **R78d. `read_file` no longer slurps whole files** — it read the entire
  file into memory before truncating to `MAX_READ_BYTES`; it now reads only
  the first `MAX_READ_BYTES + 1` bytes (a multi-GB file cost GBs of RAM for
  a 200KB result).
- **R78e. Dead code removed**: `Engine.set_max_iterations` (orphaned since
  `/max` was torn out with R61); the duplicate `aurora_home` import in
  `tui._build_app`.
- **R78f. Doc reconciliation**: R9's `/max` marked removed; R54/R62 updated
  to the tested behavior that **Esc is a no-op while a menu is open**
  (explicit pick required — the old Esc-to-safest shortcut no longer
  exists); the build-plan tree no longer lists the deleted
  `providers/anthropic.py`; test counts refreshed (165 → 178) and
  `test_expand_newlines.py` listed; a corrupted section heading restored
  ("Standalone operation" had been overwritten by a duplicated `/remember`
  heading, losing its first line); `config.persist_runtime_value`'s
  docstring now states that YAML comments are lost file-wide on write-back.

### Deep-dive batch 2: drag-select pad offset, diff-preview crash, stream/IO hardening (2026-07-16, R79)
- **R79a. Drag-select copied the WRONG lines on a short transcript.** Mouse
  positions arrive in content coordinates, and the rendered content is
  top-padded when the transcript is shorter than the pane (bottom-anchoring,
  R38/R49) — but `_sel_text()` indexed the UNPADDED transcript, so every
  drag-copy before the pane filled up grabbed lines offset by the pad (and
  the highlight matched the mouse, hiding the mismatch until paste).
  Selections are now normalized to unpadded text coords at capture
  (`Tui._unpad` in `sel_begin`/`sel_drag`) and shifted back by the pad only
  for the render overlay. Tested (`test_drag_select_accounts_for_top_pad`,
  `test_drag_select_render_overlay_shifts_back_by_pad`).
- **R79b. `approve.diff_preview` can no longer kill a turn.** It runs inside
  the agent loop AFTER the assistant message (with its tool_use) is already
  in history; a `write_file`/`edit_file` aimed at a non-UTF8 (binary) or
  unreadable file made `read_text()` raise, killing the turn and leaving the
  dangling tool_use to poison every later request. It now catches everything
  and returns `[diff unavailable: …]` — the approval challenge still shows,
  just without a diff. Tested
  (`test_diff_preview_never_raises_on_binary_target`).
- **R79c. One garbled SSE line no longer kills the whole stream** —
  `openai_compat.turn()` skips a `data:` line that fails to parse as JSON
  instead of raising a raw `JSONDecodeError` mid-turn. Tested
  (`test_sse_stream_skips_garbled_line`).
- **R79d. `web_fetch` downloads are capped, not unbounded** — it fetched the
  entire body into memory before truncating to 20k chars of text; it now
  streams and stops at 2MB (`_FETCH_CAP`).
- **R79e. `/resume`'s session listing streams each log** — `list_sessions()`
  read every session's whole JSONL just to find the first user line; it now
  reads line-by-line and stops at the first hit (and skips a corrupt line
  instead of crashing the listing).

### `/model add` — add an OpenRouter model by URL (2026-07-16, R80)
- **R80. `/model add <url-or-id>` appends an OpenRouter model to
  `config.yaml` and switches to it.** Accepts the model's OpenRouter page
  URL (`https://openrouter.ai/<org>/<model>`, `models/` prefix tolerated)
  or the bare `<org>/<model>` id (`ui._parse_openrouter_model`); anything
  else prints usage. OpenRouter-only for now — the entry is written under
  the config's `openrouter` provider (`{provider, model, tools: true}`),
  and the command errors cleanly if no such provider is configured.
  - **Key flow**: if `OPENROUTER_API_KEY` isn't available, the same
    fetch-command-then-hidden-prompt flow as the picker
    (`ui._prompt_and_store_key`) runs first; skipping it still ADDS the
    model (config is harmless without a key) but doesn't switch to it,
    printing the manual `aurora key set` fallback instead.
  - **Persistence**: `config.persist_model_entry` — the same raw-text
    round-trip as `persist_runtime_value` (${VARS} survive, YAML comments
    don't), appending to the LIVE `cfg["models"]` list too (which
    `Engine.models` aliases), so the picker sees the new entry without a
    restart. `Engine.add_model` dedupes on the exact (provider, model)
    pair — re-adding is a no-op that just re-selects.
  - **Catalog fetch**: `openai_compat.fetch_openrouter_model_info` looks
    the model up in OpenRouter's public `/api/v1/models` (no key needed) —
    context size, prompt/completion pricing (converted to $/Mtok), and the
    catalog description — and `save_remote_model_info` writes it into
    `remote_context_limits.json` (house format: model/provider/code/
    pricing_url/description) AND the in-memory table, so the footer's ctx
    gauge (R71), the `$` cost badge (R73), and the picker's info line work
    for the just-added model immediately. **Caveat, deliberate**: the API
    returns the listed route price, not the usage-weighted average the
    hand-maintained entries use (R73) — close enough for a fresh add,
    printed as "(listed price)"; refine the JSON by hand if it matters.
  - **The catalog lookup doubles as validation (2026-07-16 revision)**: it
    runs FIRST, and a model the reachable catalog doesn't list is REFUSED
    ("not found on OpenRouter") with nothing written — a typo'd id must
    fail at the add, not on the first send. Only when the catalog itself is
    unreachable (offline) does the add proceed, marked "unverified", with
    ctx/pricing unknown. `fetch_openrouter_model_info` returns
    `(info, catalog_ok)` so the caller can tell the two apart. Tested
    (`test_model_add_refuses_nonexistent_model`,
    `test_model_add_offline_adds_unverified`).
  - Tested end-to-end (`test_parse_openrouter_model`,
    `test_add_model_persists_and_dedupes`,
    `test_save_remote_model_info_updates_json_and_memory`,
    `test_model_add_command_end_to_end`, `test_model_add_rejects_garbage`).

### `/model remove` — drop a configured model (2026-07-16, R81)
- **R81. `/model remove <url-or-name>` (alias `rm`) removes a model from
  `config.yaml`.** Accepts the OpenRouter page URL (same parsing as R80) or
  the exact configured model name — ANY provider's entry, `local` included
  (it's just config; re-add by editing config.yaml or `/model add`).
  `config.remove_model_entries` drops every matching entry from the file
  and mutates the live `cfg["models"]` list in place (which `Engine.models`
  aliases), so the picker updates without a restart. Unknown name → a
  clean "not in config.yaml" notice, nothing written.
  - **Removing the CURRENT model falls back** to the first remaining model
    with a usable key (`Engine._default_model`, the same first-boot rule)
    and switches to it; removing the last configured model leaves
    `engine.current == {}` with a "no models left — /model add" warning.
  - **Cached catalog info is deliberately kept** — the model's
    `remote_context_limits.json` entry (ctx/pricing/description) survives
    removal, so a later re-add gets its footer gauge/badge instantly.
  - Tested (`test_remove_model_persists`,
    `test_remove_current_model_falls_back`,
    `test_remove_last_model_leaves_no_current`,
    `test_remove_model_command_accepts_url_and_unknown`).

### Race-condition sweep (2026-07-16, R82)
- **R82a. A mid-turn endpoint flip can no longer redirect the request.**
  `OpenAICompatProvider.base_url` is mutated by TWO threads: the worker's
  `turn()` (via its own `pick_endpoint(cache_ok=False)`) and the UI
  thread's status renders (`context_stats` → `live_context_limit` →
  `pick_endpoint(cache_ok=True)`, every ~120s when the limit cache
  expires). If a UI-side probe flipped `base_url` between the worker's
  pick and its request/retries, the request (or a retry attempt) went to a
  different endpoint than the one just probed. `turn()` now PINS the
  picked endpoint and its client in locals for the whole attempt loop —
  concurrent flips only affect the NEXT turn.
- **R82b. The per-endpoint client pool is lock-guarded.**
  `_client_for(base_url)` (new; the `_client` property delegates to it)
  guards the `_http` dict with a lock and uses `setdefault` so two threads
  racing to create the same endpoint's client keep exactly one — before,
  a UI-thread probe and a worker turn could each build a client and one
  pool leaked unclosed.
- **R82c. Happy-Eyeballs winner selection is atomic.** Two racers could
  both pass the bare `stop.is_set()` check before either called
  `stop.set()` — both claimed the win and the loser's connected socket
  leaked (nobody left to close it once the caller returned with the first).
  A `win_lock` now makes test-and-set atomic; exactly one winner, every
  loser closes its socket.
- **R82d. `Engine.send` with no model configured notifies instead of
  building a blank provider** — possible since `/model remove` (R81) can
  empty the config; previously it went through a keyless, URL-less provider
  to a generic "request failed" error. Tested
  (`test_send_with_no_model_notifies_instead_of_crashing`).
- **Reviewed and deliberately left as-is**: the worker-thread queue handoff
  for `ask()`/`select_menu()` (documented design, §6 of ARCHITECTURE.md —
  including its buffer writes from the worker, which prompt_toolkit
  tolerates for plain `.text` assignment); the UI thread's periodic /props
  probe (bounded, 120s-cached, and now harmless to in-flight requests per
  R82a); `Session.log`'s open-per-event writes (single-writer by design).

### Shipped bootstrap prompt + boot hero image (2026-07-16, R83)
- **R83. The repo ships a recommended start prompt,
  `bootstrap.example.md`** — orient in the project before touching
  anything: read README + rules files (AGENTS.md/CLAUDE.md/CONTRIBUTING.md),
  bootstrap `.agentic_context/` when present, check the git state (branch/
  dirty files/last commits), reply with a short brief and wait; read-only
  until then. Installed with `/bootstrap set bootstrap.example.md`
  (optionally `project`). Documented in README ("Start every session
  oriented"). The hero image (`images/aurora.png`, used by both the README
  and ricardopsantos.org/aurora) now shows Aurora at boot offering to run
  this prompt — banner, bootstrap ask menu, collapsed input, two-line
  status bar, rendered in the TUI's real colors/layout.

### `/compact` gauge fix (2026-07-22, R84)
- **R84. The context-usage gauge and its `>80% — /compact?` hint now drop
  immediately after `/compact`.** `Engine.compact_history()` folded the
  message history but never reset `Engine._used`, which the footer's
  `context_stats().pct` reads directly — so the gauge (and the derived
  80% warning, driven by the same `pct`) kept showing the pre-compact
  value until the next real turn overwrote it. Fixed by re-estimating
  `_used` from the folded summary's token count right after the fold.

### Model picker: ESC to cancel (2026-07-22, R85)
- **R85. The model-picker `select()` menu (`/model`) can be dismissed with
  a bare Esc.** Every other `select()`/confirm menu in the TUI still
  requires an explicit pick (arrow keys + Enter, or a number key) — Esc is
  a no-op while they're open, by design. The model picker is the one
  exception: it's a picker over the CURRENT model, so backing out with no
  change is a valid outcome, same as a second click on the status bar's
  model name (`_open_model_picker`). `TuiFrontend._on_escape` special-cases
  `self._menu_prompt == "Select model"` to push `None` onto the answers
  queue instead of doing nothing; `ui._pick_model`'s existing `chosen is
  None` branch already treated that as "no change." The status-bar tip
  during that menu also now reads "select one, or ESC to cancel" instead of
  the generic "select one" shown for every other menu.

### `/bootstrap set` accepts a URL; startup offers cached-vs-redownload (2026-07-22, R86)
- **R86. `/bootstrap set <url>`** downloads the URL's contents (plain GET,
  no HTML stripping — bootstrap prompts are markdown/plain text, e.g. a
  GitHub raw link) and caches them the same way a local file/paste would,
  via `bootstrap.fetch_url()`/`bootstrap.is_url()`. The URL itself is
  remembered in a `bootstrap.md.source` sidecar file
  (`bootstrap.save(..., source_url=...)`) next to the cached prompt —
  project vs global sidecar mirrors whichever `bootstrap.md` it belongs to.
  Overwriting a URL-sourced prompt with a plain paste/file drops the stale
  sidecar; `/bootstrap clear` removes it too.
- **Startup now asks a 3-way choice when the active bootstrap prompt is
  URL-sourced:** run the cached copy (default), re-download and run, or
  skip — instead of silently doing either every session.
  `ui._bootstrap_run_choice(url)` returns the plain "run"/"skip" yes-no
  choice when there's no URL, or the 3-way `select()` when there is;
  `_run_bootstrap(engine, fe, redownload=True)` re-fetches via
  `bootstrap.refresh_from_source()` (re-downloads, re-persists to the same
  path, keeps the sidecar) before running, falling back to the cached copy
  if the re-fetch fails. Shared by both the classic REPL (`ui.run`) and the
  TUI worker (`TuiFrontend._worker`) — the TUI's `ui.select`/`ui.confirm`
  monkeypatch means the same helper renders correctly in both frontends.
  `/bootstrap show` also displays the origin URL when one is set.
- **Tested against a real URL, not just a mocked `fetch_url`:**
  `tests/test_bootstrap_network.py` is the one deliberate exception to the
  rest of the suite's no-network rule — it downloads the AgenticContext
  repo's `MAIN_PROMPT.md`
  (`https://raw.githubusercontent.com/ricardopsantos/AgenticContext/refs/heads/main/MAIN_PROMPT.md`)
  for real via `set`/`refresh_from_source`, skipping (not failing) if the
  network isn't reachable.

### `/remember` scoped save + ~/AURORA_PFCS fallback (2026-07-22, R87; fallback path revised same day)
- **R87.** *(Removed 2026-07-29 along with R12 — see that entry.)*
  ~~**`/remember [all|last [k]]`** controls how much of the session
  `memory.py` checks before saving: no argument or `last` checks just the
  last question/reply pair, `last k` the last `k` pairs, `all` the whole
  session (the original R52 scope, still what "save everything" means
  when explicitly asked for). `memory._last_k_messages` slices `engine.
  messages` at the k-th-last user-role message and keeps everything after
  it, so a multi-iteration tool-call reply is kept whole. A malformed
  argument (e.g. `last abc`) prints usage instead of guessing. Restored to
  `/` autocomplete and the README table (superseding the 2026-07-12
  hide-while-reworking note above).~~
- ~~**`~/AURORA_PFCS/MEMORY/` fallback when there's no real `.agentic_context`
  (originally `AURORA_MEMORY/` at the project root; revised same day).**
  `find_context_root` now requires BOTH a `KNOWLEDGE/` and a `MEMORY/`
  subfolder to count — a bare `MEMORY/` alone no longer qualifies. When
  neither is found walking up from cwd, `/remember` writes findings flat
  into `memory._fallback_root()` (`Path.home() / "AURORA_PFCS" / "MEMORY"`)
  instead of refusing outright — deliberately a FIXED, machine-wide
  location, not per-project, since there's no project root to anchor a
  per-project fallback to when the whole point is that none was found.
  Same house `.md` format (title/`> summary:`/discovered/context/body) via
  `render_finding(..., flat=True)`, but no group subfolders and no
  INDEX.md/rebuild-index.sh step (that tooling is specific to
  `.agentic_context`) — the notify message says so explicitly.~~

### `find_context_root` detects by contents, never by folder name (2026-07-22, R88)
- **R88.** *(Removed 2026-07-29 along with R12 — see that entry.)*
  ~~**`memory.find_context_root` no longer hardcodes `.agentic_context`
  as a literal path segment.** It walks up from cwd and, at each ancestor,
  checks every immediate subfolder for BOTH a `KNOWLEDGE/SKILL.md` and a
  `MEMORY/SKILL.md` — whichever subfolder has both, regardless of its own
  name, is the context root. `.agentic_context` remains the convention
  (and what this repo itself uses), but a differently-named folder with
  the same shape is now found too. Requiring the `SKILL.md` files (not
  just the `KNOWLEDGE`/`MEMORY` dirs) rules out an unrelated folder that
  happens to have similarly-named subfolders with no actual content.~~

### `/agentic_report` command + status-bar link (2026-07-22, R89)
- **R89.** *(Removed 2026-07-29 along with R12 — see that entry.)*
  ~~**`/agentic_report`** (`ui._agentic_report_cmd`): asks "Stats" or
  "Index" via the normal `select()` menu. **Stats** runs the context
  folder's own `scripts/stats.sh` (size/count stats for
  KNOWLEDGE/MEMORY/SKILLS — `memory.run_stats`) as-is. **Index**
  pretty-prints `KNOWLEDGE/INDEX.md` and `MEMORY/INDEX.md` through
  `mdrender.LineRenderer` (the same markdown→ANSI renderer chat replies
  use) instead of dumping raw markdown.~~
- ~~**Only exists as far as the user is concerned when a context protocol
  folder is detected** (`memory.find_context_root(".")`, by contents —
  R88): hidden from `/` autocomplete (`SlashCompleter.__init__` computes
  `self._has_agentic_context` once per completer lifetime, not per
  keystroke) and from `/help`/`?` (`ui.help_text(has_agentic_context)`
  appends the `/agentic_report` line only when true). Typing it manually
  when nothing is detected still works and just says so — same
  discoverability-only pattern as `/remember`'s 2026-07-12 hide (R52's
  note above).~~
- ~~**The TUI's line-1 status bar shows a clickable, underlined "agentic
  report" link** — same `class:status.id` style as "session id"/"copy
  last"/"copy all" — under the same detection, cached once as
  `self._agentic_root` in `Tui.__init__` (not re-walked on every render
  tick). Clicking it (`_agentic_report_click`) queues `/agentic_report`
  onto the worker's inbox exactly like the model-picker click queues
  `/model` — the Stats/Index choice is a blocking `select()`, which must
  never run on the UI thread.~~

### Deep-dive batch 3: boundary guard, tool reach, gauge, scan cost (2026-07-22, R90)
From a full requirements-vs-code review of the whole project.
- **R90a. The engine/UI boundary is enforced against RELATIVE imports too.**
  `engine.py`'s `compact_history` did `from .ui import estimate_tokens`,
  pulling `prompt_toolkit` into the engine half and breaking R25 — and
  `test_architecture.py` passed the whole time, because it checked
  `node.module.endswith(".ui")` and a relative `from .ui import x` parses as
  `module="ui", level=1`, which never matches. The guard now rebuilds the
  dotted name from `level` (and also checks plain `import` statements), and
  the two token helpers moved out of `ui.py` into a new engine-side
  **`aurora/tokens.py`** (`estimate_tokens`, `fmt_token_count`), re-exported
  from `ui` since the TUI and tests already reach for them as `ui.<name>`.
  A silently-broken invariant test is worse than no test: this one is
  load-bearing for the "swap the UI, keep the engine" promise.
- **R90b. The read/search tools can actually reach what they're told to.**
  - `grep` runs with **`-E`** (extended regex), not the default BRE. Models
    write ERE by habit (`(foo|bar)`, `a+`, `x?`); under BRE those are
    literals, so the search returned `[no matches]` — a SILENT wrong answer,
    the worst failure mode for an agent, which then concludes the code
    doesn't exist.
  - `read_file` takes optional **`offset`/`limit` (1-based line range)**,
    streamed, never slurping the file. The truncation notice already told
    the model to "read a specific range" after a big file; there was no
    parameter to do it with, so its only recourse was re-reading the same
    head. The notice now names the parameters.
- **R90c.** *(Removed 2026-07-29 along with R12 — see that entry.)*
  ~~ONE context-root detector, shared by every surface. R88 made
  `memory.find_context_root` name-agnostic (by contents: an immediate
  subfolder with BOTH `KNOWLEDGE/SKILL.md` and `MEMORY/SKILL.md`, hidden or
  not, nearest first walking up) but `context.py` — the module that does the
  actual **bootstrap** and backs `open_context_doc` — still hardcoded
  `.agentic_context` as a literal path segment AND only looked in the cwd,
  never walking up. A differently-named folder was found by `/remember` and
  `/agentic_report` yet never bootstrapped; a subdirectory of a project
  bootstrapped nothing at all. The detector now lives in `context.py`,
  `context.detect()` is that function, and `memory.find_context_root` is a
  re-export of it — one implementation, one answer.
  - Call sites unified on the **CWD** as well: the `/`-autocomplete and
    `/help` gates keyed on the config's `_base_dir`, which is the Aurora
    checkout — it has its own context folder, so `/agentic_report` was
    offered in every project and then reported "nothing detected" when run.~~
- **R90d. The context gauge counts what's actually in the window.** It read
  `input_tokens + output_tokens`, where `input_tokens` is the LAST request's
  prompt but `output_tokens` is the SUM of completions across every
  iteration (R37's cost accounting). Each earlier round's reply is already
  inside the next round's prompt, so summing them double-counted and
  overstated the gauge — and with it the ≥80% `/compact` hint — on every
  multi-tool turn. `Turn.last_output_tokens` (new) feeds the gauge;
  `output_tokens`/`billed_input` still feed cost, which really is billed per
  round.
- **R90e. `secrets.scan` is linear again.** Overlap tracking was a list of
  `(start, end)` spans that every later candidate re-scanned — O(matches²)
  on a match-dense block (a fixtures file of UUIDs, a big `.env`, a
  token-heavy log), on the worker thread, on by default. It's now a
  per-character `bytearray` mask: O(span) to claim, O(span) to test.
  Behaviour is identical — first claim on a span still wins, spans never
  overlap (they'd misalign `redact`'s right-to-left substitution).
- **R90f. Smaller reach/robustness fixes.**
  - `run_command` takes an optional **`cwd`** (the model had to prefix every
    call with its own `cd … && …`, which breaks the moment a path needs
    quoting) and honours **`runtime.timeout`** instead of a hardcoded 300s a
    user couldn't reach.
  - `edit_file` takes **`replace_all`** — renaming a symbol that appears 20×
    was 20 uniquely-anchored calls. The unique-anchor guard is unchanged by
    default and its error message now names the escape hatch.
  - `Session.iter_records()` **streams** the JSONL; `export_markdown` and
    `resume_from` use it. A session log is unbounded by design (R20, nothing
    is auto-deleted) and both used to hold the whole file in memory on top
    of the parsed records. A corrupt line is still skipped, never fatal.
  - `resume_from` **re-estimates the context gauge** from the restored
    history — it read 0 until the first new turn while a full conversation
    was already loaded.
  - `context_stats()` **returns early when no model is configured**
    (possible since `/model remove`, R81) instead of building a keyless,
    URL-less provider on every status render.

### Prompt caching, provider-agnostic (2026-07-22, R91)
- **R91. The system prompt is marked as a cache breakpoint so it isn't
  re-billed on every request.** The successor to the retired R15 (which was
  Anthropic-API-specific and died with that provider), rebuilt on the
  OpenAI-compatible mechanism so it works for whatever `openai_compat`
  talks to.
  - **Why it matters here specifically:** Aurora's system prompt is not a
    one-liner — it's the base preamble + `AGENTS.md` + all three `INDEX.md`
    files + every `[CORE]` doc (~6k tokens in this repo). It is re-sent on
    every request, and a turn makes one request per tool iteration, each
    billing the full prompt (R37). A five-tool turn paid for that preamble
    five times.
  - **Mechanism**: `openai_compat._system_message(system, cache)` sends the
    system message as a content block carrying
    `cache_control: {"type": "ephemeral"}` instead of a plain string. This
    covers both halves of the OpenAI-compatible world: OpenAI/DeepSeek-style
    backends cache long prefixes automatically and ignore the marker;
    Anthropic-family models routed through OpenRouter cache ONLY at an
    explicit breakpoint. The system prompt is the right (and only sensible)
    breakpoint — it's the one part byte-identical across a whole session.
  - **Under `_CACHE_MIN_CHARS` (4k chars ≈ 1k tokens) the marker is not
    sent at all** and the payload is byte-identical to the pre-R91 shape.
    Anthropic won't cache below ~1024 tokens anyway, and a cache WRITE costs
    more than a plain read — marking a short prompt is a pure loss.
  - **Per-model, defaulting sensibly**: `Engine.cache_enabled()` — global
    `runtime.prompt_cache` (default on, `/cache on|off` persists it), then a
    model entry's own `cache:` flag if present (same shape as `tools:`),
    else ON for a remote model and **OFF for the `local` sentinel**:
    llama.cpp keeps its own KV prefix cache locally, there is nothing to
    bill and nothing to mark, and sending it a structured system message is
    needless compatibility risk.
  - **Plumbed as an attribute** (`provider.cache_prompt`), set per turn by
    `Engine.send` exactly like `extra_body`/`on_think` — the `Provider.turn`
    signature is unchanged, so no front end, subclass or test fake had to
    move.
  - **The payoff is visible, not assumed**: `usage.prompt_tokens_details.
    cached_tokens` is read into `TurnResult.cached_input_tokens`, summed
    across a turn into `Turn.cached_input`, logged per turn, and reported by
    `/cost` (R92). It is **not** subtracted from `billed_input` — a cache
    read is cheaper but not free and the discount isn't reported uniformly,
    so the cost estimate stays a deliberate UPPER bound rather than a
    confidently wrong lower one.

### `/cost` — per-model token and $ breakdown (2026-07-22, R92)
- **R92. `/cost [all]`** shows turns / billed input / output / cached tokens
  / estimated $ per model, for the current session or (`all`) every session
  logged on this machine.
  - **A pure read over data Aurora already writes.** Every `assistant` event
    in the session JSONL has carried `model`/`input_tokens`/`output_tokens`
    since R20; `session.usage_by_model()` just aggregates them. No new
    bookkeeping, no state to keep in sync, and it works on sessions that
    ended weeks ago.
  - `send()` now also logs **`billed_input`** (the sum across a turn's
    iterations — the real cost basis, R37) and **`cached_input`** (R91).
    Older logs lack both: `billed` falls back to `input_tokens`, `cached`
    to 0, so historic sessions still report, just less precisely.
  - Pricing comes from the same per-model table as the footer badge, via
    the new `openai_compat.price_for(model)` — the one place the table is
    read without a live provider instance. A model with no entry prints
    "no price" rather than a `$0.00` that would imply it was free (the same
    honesty rule as R73's badge).
  - The report labels itself an estimate and an upper bound (cached tokens
    bill cheaper than shown) — it is a spending *gauge*, never an invoice.

### `todo_write` — a task list for multi-step work (2026-07-22, R93)
- **R93. REMOVED 2026-07-24.** User didn't want the feature — dropped
  entirely: `aurora/todo.py` deleted; `tools.py`/`engine.py`/`ui.py` lost
  their `todo` wiring (`TODO_ENABLED`/`set_todo_enabled`, the SPEC/RUNNERS
  entries, `todo.clear()`); `/todo` and its help/man text removed;
  `config.yaml.example`'s `todo_tool` key removed. Kept below for history.
- ~~The model can keep a visible task list~~ (`aurora/todo.py`,
  tool `todo_write`, shown by `/todo`).
  - **Why**: the loop nudge (R27) and the iteration cap (R9) both exist
    because models drift on multi-step work — re-running a call, or
    wandering off the original request three tools deep. Both are *brakes*.
    A task list is the cheap structural fix from the other side: the model
    writes the plan down, then re-reads its own list every time it calls the
    tool again, so "what was I doing" is answerable from the conversation
    instead of re-derived from the transcript.
  - **Deliberately dumb**: a list of `{task, status}` (pending/in_progress/
    done) in memory for the session, **rewritten wholesale** by each call —
    no ids, no partial updates, no persistence, no file on disk. Fewer ways
    for the model to get it wrong, and nothing to migrate later. `/clear`
    resets it with the rest of the conversation (it belongs to the
    conversation, not the machine).
  - Sloppy input is tolerated rather than rejected — a bare string, a
    `content` key instead of `task`, an unknown status — because small local
    models produce all three and a hard error there just burns an iteration.
    An empty list clears.
  - `render()` is the single representation: the same text goes back to the
    model as the tool result and is what `/todo` prints, so the two can't
    drift.
  - `runtime.todo_tool` (default true) removes it from the tool list
    entirely — for a small local model that loses more to one extra tool
    than it gains from a plan. Engine-side module: no UI imports.

### Read-only tools run in parallel (2026-07-22, R94)
- **R94. A round's read-only tool calls run CONCURRENTLY.** When the model
  asks for four files (or three greps and a fetch) in one message, those
  calls are independent — running them one after another is latency nobody
  chose. `agent.run_turn` prefetches them through
  `tools.run_tools_parallel` (a `ThreadPoolExecutor`, ≤8 workers) and the
  sequential loop then consumes the results.
  - **`tools.PARALLEL_SAFE` is an explicit allowlist**, deliberately NOT
    "everything outside `NEEDS_APPROVAL`": the real test is "read-only AND
    no shared state", which `todo_write` (R93, removed) failed despite
    being ungated even while it existed.
    Members: `read_file`, `list_dir`, `grep`, `open_context_doc`,
    `web_search`, `web_fetch` — all of them only read the filesystem or the
    network, so ordering between them is unobservable.
  - **Everything the user sees stays sequential and in the model's original
    order**: tool starts (announced in order at dispatch), approvals, secret
    challenges (R58), transcript entries, and the history messages. Only the
    *waiting* overlaps. `run_tool` already converts every exception into a
    `[tool error: …]` string (R42), so a worker can neither raise nor
    corrupt shared state.
  - Only fires with **≥2** eligible calls in a round; `runtime.parallel_tools`
    (default true) disables it.
  - **Accepted caveat**: a later deny/stop/cancel in the same round means
    some reads already ran. They have no side effects, so the only cost is
    discarded work, and their results are still answered `[skipped: …]` so
    history stays valid. The iteration-cap ask runs BEFORE the prefetch, so
    stopping there prefetches nothing.

### Deep-dive batch 4: guards that didn't guard (2026-07-22, R95)
Four independent findings from an audit pass, one shape: a mechanism that
looks like it is protecting something, reports success, and isn't.

- **R95a. The approval diff shows what the edit will ACTUALLY do.**
  `approve.diff_preview` previewed `text.replace(old, new, 1)` — a hardcoded
  count of 1 — while `tools.edit_file(replace_all=True)` (R90g) replaces
  every occurrence. On a 3-occurrence file the human approved a one-line
  diff and got three lines changed. The preview now passes `-1` when
  `replace_all` is set. R8's premise is that the diff IS the change; a
  preview that under-reports is worse than no preview, because it buys
  consent for something else.
- **R95b. `grep` reports errors as errors, not as "[no matches]".**
  `grep` exits 0 for a match, 1 for no match, and **≥2 for a real error** —
  the runner checked neither the exit code nor stderr and returned
  `[no matches]` for an unbalanced regex or a bad path. That is exactly the
  R90b failure mode wearing a different hat: the model reads "no matches",
  concludes the code does not exist, and moves on instead of fixing its
  pattern. Errors now surface as `[grep error: …]`; a genuine miss is still
  `[no matches]`, and stdout always wins (a partial result with a
  permission-denied warning is a result, not an error).
- **R95c. A timed-out command dies with its whole process group.**
  `subprocess.run(shell=True, timeout=…)` kills only the shell. Every child
  it spawned survived, reparented to init, and kept running for the rest of
  the session — a timed-out build, dev server or test run burning CPU
  invisibly. `run_command` now uses `Popen(start_new_session=True)` and
  SIGKILLs the group.
  - **The pgid is read immediately after spawn, not at timeout.** The case
    that matters most is a command that backgrounds something and exits
    (`(build &)`): the grandchild keeps the stdout pipe open, so
    `communicate()` blocks the full timeout on a shell that is already
    gone — and `os.getpgid()` then raises `ProcessLookupError`, losing the
    handle on the very orphan we came to kill. Looking it up late fixed the
    easy case and missed the real one.
  - Partial output is kept (from `TimeoutExpired.stdout`, which carries what
    was read before the deadline) and printed above the timeout line — a
    truncated build log is far more useful than a bare `[timeout]`.
- **R95d. R58 detects the canonical credential spellings.** The Env-credential
  pattern required at least one character BEFORE the credential word, so
  `MY_API_KEY=` matched but a bare `API_KEY=`, `SECRET=`, `TOKEN=`,
  `PASSWORD=` or `PASSWD=` — the normal shape in a `.env` file or an `env`
  dump, and the commonest of all — matched nothing. The prefix is now
  optional.
  - **`PWD` keeps its mandatory prefix**, deliberately: bare `PWD=` is the
    shell's own working-directory variable, present in every `env` dump and
    never a credential, while `DB_PWD=` is. Widening a detector is only
    correct if the new matches are real; this one exception is what keeps
    the change from trading a false negative for a daily false positive.

### Deep-dive batch 4, part 2: accounting, reach, and the render path (2026-07-22, R95e–j)
The rest of the same audit. R95e–g are correctness; R95h–j are the
performance half, all three of the same shape — work repeated on a path that
runs far more often than the thing it is recomputing changes.

- **R95e. A turn that produced nothing logs nothing.** `Engine.send` pops the
  dangling user message when a turn dies before any assistant output (so
  history never stacks two consecutive user turns), which leaves
  `messages[-1]` pointing at the **previous** turn's reply — and that got
  logged as a fresh `assistant` event. A provider outage therefore re-recorded
  the last good answer, inflating `/cost`'s turn count (R92) and duplicating
  the answer in the markdown export. `send` now tracks whether the turn
  appended anything and returns early when it didn't.
- **R95f. `read_file`'s range stops at the byte cap.** With `offset` and no
  `limit` the loop accumulated every remaining line and truncated only at the
  end — on a multi-GB file that is exactly the slurp the streaming loop was
  written to avoid. It now breaks at `MAX_READ_BYTES` and reports
  `more follow`, which was already the honest label for stopping early.
- **R95g. File allowlist rules survive path spelling.** `run_command` rules
  normalize their tokens (quotes stripped, `~` expanded) so equivalent
  spellings collapse onto one rule; `write_file`/`edit_file` did a raw
  `fnmatch` on whatever the model passed, so `~/notes.md` and its expansion
  were two different rules and "always allow" re-prompted on the other
  spelling. Both sides now normalize, which also keeps pre-R95g raw rules
  working. Expanded but **not** resolved — resolving would follow symlinks
  and collapse the `*` in a glob, and a rule is allowed to be a glob.
- **R95h. The endpoint probe respects its own cache.** `turn()` called
  `pick_endpoint(cache_ok=False)`, forcing a probe. But `turn()` runs once per
  agent ITERATION, not once per user message, so a 10-round tool turn paid ten
  extra probe round trips — invisible on localhost, real over a tailnet. It now
  honours the 10s TTL: a human turnaround exceeds it, so failover between
  messages is unchanged, and a connection failure still expires the cache
  explicitly (`_working_url_at = 0.0`). `_probe` also reuses the endpoint's
  pooled client instead of building a fresh one (and so a fresh TCP+TLS
  handshake) per probe.
- **R95i. The status bar never blocks on a socket.** `context_stats()` is
  called from the TUI's `status()` — the UI event-loop thread, every render.
  For the `local` model it ran a live `/props` lookup behind an endpoint
  probe, so a backend that was down froze the entire app for ~6s each time
  the 120s cache expired. (`live_context_limit` already carried a comment
  about this exact class of freeze; only the remote half had been fixed.)
  The cache is now served immediately and refreshed on a daemon thread:
  - `Provider.static_context_limit()` is the new offline answer (config /
    `remote_context_limits.json`, no network), served until the first live
    value lands. Stale beats blocking.
  - A failed refresh caches the static fallback, so a down backend backs off
    for the TTL instead of spawning a probe thread per frame; `_limit_pending`
    keeps a burst of renders to one in-flight probe.
  - The 120s TTL itself is unchanged — LlamaDesk can reload the same model at
    a different ctx, so a live `n_ctx` must not be cached forever. Only the
    *waiting* moved off the render path.
- **R95j. A live think row invalidates once per second, not once per render.**
  Its header carries a running clock, so the transcript cache was dropped
  outright while one existed — rebuilding the whole scrollback on the 0.5s
  ticker AND on every keystroke, mouse move and status invalidate, for a
  clock that changes once a second. The cache key is now the displayed whole
  second, so it rebuilds exactly when the display would differ.

### Deep-dive batch 5: the render path and per-keystroke I/O (2026-07-22, R96a–m)
A measured performance pass over every hot path — each finding was
benchmarked against the real code before and after, not estimated. The theme:
work that is proportional to the whole session (or to the whole file, or to
the whole log) sitting on a path that runs per frame, per keystroke, or per
tool result. One candidate fix was **rejected by measurement** and is
recorded with the others (R96i).

- **R96a. The `/command` completer never touches the filesystem per
  keystroke.** `SlashCompleter.get_completions` called `skills.discover()`
  (a directory walk) and then `skills._blurb()` — which `read_text()`'d the
  ENTIRE skill file and split every line — for every installed skill. The
  TUI wires the completer with `complete_while_typing=True`, and
  prompt_toolkit's default `get_completions_async` just iterates
  `get_completions` inline, so all of it ran **on the event-loop thread**:
  blocking filesystem I/O directly inside keystroke latency, and that was
  the warm-page-cache case. Measured at 20 skills of ~16KB: **2.05ms →
  0.089ms per keystroke (23×)**.
  - `_blurb` now reads three bounded `readline(512)`s instead of the whole
    file — it only ever inspected the first three lines.
  - The listing is cached in the completer against `skills.dir_stamp()` —
    `(path, mtime_ns)` per skills dir. A dir's mtime moves when a skill is
    added or removed, which is exactly what `discover()`'s answer depends
    on, so this stays correct for a skill dropped in mid-session (tested)
    while costing two `stat()` calls. Known, accepted limit: editing an
    existing skill's blurb line in place doesn't move the dir mtime, so that
    one string can lag until restart; which skills *exist* is always current.
  - `skills.discover()` / `skills.run()` / `/skills` are unchanged and still
    read live — only the completer caches.
- **R96b. The chat transcript renders in time independent of session
  length.** R95j fixed *when* the transcript cache was dropped; this fixes
  *how much* is rebuilt when it is. The per-entry parse cache (`_cache[i]`)
  was already right, but the FLATTENED fragment list was thrown away on
  every append — so each frame re-concatenated every fragment in the
  session. Measured on realistic ANSI-coloured scrollback: **7.0ms/frame at
  1MB and 33.8ms/frame at 4MB, now a flat ~1.5ms at both** — the 4MB case
  was capping the app at ~29fps before prompt_toolkit rendered anything, and
  it got worse for as long as the session ran.
  - Appends always land on the LAST entry, so `_dirty(i)` now records the
    LOWEST changed index (`_dirty_from`) and `_rebuild_locked()` re-flattens
    only from there. `_offsets[i]` — (fragment index, line count) at the
    point entry i begins — makes truncating to any dirty index a `del` on
    the tail instead of a full re-concatenation.
  - This also covers the non-tail case properly: expanding a collapsed think
    block re-flattens from that row on, not from zero.
  - A live think row is no longer force-re-parsed every frame (the old
    `cached = None` did that regardless of R95j's clock key). The clock key
    now marks just the live rows dirty when the displayed second moves,
    which is what R95j intended.
  - `_text_cache` is mutated in place. Safe because every reader
    (`_render_fragments`, `_sel_text`) is on the UI thread — the worker only
    ever marks entries dirty, never flattens.
  - The regression test asserts **complexity, not output**: it counts
    per-entry cache reads across 100 appends. The old code scored exactly
    5050 (n(n+1)/2); the bound is 3n. A second test asserts the incremental
    result is byte-identical to a naive full rebuild, including after a
    non-tail entry changes.
- **R96c. Drag-select stops rebuilding the whole transcript per mouse-move.**
  `_overlay()` re-styles the dragged range in reverse video and runs on
  every frame while a selection is live or frozen — a drag fires
  `app.invalidate()` on every mouse-move. It early-out for fragments fully
  outside the selection, but still did it by **appending each one to a new
  list**, so it was O(total fragments) in both time and allocation.
  Measured at 102k fragments: **21ms/frame → identical output, ~18×
  faster** (constant-factor, not complexity — a fragment's position is only
  knowable from everything before it, so the walk to the first crossed
  fragment stays linear).
  - Untouched fragments now come from **list slices**
    (`frags[:i] + mid + frags[j:]`) instead of a Python-level append loop —
    a slice is a C-level pointer copy. Only the fragments the selection
    actually crosses go through the per-character re-split.
  - The regression test compares directly against a copy of the pre-fix
    implementation kept in the test file (`_overlay_naive`) rather than an
    arbitrary threshold, so it measures the real claim (meaningfully
    faster, same output) instead of a guessed constant.
- **R96d. `secrets.redact()` is linear, not quadratic.** It rebuilt
  `text = text[:m.start] + "<secret>" + text[m.end:]` per match,
  right-to-left so earlier indices stayed valid — but every substitution
  copies the ENTIRE string, so redacting a match-dense blob (a big `.env`,
  a token-heavy log — exactly the case R58 exists for) was
  O(matches × len(text)). Measured on 57KB with 1500 matches: **7.6ms →
  0.2ms (38×)**.
  - Now a single left-to-right pass: accumulate the untouched
    between-matches slices plus `"<secret>"` into a list, `"".join()` once.
    Same "earlier spans stay valid" property (every slice is read before
    any substitution happens), linear instead of quadratic.
  - `redact()` no longer assumes its caller passed matches in document
    order — it sorts internally, same as before, but a test now covers
    calling it with matches in reverse/scrambled order directly (a caller
    may reasonably do this after filtering an allowlist).
  - The regression test compares wall-clock scaling at 4× the matches/text:
    quadratic old code scored ~18× the time; the bound is 8×.
- **R96e. `/cost` stops parsing every log line to find the few it wants.**
  `Session.iter_records()` now takes an optional `events` set; a line whose
  substring `'"event": "<name>"'` doesn't appear for any wanted event is
  skipped WITHOUT calling `json.loads` on it. `usage_by_model` (what `/cost`
  and `/cost all` read) only wants `assistant` records, but `tool` records
  dominate a session's log — one per tool result, each carrying up to 4KB of
  output (`Engine.send`'s `output=o[:4000]`) — so parsing every line just to
  discard most of them was most of the cost. Measured on an 8MB log:
  **18ms → 6.9ms**.
  - The substring check can only false-POSITIVE (a tool result whose output
    happens to contain the literal marker text still gets parsed and then
    correctly rejected by the real `event` check that follows) — never a
    false negative, since every record is written by the same `log()` via
    plain `json.dumps` defaults, so the marker's exact quoting/spacing is
    guaranteed.
  - `usage_all_sessions()` inherits the fix for free — it calls
    `usage_by_model` per session log, so this is also what makes `/cost all`
    (which reads every session ever logged, R20 never deletes them) scale
    better with total history.
  - `list_sessions()` was NOT touched: it already breaks at the first
    matching record, so it's bounded by "how far into one file the first
    real user message is," not by the log's total size — there was nothing
    to fix there.
  - One test asserts `json.loads` is called exactly once while filtering 3
    lines to 1 match (a monkeypatched counting wrapper around `json.loads`
    itself, not an internal hook); another confirms a tool-output string
    containing the literal marker text doesn't produce a false record.
- **R96f. The TUI stops paying for a per-turn thread it never needed.**
  `ui._run_turn` wraps `engine.send()` in its own thread purely so the MAIN
  thread can catch `KeyboardInterrupt` (R17) while `input()` blocks. In the
  TUI that handler was unreachable: `SIGINT` is delivered only to the
  process's main thread, and `_run_turn` was being called from the TUI's
  `_worker` thread, not it; prompt_toolkit also runs the terminal in raw
  mode, so `^C` never becomes a signal there at all — TUI cancellation is
  entirely separate (`fe.cancel_event.set()` via the Esc-Esc menu). So every
  TUI turn paid for an extra thread plus a 10Hz join-poll for the whole
  duration of the turn, for a mechanism that only ever fires in the classic
  REPL.
  - `_run_turn`'s body split into `_send_turn` (clear cancel, begin/send/end,
    catch-and-print an error) and a thin thread+`KeyboardInterrupt` wrapper
    around it. The TUI's `_worker` now calls `_send_turn` directly for a
    plain turn, and `_run_bootstrap(..., sync=True)` for the bootstrap
    prompt — both already run on `_worker`, which is not the main thread, so
    the wrapper bought nothing there either. The classic REPL's call sites
    are unchanged: `_run_turn` (still thread-wrapped) and
    `_run_bootstrap(sync=False)` (the default).
  - This also collapses "which thread is a mid-turn key prompt on" from
    four levels deep (UI → worker → per-turn thread → provider) to three —
    one less thread identity a session builder has to reason about.
  - Three tests: `_send_turn` and `_run_bootstrap(sync=True)` each assert
    `engine.send()` runs on the CALLING thread (a fake engine records
    `threading.current_thread()`); a third drives `Tui._worker` end-to-end
    and asserts it reaches `ui._send_turn`, never `ui._run_turn`, guarding
    against a regression sliding back to the thread-wrapped call.
- **R96g. `secrets.scan()`'s shape pass skips patterns that can't possibly
  match.** The 10-pattern shape pass ran `finditer` for every pattern over
  the WHOLE tool result, even though 8 of the 10 require a specific literal
  substring (`AKIA`/`ASIA`, a `gh*_` prefix, `xox*-`, `_live_`, `sk-`,
  `Bearer`, `-----BEGIN`) that ordinary text almost never contains. Measured
  on 60KB of ordinary source (this runs on every tool result, on the worker
  thread, whenever `runtime.redact_secrets` is on — the default): **10.84ms
  → 7.10ms**.
  - `_LITERAL_GUARD` maps each pattern name to a tuple of literals (or
    `None` for the two patterns with no fixed prefix — `GUID/UUID`'s is a
    dash-separated hex shape, `Env credential`'s is five different
    variable-name shapes, both always scanned) — if NONE of a pattern's
    literals appear anywhere in the text, `finditer` is skipped entirely.
    `in` on a plain `str` is a C-level substring search, far cheaper than
    even a fast regex engine over the same text.
  - **A guard only needs to be a superset of what its regex requires** —
    correctness means "every string the regex can match contains at least
    one guard literal," never the reverse. A looser guard (e.g. plain `"gh"`
    instead of the five real prefixes) would still be correct, just filter
    less; there's no failure mode from being imprecise, only from being too
    narrow.
  - A parametrized test checks this invariant directly against real matching
    samples for every guarded pattern, so a future edit to `PATTERNS` that
    adds a new prefix shape without updating its guard fails immediately —
    the exact mistake that would turn this into a silent false negative
    (missing a real secret) rather than a mere missed optimization.
  - **Rejected during this same pass**: collapsing the 10 patterns into one
    alternation regex (one `finditer` call instead of ten) measured
    **slower** — 13.99ms vs 9.56ms unguarded — because Python's `re` doesn't
    optimize large alternations and loses each pattern's own literal
    prefilter that the engine could otherwise use internally. Not applied.
- **R96h. `approve.is_allowed()` stops re-tokenizing the same allowlist
  rule on every check.** `_norm_command` (`shlex.split` + `~` expansion) ran
  once per RULE per check — an allowlist with 200 "always allow" entries
  re-tokenized all 200 rule strings on every single tool call in a turn,
  even though the rules themselves only change when the user adds one.
  Measured: **20 rules: 147.5µs → 4.5µs (33×); 200 rules: 1453.4µs → 41.6µs
  (35×)** per check (warmed cache; a cold call still pays one real
  tokenization, same as before).
  - `_norm_command` is now `functools.lru_cache(maxsize=512)`-wrapped and
    returns a `tuple` instead of a `list` (hashable, so a shared cached
    result can't be corrupted by one caller mutating it — every existing
    caller only ever compares/slices/indexes it, which works identically on
    a tuple). 512 is comfortably above any real allowlist plus a session's
    distinct commands; a miss just re-tokenizes, so eviction costs nothing
    beyond the one-time work this fix removes.
  - The remaining per-check cost is the O(rules) scan itself — inherent to
    the linear-match design, not this fix's target — so this closes the
    tokenization overhead, not the algorithm's shape.
  - Three tests: a monkeypatched counting wrapper around `shlex.split`
    asserts 50 identical calls tokenize once; a correctness test confirms
    the cache is transparent (two different strings still get their own
    right answer); a third runs `is_allowed` 20 times over a 50-rule
    allowlist and asserts exactly 51 real tokenizations (50 rules + the
    incoming signature), not 50×20+20.
- **R96i. `_limit_pending`'s check-then-add is now atomic.**
  `Engine._context_limit_nonblocking` (R95i) tracks which cache keys already
  have a refresh probe in flight so a burst of renders spawns one probe, not
  one per frame — but `if key not in pending: pending.add(key)` is two
  operations, and the code comment claimed this was "atomic under the GIL,
  no lock needed," which is true of `.add()`/`.discard()` alone but not of
  the `if` around them. Two near-simultaneous callers (a TUI render racing
  the classic footer, say) could both observe "not pending" before either
  added the key, spawning two probe threads for one key.
  - `Engine.__init__` now creates `self._limit_pending_lock`
    (`threading.Lock`), and `_context_limit_nonblocking` wraps only the
    check-then-add in it — never the network call itself, which stays on
    the background `_refresh` thread, unguarded. The lock is never held
    across anything that could block, so it can't turn into a stall on the
    UI thread (the one invariant this whole code path exists to protect).
  - The consequence was bounded (one wasted probe thread, never
    corruption), so this is a correctness/cleanliness fix, not a
    user-visible latency one.
  - The regression test needed care: a plain concurrent-threads test passed
    even on the UNFIXED code across 8 runs, because CPython's GIL makes the
    real check-then-add race too narrow to hit by chance. The test
    deterministically forces the window open with a `set` subclass whose
    `__contains__` sleeps after reading, and the fake probe stays "in
    flight" for the test's duration so a probe that legitimately finishes
    and `discard()`s its key mid-test isn't mistaken for the bug.
- **R96j. `_client_for()` no longer leaks the losing side of a connection-
  pool race.** Two threads can race to build a client for the same
  not-yet-pooled endpoint (the worker mid-turn vs. the UI thread's `/props`
  status probe, both call `_client_for`) — `self._http.setdefault(base_url,
  new_client)` correctly makes both callers converge on whichever client won
  the race, but the LOSER's freshly built `httpx.Client` (a real connection
  pool — sockets, not just Python memory) became unreachable from anywhere
  except the local variable that built it, and was never `close()`d. Rare
  (only the first touch of an endpoint can race) but a genuine fd leak.
  - Renamed the local to `new_client` and compare it against what
    `setdefault` actually returned: `if client is not new_client:
    new_client.close()`. The winning client (whichever one is now shared) is
    never touched.
  - The regression test forces the race deterministically rather than hoping
    two real threads happen to interleave at the right instant: a patched
    `httpx.Client` constructor holds the FIRST call open on a barrier while a
    second call runs to completion and installs its own client first, so the
    first call's build is guaranteed to be the one `setdefault` discards.
    Asserts both callers converge on the same client, the discarded one gets
    `close()`d, and the shared one never does.
- **R96k. `resume_from`'s token estimate no longer copies the whole history
  first.** `tokens.estimate_tokens("".join(str(m.get("content", "")) for m
  in msgs))` built one big string spanning every restored message just to
  take `len(...) // 4` — a transient full-history-sized allocation on every
  resumed session, for no reason: a bare `"".join` adds no separator chars,
  so `sum(len(...))` is the identical number without ever materializing the
  joined string. Trivial in cost (this pass's smallest finding), included
  for completeness.
  - A pinned-value test (`(123 + 77 + 50) // 4`) guards the exact number
    against future drift — this fix is a pure refactor with identical
    output before and after, so unlike the other findings here there is no
    "fails without the fix" version of this test to write; the existing
    R90g gauge-restoration test already covered the behavior.
- **R96l. P6 (`append()`'s `+=` amplification into a `_MERGE_LIMIT`-sized
  entry) investigated, NOT applied — like R96g's union-regex, recorded here
  because it was tried and measured, not skipped.** The original audit
  suggested lowering `_MERGE_LIMIT` now that R96b makes the render cache
  tail-incremental (more, smaller entries no longer costs an O(session)
  rebuild per extra one). Measured against that premise:
  - **Shrinking `_MERGE_LIMIT`** (4096 → 256): append cost drops only ~7%
    (67.5ms → 62.5ms per 1MB streamed) — the original report's 4096-vs-65536
    comparison made the effect look larger than it is; the curve is flat
    below 4096. Meanwhile `_live_clock_key()` (scans every open think row
    per frame) and every other O(entries) path get proportionally SLOWER as
    entry count rises 16× (measured: 10.9µs → 158.4µs per call). Under a
    live think row — exactly when this matters, since that's what makes
    `_live_clock_key()` run every frame — this is a net loss, not a win.
  - **List-based accumulation** (buffer chunks in a list, join lazily
    instead of repeated `+=`) gets the real fix — no `+=` amplification at
    all, at the SAME entry granularity, so no downside on the O(entries)
    paths: **60.2ms per 1MB, ~20% better than today**, with no tradeoff.
    But it means an accumulating entry is no longer always a plain `str` —
    it touches all 6 places in `tui.py` that branch on
    `isinstance(item, str)` / `isinstance(item, dict)` to distinguish plain
    text from a think-row dict, in the same file R96b/R96c just reworked.
  - **Not applied.** The real-world magnitude here is sub-millisecond per
    typical LLM response either way (a few KB of streamed text, not
    megabytes) — genuinely the smallest-impact finding in this whole pass
    once measured precisely, and not worth the integration risk of a third
    consecutive change to this file's core render-cache invariants for that
    payoff. Revisit if a future profile shows streaming append actually
    costing something a user would notice.
- **R96m. `grep` bounds the PRODUCER, not just the final string — the one
  finding in this whole pass with a real failure mode (OOM), not just
  latency.** `subprocess.run(capture_output=True, ...)` buffered grep's
  COMPLETE stdout before the old `out[:MAX_READ_BYTES]` truncation ever ran.
  A broad pattern over a large tree (`grep -rn "e" ~`) can produce gigabytes
  within the 30s timeout — and the model, which picks its own search
  pattern and path, is exactly the actor most likely to issue an
  over-broad one.
  - `grep` now uses `subprocess.Popen` directly and reads stdout
    INCREMENTALLY via `select.select([proc.stdout], [], [], remaining)`,
    where `remaining` is recomputed from a wall-clock deadline every
    iteration — so a stall between chunks (not just total elapsed time) is
    still caught by the same loop, not a separate mechanism. The process is
    `kill()`ed the moment `MAX_READ_BYTES` worth of stdout has arrived,
    instead of being left to keep producing output that would only be
    discarded at the string-slicing step.
  - The whole process lifecycle (kill decision, draining stderr, closing
    both pipes, `wait()`) is now in a `finally` block — ANY exit from the
    read loop (normal EOF, truncation, timeout, or an unexpected exception)
    still reaps the child. Without this, an exception mid-loop would hit the
    function's outer `except Exception` and return before the process was
    ever waited on — the same zombie/orphan class of bug R95c's
    process-group kill exists to prevent for `run_command`, just via a
    different mechanism here (no shell, so no process GROUP to kill —
    grep itself is the only process, and `kill()` is enough).
  - Output truncation now carries an explicit
    `[output truncated at N chars — narrow the pattern/path]` notice, same
    spirit as `read_file`'s and `run_command`'s existing truncation
    markers. The old code silently sliced the string with no notice at all
    — a real, if minor, side-effect improvement this fix surfaced.
  - **Three tests, each needed care to actually discriminate old vs. new
    behavior** (the naive version of two of them passed on the OLD code
    too, since both old and new code produce the same truncated final
    string):
    - A truncation test against ~20MB of real matches with the cap set
      tiny, asserting the killed process's returncode is **negative**
      (`SIGKILL` → `-9` on POSIX) — the only way to prove the producer,
      not just the string, was actually bounded, since grep finishes this
      workload well inside 30s if left to run.
    - A timeout test that patches `select.select` to report "never ready"
      against a real (harmless, fast) grep process, asserting the timeout
      message fires and the process is still reaped.
    - A correctness test confirming an ordinary under-the-cap search still
      returns every match, complete and untruncated.

### R97. `apply_patch` — a real multi-hunk diff tool
`edit_file` needs one call per uniquely-anchored change; five small,
unrelated edits in one file meant five approvals, or a `write_file` of the
whole thing (loses granularity, riskier on a large file). `apply_patch`
takes one unified diff — the format every model has seen a million times as
`git diff`/`diff -u` output — and applies every hunk as ONE atomic change:
all hunks match and apply, or none do and the file is untouched.

- **New engine-side module `aurora/patch.py`** (no I/O, no UI imports):
  `parse(diff_text) -> list[Hunk]` and `apply(text, hunks) -> str`. A `Hunk`
  is `(old, new, header)` — `old`/`new` are the joined context+removed /
  context+added lines, `header` is the raw `@@ ... @@` line kept only for
  error messages.
- **Hunks are matched by CONTENT, never by the diff's own line numbers** —
  the same reason `edit_file` requires a unique anchor. A model's
  `@@ -l,s +l,s @@` numbers drift the moment any earlier hunk in the same
  patch has already changed the file, and a patch generated from a
  slightly-stale read is common; trusting them would misapply silently.
  Instead each hunk's context+removed block must match **exactly once** in
  the text as it stands after every earlier hunk in the same patch has
  already applied (hunks apply IN ORDER against the running result, same
  "old text must match exactly and uniquely" contract `edit_file` already
  has, extended to N hunks with an all-or-nothing outcome).
- **`--- `/`+++` file-header lines are read and discarded.** The tool's own
  `path` argument is the ONLY authority on which file gets written — a
  model-supplied header naming a different file must never redirect where
  the patch lands.
- **Three deliberate edge-case decisions, each backed by a test:**
  - A hunk with `old == new` (every line was context, no real change) is a
    silent no-op, not an error — no reason to fail a harmless hunk.
  - A hunk with **zero** context/removed lines (pure insertion, all `+`) is
    a **parse-time error**, not a silent misapplication: `text.count("")`
    matches everywhere, so `text.replace("", new, 1)` would insert at the
    very START of the file — almost never what's intended. The model is
    told to add at least one surrounding context line.
  - A bare blank line inside a hunk (a model that forgot the leading space
    marker for an empty source line — common) is treated as an empty
    CONTEXT line, not a parse error. `\ No newline at end of file` marker
    lines are skipped.
- **The approval preview shows the REAL computed result, never the raw
  submitted diff** — same principle as R95a ("the diff IS the change; a
  preview that under-reports is worse than none"). `approve._diff_preview`
  actually parses and applies the patch against the real file content and
  runs `difflib.unified_diff` on the result, exactly like `write_file`/
  `edit_file`'s previews already do. A patch that would FAIL to apply
  (context not found, ambiguous match) surfaces that failure AT the
  approval prompt, via `diff_preview()`'s existing outer exception guard —
  not only discovered after the human already said yes.
- **Approval gate + allowlist wiring**: added to `NEEDS_APPROVAL`, never to
  `PARALLEL_SAFE` (it mutates). `approve.load()`'s tool list
  (`run_command`/`write_file`/`edit_file`) is now `_TOOLS`, extended to
  include `apply_patch`, so "always allow" on a patched path works the same
  way it already does for `write_file`/`edit_file` — without this, the
  first "always allow" on an `apply_patch` result would `KeyError` inside
  `add_rule`, since `load()`'s `setdefault` never created that bucket.
- **Tests**: `tests/test_patch.py` covers `patch.py` in isolation (13
  cases: multi-hunk ordering, header-line stripping, no-newline markers,
  the blank-context-line accommodation, the no-op/pure-insertion/ambiguous/
  not-found edge cases). `tests/test_core.py` covers the tool + approval
  integration (11 cases): the real file write, all-or-nothing across
  hunks, the true-no-op path never touching disk, registration in
  `RUNNERS`/`SPEC`/`NEEDS_APPROVAL`, the allowlist round-trip, and — the
  two tests that matter most — the preview showing the actual diff for a
  good patch and surfacing the real error for a bad one.

### R98. Up/down arrow only recall history from an EMPTY draft
The TUI's input line binds ↑/↓ to move within a multi-line draft OR recall
`/model`-style command history, depending on where the cursor is — but the
old test was `cursor_position_row == 0` (for ↑) / `== line_count - 1` (for
↓) **alone**. A multi-line draft (a pasted error log, a longer message
being composed) that happened to put the cursor back on the first/last row
after an earlier cursor move meant the very next ↑/↓ jumped straight into
command history, discarding the user's place in their own in-progress
draft — with no warning and no way to tell it was about to happen.

- Both bindings now gate on `not buf.text` (the draft is completely empty)
  instead of the cursor's row. A non-empty draft always moves the cursor,
  regardless of which row it's currently on; only a genuinely empty prompt
  recalls history, preserving the classic REPL muscle-memory (empty prompt,
  press ↑, get the last submitted line).
- This does mean the old "press ↑ repeatedly to cycle further back through
  history" pattern only continues while the recalled text is then cleared
  back to empty between presses — once a recalled entry sits in the draft,
  a further ↑ now moves the cursor within it rather than loading the entry
  before it. This is the intentional trade the fix makes: an in-progress
  multi-line draft must never be silently clobbered by history recall, and
  that guarantee is only possible by treating "the draft has ANY text" as
  the line, including text that arrived via a previous recall.
- Tests (`tests/test_tui.py`) invoke the registered ↑/↓ key-binding handlers
  directly (no real terminal/event loop needed, since neither handler reads
  anything off the key-press event itself) via a small `_press()` helper
  that finds the binding by its registered key. The history-recall path is
  tested by spying on `history_backward`/`history_forward` rather than
  exercising prompt_toolkit's own history-loading machinery, which needs a
  running asyncio event loop (`Buffer.load_history_if_not_yet_loaded`
  schedules a background task via `get_app()`) that a headless test
  fixture doesn't have — that machinery is prompt_toolkit's own concern,
  not what this fix changes. The plain cursor-movement path (no history
  involved) is verified against the buffer's real cursor position instead.

### R99. A 429 gets its own backoff-and-retry, distinct from connection retry
`turn()` already retries transient connection failures (a stale pooled
keep-alive reset) with a flat `0.3 * (attempt+1)` delay. A 429 rate limit —
routinely hit on an OpenRouter free-tier model shared across everyone
without their own key on that upstream — instead failed the WHOLE turn
immediately: `agent.py`'s notify was friendly ("try again shortly, add
your own key, or /model to switch"), but it was still a dead end, not an
actual retry. A shared quota clears in a few seconds; that's exactly the
kind of wait a program can do for the user instead of asking them to.

- **`_RateLimited`** (`openai_compat.py`), a small internal-only exception
  — never raised past `turn()` — lets the retry logic distinguish a 429
  from a generic 4xx/5xx without parsing message text. Raised at the same
  status-check point that already classifies `a >= 400`, before the
  generic `ProviderError`/`MalformedToolCall` branches.
- **`_RATE_LIMIT_BACKOFF = (1.0, 3.0)`** — its own schedule, deliberately
  NOT the connection-retry's flat delay: a stale connection resets
  instantly on retry, a shared quota needs real seconds. Two backoff waits
  across the existing 3-attempt budget (`_ATTEMPTS`, unchanged).
  Exhausting all three still raises `ProviderError` with a message
  containing both "429" and "rate limited" — `agent.py`'s existing
  message-based classifier (`"429" in msg or ("rate" in msg.lower() and
  "limit" in msg.lower())`) needed no changes to keep recognizing it.
- The `except httpx.HTTPError as e:` clause widened to
  `except (httpx.HTTPError, _RateLimited) as e:` — the partial-text
  "keep what streamed so far" branch above the retry logic stays generic
  over exception type (a 429 can never actually reach it with
  `result.text` set, since it's always the very first event of the
  stream, but the check isn't gated on exception type regardless).
- **Four tests**: a 429-then-succeed case (retried exactly as many times
  as needed, backoff slept in the right order); an always-429 case
  (exhausts all 3 attempts, raises with "429" in the message); a check
  that the sleep durations used are the 429 schedule, not the connection-
  retry's `0.3 * attempt` one; and a check that `agent.py`'s existing
  classifier still recognizes the exhausted-retry message and produces
  the friendly shared-quota notice, not a raw error dump.

### R100. `wait_until` — poll a shell command until it succeeds
"Wait for the dev server to start listening", "wait until the build
produces this file" today force the model to guess a single `sleep N`
duration inside `run_command` and hope it was long enough — too short and
the next step fails spuriously, too long and the turn wastes time waiting
past when the condition was already true. `wait_until` reuses the "poll
until true or give up" shape `llamadesk.LlamaDesk.wait_ready` already uses
internally for a model load, exposed as a general tool.

- **Refactored `run_command`** to extract `_run_command_once(command,
  workdir) -> (output, returncode | None)` — the exact same process-group-
  safe execution (R95c) `run_command` already had, unchanged behavior and
  output text, just factored so `wait_until` can check the REAL exit code
  per attempt. Parsing `run_command`'s own display text (`"[exit N]"`) back
  out to decide whether to keep polling would have been fragile — exactly
  the kind of thing that silently breaks the moment that text format
  changes for an unrelated reason.
- `wait_until(command, cwd="", interval=2.0, timeout=60.0)`: re-runs
  `_run_command_once` every `interval` seconds until it exits 0 or
  `timeout` elapses (clamped to 300s max — a wait tool must not become an
  unbounded background job the agent loop can't see, same ceiling spirit
  as `COMMAND_TIMEOUT`'s own default). A per-attempt timeout (the command
  itself hanging) is reported as "timed out mid-command", distinct from an
  ordinary nonzero exit.
- **Approval is asked ONCE for the whole call**, not per poll — `agent.py`'s
  gate wraps the tool call itself; `wait_until` calls `_run_command_once`
  directly inside its loop, bypassing the gate a second time (which already
  ran). Re-approving every 2-second poll would make the tool unusable.
- **Its own allowlist bucket, separate from `run_command`'s.** `approve.py`'s
  `run_command`-specific branches (`_signature`/`is_allowed`/`add_rule`)
  became `_COMMAND_TOOLS = ("run_command", "wait_until")` so both get
  identical token-prefix matching — but an "always allow" made for one
  never silently covers the other (own bucket in `allowlist.yaml`,
  `data[tool]` rather than a hardcoded `data["run_command"]`). A plain
  one-shot command and "keep re-running this until it succeeds" are
  different enough risk shapes that conflating their rules would surprise
  someone who only meant to approve one of them.
- **Tests**: success-on-first-check, a condition that starts false and
  becomes true mid-poll, timeout-and-give-up (exit code shown), the
  per-attempt-timeout-vs-nonzero-exit distinction, `cwd` handling, the
  300s clamp (exercised with a fake fast-forwarding `time.monotonic` rather
  than actually waiting real minutes), registration in
  `RUNNERS`/`SPEC`/`NEEDS_APPROVAL`, and the allowlist-bucket separation
  from `run_command`.

### R101. `/commit` — stage, draft, review, commit
Every coding session that touches code ends the same way — `git add`,
draft a message, `git commit` — entirely by hand, with no model
assistance, no matter how much of the actual coding Aurora just did.
`/commit [message]` removes that repetitive manual step.

- **New engine-side module `aurora/gitcommit.py`** — plain git-shelling
  functions (`is_repo`, `staged_diff`, `unstaged_summary`, `stage_all`,
  `recent_log`, `commit`) plus `draft_message(engine, diff, recent)`, a
  one-off model completion in the same shape as `memory._draft()` (a plain
  user-turn request outside the normal conversation, not a tool call).
  Operates on the **real project `.git`** — a completely different target
  from `rewind.py`'s shadow repo (a parallel, separate history under
  `AURORA_HOME` used purely for undo). Neither module imports the other;
  conflating them would mean `/commit` accidentally committing into the
  wrong repository.
- **`_commit_cmd`** (`ui.py`, shared by both front ends via
  `_handle_command`) orchestrates entirely with EXISTING primitives —
  `select()`, `confirm()`, `input()`, `colour_diff()` — no new `Frontend`
  protocol method needed, since staging/diffing/committing are plain
  synchronous git calls, not a new kind of human interaction the engine
  side has to request.
- **Never silently stages anything.** If nothing is staged, `/commit`
  shows exactly what `git status --porcelain` reports (what `git add -A`
  WOULD include) and asks first (`confirm(..., default_yes=False)`) — an
  auto-stage-and-commit that swept up an unrelated stray file would be
  exactly the kind of surprise Aurora's approval gate exists to prevent
  everywhere else.
- **The message**: an explicit `/commit <message>` argument skips drafting
  entirely; otherwise the model drafts one from the staged diff, shown
  alongside the last 5 commit subjects (`recent_log`) as a style reference
  so the draft matches the repo's own commit voice rather than a generic
  one.
- **Review before committing, every time** — the diff (`colour_diff`,
  capped at 4000 chars with a truncation note, same shape as the approval
  gate's own diff preview) and the drafted/given message are both shown,
  then a menu: **Yes, commit** / **Edit the message** (free-text, loops
  back to the same review) / **Cancel** (leaves the change staged, not
  discarded). An empty message — an empty draft, or an empty edit — is
  refused rather than silently committed; the loop asks again.
- **Tests**: `tests/test_gitcommit.py` covers the module against a real
  throwaway git repo (10 cases: repo detection, staged-vs-unstaged diff
  content, `stage_all`, `recent_log`, a real commit landing with the right
  subject, committing nothing staged failing cleanly, and `draft_message`
  against a fake provider). `tests/test_core.py` covers the command's
  orchestration (10 cases): not-a-repo, clean-tree, an explicit message
  skipping the draft call entirely, the auto-draft path, the
  nothing-staged confirm (both accept and decline), declining at the final
  review (change stays staged, nothing committed), the edit-then-confirm
  loop, and the empty-message refusal.

### R102. The status bar shows what's actually running, not just "thinking…"
`on_request()` sets the status bar's phase word to "thinking" once per LLM
request — but nothing re-labels it for the window between the model
returning `tool_calls` and the next request, which is exactly when a
`run_command`/`wait_until` call is executing a real subprocess. The status
bar kept saying "thinking… Ns" for the WHOLE duration of a long build or
test run, which is actively misleading (it isn't thinking, it's waiting on
your shell).

- **`Tui.set_running_note(note)`** — a short label shown INSTEAD of the
  phase word while set; empty reverts to the normal phase text. Wired from
  a new `TuiFrontend.on_tool_start`/`on_tool_result` override: for
  `run_command`/`wait_until` specifically (`_RUNNING_COMMAND_TOOLS`,
  mapping tool name → label: "running" / "waiting on"), the command text is
  collapsed to one line, truncated to 60 chars, and shown as
  `"running: npm test"` / `"waiting on: curl -sf localhost:3000"`. Every
  other tool (`read_file`, `grep`, `edit_file`, …) leaves the note alone —
  those are near-instant, and naming every tool call would be status-bar
  churn, not signal.
- **Deliberately NOT every tool.** The bar exists to answer "is this taking
  a while, and if so, on what" — reads/greps/edits already show up in the
  chat pane the instant they start and finish; only the two tools that can
  run long AND opaquely get a persistent label.
- **A real edge case, closed with a safety net, not a special case**: if a
  secret challenge on a command's OUTPUT returns "stop" mid-loop,
  `agent.run_turn` returns without ever calling `on_tool_result` for that
  specific call — so the note would never clear on its own. `begin_turn()`
  and `end_turn()` (which always fire exactly once per turn, regardless of
  how it ended) both clear it unconditionally, rather than trying to catch
  every internal early-return path individually.
- **Tests** (`tests/test_tui.py`): set-on-start/clear-on-result for both
  command tools with their distinct labels, non-command tools left
  untouched, truncation, embedded-newline collapsing, and the
  begin_turn/end_turn safety-net clear (exercised directly, not by trying
  to reproduce the secret-challenge-stop path — the safety net's whole
  point is not needing to enumerate every path that skips
  `on_tool_result`).

### R103. "Explain" at the approval gate
The approval challenge (y/n/a/s/c) had no way to ask the model what a
pending call actually does before deciding — a user unsure about an
unfamiliar command had to either approve blind or deny and ask separately.
A new `e` option: describe what this will do, then re-ask the same
challenge.

- **`agent._explain_tool_call(provider, model, name, args)`** — a one-off,
  tool-free model completion (`provider.turn(model, [{"role": "user", ...}],
  "", None, ...)`), same "side completion, never touches history" shape as
  `memory._draft()`/`gitcommit.draft_message()`. Deliberately asks the SAME
  provider/model already active for the turn — an explanation from a
  different model would be answering for a decision it didn't make.
- **`e` is never a terminal answer.** `run_turn`'s approval-gate call site
  wraps `cb.approve(...)` in a loop: on `"e"`, it calls
  `_explain_tool_call`, feeds the result through `cb.notify(...)`, and
  re-asks the IDENTICAL challenge (same `diff`, same args) — only a
  non-`"e"` answer breaks the loop and falls through to the existing
  y/a/n/s/c handling. Nothing caps how many times a user can pick "explain"
  in a row before deciding.
- **`ui.TerminalFrontend.approve()`** just offers and returns the new
  option like any other key — the loop and the provider call both live in
  `agent.py`, which already has `provider`/`model` in scope; adding either
  to the `Frontend` protocol would mean the frontend calling a provider
  directly, which breaks the engine/UI boundary `ARCHITECTURE.md` §1
  enforces. `frontend.py`'s `approve()` docstring — the authoritative
  contract — documents the new key and its "loops, never terminal" nature.
- **Tests**: the full loop (explain once then approve, explain twice then
  deny — each counting the exact number of `provider.turn()` calls to
  prove the loop iterates the right number of times and no more),
  `_explain_tool_call`'s prompt construction (names the tool, embeds the
  JSON args), its provider-error path, its empty-reply placeholder, and
  `ui.approve()` offering and returning the raw `"e"` key.

### R104. The "Esc again to quit" status-bar hint is gone
User-requested removal, scoped narrowly: the double-Esc-to-quit GESTURE is
unchanged (first Esc arms it, second opens the Yes/No quit menu) — only the
line-2 reminder text shown during the 2s arm window is suppressed, and only
for the quit case specifically. The `cancel` (mid-turn) and `bash` (leave
bash mode) variants of the same shared gesture keep their hints — those
happen mid-work, where a heads-up is more likely to matter; an idle empty
prompt arming to quit is judged unambiguous enough without one.

- One-line change: the render guard gained `and self._esc_armed != "exit"`,
  and the `"exit"` case fell out of the `what` lookup dict since it's no
  longer reachable. When suppressed, the code falls through to the
  existing `elif self._exit_confirm: pass` branch (originally meant for
  "the window already expired" — now also correctly covers "the window is
  active but this variant never shows text" without needing a new branch).
- The existing test asserting the OLD "Esc again to quit" text was
  rewritten to assert its ABSENCE, while still confirming the underlying
  arm state (`_exit_confirm`/`_esc_armed`) still sets correctly — the
  gesture itself must keep working, only the text goes away.

### R105. The status-bar mode label is clickable — toggles prompt/bash mode
The line-1 `prompt mode` / `bash mode` text was plain, unclickable status
text (unlike every other status-bar segment, which is already a click
target — model name, session id, copy actions, agentic report). Tapping it
now toggles mode the same way the existing line-2 `! bash` / `> prompt`
hint buttons do — it reuses `_enter_bash_mode_click()` /
`_leave_bash_mode_click()` directly rather than adding new handlers, so the
click-guard semantics and bash-mode-exit behavior (clears any typed shell
command) stay identical to the pre-existing hint-row toggle.

- `tests/test_tui.py`: `test_click_mode_label_enters_bash_mode` and
  `test_click_mode_label_leaves_bash_mode` assert the label's own fragment
  carries the right handler and that clicking it flips `_bash_mode` (and,
  leaving bash mode, clears the input buffer) exactly like the existing
  line-2 toggle tests.

### R106. Tab-completion in bash mode completes filesystem paths
Bash mode previously had no Tab-completion at all — the input's completer
was a single static `SlashCompleter` (fires only on a leading `/`), so
`cd Xxx<Tab>` did nothing. A new `_ModeCompleter` wraps the input's
completer and dispatches by `self._bash_mode`: prompt mode keeps
`SlashCompleter` unchanged; bash mode delegates to prompt_toolkit's
`PathCompleter`, narrowed to just the trailing whitespace-separated word
(PathCompleter treats its whole input as the path to complete, so the full
line `cd Xxx` has to be trimmed to `Xxx` first, mirroring how a real shell
only completes the token under the cursor). This only completes path
segments, not shell command names/builtins.

- `tests/test_tui.py`: `test_bash_mode_completes_paths_not_slash_commands`
  and `test_prompt_mode_still_completes_slash_commands` exercise the
  dispatcher directly against the real `t.input.completer`.

### R107. `cd` persists across bash-mode commands (bug fix)
Bash mode ran every `!` command as its own throwaway
`subprocess.run(..., shell=True)`, so `cd script` only changed the child
process's directory — the very next command landed back in the original
directory, silently. `cd` is now intercepted before it ever reaches
`subprocess.run`: `Tui._bash_cd_target()` recognizes a plain `cd`/`cd
<path>` (bailing out — returning `None` — on any chaining operator like
`&&`, `;`, `|` so those still fall through to a real subprocess, unhandled
rather than half-emulated), resolves it against a new `self._bash_cwd`
tracked on the `Tui` instance, and every other command now runs with
`cwd=self._bash_cwd` instead of inheriting the process's actual cwd. Tab
path-completion (R106) was updated to complete relative to `_bash_cwd` too,
so it stays consistent with whatever directory the user has actually `cd`'d
into.

- `tests/test_tui.py`: `test_bash_mode_cd_persists_across_commands` (a `cd`
  then a later command both observe the new directory),
  `test_bash_mode_cd_to_missing_dir_reports_error` (cwd unchanged, error
  reported), `test_bash_cd_target_parsing` (plain `cd` forms recognized,
  chained/multi-arg forms correctly left to fall through).

### R108. `install.sh` offers to set up an API key on first install
Previously `install.sh` finished silently even with zero API keys
configured — the user only found out on first run, or after digging
through the README. It now checks `aurora key status` right after the venv
install and PATH symlink: if every configured key (`api_key_env`/
`token_env` names from `config.yaml`) reports `not set`, it prints the
list, asks "Set one up now? [y/N]", and on yes — picking automatically if
there's exactly one configured key, or via a `select` menu if there's more
than one — runs `aurora key set <ENV_VAR>` inline, handing off to that
command's existing interactive flow (hidden input, then keyring or
encrypted-file passphrase, whichever `keystore.store_key` picks). If at
least one key is already set, or none are configured in `config.yaml` at
all, the prompt is skipped entirely — this is a first-run nudge, not a
gate. README's install section now also states this up front and marks
the `curl | bash` one-liner (which already avoided any manual `cd` — it
clones the repo and execs `install.sh` itself) as the recommended path,
since the manual clone+cd+`./install.sh` instructions were reportedly what
the user actually followed.

- No new automated test: `install.sh` is a bash script with no existing
  test harness in this repo, and the added logic (a `key status` check, a
  `read -rp` gate, and a `select` menu) was instead verified by hand —
  dry-running the script end to end with `HOME` pointed at a scratch
  directory (so it wouldn't touch the real `~/.local/bin/aurora` symlink or
  `~/.aurora-path`) across all four cases: no keys configured, one key
  configured, multiple keys configured, and all keys already set.

### R109. "copy last" also copies bash-mode command output
"copy last" (the status-bar button and `/copy-last`) only ever read the
last LLM turn's raw response via `ui._raw_last_response_text` — bash mode's
`!<cmd>` output was captured and printed but never stored anywhere, so a
`!ls` followed by "copy last" gave "nothing to copy yet" even though output
was right there on screen. `Tui` now tracks `_last_bash_output` +
`_last_bash_at` (set whenever a real bash command runs — not on a bare
`cd`, which has no meaningful output) alongside a new `_last_llm_at`
(set when an LLM turn finishes). A new `ui._last_copyable_text(engine, fe)`
picks whichever happened more recently and returns `(text, label)` — label
is `"raw response"` or `"command output"`, used in both the status-bar
notice and `/copy-last`'s printed confirmation. Frontends without TUI bash
state (the classic REPL, where `!<cmd>` isn't captured at all) fall back to
LLM-only behavior unchanged, via `getattr(fe, "_tui", None)`.

- `tests/test_tui.py`: `test_copy_last_with_nothing_yet`,
  `test_copy_last_copies_llm_response_when_no_bash_output`,
  `test_copy_last_copies_bash_output_when_no_llm_response`,
  `test_copy_last_prefers_whichever_is_more_recent` (asserts the recency
  comparison picks correctly in both directions, not just "bash always
  wins").

### R110. `/nano <file>` — a built-in text editor, plus click-to-open from bash output
A small self-contained editor (open/edit/save/close only — no syntax
highlighting, no search), NOT a shell-out to real `nano`: bash mode has no
PTY (see R107), so an actual interactive terminal program can't run through
it regardless. `/nano <file>` works standalone (no `.agentic_context`
dependency), restricted to `.txt/.md/.json/.yml/.yaml/.xml/.sh`, ≤1MB, and
never auto-creates a missing file — it refuses with a short message
(`nano: unsupported file type`/`no such file`/`file too large`) and never
opens the editor in any of those cases.

Staying inside R53's 3-fixed-area rule (chat/input/status, nothing renders
across two), `/nano` follows the same "swap a `ConditionalContainer`"
precedent the (undocumented) help pane already set, rather than adding a
4th area or a `Float` popup:
- **Section 1** (chat) swaps to a new persistent `self._editor_area`
  (a real `TextArea`, not the read-only `FormattedTextControl` the chat
  pane normally uses) — plain typing/arrows/backspace/Enter all work
  normally; only save/close have no key binding.
- **Section 2** (input) collapses to a hidden single row, same mechanism
  already used while a `select()` menu owns the screen.
- **Section 3** (status bar) swaps to a "type 2" toolbar: `close` (clean)
  or `save`/`save and close` (dirty) first, then `page up`/`page down`
  always last (scrolling isn't gated on dirty, and per an explicit
  ordering request they come after close/save rather than before) —
  click-only, deliberately no shortcut keys for ANY of these, paging
  included. `close` always discards unconditionally, no confirm either
  way. `self._editor = None` (not tracked open) drives every one of these
  conditions; `_nano_dirty()` caches its buffer-vs-`original` compare,
  invalidated by the editor buffer's own `on_text_changed` and reset
  directly (no recompute needed) right after an open or a save — see the
  deep-dive section below for why
  a plain per-render compare wasn't good enough. `page up`/`page down`
  (`_nano_scroll`) reuse prompt_toolkit's own `scroll_page_up`/
  `scroll_page_down` (`key_binding.bindings.scroll`) rather than
  reimplementing page math — both only read `event.app`, so a minimal
  stand-in object with just that attribute is enough to call them from a
  mouse handler instead of a real key-press event.

**Click-to-open from bash-mode output**: typing `ls` and tapping a
listed filename (matching the same extension allowlist) opens it the same
way. Scoped deliberately to bash-mode output only, not LLM prose that
happens to mention a filename — `append_bash_output()` tags that chat
entry with `{"kind": "bash_output", ...}` (parallel to the existing
`"think"` entry kind), and only entries of that kind get a second
`_linkify_filenames` pass alongside the existing URL pass. The clicked
name resolves against the tracked `_bash_cwd` (R107), not the process's
real cwd. A click-time failure (file deleted since the `ls`, etc.) reuses
`open_nano`'s own validation and message — one code path either way.

**Bug found and fixed while wiring this up**: `_linkify_fragments` (the
existing bare-URL-click feature) was silently non-functional.
`ANSI(text).__pt_formatted_text__()` emits ONE FRAGMENT PER CHARACTER, so
a multi-character regex (`URL_RE`, and the new `_NANO_FILENAME_RE`) could
never match against any single-character fragment — URL-click in chat has
apparently never actually worked. Fixed with `_merge_char_runs()`, which
coalesces consecutive same-style fragments back into runs before either
linkify pass runs; both `_entry_fragments` call sites now merge first.

**More bugs found on a deep-dive pass over the first cut of this feature**
(none of these shipped/committed before being caught):

- **Editing was fundamentally broken.** Every REPL-muscle-memory global
  key binding — Enter, space, backspace, up/down, digits 1-9, `!`, `?`,
  Ctrl+J, Escape, PageUp/PageDown — reads or writes `self.input.buffer`
  (or scrolls the chat pane) UNCONDITIONALLY, with no check on what's
  actually focused. While the editor owned focus, a space silently
  vanished into the hidden, empty `self.input` instead of appearing in the
  file; arrow keys couldn't navigate the file at all (they moved a cursor
  in a buffer nobody could see); and worst, Enter could `_submit()`
  whatever had silently piled up in that hidden buffer as a genuine chat
  message the moment nano closed and focus returned. Fixed with a shared
  `_no_editor = Condition(lambda: self._editor is None)` added as
  `filter=` to every one of those bindings (and folded into the existing
  `_click_guard()` for `?`, which already served as a filter) — verified
  against a real running `Application` (not just unit-level state
  assertions) that this is the correct fix: a `filter=`-ineligible binding
  lets prompt_toolkit try the next match, which for a focused `TextArea` is
  its own default character/newline/cursor handling; an in-body early
  return does not — it still swallows the key.
- **`_NANO_FILENAME_RE` matched a filename's PREFIX**, not the whole
  token: `notes.txtbak` or `archive.txt.bak` linkified as `notes.txt`/
  `archive.txt` — nothing forced the match to consume what came after the
  extension. Clicking it opened a wrong or nonexistent file (or, worse,
  a genuinely different file that happened to share that prefix). Fixed
  with a trailing `(?!\S)`.
- **`open_nano()`/`_nano_save()` had no exception handling**, reachable
  directly from a UI-thread mouse handler (a filename click) with nothing
  above it to catch a fault — a `.txt` file that isn't valid UTF-8, or a
  save that hits a permissions/disk-full error, would propagate an
  uncaught exception out of a mouse-event callback. Now caught and
  reported the same way a validation refusal is (`nano: can't open/save
  …`); a failed save also leaves `dirty` alone (via `_nano_save()`
  returning whether it actually wrote) so `save and close` doesn't
  discard the buffer on a failed write.
- **Opening a second file while one was already open silently discarded
  unsaved edits** — `open_nano()` had no guard against re-entry. In
  practice this wasn't independently reachable (both entry points live in
  areas that are themselves hidden while `self._editor` is set), but it's
  cheap, cost-free insurance against any future second call path, so
  `open_nano()` now refuses with a message if `self._editor is not None`.
- **`self.app.layout.focus()` was called from whichever thread invoked
  `open_nano()`** — the worker thread for `/nano`, the UI thread for a
  filename click — with no thread-safety contract of its own (unlike
  `app.invalidate()`, which every existing worker-thread caller in this
  file already relies on). Now dispatched through
  `self.app.loop.call_soon_threadsafe(...)` when called off the UI thread,
  matching the one existing precedent for this (`/quit`'s `app.exit()`).
- **Performance**: `_nano_dirty()` did a full buffer-vs-`original` string
  compare on every status-bar render — every keystroke, every 0.5s ticker
  tick, every scroll — which for a near-1MB file is real, repeated work
  for a value that only actually changes once per edit. Now cached,
  invalidated by the editor buffer's own `on_text_changed` and reset
  directly (no recompute) right after an open or a save — same idea as
  `_live_clock_key`'s "only reparse when the displayed value could
  actually differ."

**Investigated and confirmed NOT a bug**: user-reported "page up/down
don't work well" once new lines are typed traced to `page up`/`page down`
moving the editor's CURSOR along with the view — `scroll_page_up`/
`scroll_page_down` reposition the cursor to keep it inside the newly
visible range (confirmed: cursor at line 10, click page down, cursor jumps
to line 34; typing right after lands at line 35, not where the user had
been editing). Tried the obvious fix (scroll the view, then restore the
cursor) — doesn't work: prompt_toolkit's `Window.do_scroll()`
(`layout/containers.py:2492-2497`) unconditionally re-forces the cursor
back into view on the very next render regardless of any
`get_vertical_scroll` override (confirmed: scroll snapped `34 → 10` on the
next render after restoring the cursor). A true view-only scroll (page
the viewport, leave the cursor exactly where the user left it) is not
achievable with a single prompt_toolkit `Window`+`Buffer` — the
cursor-follows-view coupling is load-bearing in how it renders; decoupling
it would mean rebuilding the editor as a read-only scrollable view (like
the chat pane's own wheel-scroll) that only becomes a real edit buffer on
a click inside the text — a materially bigger redesign. Asked the user:
confirmed to keep the current behavior — cursor moving to the new page on
Page Down/Up is standard across real editors (nano, vim, most GUI
editors); the fix, same as any of them, is to click into the text to
place the cursor before continuing to type after paging.

**A second, real bug on a follow-up report** ("the problem is to go up to
the top top" — repeated Page Up clicks not actually reaching the start of
a long file): `scroll_page_up`/`scroll_page_down` compute against
`Window.render_info`, which is only updated by an actual render pass —
`app.invalidate()` merely SCHEDULES one for whenever the event loop next
gets to it. Clicking faster than a redraw can keep up — exactly how
someone clicks "page up" repeatedly to reach the top of a long file
quickly — meant every click after the first computed against the SAME
stale `render_info` from the last real draw: each one only nudged the
cursor up by a single line instead of a full page, so reaching row 0 took
dozens of clicks instead of a handful. Confirmed both the bug (10 rapid
clicks over 200 lines only reached row 156, not row 0) and the fix (the
same 10 rapid clicks correctly reached row 0 in 5). Fixed by having
`_nano_scroll` call `self.app._redraw()` — a forced SYNCHRONOUS render,
documented "not thread safe — from other threads use invalidate()", safe
here since mouse-click handlers already run on the UI/event-loop thread —
right after each scroll, so even zero-delay repeated clicks each see fresh
`render_info`. The click handlers' own now-redundant `app.invalidate()`
calls were removed since `_redraw()` already renders.

- `tests/test_tui.py`: open (valid/bad-extension/missing/oversized),
  refusing a second open while one is already open, a decode-error open
  and a write-error save both reporting instead of raising, `save and
  close` refusing to discard on a failed save, the dirty cache's
  invalidate/reset points, the cross-thread focus dispatch (mocked loop +
  layout), the filename-regex prefix-matching regression, `_nano_dirty()`
  after an edit, `save`/`close`/`save and close` click handlers
  (write-and-stay-open, discard-unconditionally, write-then-close),
  toolbar fragments for clean vs. dirty, filename click resolving against
  `_bash_cwd` (including a sub-directory case mirroring R107's own
  regression test), filenames NOT linkified outside `bash_output` entries,
  `test_url_click_works_after_char_run_merge_fix` as the regression test
  for the linkify bug above, and
  `test_editing_keys_reach_the_editor_not_the_hidden_input` — a REAL
  `Application` driven with piped input (not just direct state
  assertions) proving space/Enter/digits/`!` land in the editor, not
  `self.input`, and that nothing gets submitted to the worker. Also:
  `page up`/`page down` present in both the clean and dirty toolbar,
  their click handlers calling through to prompt_toolkit's scroll
  functions (and NOT calling through when no editor is open), and
  `test_nano_page_down_then_up_scrolls_editor_window` — another
  real-`Application` test (scroll needs an actual render pass) confirming
  the editor's `Window.vertical_scroll` actually moves forward then back —
  and `test_nano_rapid_page_up_clicks_reach_the_top`, which fires 10
  page-up clicks with no `await`/yield between them (reproducing the
  no-redraw-between-clicks bug above) and asserts the cursor actually
  reaches row 0; verified this test fails without `_redraw()` (row 156,
  not 0) and passes with it.
- `tests/test_core.py`: `/nano` dispatch — refuses outside the TUI
  (`getattr(fe, "_tui", None) is None`), requires an argument, and calls
  `tui.open_nano()` with the right `Path` when present.

### R111. `/nano` deep-dive: a deadlock bug fixed, line numbers + a cursor/dirty indicator added
A deliberate post-ship review of R110 (bugs, performance, small missing
things) — R110 was already committed, so this is its own entry rather
than folding into that one.

**Bug found and fixed — could deadlock the worker thread.** The chat
pane's visibility is gated on `self._editor`/`self._help_visible`, NOT on
`self._menu_options`/`self._busy` — so a bash-output filename click was
reachable from the mouse at any time, including while an arrow-key menu
(`/model`'s picker, or the Esc-Esc "quit?"/"leave bash mode?" confirm) was
already open. Opening the editor on top of it made `self._editor is not
None`, which via `filter=_no_editor` makes Enter/arrows/digits — exactly
what the menu needs to resolve — ineligible; the menu became permanently
unresolvable, and the worker thread (still blocked on `select_menu()`'s
`self._answers.get()`) deadlocked for the rest of the session. A narrower
version of the same race existed even without an active menu: a
background LLM turn or bash command with no menu/question active YET
could still reach one moments later (a tool-call approval gate), after
nano had already opened successfully. `open_nano()` now refuses (with a
short message, editor never opens) while `self._busy` OR
`self._menu_options`/`self._question`/`self._secret` is set —
`self._busy` stays True for a submitted line's entire processing,
menu/question included, so it's a strictly broader and simpler guard than
enumerating every individual blocking state, and closes the race too.

**Added — line numbers.** `self._editor_area = TextArea(..., 
line_numbers=True)` — one keyword, `TextArea` already supports it
natively. No interaction with the wrap-aware scroll fixes: line numbers
are a left-margin rendering concern, computed from `render_info`'s
already-visible rows, not something that affects `first_visible_line()`/
`last_visible_line()`'s document-line math.

**Added — cursor position + dirty indicator on line 1.** The toolbar's
line 1 (`nano: <path>`) now also shows `— line N/M` and, when dirty,
`[modified]` — reads `self._editor_area.buffer.document.cursor_position_row`/
`.line_count`, which `_nano_dirty()`'s change-listener-based caching
doesn't need to help with: these are `Document` properties already
cached per-instance and already computed as part of prompt_toolkit's own
normal render pipeline for any multiline buffer, so surfacing them costs
nothing extra beyond formatting a string.

**Investigated, no change made**: `_nano_status_fragments()` allocates 5
fresh click-handler closures every render (once per keystroke, since
editing invalidates the whole layout including the status bar) — real,
but not nano-specific: every other status-bar button in this file
(copy last, copy all, agentic report, the model picker, ...) does the
same via its own `_..._click()` factory-per-render pattern. Fixing only
nano's would be inconsistent with the rest of the file and is out of
scope for a feature-focused review; noting it here in case a future pass
wants to address it file-wide. Also checked and found NOT to be a bug:
undo history does not leak between files opened in sequence (`Buffer.
reset()`, called by `_nano_close()`, clears `_undo_stack`/`_redo_stack`;
`open_nano()`'s `buffer.document = ...` assignment does not, but by then
the stack is already empty from the previous close) — Ctrl+_ / Ctrl+X
Ctrl+U undo (prompt_toolkit's default emacs-mode bindings, not something
this feature added) works correctly across open/close cycles. A named
pipe with a matching extension can't cause `open_nano()` to hang reading
it forever: `Path.is_file()` returns False for a FIFO (not `S_ISREG`),
so it's refused as "no such file" before any read is attempted.

- `tests/test_tui.py`: `test_nano_click_refuses_while_menu_is_active`,
  `test_nano_open_nano_refuses_during_question`,
  `test_nano_open_nano_refuses_while_worker_busy` for the deadlock fix;
  `test_nano_status_shows_cursor_line_and_dirty_mark` and
  `test_nano_editor_has_line_numbers` for the two additions.

### R112. Two live crash/bug reports: a chat-entry KeyError, and `cd` with spaces in bash mode
Both hit by the user in normal use, not review — real production bugs.

**Crash: `KeyError: 'done'` in `_close_think_locked`, taking down the
worker thread** (reported live, full traceback: a `!` bash command
followed by an LLM turn). R110 added a SECOND dict chat-entry kind
(`{"kind": "bash_output", ...}`, `append_bash_output()`), but every
"is this an open think row?" check elsewhere in the file — written before
R110 — assumed ANY dict entry in `self._chat` was a think row and indexed
straight into `item["done"]`, which a `bash_output` entry doesn't have.
Five call sites had this assumption (`begin_think`, `think_chunk`,
`_close_think_locked`, `_live_clock_key`, and the live-clock refresh loop
in `_fragments()`); `_entry_fragments`'s own think-rendering fallthrough
was already safe by elimination (it explicitly checks for `"bash_output"`
first). Fixed by adding `item.get("kind") == "think"` to all five checks.

**Bug: `cd` into a folder with a space in its name failed in bash mode**
(reported live: `cd Delete Latter` → `cd: Delete: No such file or
directory`, from `/bin/sh`, not Aurora — meaning the command fell through
to a real subprocess instead of being intercepted). Root cause:
`_bash_cd_target` used to `shlex.split(cmd)` and refuse anything past 2
tokens — but Tab-completion's `PathCompleter` does NOT escape spaces
(confirmed: completing `Del<Tab>` against a `Delete Latter` directory
inserts the literal, unescaped text `Delete Latter`), so completing a
space-containing name and pressing Enter reliably produced exactly this
failure. `cd` only ever takes ONE path argument in any shell, so there is
no real ambiguity to preserve — `_bash_cd_target` no longer requires
proper shell quoting for its own interception: everything after `cd ` is
now the literal target, with one layer of straight quotes stripped if the
whole remainder is quoted (so a traditionally-quoted `cd "some dir"` still
works, not literally including the quote characters). The `shlex` import
became unused and was removed.

- `tests/test_tui.py`: `test_bash_output_entry_does_not_crash_finish_think`
  and `test_bash_output_entry_does_not_crash_live_clock_key` reproduce the
  exact crash scenario (a `bash_output` entry, then a think row) against
  both broken call sites directly; `test_bash_cd_target_handles_unquoted_spaces`,
  `test_bash_cd_target_strips_matching_quotes`, and
  `test_bash_mode_cd_into_folder_with_space` (end-to-end through the real
  worker loop) for the `cd` fix. `test_bash_cd_target_parsing`'s old
  `"cd foo bar" is None` assertion was replaced (that input is no longer
  ambiguous under the new design) with `"cdfoo" is None`, covering the
  still-required "cd" must be a whole word, not a prefix, check.

### R113. `/nano` toolbar: actions on line 1 (underlined), bare filename + line count on line 2
User-requested toolbar rework, three parts: ① `save`/`save and close`/
`close`/`page up`/`page down` moved from line 2 to line 1, restyled
`class:status.id` (the same underlined class every other clickable
status-bar link already uses — model name, session id, copy links) so
they read as tappable, same as the rest of the app; ② file identity moved
to line 2, restyled plain `class:status` (no underline, since it isn't a
click target); ③ line 2 now shows the bare filename (`path.name`) instead
of the full path, plus the existing `— line N/M`/`[modified]` info that
already lived there.

- `tests/test_tui.py`:
  `test_nano_status_actions_on_line1_underlined_filename_on_line2_plain`
  asserts, by finding the `"\n"` fragment and splitting on it, that every
  non-separator line-1 fragment is `class:status.id`, that line 2 contains
  the bare filename but not the full path, and that no line-2 fragment is
  `class:status.id`.

### R114. Startup banner: "remote API (no health endpoint)" trimmed to "remote API"
User feedback: the parenthetical added no useful information for a remote
model (there's no local health probe to run against it, which is exactly
why it isn't run — the phrase was explaining an absence, not reporting a
fact). `Engine._provider_health_uncached()`'s early-return `detail` for
any non-`local` model is now just `"remote API"`.

### R115. LlamaDesk integration removed (R3, R68 marked removed above)
Not in use — no `llamadesk:` block was configured. Removed rather than
carried as dead weight: `aurora/llamadesk.py` deleted; `ui.py` lost
`_llamadesk`/`_llamadesk_mark_failed`/`_llamadesk_mark_ok`, the
`_LLAMADESK_RECHECK_S` failure-cache, `_pick_ctx`/`_CTX_OPTIONS` (the R68
context-size picker, which only existed for library loads), and the
library-load branch + "library" entries in `_pick_model` — the picker now
only shows configured models. `__main__.py`'s `_known_key_names()` no
longer folds in `llamadesk.token_env`; `man.py` dropped the
`LLAMADESK_TOKEN` env-var line. `config.yaml.example`'s commented
`llamadesk:` block removed. Tests removed:
`test_llamadesk_unreachable_is_cached_briefly`,
`test_llamadesk_parses_model_shapes`, the four `test_pick_ctx_*` cases, and
`test_pick_model_library_load_prompts_for_ctx_and_passes_it_through`
(`tests/test_core.py`, `tests/test_finish.py`). No replacement mechanism —
Aurora's `local` provider talks to whatever `llama-server` already has
loaded; switching what's loaded is left entirely to the user/server, not
Aurora's job.

### R116. Bash mode: `clear`/`cls` wipes the TUI scrollback instead of running a real `clear`
In the full-screen TUI, `!`-prefixed commands run via `subprocess.run` with
captured (not inherited) stdio, so a real terminal `clear` just produces an
ANSI escape blob that got dumped verbatim into the transcript as "output" —
it never touched the visible screen. `TuiFrontend._worker`'s bash-mode
dispatch (`aurora/tui.py`) now special-cases a bare `clear`/`cls` the same
way it already special-cases `cd`: instead of shelling out, it calls the
new `TuiFrontend.clear_screen()`, which empties `_chat`/`_cache` and
invalidates the app. The plain (non-full-screen) frontend's bash path
(`aurora/ui.py`) inherits a real tty, so `clear` already worked there —
untouched.

**Bug fix (same day):** `clear_screen()` cleared `_chat`/`_cache` but left
`_text_cache` — the flattened fragments `_fragments()` actually hands the
renderer — untouched, and reset `_dirty_from` to `None`. `_rebuild_locked`
only re-flattens when `_dirty_from is not None or _text_cache is None`
(R96b's fast path), so with both of those false the screen never rebuilt:
the pre-clear transcript kept rendering forever, i.e. `clear` visibly did
nothing. `clear_screen()` now also resets `_text_cache = None`,
`_offsets = []`, `_nlines = 0`, forcing the full (cheap, since `_chat` is
now empty) rebuild.
Test: `tests/test_tui.py::test_bash_mode_clear_wipes_scrollback` (now also
asserts on `_fragments()`, not just `_chat`/`_cache`, since that's what
missed this the first time).

### R117. `/nano <file>` fixes: self-blocking "busy" refusal, and no path completion
Two bugs found together in real use:
- **`/nano <file>` always refused with "nano: busy — try again once the
  current command/turn finishes"**, even on an idle session. The worker
  (`Tui._worker`) sets `self._busy = True` for a line BEFORE dispatching
  it, and `/nano` reaches `TuiFrontend.open_nano()` synchronously from
  that same dispatch (`ui._handle_command`) — so it always found itself
  "busy". The `self._busy` guard in `open_nano()` was only ever meant to
  protect its OTHER entry point (a bash-output filename click, reachable
  from the mouse on the UI thread at any time, racing an unrelated command
  already running on the worker thread) — that race can't happen to
  `/nano`'s own synchronous call. Fixed with an `open_nano(path,
  check_busy=True)` parameter; the `/nano` command's call site in
  `ui.py` passes `check_busy=False`, the filename-click path keeps the
  default.
- **No path completion for `/nano <file>`.** `_ModeCompleter` only ever
  dispatched to `SlashCompleter` (command-name completion) or, in bash
  mode, `PathCompleter` — `SlashCompleter.get_completions` returns nothing
  once the text has a space in it, so `/nano partial-name<Tab>` never
  offered a single path, unlike `cd` right next to it in bash mode.
  `_ModeCompleter` now special-cases a `/nano ` prefix in prompt mode too:
  narrows to the text after `/nano ` and hands it to a `PathCompleter`
  rooted at the real process cwd (not `_bash_cwd`, since `/nano` resolves
  relative paths the normal Python way, not against the tracked bash-mode
  directory).
Tests: `tests/test_tui.py::test_nano_command_opens_file_despite_worker_busy_flag`,
`tests/test_tui.py::test_nano_path_completion_offered_in_prompt_mode`.

### R118. Auto-compact + partial compaction (ideas surfaced by comparing against a competing agent, "Pi")
Two related additions, prompted by a documentation review of Pi
(pi.dev/docs) that turned up two things Aurora's `/compact` didn't do:
- **`compact_history()` now folds only OLDER history when asked to** —
  `compact.cut_index(messages, keep_recent_tokens)` walks backward from the
  end accumulating each message's estimated token size until
  `keep_recent_tokens` is exceeded, then advances forward to the nearest
  `user`-role message so the cut always lands on a turn boundary (a turn is
  `user -> assistant(+tool_calls) -> tool(s) -> ... -> assistant`; cutting
  mid-turn would separate a `tool` result from the `tool_calls` message it
  answers, an invalid sequence for the next request). `compact_history`'s
  existing `keep_recent_tokens=0` default is UNCHANGED behavior — manual
  `/compact` still folds the entire history, a deliberate "start fresh from
  a summary" action, not a trim (see the existing R14 tests, still
  passing). `keep_recent_tokens > 0` is new: fold only what's old enough to
  fall outside that budget, keeping the recent tail raw.
- **`auto_compact` (off by default, `runtime.auto_compact` /
  `/autocompact on|off`): silently calls `compact_history(keep_recent_tokens
  =runtime.compact_keep_recent_tokens)`** once `context_stats().pct` crosses
  `auto_compact_threshold_pct` (default 90 — deliberately above the
  existing manual ">80%" hint, so a user who acts on that hint never even
  notices auto-compact firing; it's the safety net for a session that keeps
  going past it). Runs from `Engine._maybe_auto_compact`, called at the end
  of every `send()`. `context_stats()` never blocks (R95i) and `limit == 0`
  (real limit not known yet) is a quiet no-op, never a guess.
  `fe.notify(...)` reports what got folded and at what usage.
Tests: `tests/test_core.py::test_cut_index_keeps_whole_history_when_it_fits_the_budget`,
`test_cut_index_lands_on_a_user_boundary`,
`test_cut_index_never_splits_a_tool_call_from_its_result`,
`test_compact_history_default_still_folds_everything`,
`test_compact_history_partial_keeps_recent_turn_raw`,
`test_auto_compact_off_by_default`, `test_auto_compact_fires_only_past_threshold`.

### R119. Extensions + a bundled MCP client, inspired by a competing agent ("Pi")
Two pieces, designed together:

- **`aurora/extensions.py`**: drop a `.py` file into
  `AURORA_HOME/extensions/` (flat, single-file only for now — no subfolder,
  no manifest) and its tools become callable by the model. Two ways a file
  can register tools, both mergeable in the same file:
  - **static** — module-level `SPEC` (list) + `RUNNERS` (dict), the exact
    shape `aurora/context.py` and the bundled extensions already use.
    The simplest possible extension is ~6 lines (see the module's
    docstring for the worked `current_datetime` example).
  - **dynamic** — a `register(engine) -> (specs, runners)` function, for a
    tool set that depends on runtime config rather than being fixed at
    import time.
  Loaded once per `Engine` lifetime (`Engine.__init__` → `tools.
  set_extensions(...)`), from two directories: `aurora/extensions_bundled/`
  (ships with Aurora) then `AURORA_HOME/extensions/` (user-authored). A
  file that fails to import, or a `register()` that raises, is skipped —
  collected as a warning string, never fatal to the rest of the session
  (same posture as a broken skill). Warnings never print from inside
  `extensions.py`/`engine.py` themselves (R25/R90a: engine-side code does
  no terminal I/O) — they ride on `Engine.extension_warnings`, printed once
  by `__main__.py` (a UI-side module) right after construction.

- **`aurora/mcp.py` + `aurora/extensions_bundled/mcp_extension.py`**: a
  bundled default extension connecting Aurora to any MCP (Model Context
  Protocol) server named in `config.yaml`'s new `mcp_servers:` list —
  **stdio transport only** (Aurora spawns each one as a child process,
  newline-delimited JSON-RPC 2.0 over its stdin/stdout, per the MCP spec;
  no remote SSE/HTTP transport yet). Each server's tools are discovered via
  the `initialize`/`tools/list` handshake and translated into Aurora's
  `SPEC` shape, name-prefixed `mcp_<server>_<tool>` so two servers can't
  register colliding names.
  - **Every `mcp_*` tool call needs approval, unconditionally** — MCP has
    no universal "this tool is safe" flag, and Aurora's whole safety model
    is approval-gated writes; silently trusting a configured server would
    be exactly the gap that gate exists to close. `tools.needs_approval()`
    replaces the old `call.name in tools.NEEDS_APPROVAL` checks in
    `agent.py` with a prefix check, since the tool names aren't known
    until a server's handshake completes (Engine-construction time), not
    at the point `NEEDS_APPROVAL` is defined.
  - **Credentials** route through the existing keystore (`aurora key set`)
    via `env: {CHILD_VAR: AURORA_KEYSTORE_VAR}` in a server's config —
    never plaintext in `config.yaml`, non-interactive lookup
    (`interactive=False`) so a missing credential means "that server
    likely reports an auth error," not "Aurora hangs during construction."
  - **One bad server never takes down the others, or Aurora itself**
    (`MCPManager` catches per-server startup failures into `.errors`,
    same "a tool must not kill the turn" principle as R42, applied here at
    startup instead of per-call).
  - Child processes are cleaned up via `atexit`.
  - Approval UI: `ui.py`'s `approve()` gained an `mcp_*` branch (args
    rarely have a "path"/"command" key the generic branches expect — shown
    as key: value instead, same shape as `on_tool_start`'s own rendering).

**Scoped down from the comparison that prompted this** (Pi's own
`pi-subagents`/`pi-hermes-memory` extensions, and its TypeScript extension
API): no lifecycle-hook system, no custom `/commands` from an extension, no
custom TUI rendering, no project-local extensions/trust prompt yet (global
`AURORA_HOME/extensions/` only) — Phase 1 of a larger plan, not the whole
thing.

Tests: `tests/test_mcp.py` (16 cases: the MCP client against a real fake
stdio server in `tests/fixtures/fake_mcp_server.py`, the extension
discovery mechanism's static/dynamic/broken-file paths, `tools.
needs_approval`'s mcp_ prefix rule, and end-to-end Engine wiring).

### R120. Denylist ("always DENY this") + an add_rule/add_deny_rule KeyError bug fix
Inspired by comparing Aurora's approval model against a competing agent's
permission-system extension,
**[`@gotgenes/pi-permission-system`](https://pi.dev/packages/@gotgenes/pi-permission-system)**
— worth reading its own example config for the fuller design Aurora's
version is a deliberate subset of:

```json
{
  "permission": {
    "*": "allow",
    "path": {
      "*": "allow",
      "*.env": "deny",
      "*.env.*": "deny",
      "*.env.example": "allow"
    },
    "bash": {
      "*": "ask",
      "rm -rf *": "deny",
      "sudo *": "ask"
    },
    "external_directory": "ask"
  }
}
```

That design has four composed layers (cross-cutting path surface, an
external-directory boundary gate, per-tool patterns, bash patterns; "last
matching rule wins" within a layer, most-restrictive layer wins overall)
and a THREE-state model (`allow`/`deny`/`ask`) with wildcard defaults.
Aurora's version is deliberately simpler — no layered composition, no
`external_directory` boundary concept, no wildcard default rule — because
Aurora already had a working two-state model (allow-listed vs. "ask every
time," R7); this adds the missing third state (**deny**, silently skip the
ask) as its own file, rather than redesigning what already works.

- **New: `AURORA_HOME/denylist.yaml`** — same shape and matching rules
  (command-prefix / path-glob) as the existing `allowlist.yaml`, but for
  "always DENY, no question asked" instead. `approve.is_denied(tool, args,
  data)` reuses the exact matching logic `is_allowed` already had —
  factored into a shared `_matches()` so the two can never drift apart.
  Checked BEFORE the allowlist in `agent.py`'s approval gate: a call
  matching both an allow and a deny rule is a config mistake, not a case
  worth new precedence rules for — **deny always wins**, same fail-closed
  spirit as the design that inspired this.
- **New approval-menu option: "Always DENY this (blocklist — never ask
  again)"** (key `d`) — persists via the new `approve.add_deny_rule()`,
  same rule-derivation logic (`_rule_for`) `add_rule` already used, shared
  rather than duplicated.
- **New `/denylist` command** mirrors the existing `/allowlist` — shows
  what's denied by policy, points at the file to hand-edit.
- **Bug fix, found while touching this code**: `add_rule()` did
  `data[tool].append(...)`, assuming `tool` was one of the 5 hardcoded
  `_TOOLS` names `load()` pre-populates. Any OTHER tool name — in
  particular an R119 extension's, e.g. `mcp_github_create_issue` —
  **KeyError'd**, meaning "Always allow" on any extension tool crashed the
  turn instead of persisting the rule. Fixed with `data.setdefault(tool,
  [])` in both `add_rule` and the new `add_deny_rule`; `is_allowed`'s
  command-tools branch had the same latent assumption
  (`for p in data[tool]`) and is fixed the same way (`data.get(tool, [])`)
  even though no test had exercised it yet.
Tests: `tests/test_core.py` — `test_denylist_empty_by_default`,
`test_denylist_command_prefix_matches`, `test_denylist_path_glob_matches`,
`test_add_deny_rule_persists_and_is_denied_sees_it`,
`test_add_rule_does_not_crash_for_a_non_core_tool_name`,
`test_add_deny_rule_does_not_crash_for_a_non_core_tool_name`,
`test_agent_denylist_match_never_prompts`,
`test_agent_deny_option_persists_and_stops_future_prompts`.

### R121. Bundled `lint_check` extension, inspired by a competing agent's `pi-lens`
`aurora/extensions_bundled/lint_extension.py` — a model-callable
`lint_check(path)` tool, registered exactly like the MCP extension (R119):
static `SPEC`/`RUNNERS`, the simplest possible extension shape, no new
mechanism needed.

Scoped **way** down from `pi-lens` (full LSP diagnostics, "impact cascade"
cross-file checks, tree-sitter structural analysis, multi-language bundled
parsers/formatters): Python only, no LSP client. Runs
[`ruff`](https://docs.astral.sh/ruff/) if it's on `$PATH`; if not, falls
back to a syntax-only check via the stdlib's `py_compile` (always
available, zero setup) and says so explicitly in the result — never
silently implies a real lint pass happened when it only checked syntax.

**This is a model-callable tool, not a guaranteed automatic hook** — it
only runs when the model chooses to call it (nudged by its own tool
description: "call this after writing or editing a .py file"). A version
that runs after EVERY write/edit unconditionally would need Aurora's
extension mechanism to support lifecycle hooks, which it doesn't yet (see
`EXTENSIONS.md`'s "What's scoped out" section) — that's real future work,
not this requirement.

Tests: `tests/test_lint_extension.py` (8 cases — missing file, unsupported
extension, the ruff-present and ruff-absent paths, a real syntax error
caught by the `py_compile` fallback, a tool crash surfacing as a result
string rather than raising, and end-to-end discovery as a bundled
extension).

### R122. `/extensions` command + startup-banner visibility
Found while demoing R119/R121 live: there was no way to see loaded
extensions from inside Aurora at all — only `engine.extension_warnings`
(failures) printed at startup, nothing positive ("here's what's active").
- **New `ui._extension_tool_specs()` helper**: the extension-provided tool
  specs specifically — `tools.specs()` also folds in the built-ins plus
  `web_search`/the `.agentic_context` doc tool when active, neither of
  which is an extension, so those are named out explicitly rather than
  assuming "everything past the built-ins" is extension-provided. Shared
  by the command and the banner so the two can never disagree about
  what's loaded.
- **New `/extensions` command**: lists each loaded extension tool + its
  description, any `engine.extension_warnings`, and a short "how to work
  with them" footer — bundled tools, where to drop your own
  (`AURORA_HOME/extensions/`), and a pointer to `EXTENSIONS.md`.
- **Startup banner** (both `ui.py`'s classic REPL and `tui.py`'s
  full-screen banner — they duplicate this info card, so both needed the
  same line) gained an `extensions  N tool(s) loaded  (/extensions for
  details)` row right below `session`, shown only when at least one
  extension tool is loaded.
Tests: `tests/test_mcp.py` — `test_extension_tool_specs_excludes_builtins_
and_web_and_context`, `test_extension_tool_specs_includes_loaded_extension_
tools`, `test_extensions_command_lists_tools_and_how_to_add_one`,
`test_extensions_command_reports_none_loaded`,
`test_lint_check_always_bundled_by_default`.

### R123. `/think` removed — the TUI's existing click-to-expand replaces it
`_think_toggler`/`_entry_fragments` (`aurora/tui.py`) already made EVERY
"thought for Ns" row in the scrollback clickable, toggling that entry's own
`open` flag and expanding to the full reasoning in place, at any point in
the chat history, not just the most recent turn — this was already fully
built and tested (`test_think_entry_collapsed_then_toggle`,
`test_begin_think_row_without_text_is_timed_not_clickable`) before this
requirement. The `/think` command (`ui.py`: print `fe.think_buffer`
standalone) was redundant with it in the TUI, so it's removed —
`fe.think_buffer` itself stays (still feeds `/copy-last`'s raw-response
text, unrelated to `/think`).

**Known tradeoff, accepted rather than silently hidden**: the classic REPL
(`--classic`, or any non-tty session — pipes, CI) has no mouse, so it has
no equivalent to the click-to-expand affordance. Removing `/think`
universally means a classic-mode session loses the ability to read past
reasoning after the fact entirely (only `/thinking`'s LIVE stream, or
`/copy-last`'s raw-response text for the last turn, remain). Accepted
because the TUI is Aurora's primary interface and classic mode is
explicitly the plain-terminal fallback — but this is a real capability
loss for that fallback, not a wash.
Tests: `tests/test_core.py::test_think_command_removed_falls_through_to_unknown_skill`,
`test_thinking_toggle_still_works`.

### R124. "copy last" / `/copy-last` also copies the prompt, not just thinking + response
`ui._raw_last_response_text` only ever joined `fe.think_buffer` (thinking)
and `engine.last_response()` (the final answer) — the user prompt that
started the turn was never included, so a copied "raw record" was missing
the one piece of context that makes the thinking/response actually legible
out of context.

- **New `Engine.last_prompt()`**: walks `self.messages` backward for the
  most recent `role: "user"` message, same shape as the existing
  `last_response()`. A user message's `content` is always a plain string
  here (never assistant messages' `tool_calls` shape), so no
  `_assistant_text`-style unwrapping is needed.
- **`_raw_last_response_text` now joins three sections** —
  `[prompt]`/`[thinking]`/`[response]` — omitting whichever of
  prompt/thinking is empty rather than printing an empty labelled section.
  Both the `/copy-last` command and the status bar's "copy last" button
  share this one function (unchanged), so both picked up the fix from a
  single change.
Tests: `tests/test_core.py::test_engine_last_prompt_finds_the_most_recent_user_message`,
`test_engine_last_prompt_empty_when_no_user_message`,
`test_raw_last_response_text_includes_prompt_thinking_and_response`,
`test_raw_last_response_text_without_thinking_still_includes_prompt`.

### R124a. "copy last" formatting: a banner + rules between sections
Follow-up the same day: plain `[prompt]\n...\n\n[response]\n...`
concatenation read as one run-on block with no visual seam between the
question and the answer. `_raw_last_response_text` now prefixes a
double-bar banner (`----------\n----------`) and joins sections with a
single-bar rule (`\n\n----------\n\n`) between them — e.g.:

```
----------
----------

[prompt]
whats the weather in lisbon?

----------

[response]
Here's the current weather in Lisbon: ...
```

No banner/rule at all when there's only one section (no prompt found and
no thinking captured — just the bare answer, unchanged from before R124).
Tests: `tests/test_core.py::test_raw_last_response_text_exact_separator_layout`,
`test_raw_last_response_text_no_separator_when_only_one_section`.

### R125. Deep-dive review of R116–R124a: 4 real bugs found and fixed
A full review pass over the day's R116–R124a work (a code-review agent,
verified by hand before fixing) turned up four genuine issues, none of
them cosmetic:

- **"Always allow"/"Always DENY" silently did nothing for MCP and most
  extension tools** (`approve.py`'s `_rule_for`). Real MCP tool args carry
  no `path` key (`{"title": ..., "body": ...}`, not `{"path": ...}`), so
  the derived rule string was `""` — falsy, so `add_rule`/`add_deny_rule`'s
  `if rule` guard silently skipped saving anything. Picking "Always
  allow"/"Always DENY" from the prompt looked like it worked (no error)
  but the identical call re-prompted (or was never actually blocked) next
  time. Root cause: R120's own tests passed `{"path": "whatever"}` to MCP
  tool names — args real MCP calls never send — manufacturing the one
  condition under which persistence worked, so the bug shipped invisibly.
  Fix: `_rule_for` returns `"*"` ("this tool, any args") when there's no
  path — `fnmatch.fnmatch(sig, "*")` matches unconditionally, including an
  empty signature, so `is_allowed`/`is_denied` treat it exactly like a
  path-glob rule already would. This is the accepted, documented
  granularity limit for these tools (tool-level, not per-argument) — the
  bug was that it didn't even work at that granularity.
- **Auto-compact could produce two consecutive `user` messages**
  (`engine.compact_history`'s partial-fold path). `cut_index` deliberately
  lands the cut ON a `user`-role message (a turn boundary); the code then
  built `[summary_msg] + recent`, where `summary_msg` is ALSO `role:
  "user"` — two adjacent user turns, which providers enforcing strict
  role alternation (Anthropic-family models via OpenRouter) reject on the
  next request. The manual `/compact` path (`keep_recent_tokens=0`) never
  hit this since `recent` is always empty there — only the NEW
  auto-compact path (R118) could trigger it, and it would have on every
  single auto-compact firing. Fix: merge the summary into `recent[0]`'s
  own content instead of prepending a separate user message, keeping the
  sequence strictly alternating exactly as it was before compaction
  touched anything.
- **MCP's read timeout didn't work against a server that never replies at
  all** (`mcp.py`'s `_read_response`). `readline()` is a BLOCKING call —
  the `while time.monotonic() < deadline` loop only re-checked the
  deadline BETWEEN lines, so the timeout was enforced against a SLOW
  server but not a SILENT/hung one. Since `MCPServer()` runs during
  `Engine` construction, one wedged MCP server would hang Aurora's entire
  startup with no way out — verified live: a test server that replies to
  `initialize`/`tools/list` then sleeps forever on `tools/call` hung the
  old code indefinitely (confirmed via a real backgrounded process still
  alive after 6+ seconds; killed manually to clean up). Fix: `select.
  select` on the deadline before each `readline()` (same pattern
  `tools.py`'s `grep` timeout already uses), plus killing the unresponsive
  child on timeout rather than leaving it running.
- **Dead code**: `_raw_last_response_text` had an unreachable duplicate
  `return` left over from the R124a edit. Harmless, removed.

Also, structural (not a bug, a robustness improvement): new
`tests/conftest.py` with an autouse fixture snapshotting/restoring
`tools._EXTENSION_SPECS`/`_EXTENSION_RUNNERS` around every test — this
process-global mutable state was previously reset only by per-test
`finally` discipline; a test that forgot (or failed before reaching its
cleanup) would leak extension tools into unrelated later tests.

Tests: `tests/test_core.py::test_add_rule_persists_for_a_realistic_mcp_call_with_no_path_arg`,
`test_add_deny_rule_persists_for_a_realistic_mcp_call_with_no_path_arg`,
`test_compact_history_partial_never_produces_adjacent_same_role_messages`,
`tests/test_mcp.py::test_mcp_read_timeout_fires_against_a_server_that_never_replies`
(new fixture: `tests/fixtures/hanging_mcp_server.py`).

### R125b. Second deep-dive pass over R97–R125: 3 more bugs found and fixed
R125's review was scoped to R116–R124a. A follow-up pass widened the
window back to R97 (the start of this two-day span) and added a
performance/correctness sweep, turning up three more real issues:

- **Bash mode's `!<cmd>` had no timeout and no process-group ownership**
  (`tui.py`'s `_worker`). It ran a bare `subprocess.run(cmd, shell=True,
  cwd=self._bash_cwd)` — every other shell entry point (`run_command`,
  `wait_until`) got the R95c process-group hardening, bash mode never did.
  Since `_worker` is the sole consumer of the TUI's inbox queue, a hanging
  command (`!ssh host`, a REPL that reads stdin) wedged the entire session
  with no cancel path. Fix: route through `tools._run_command_once` — same
  timeout/process-group-kill behavior as `run_command`, plus a `[timeout
  after Ns]` message on the same path.
- **`wait_until`'s own `timeout` could be outrun by a single hung
  attempt** (`tools.py`). Each attempt reused `_run_command_once`, which
  was only ever bounded by the global `COMMAND_TIMEOUT` (300s default) —
  not by whatever (possibly much smaller) `timeout` the caller passed to
  `wait_until` itself. A `wait_until(..., timeout=5)` could still block
  ~300s on one bad attempt. Fix: `_run_command_once` now takes an optional
  `timeout` override; `wait_until` passes the remaining budget
  (`timeout - elapsed`) on every attempt instead of relying on the global
  default.
- **`/commit`'s draft prompt sent the full, uncapped staged diff to the
  model** (`gitcommit.py`'s `draft_message`). Only the *displayed* preview
  in `ui.py`'s `_commit_cmd` was capped (`_COMMIT_DIFF_PREVIEW_CAP` =
  4000 chars) — the model call itself had no cap, so a large staged diff
  (vendored deps, a regenerated lockfile) could blow a local model's
  context or produce a surprise token bill on a remote one, while the
  capped preview gave a false sense that this was bounded. Fix: a
  separate `_DRAFT_DIFF_CAP` (20,000 chars) truncates what's sent to the
  model, independent of the display cap.

Tests: `tests/test_tui.py::test_bash_mode_command_is_timeout_and_process_group_safe`,
`test_bash_mode_reports_a_timed_out_command`,
`tests/test_core.py::test_wait_until_caps_each_attempt_to_its_remaining_time_not_command_timeout`,
`tests/test_gitcommit.py::test_draft_message_caps_a_huge_diff_before_sending_to_the_model`.

### R125c. Third deep-dive pass, new ground: nano close, extension collisions, 429 Retry-After
Two prior passes covered R116–R124a and then R97–R125's own fixes. This
third pass deliberately targeted areas neither had touched yet — the nano
editor's close path, the extension-loading/dispatch merge, and the 429
backoff's interaction with what the server actually asks for — and found
three more real issues:

- **Nano's "close" button discarded unsaved edits with zero confirmation**
  (`tui.py`'s `_nano_close_click`/`_nano_status_fragments`). Every other
  risky action in the TUI (quit, leaving bash mode) goes through an
  arm-then-confirm gesture; nano's close button — the ONLY way out of the
  editor, no key binding exists — closed and discarded on a single click
  even while dirty. Fix: a `_nano_close_confirm` flag armed by the first
  click on "close" while dirty (relabels to "discard changes?" instead of
  closing), consumed by the second click; any further edit or a save
  disarms it again so a stale arm can't linger and fire on an unrelated
  later click.
- **Extension tool names could silently collide** — with a builtin, or
  with another extension — and both failure modes were invisible
  (`tools.py`'s old `set_extensions`). A name matching a builtin got
  double-listed in the tool spec sent to the model, and the extension's
  own runner was permanently dead code since `run_tool`'s lookup checks
  `RUNNERS` before `_EXTENSION_RUNNERS` — the model could call the tool,
  the WRONG implementation would run, with no error. Two extensions
  defining the same name silently let `dict.update` pick a runner while
  the spec list carried both, duplicated, in what the model sees. `/
  extensions` showed neither problem since it only lists what made it into
  `_EXTENSION_SPECS`. Fix: `set_extensions` now dedupes against builtin
  names and against itself, drops the losing tool, and returns warnings
  that flow into the same `engine.extension_warnings` surface load/register
  failures already use.
- **429 backoff ignored the server's own `Retry-After`** (`openai_compat.
  py`). The fixed (1s, 3s) schedule applied even when a provider's 429
  response said exactly how long to wait — sometimes too short (an
  impatient retry that just gets rate-limited again) and sometimes far
  longer than necessary. Fix: `cancellable_sse` (`base.py`) now yields
  response headers alongside status/body; a `Retry-After` seconds value is
  parsed and honoured, capped at 30s so a provider asking for minutes
  can't stall a turn the user is actively watching. A malformed or
  HTTP-date-form `Retry-After` falls back to the fixed schedule rather than
  guessing.

Tests: `tests/test_tui.py::test_nano_close_while_dirty_requires_a_second_click_to_discard`,
`test_nano_editing_after_arming_close_disarms_it`,
`test_nano_saving_after_arming_close_disarms_it`,
`tests/test_core.py::test_set_extensions_drops_a_tool_that_shadows_a_builtin_name`,
`test_set_extensions_drops_a_duplicate_name_between_two_extensions`,
`test_429_honours_a_server_provided_retry_after`,
`test_429_retry_after_is_capped_so_it_cant_stall_a_turn_for_minutes`,
`test_429_ignores_a_malformed_retry_after_and_falls_back`.

### R126. MCP: `select()` polls the fd, but `readline()` read a Python buffer
Found in a full requirements-vs-code review (2026-07-25). R125 fixed "an MCP
server that never replies hangs Aurora's startup" by putting
`select.select` in front of `_read_response`'s `readline()`. That fix was
half right: `select` reports readability of the **file descriptor**, while
the text-mode `readline()` read from the `TextIOWrapper`'s own **internal
buffer**. The two see different things.

An MCP server routinely emits a notification line and its reply in one
flush (`notifications/*` are part of the protocol; real servers log progress
this way), so a single OS read pulls BOTH lines into that buffer. The old
client returned the notification, looped, selected again, found the fd idle
— and waited out the entire timeout on a reply it was already holding in
memory, then called `_kill_unresponsive()` on a perfectly healthy server.
Since `MCPServer()` runs during `Engine` construction, this hit at startup.
The same mismatch also made the timeout unenforceable mid-line: a server
that wrote half a line and stalled left `readline()` blocked inside the
call, where the deadline loop around it could never re-check.

- Fix: **binary pipes (`bufsize=0`) and our own line buffering**
  (`self._buf`, `_next_buffered_line()`). `_read_response` now drains a
  complete buffered line BEFORE ever waiting on the fd again, and only
  `select`s when the buffer genuinely holds no full line — so a buffered
  reply is never invisible, and a mid-line stall returns to the deadline
  check instead of blocking. `_send` encodes to bytes to match.
- R125's own test only covered a server emitting **nothing at all**, which
  is why this shipped: that case never exercises the buffer at all. The new
  fixture (`tests/fixtures/chatty_mcp_server.py`) emits a notification in
  the same burst as every reply, which is the ordinary case.
- Tests: `tests/test_mcp.py::test_mcp_handles_a_notification_sent_in_the_
  same_burst_as_the_reply` (verified: raises "timed out" on the old code,
  and the handshake itself fails, so construction never completes) and
  `test_mcp_timeout_is_enforced_when_a_server_stalls_MID_LINE` (asserts the
  CLOCK, not the exception — both versions eventually raise, but the old one
  only after the server's 6s stall ends; measured 6s → ~1s).

### R127. `grep`'s 30s timeout was not actually enforced
Same class of bug as R126, in the other module that pairs `select` with a
buffered read. R96m made `grep` read stdout incrementally so it could bound
the PRODUCER, and claimed "a stall between chunks (not just total elapsed
time) is still caught by the same loop". It wasn't:
`proc.stdout.read(65536)` on a text-mode pipe blocks until 65536 characters
have arrived or the process exits, so once `select` reported readable, the
loop could sit inside that one read far past the deadline it had just
computed. Only the wait BETWEEN reads was bounded.

That is grep's *normal* shape — print a few early matches, then scan a large
tree for a long time — so the cap silently did not hold, on the worker
thread, for the tool most likely to be pointed at an over-broad path.
Measured: with a 1s timeout against a producer that emits one line then
sleeps 10s, the old code returned after **10.1s**.

- Fix: binary pipes and a raw **`os.read(proc.stdout.fileno(), 65536)`**,
  which returns whatever is available right now (≥1 byte, since `select`
  just said readable) instead of waiting to fill a buffer — so every
  iteration gets back to the deadline check. stdout/stderr are decoded once
  at the end. The truncation cap, process-group reaping and `finally` block
  are unchanged.
- The pre-existing timeout test forced the branch by making `select` report
  "never ready", so it could not catch this — it never let a read start.
  Tests: `tests/test_core.py::test_grep_timeout_is_enforced_when_the_
  producer_stalls_MID_CHUNK` (fails at 10.1s without the fix).

### R128. `--continue` could restore two consecutive `user` messages
`Engine.send` logs its `user` event unconditionally, but R95e deliberately
skips the `assistant` log when a turn produced nothing (provider error,
interrupt, cancel). So a failed turn followed by a retry leaves two adjacent
`user` records in the session JSONL — and `resume_from` replayed every
`user`/`assistant` record verbatim, rebuilding exactly the invalid sequence
R44, R95e and R125's auto-compact fix each exist to prevent on the live
path. Providers that enforce strict role alternation (Anthropic-family
models via OpenRouter) reject it on the first send after `--continue`, so
the failure landed one turn after the resume, far from its cause.

- Fix: `resume_from` collapses consecutive same-role records —
  role-generic, not a `user`-only special case. **Merged, not dropped**: the
  abandoned prompt is real context the user typed, and silently discarding
  it would be its own surprise.
- Tests: `tests/test_core.py::test_resume_never_restores_two_consecutive_
  user_messages` (asserts the merge keeps both prompts) and
  `test_resume_merges_consecutive_assistant_messages_too`.

### R129. "copy all" did unbounded work on the UI event-loop thread
The status bar's **`copy all`** button (R56) ran its whole job inline in the
mouse handler: `session.export_markdown()` parses the ENTIRE session JSONL —
a file that only grows, since R20 never deletes anything — and the result is
then handed to `clipboard.copy`, which spawns `pbcopy`/`wl-copy`/`xclip`
with a 5s timeout. A mouse handler runs on the UI event-loop thread, so both
froze the app. Measured on a 19MB log: ~0.17s of dead UI per click on the
*fast* path, growing with session length, with a 5s subprocess ceiling above
it.

This is the same class R95i ("the status bar never blocks on a socket") and
R89 ("a blocking `select()` must never run on the UI thread") already
closed, so it takes the same fix rather than a new mechanism:
`_copy_all_chat` now queues `/copy-all` onto the worker's inbox exactly as
`_agentic_report_click` queues `/agentic_report`. Feedback moves from the
status-bar notice to the chat line `/copy-all` already prints — identical to
how the agentic-report button reports.

The other clipboard buttons (session id, `copy last`, drag-select) are
deliberately left inline: their payloads are bounded and small, so they
carry no O(session) work — only this one read a file with no ceiling.

- Test: `tests/test_tui.py::test_copy_all_click_queues_the_command_instead_
  of_working_inline` asserts neither `export_markdown` nor `clipboard.copy`
  is reached on the calling thread — a failure here is a frozen UI, which no
  output assertion would catch.

**Also measured on this pass, and NOT applied** (recorded like R96i/R96l,
because it was tried and measured rather than skipped): `rewind.checkpoint`
runs `git add -A` before every approved mutation, which looked like an
O(tree) cost per tool call. Measured against a 4000-file tree: **0.77s for
the first checkpoint, then ~0.07s** for each one after — git's index
stat-cache makes the repeat case cheap, and 70ms against an LLM turn is not
a cost a user can perceive. No change made.

### R130. The approval gate now says when `/rewind` can't undo a write
R47 promises a snapshot before "**every** approved mutation", but a
checkpoint's work-tree is the **cwd** — so a `write_file`/`edit_file`/
`apply_patch` aimed anywhere else (`~/Desktop/notes.md`, `/etc/hosts`, a
sibling project) was approved, executed, and then not recoverable by
`/rewind` at all. Nothing said so. This is not an exotic case: R30's base
system prompt actively tells the model to prefer absolute and `~` paths.

Same principle as R95a — the approval prompt is what buys consent, so it
must not imply a guarantee that doesn't exist:
- **New `rewind.covers(path, cwd=".")`** — is this path inside the tree a
  checkpoint actually snapshots? Best-effort like the rest of the module: an
  unresolvable path counts as NOT covered, so the honest warning is the
  failure mode, never a false assurance.
- `approve.diff_preview` prefixes `[note: outside the checkpointed tree —
  /rewind cannot undo this write]` for the three path-taking mutating tools.
  The real diff still follows; the note is a prefix, not a replacement.
- **A note, not a refusal** — writing outside the project is legitimate;
  being told it's unrecoverable is the point.
- **`run_command` is deliberately excluded**: what a shell command touches
  isn't knowable from its arguments, so a note keyed on them would be
  guesswork in both directions.
- Tests: `tests/test_core.py::test_rewind_covers_paths_inside_the_cwd_only`,
  `test_approval_preview_warns_when_a_write_escapes_the_checkpoint`,
  `test_approval_preview_rewind_note_skips_run_command`.

### R131. `wait_until`'s command gets R58's secret notice too
R58's parameter notice — scan a shell command's own text, report a likely
credential, never block and never rewrite it — was wired to `run_command`
only. `wait_until` (R100) is the second shell entry point, takes the same
`command` argument, and already inherited run_command's allowlist shape
(`approve._COMMAND_TOOLS`) and its process-group hardening (R95c); it just
never got this. So the identical string was reported in a one-shot command
and passed unflagged in a polled one — an omission, not a decision.

`agent.py` now keys the notice on `_COMMAND_ARG_TOOLS = ("run_command",
"wait_until")`, mirroring `approve._COMMAND_TOOLS` so the two lists of
"tools taking a shell command string" can't drift. Contract is unchanged:
notice only, the real command still runs with its real argument.
Test: `tests/test_core.py::test_wait_until_param_secret_gets_the_same_
notice_as_run_command`.

### R132. The lint bar is actually checkable now
`AGENTS.md` requires "build + lint must pass before done", but nothing in
the repo made that possible: `ruff` was not in `pyproject.toml`'s `dev`
extra, and there was no `ruff.toml` or `[tool.ruff]` section anywhere. A
`.ruff_cache` proved it had been run; nothing recorded *how*. So a fresh
clone had no linter, and whoever installed one got whatever ruleset their
version happened to default to — on ruff 0.16 that is a very wide set,
reporting **263 findings** against a codebase that had clearly never been
held to them.

- **`ruff>=0.16,<0.17` added to the `dev` extra, and pinned deliberately**:
  ruff's DEFAULT ruleset widens between releases, so an unpinned lint is a
  different check on every machine — exactly how this drifted.
- **`[tool.ruff.lint]` ignores 15 rules, each with the reason inline**, so a
  future reader can tell "deliberate" from "not looked at yet". They are all
  patterns this codebase uses ON PURPOSE, not debt:
  `BLE001`/`S110`/`S112` (a tool, checkpoint or state write must never kill
  the turn — R42/R47/R51 make swallowing the documented contract);
  `B023` (`agent.py`'s `_flush`/`_finish` close over the loop variable but
  are called INSIDE the same iteration, never deferred — verified by hand,
  the rule cannot see the call site); `PLW1510` (`check=False` where a
  non-zero exit is an expected outcome to inspect); `RUF012`; `RUF059`;
  `EXE001` (test fixtures carry a `#!` for documentation and are run as
  `python <path>`); `DTZ001/005/006` (session listings show LOCAL time to a
  human at this machine — the JSONL itself is UTC-aware); `ISC004` (all six
  sites are f-strings wrapped for width, checked — not a missing comma);
  `SIM115` (`session.log`'s single-writer append, per ARCHITECTURE.md).
- **The remaining 108 were fixed, not silenced** — 14 unused imports, 3
  unused locals, 3 stale `noqa`s, 67 unsorted import blocks, plus assorted
  modernizations. Every change is behaviour-preserving: `re.M`/`re.I` →
  `re.MULTILINE`/`re.IGNORECASE` are aliases, `socket.timeout` **is**
  `TimeoutError` on the project's `requires-python = ">=3.11"`, and the one
  deleted `pass` followed a docstring that was already the function body.
  `providers/__init__.py` gained an `__all__` because its `ToolCall`/
  `TurnResult` imports are deliberate re-exports, not dead ones.
- `ruff check aurora tests` now reports **All checks passed**, with the full
  suite still green — so the `AGENTS.md` bar is enforceable from a clean
  clone (`pip install -e '.[dev]'`) instead of aspirational.

**Not done here, deliberately**: no formatter (`ruff format`) was
introduced. That would reflow the whole codebase in one commit and bury
every future diff; the ignore list above encodes the house style without
touching layout.

### R133. The session log records what it always claimed to (2026-07-25)
Groundwork for a `/context` tree view (planned, R134) that renders a session
as turns with their token/cost/tool shape. Building it surfaced that three
things the view needs are things Aurora *has* but never writes down. Each is
a gap in its own right, so they land here, before and independent of any UI.

- **R133a — reasoning tokens.** `openai_compat.py` read
  `usage.prompt_tokens_details` (R91's cache hits) but never
  `usage.completion_tokens_details.reasoning_tokens`, so a thinking model's
  reasoning was billed inside `completion_tokens` and invisible afterwards.
  `TurnResult.reasoning_tokens` / `Turn.reasoning_tokens` now carry it, and
  the `assistant` record logs it. **It is a SUBSET of `output_tokens`, never
  additive** — summing the two double-counts every thinking turn, and any
  display putting them side by side has to say so.
  Local llama.cpp streams `reasoning_content` but reports no
  `reasoning_tokens`, which would leave m7 turns showing zero thinking. So
  `reasoning_chars` counts the streamed reasoning text as a fallback —
  exact as characters, an ESTIMATE as tokens, and counted even when no
  `on_think` listener is attached, because the count is accounting, not
  display.
- **R133b — tool outcome and true size.** `engine.py` logged
  `output=o[:4000]`, so a 60KB capped result and a 4KB one were
  indistinguishable on disk; `chars` now carries the real length alongside
  the (still truncated — never write 60KB twice) preview. Success/failure
  was only ever expressed as a bracketed marker at the start of the output
  (`[error: …]`, `[grep error: …]`, `[tool error: …]`, `[skipped: …]`,
  `[denied …]`, `[not run — …]`) — a convention the model reads and readers
  had to re-derive by sniffing strings. `tools.result_status()` names it and
  the `tool` record stores `ok` / `error` / `skipped`.
- **R133c — approvals were never logged at all.** `session.py`'s module
  docstring has claimed since R20 that "every turn, tool call/result,
  **approval**, switch, and error is appended". No `session.log("approval",
  …)` existed anywhere; the docstring described an intent. Every session on
  disk is missing the record, and nothing could reconstruct it. A new
  `AgentCallbacks.on_approval(tool, decision, detail)` fires at **every**
  outcome of the gate — `allowlisted`, `approved`, `always_allow`, `denied`,
  `denied_policy`, `always_deny`, `steered`, `stopped` (the closed set lives
  in `agent._APPROVAL_DECISIONS` so writer and reader share a vocabulary).
  Wired at the gate itself, not around `cb.approve`: a daily driver with an
  allowlist takes the never-asked path most of the time, so wrapping the
  prompt would have logged only the rare case and called it complete. The
  callback is exception-swallowed on the same contract as R42/R47/R51 — a
  broken log sink must not cost the user their turn.

Tests: `tests/test_core.py::test_result_status_classifies_every_tool_outcome`,
`::test_approval_gate_reports_the_allowlisted_path_too`,
`::test_approval_gate_reports_asked_outcomes`,
`::test_a_failing_approval_logger_never_kills_the_turn`,
`::test_turn_sums_reasoning_without_inflating_output`,
`::test_reasoning_usage_and_streamed_chars_come_off_the_wire`,
`::test_session_log_records_approvals_and_real_tool_size`.

**Backward compatible by construction**: these are added fields on new and
existing event types, so old logs still parse — any reader must treat a
missing `chars`/`status`/`reasoning_*` as "not recorded", exactly like R91's
`cached_tokens`, and must not infer approvals were absent from a session
logged before this.

### R134. `/context` — the cost tree (2026-07-25)
`/cost` (R92) answers "what did this session cost" as a per-model total.
It cannot answer "**where** did it go" — which turn ballooned, which tool
returned 60KB, where the thinking happened, what got approved. All of that
was already in the session JSONL (R20, R92, R133); nothing rendered it.

`/context [all] [<session-id>]` draws a session as a tree: one row per turn
with its badge line, tool calls nested under it, and `/compact` folds and
`/model` switches in place at the point they happened. New module
`aurora/ctxtree.py` — a **pure read**, no new accounting and no state to
keep in sync, so it works on any session log on the machine.

- **UI-side module.** It colours, so `engine.py` must not import it (R25,
  enforced by `test_architecture.py`). Both front ends reach it through the
  single `ui._handle_command` dispatch, which is also what puts it in `/`
  autocomplete via `COMMAND_INFO` for free.
- **💭 placement is a correctness question, not a layout one.** A REPORTED
  `reasoning_tokens` is documented to be part of `completion_tokens`, so it
  renders *inside* 📤 as `📤 2.3k (💭 1.8k of it)` — printing the two side
  by side reads as a sum and makes a thinking turn look twice as expensive
  as it was. An ESTIMATE from `reasoning_chars` (R133a's local-llama.cpp
  fallback) gets the opposite treatment: nothing says a backend that
  declines to count reasoning folded it into its completion total anyway,
  and asserting it did produced the visibly impossible
  `📤 900 (💭 ~12k of it)` on the first real render. Estimates therefore
  stand beside 📤 as their own term, marked `~`.
- **📥 shows `4.5k→14.2k billed` only when they differ** — they only can on
  a multi-tool turn, where every round re-pays for the whole prompt (R37).
  That gap is the multi-tool tax and it is invisible everywhere else in the
  UI; on a single-round turn it would be noise.
- **⚡ cache share only at ≥10%.** Below that it is noise. The `$` stays a
  deliberate UPPER bound (R91) — cached reads bill cheaper but not free and
  the discount isn't reported uniformly — so ⚡ reads as "the real bill is
  under that", not as a correction of it.
- **Approvals mark only what was ASKED.** An `allowlisted` pass was silent;
  a ✅ on every allowlisted write would drown the ones the user actually
  answered. A refusal shows `⛔` alone (the ⊘ beside it is noise — the
  refusal already says it never ran), but an approved call that then FAILED
  keeps its `✗`: "you said yes and it broke" is the whole point.
- **Turns are numbered against the whole session**, not the visible slice —
  with `showing last 20` renumbering from 1 would make the numbers lie.
- **Last 20 turns by default** (`all` overrides). A session log only grows
  (R20) and the chat area is not a pager.
- **Old sessions degrade honestly.** Pre-R133 logs have no tool `status`, no
  reasoning counts, and **no approval records at all**; the tree appends
  "some fields predate R133 and were not recorded" rather than rendering an
  absence as "no approvals happened", which would be a lie about every
  session logged before yesterday. A turn with no `assistant` record (R95e:
  the turn produced nothing) renders as such instead of vanishing.

**Status bar**: a `cost tree` link sits immediately after the price it
breaks down (R56's fragment-level mouse-handler mechanism, same as the
click-to-copy session id). *(Superseded by R135c: the label is gone and the
`ctx` gauge itself carries the handler — same behavior, one word less on the
row. The rest of this paragraph still holds.)* It **queues `/context` on the inbox** rather than
rendering inline — a mouse handler runs on the UI event-loop thread and this
walks the entire session JSONL, the exact defect R129 fixed for "copy all".
TUI-only; the classic REPL has no status bar, so there it is `/context`.

Tests: `tests/test_core.py::test_context_tree_*` (10),
`::test_context_command_is_registered_and_completes`,
`tests/test_tui.py::test_cost_tree_click_queues_the_command_instead_of_
working_inline`, `::test_status_bar_ctx_gauge_is_the_cost_tree_link`
(renamed with R135c).

#### R134a–f. Review pass on the above (2026-07-25)
A deliberate bug-hunt over R133+R134 immediately after landing them. Six
real defects, all in the same class: the tree faithfully reported what was
logged, and what was logged was incomplete.

- **R134a — a fold logged no numbers.** `compact` recorded `folded` (a
  MESSAGE count) and nothing about size, so the one row whose entire purpose
  is the drop rendered without it and the context spine just stopped
  descending. `engine.compact_history()` already computes the post-fold
  estimate on the line above the log call; it now records `used_before` /
  `used_after`, and the row reads `folded 18 messages · 12k → 3.4k`.
- **R134b — the header's `ctx` ignored folds.** It took the last *turn's*
  `input_tokens`, so any session ending on `/compact` reported the PRE-fold
  size — overstated by the whole drop, in the single number a reader glances
  at to answer "where am I now". It now takes the last context figure in
  record order, whichever kind of row produced it.
- **R134c — approvals attached by position.** A `stopped` outcome answers
  the remaining calls internally (`_flush`, never `cb.on_tool_result`), so
  it has no `tool` record behind it — and the marker drifted onto whatever
  row came next, labelling an unrelated tool with someone else's decision.
  The `approval` record already carried `tool`; it is now matched on it, and
  an outcome with no result behind it renders as its own row.
- **R134d — no session total.** Per-turn `$` with no sum defeats the point
  of putting money on the tree. The header now totals it, on `/cost`'s exact
  basis and the same deliberate upper bound — with a trailing `+` when any
  turn ran on an unpriced model, because a bare figure there would read as
  the whole bill rather than a floor.
- **R134e — a typo'd session id read as an empty session.** `/context
  nosuchid` said "has nothing logged yet", which is a statement about a
  session that exists. It now distinguishes the two. `/context <n>` also
  became a turn count (session ids are 12 hex chars, so a bare number can't
  shadow one).
- **R134f — the whole log was parsed.** `iter_records()` was called with no
  event filter, so every `tool` record's multi-KB `output` was
  `json.loads`-ed just to read a name — the exact cost R96e's substring
  pre-filter exists to avoid. The six event types this view reads are now
  named.

Tests: `::test_compact_logs_the_drop_it_causes`,
`::test_context_tree_renders_a_compact_as_a_visible_drop`,
`::test_context_tree_header_ctx_follows_a_trailing_compact`,
`::test_context_tree_keeps_an_orphan_approval_on_its_own_row`,
`::test_context_tree_totals_cost_and_flags_an_unpriced_model`, plus the
updated `::test_context_tree_report_parses_its_arguments`.

#### R134g. The three the review pass deferred (2026-07-25)
All three were reported as findings under R134a–f and left alone as
out-of-scope; taking them properly.

- **Abandoned tool calls now reach the log.** A stop at the approval gate, a
  stop at the iteration cap, and a Ctrl+C mid-round each answered their
  remaining calls by extending `round_out` directly — history stayed valid,
  but `cb.on_tool_result` was bypassed, so those calls produced **no session
  record at all**. The log said a turn made one tool call when the model had
  asked for three, and `/context`'s `🔧` undercounted every stopped turn.
  A shared `_skip(calls, reason)` routes them through the callback. This
  **does** change the transcript: the skipped calls now appear as
  `[skipped: …]` rows where before they were silent. That is the honest
  display — the model asked, and this is what happened to the request —
  and it is why the change is its own entry rather than a quiet fix.
- **`result_status` required the marker to bracket the WHOLE output.**
  Reading an application log whose first line is `[error: …]` was reported
  as a failed tool call. Every marker Aurora produces is a single bracketed
  form and nothing else, so the closing `]` is always last — including after
  `run_tool`'s truncation notice, which ends in one too. Residual ambiguity
  is irreducible from the outside (a file that both opens with a marker and
  ends with `]` still reads as an error); only the producer could be
  certain, and it does not say.
- **Status-bar line 1 fits again.** It holds identity (R56) and had reached
  ~117 characters with a real model id — past 80 columns, where
  prompt_toolkit clips it and the rightmost links stop being clickable.
  `_short_model()` drops the vendor prefix (`moonshotai/kimi-k2.7-code` →
  `kimi-k2.7-code`), which never disambiguates anything on screen and is the
  cheapest 11 characters there; the full id is one click away in the picker.
  **Unless it does disambiguate**: two configured models sharing a short
  name keep their full ids, since a status bar that can't tell you which one
  you're on is worse than a long one. Net 106 chars — below the 107 the bar
  measured BEFORE the cost-tree link was added.

Tests: `::test_result_status_needs_the_marker_to_bracket_the_whole_output`,
`::test_a_stopped_turn_logs_the_calls_it_never_ran`,
`::test_an_interrupted_round_logs_its_skipped_calls`,
`::test_iteration_cap_stop_logs_its_skipped_calls`,
`tests/test_tui.py::test_status_bar_drops_the_vendor_prefix_from_the_model`,
`::test_status_bar_keeps_the_vendor_prefix_when_it_disambiguates`.

#### R134h. Repeated tool rows collapse (2026-07-25)
Found by using it: the very first real `/context` was a bootstrap turn with
14 calls, and the tree spent 14 lines saying `read_file` and `run_command`.
One repeated word filled the screen and buried the rows that differed.

Consecutive identical rows now render as `read_file (x7)`. Two rules make it
informative rather than merely shorter:

- **Identity is the WHOLE row** — name, status *and* approval decision. A
  failure is never absorbed into a run of successes, and an approved call
  never merges with a denied one. Those outliers are precisely what a reader
  scans for.
- **Only ADJACENT rows merge.** A single `✗` in the middle of ten
  `read_file`s splits them into `(x3)`, `✗`, `(x7)` — which says more than
  `(x10)` would, not less. That was the shape of the real session that
  prompted this.

`🔧 n` still counts CALLS, not rows, so the badge line and the tree can't
disagree about how much happened.

Tests: `::test_context_tree_collapses_consecutive_identical_tool_rows`,
`::test_context_tree_never_merges_across_a_different_approval`.

### R135. The prompt field and the status bar as click targets (2026-07-25)
Four small affordance fixes from one dogfooding pass. Theme: things that
*look* like they should be selectable or clickable now are, and the status
bar's line 1 stopped naming the same thing twice.

- **R135a — double-click in the prompt selects the whole draft.**
  prompt_toolkit's `BufferControl` selects the word under the cursor on a
  double-click. A prompt draft is one thought you retype or copy wholesale,
  not prose you edit word by word, so the gesture now selects everything;
  word-and-range selection is still reachable by dragging. Implemented in
  `tui.Tui._input_mouse_handler`, which **wraps** `self.input.control.
  mouse_handler` rather than subclassing `BufferControl` — `TextArea` builds
  its control internally, so there is no constructor to hook. Every event but
  the second `MOUSE_UP` inside `_DOUBLE_CLICK_S` (0.3s, prompt_toolkit's own
  window) passes through untouched, including the double-click's own
  `MOUSE_DOWN`s — those just move the cursor and drop the selection, which
  the handler then redoes.
- **R135b — "copy selected" serves the prompt field too.** The button (R48)
  only ever copied a frozen *chat* drag; a selection in the prompt had no way
  to reach the clipboard. It now copies the prompt's selection when there is
  one, and the chat's otherwise. **Only one of the two is ever live**:
  `sel_begin()` clears the prompt's selection and the double-click clears
  `_sel_frozen`, so a single button is never ambiguous about which pane it
  copies. `_input_sel_text()` reads through `buffer.document.cut_selection()`
  and *not* `Buffer.copy_selection()` — the latter clears `selection_state`
  as a side effect, and the status bar calls this on every render tick to
  decide whether to draw the button at all, so that shape would have made
  rendering the button destroy the thing it copies.
- **R135c — the `ctx` gauge IS the cost-tree link.** R134 gave the tree its
  own ` cost tree` label sitting after the price. But the gauge already names
  what the tree breaks down, so the row carried two words for one thing on a
  line that has to fit 80 columns (the constraint R134g was about). The label
  is removed and `ctx 12k/200k - 41%` carries the handler; behavior is
  otherwise identical (still queues `/context` on the inbox — see R134). A
  `│` now separates it from the model name so line 1's two click targets read
  as two buttons rather than one run-on label. The separator is written
  `" │{cost} "` so an empty price slot (local models report no cost) can't
  leave a doubled `│  │`.
- **R135e — the price gets its own `│` section, without parentheses.** It
  had been a parenthetical trailing the model name (`kimi-k2.7-code
  ($1.50)`), which reads as an aside about the model — but it's the one
  number on the row that is real money, and it belongs to the ctx/cost group
  the gauge links into, not to the model. Now `model │ $1.50 │ ctx 12k/200k
  - 41%`. The separator ships **with** the price (`f" │ ${…}"`) rather than
  being emitted around it, because a local model reports no price at all and
  a standalone empty section would render a doubled `│  │`.
- **R135d — `/exit` and `/quit`, confirmed identical.** Already true in
  `ui._handle_command` and already in `/` autocomplete, but untested, so
  nothing stopped a future edit from handling only one. Now locked by a test.

Tests: `tests/test_tui.py::test_double_click_in_the_prompt_selects_the_whole_
draft`, `::test_two_slow_clicks_in_the_prompt_are_not_a_double_click`,
`::test_copy_selected_copies_the_prompt_selection`,
`::test_copy_selected_button_appears_for_a_prompt_selection`,
`::test_rendering_the_button_does_not_destroy_the_prompt_selection`,
`::test_only_one_selection_is_live_at_a_time`,
`::test_status_bar_ctx_gauge_is_the_cost_tree_link`,
`::test_status_bar_separates_the_model_from_the_ctx_gauge`,
`::test_status_bar_gives_the_price_its_own_bare_section`,
`::test_model_name_and_ctx_gauge_run_their_own_commands`,
`tests/test_core.py::test_exit_and_quit_are_the_same_command`.

### R136. `refresh_model_prices` — a bundled extension, batch price refresh (2026-07-25)
`/model add` (R80) already looks a model up in OpenRouter's public catalog
and saves ctx/price into `remote_context_limits.json` — but only for the one
model being added. A model configured before pricing existed, or whose
listed price has since changed on OpenRouter, had no way to refresh short of
removing and re-adding it. `refresh_model_prices` is a model-callable tool
(bundled, `aurora/extensions_bundled/price_refresh_extension.py`) that walks
every `provider: openrouter` entry in config.yaml's `models:` list and
re-pulls each one's ctx/price from the catalog. The local model is skipped —
it has no OpenRouter listing and no price to refresh.

- **One catalog fetch, not one per model.** `fetch_openrouter_model_info`
  (R80) does its own `GET /api/v1/models` per call — fine for adding one
  model, wasteful for refreshing N configured ones (each a 10s-timeout round
  trip). `openai_compat.py` now splits the fetch out as
  `_fetch_openrouter_catalog()` and adds `refresh_prices_for(model_ids)`,
  which fetches the catalog exactly once and matches every id against it
  locally; `fetch_openrouter_model_info` is rewritten in terms of the same
  split so there's one source of truth for parsing a catalog entry
  (`_model_info_from_catalog_entry`).
- **Model-callable, not automatic** — same posture as `lint_check` (R121):
  it runs when the model chooses to call it, nudged by the tool's own
  description ("call this if a status-bar price looks stale…"). Aurora's
  extension mechanism has no lifecycle-hook system yet (EXTENSIONS.md's
  "what's scoped out"), so there is no "refresh automatically on startup"
  version without that being built first.
- **The `_SPEC` naming trap.** `extensions.discover()` merges a module's
  top-level `SPEC`/`RUNNERS` *and* whatever `register()` returns, since a
  file may use both the static and dynamic style at once (EXTENSIONS.md).
  The tool table here is named `_SPEC` (leading underscore) specifically so
  it is NOT picked up as a top-level `SPEC` too — naming it `SPEC` got the
  same tool registered twice (once runner-less via the static path, once via
  `register()`), tripping `tools.set_extensions()`'s duplicate-name guard
  and dropping the tool from `_EXTENSION_SPECS` entirely. Caught by two
  existing MCP tests that assert `engine.extension_warnings == []` — a
  reminder that "add a bundled extension" isn't as isolated a change as it
  looks; anything asserting an exact extension/warning count needs a look.

Tests: `tests/test_price_refresh_extension.py` (7 — no-models-configured,
skips the local model, refreshes+saves each configured model, catalog-
unreachable is reported distinctly from a per-model miss, registered as a
bundled extension, the single-fetch-for-N-models batch behavior).
`tests/test_mcp.py::test_lint_check_always_bundled_by_default` updated for
the new bundled-tool count (1 → 2).

### R137. A blank row between the status bar's two lines (2026-07-25)
R56's two content lines (identity / tooltips-or-transient) sat directly on
top of each other with no breathing room. A terminal is a fixed character
grid — there is no fractional line-height, so "more space between them"
can only mean a full blank row, not a few extra pixels. The status
`Window` grows from `height=2` to `height=3`; `status()`'s line-1 fragment
list now ends `"\n\n"` instead of `"\n"`, and the `/nano` "type 2" toolbar
(`_nano_status_fragments`, R110) gets the identical blank row so its
two-line block matches the same 3-row window rather than leaving a
dangling empty line. R56's content model is unchanged — still two lines
of content, just visually separated now.

Tests: `tests/test_tui.py::_status_line2` (helper used by ~6 existing
tests) updated to read line index 2 instead of 1;
`::test_nano_status_actions_on_line1_underlined_filename_on_line2_plain`
updated to split on `"\n\n"`.

### R138. Deep-dive review of R126–R137: 7 real bugs found and fixed (2026-07-26)
A dedicated bug-hunt over everything from R126 through R137 (MCP, the cost
tree, the R135–R137 click-target work, the price-refresh extension) —
parallel review passes over four areas, each finding verified against the
actual code before fixing, each fix given a regression test confirmed to
fail on the pre-fix code.

- **R138a — `_input_click_at` wasn't consumed after firing (R135f).** The
  double-click-selects-all gesture (R135a) recorded the firing click's own
  timestamp, so a third click shortly after (meant to place the cursor)
  paired with THAT click and re-selected the whole draft — the user could
  never place a cursor right after a double-click. Fixed by resetting
  `_input_click_at` to 0.0 the moment the gesture fires, in
  `Tui._input_mouse_handler` (`aurora/tui.py`).
- **R138b — a plain drag inside the prompt didn't drop a frozen chat
  selection (R135f).** R135b's "only one selection is ever live" invariant
  was enforced on the double-click branch only; a plain click-drag in the
  prompt skipped it, so a frozen chat selection and a fresh prompt one
  could be lit up together, and "copy selected" became ambiguous about
  which one it would copy. Fixed: any `MOUSE_DOWN` in the prompt now clears
  `_sel_frozen`, mirroring `sel_begin()`'s clear for the chat side.
- **R138c — a free/zero-priced model's price was never written
  (R136).** `save_remote_model_info`'s field-copy used `if info.get(k)`,
  so a genuinely free model (`price_in_per_mtok == 0.0`) read as "the
  catalog said nothing" and was silently skipped — any stale non-zero
  price from before stuck around forever. Fixed in the new
  `_merge_model_entry` (`aurora/providers/openai_compat.py`): numeric
  fields (`context_size`, both price fields) now use `is not None`;
  `description` stays a truthiness check on purpose (an empty string
  overwriting a real description would be a regression, not a fix).
- **R138d — a malformed `context_size` could abort refreshing every OTHER
  model in the same call (R136).** `int(info["context_size"])` raised
  uncaught, both in the storage layer and in the extension's own display
  formatting. Both call sites now catch `(TypeError, ValueError)` and drop
  just that one field/model instead of the whole tool call.
- **R138e — refreshing N configured models did N full read-modify-write
  cycles of `remote_context_limits.json` (R136).** `save_remote_model_info`
  in a loop meant N models = N full-file rewrites, and a crash or Ctrl+C
  between any two of them left the file — and every OTHER already-refreshed
  model's price, not just the one in flight — half-written. New
  `save_remote_model_infos` batches one read + one write for all of them;
  `price_refresh_extension.py` calls it once instead of looping the
  single-model function. The extension also now de-duplicates `model_ids`
  (`dict.fromkeys`) so two config.yaml entries for the same id aren't
  reported/saved twice.
- **R138f — `select` on `grep`'s stdout only, never stderr — a
  stderr-heavy search could deadlock into a false timeout (R127).**
  `grep -rnI` over a tree with unreadable dirs/files prints a "Permission
  denied" line per miss; enough of them fill stderr's pipe buffer, at
  which point grep blocks WRITING to it and produces no more stdout
  either. `select()`, watching only stdout, never fires, and a search
  that would have finished instantly burns the entire `GREP_TIMEOUT` and
  reports a false timeout. Fixed in `tools.grep`: `select` now watches
  both fds, draining stderr incrementally (capped at 4KB — only the first
  line is ever shown) instead of via a single bounded read in `finally`,
  which itself could have hung with no deadline if grep were still alive
  holding stderr open.
- **R138g — the MCP read deadline was computed once and never refreshed
  (R126).** A server that is genuinely alive and doing real work —
  streaming `notifications/progress` lines while a slow tool call runs —
  got killed for taking longer than `self.timeout` even though data kept
  arriving the whole time. R126's own rationale ("don't kill a healthy
  server") argues for judging liveness by "is data still arriving," not a
  fixed line computed before the call started. `MCPServer._read_response`
  now resets `deadline` on every successfully read chunk, not just a
  matching reply; a truly silent/hung server never reaches that line and
  still times out exactly as before.
- **R138h — `MCPServer._send` could silently truncate a large request
  (R126).** R126 switched stdin to a RAW, unbuffered pipe (`bufsize=0`),
  whose `write()` is explicitly allowed to return fewer bytes than given —
  unlike a `BufferedWriter`, which loops internally on `flush()`. A payload
  bigger than one short write's worth (a long patch, a big JSON tool
  argument) could get silently truncated mid-write, and the child would
  see malformed JSON. New `MCPServer._write_all` loops until everything is
  sent.
- **R138i — a second gate outcome with no tool record silently overwrote
  the first, in the cost tree (R134).** `ctxtree._collect`'s
  `turn.pending_approval` was a single-slot field, unconditionally
  assigned. Two `approval` records in a row with no `tool` record between
  them (e.g. `denied_policy` then `stopped` — the model proposed two calls,
  the gate answered both internally) dropped the first decision entirely —
  not just mis-attached it, the exact "a decision vanishes" class R134c
  was written to close, just from the other direction. Fixed: a pending
  approval is now flushed as its own orphan row before being replaced.
- **R138j — `covers()` said yes for an in-tree path a checkpoint
  provably never snapshots (R130).** `checkpoint()` runs `git add -A`,
  which skips anything `EXCLUDES` marks (`dist/`, `.venv/`,
  `node_modules/`, caches, `.DS_Store`, `*.pyc`) — a write to
  `dist/config.json` is just as unrecoverable as one outside the tree
  entirely, the exact false assurance R130 exists to prevent. `covers()`
  now also checks `EXCLUDES` via a new `_excluded()` helper
  (`aurora/rewind.py`). Deliberately scoped down from a full fix: it does
  NOT also check the project's own arbitrary `.gitignore` patterns, which
  would need a `git check-ignore` subprocess call on every
  approval-prompt render — a much larger, riskier change to a
  safety-messaging path. A project-specific ignore pattern outside
  `EXCLUDES` is a known residual gap, not silently claimed to be handled.
- **R138k — the ⚡ cache-hit badge could exceed 100% on a pre-R92 log
  (R134).** `billed` (this round's prompt) and `cached` (the multi-round
  cumulative figure) aren't the same denominator on a log recording a
  multi-tool turn before `billed_input` existed, so the ratio could read
  e.g. `⚡240%`. Clamped at 100% in `ctxtree.py` — a display artifact of
  old data, not a real discount over 100%.

**Reviewed and deliberately left alone** (documented here so they aren't
re-discovered as "new" later): `ctxtree._collect` re-parses the entire
session JSONL on every render/click regardless of `DEFAULT_TURNS` — real
scaling cost on a long-lived session, but fixing it properly needs a
tail-read design, a larger change with real risk to the ordering-sensitive
turn/approval/tool matching logic; a header showing whole-session totals
above a 20-turn slice (ambiguous but not wrong); records logged before a
session's first `user` record are dropped (resumed/bootstrap-ordering
edge case); `price_refresh_extension`'s provider-name match is
case-sensitive (consistent with how the rest of the codebase treats
`provider:` — case-insensitivity introduced in one isolated place would be
the inconsistency); `refresh_prices_for`'s catalog-id matching is
last-wins on a duplicate id (OpenRouter's catalog has never been observed
to have one).

Tests: `tests/test_tui.py::test_third_click_after_a_double_click_does_not_
reselect_everything`, `::test_a_plain_drag_in_the_prompt_drops_a_frozen_
chat_selection`; `tests/test_core.py::test_save_remote_model_info_a_real_
zero_price_is_not_dropped`, `::test_save_remote_model_info_malformed_
context_size_does_not_raise`, `::test_save_remote_model_infos_is_one_read_
and_one_write_for_n_models`, `::test_rewind_covers_says_no_for_an_in_tree_
but_gitignored_path`, `::test_approval_preview_warns_for_a_gitignored_in_
tree_write`, `::test_context_tree_cache_badge_clamps_at_100_percent`,
`::test_context_tree_keeps_two_orphan_approvals_in_a_row`,
`::test_grep_does_not_deadlock_on_a_stderr_heavy_search`;
`tests/test_mcp.py::test_write_all_retries_a_short_write_until_everything_
is_sent`, `::test_mcp_deadline_resets_while_a_slow_server_keeps_streaming_
progress`; `tests/test_price_refresh_extension.py::test_saves_in_one_
batch_call_not_one_per_model`, `::test_a_genuinely_free_model_reports_and_
would_persist_zero_price`, `::test_malformed_context_size_does_not_abort_
the_whole_refresh`, `::test_duplicate_config_entries_are_not_double_
processed`. Every fix above was verified to fail on the pre-fix code before
being counted as caught.

### R139. Backspace deletes the whole selection, not one character (2026-07-26)
R135a made a double-click in the prompt select the WHOLE draft, on the
reasoning that a draft is one thought you replace or copy wholesale. Copying
it worked; replacing it did not. Backspace is one of the TUI's custom key
bindings (it has to be — backspace on an empty `$` prompt is what leaves
bash mode, R109), and its else-branch called `buf.delete_before_cursor()`
unconditionally. That is prompt_toolkit's *primitive*, not its default
backspace behavior: the library's own binding deletes an active selection
first. So overriding the key silently dropped selection-awareness, and
backspace on a fully-selected draft erased a single character instead of
the selection — the "select all, then retype" half of R135a's premise never
worked. The binding now checks `buf.selection_state` and calls
`buf.cut_selection()` when a selection is live (`aurora/tui.py`).

The general lesson, worth more than the fix: every custom binding that
overrides a stock editing key inherits responsibility for the default
behavior it displaced. Backspace was the one that mattered because R135a
created a selection the user is expected to type over.

Tests: `tests/test_tui.py::test_backspace_after_double_click_deletes_the_
whole_selection` — verified to fail on the pre-fix code.

### R140. `/context` dropped an approval the log stops after (2026-07-26)
`ctxtree._collect` holds an approval record back so it can be merged onto
its tool's row — the decision belongs next to the call it gated, not on a
line of its own. Every path that ends a turn therefore has to release what
is still pending, and R138i already fixed one such miss (a second approval
arriving before the first one's tool record). Three more were left: the
`compact` and `model_switch` markers both drop the current turn without
flushing, and so does simply running out of records.

The last one is the reachable one. A session log is append-only, so a
process killed between an approval record and its tool record — approve a
long `run_command`, close the terminal — just ENDS there. Re-reading that
session with `/context <id>` rendered it one row short, silently losing
"the user was asked, and answered". The three sites now share a
`_flush_approval()` helper with the existing assistant path
(`aurora/ctxtree.py`).

Tests: `tests/test_core.py::test_context_tree_keeps_an_approval_the_log_
simply_stops_after`, `::test_context_tree_keeps_an_approval_a_compact_
marker_follows` — both verified to fail on the pre-fix code.

### R141. The allowlist could be bypassed with a shell operator (2026-07-26)
**The most serious bug found in Aurora so far.** `run_command` executes with
`shell=True`, but the allowlist matches on `shlex.split` tokens — and
`shlex` has no concept of shell operators. It treats `&&`, `||`, `|`, `>`,
`<` as ordinary WORDS and a newline as plain whitespace. So `ls && rm -rf ~`
tokenized to `("ls", "&&", "rm", "-rf", "~")`, whose first token prefix-
matched a stored `ls` rule, and `is_allowed` returned True. **No approval
prompt was shown at all.**

One "always allow" on anything read-only — `ls`, `cat`, `grep`, any
`SAFE_COMMANDS` entry, which R67 generalizes to a bare command name
precisely because those "cannot write, delete, or execute arbitrary code" —
was enough to hand the model an unprompted shell for the rest of that
machine's life, since allowlist.yaml persists. Every vector worked: `&&`,
`|`, `>`, `>>`, `` ` ``, `$(`, and a literal newline. `;` happened to fail
only by the accident that `shlex` glues it to the preceding token
(`status;`) — spaced as `ls ; rm -rf ~` it worked too. The code comment
"`rm` must never auto-approve `rm -rf /`" was true and was also guarding
only the first token.

Fix (`aurora/approve.py`): `_is_compound()` tests the RAW command string —
not its tokens, which is exactly where the information was being lost —
against `_SHELL_OPS`. `is_allowed` now passes `strict=True` into
`_matches`, which disables BOTH generalizations (the two-token prefix and
the `SAFE_COMMANDS` bare-name match) for a compound command; only an exact
whole-command rule may match. `_rule_for` correspondingly stores a compound
command whole, so "always allow" on a pipeline the user genuinely read
still works — it just cannot grow a different tail.

`strict` is deliberately allowlist-only. The denylist shares `_matches`,
and applying the same restriction there would have opened the mirror hole:
a denied `rm -rf` must still be denied when chained. Both directions stay
fail-closed, toward opposite answers.

`$` alone is not in `_SHELL_OPS` (`echo $HOME` is expansion, not execution,
and is far too common to re-prompt on); `$(` is. A quoted-but-harmless
`grep 'a|b' f` does get caught by the raw-string test and re-prompts. That
trade is intended: on a security gate a false prompt costs a keystroke, a
false auto-approval costs the filesystem.

Tests: `tests/test_core.py::test_allowlisted_prefix_never_approves_a_
chained_command` (all nine vectors), `::test_always_allow_on_a_pipeline_
matches_it_exactly_and_nothing_more`, `::test_denylist_still_catches_a_
chained_command` — the first two verified to fail on the pre-fix code.

### R142. Two TUI states that outlived their trigger (2026-07-26)
Both from one audit pass, same theme: a modal flag set by a gesture, with
no path back out of it.

- **R142a — a challenge opened over an Esc-Esc confirm deadlocked the
  worker, permanently.** The Esc-Esc confirms (`_open_ui_menu`, UI thread,
  R62) and the blocking challenges (`select_menu`, worker thread) share ONE
  menu slot but resolve by different routes, and `_resolve_menu` checks
  `_menu_on_select` FIRST. `select_menu` overwrote `_menu_prompt` and
  `_menu_options` without clearing that callback. So: a turn is running, the
  user taps Esc-Esc ("Cancel this?" opens, by design it waits for an
  explicit pick), then the agent hits an approval gate — the approval menu
  replaces it on screen, the user picks "Yes", and `_resolve_menu` hands
  that answer to the *cancel* resolver, which ignores it. Nothing is ever
  put on `_answers`, so the worker blocks forever mid-turn with `_busy`
  stuck on. The only way out was quitting. `select_menu` now clears the
  stale callback (and says so in the transcript) as it takes over: dropping
  an unanswered confirm loses a question the user can ask again, keeping it
  loses the session.
- **R142b — one stray Esc made the next message vanish.** A single Esc on an
  idle empty prompt sets `_exit_confirm`, and nothing cleared it except the
  next Enter — which `_submit` then consumed as the ANSWER to the quit
  question. Type `explain this traceback`, press Enter, get `· staying`: the
  message was never sent, and the `_exit_confirm` guard had also skipped
  `append_to_history()`, so up-arrow couldn't get it back either. R104 had
  removed the line-2 hint, so the state was completely invisible while it
  ALSO froze `_click_guard` (`?`, `/ commands`, `! bash` all dead) and made
  Esc stop clearing the draft — which reads as a hung UI. Typing now
  dismisses it (`_typed_over_exit`, hooked to `on_text_insert`), the same
  way R62 already treats an Esc landing in a different state. `on_text_insert`
  specifically, not `on_text_changed`: the latter also fires for the
  programmatic `buffer.reset()`/`buffer.text = draft` in `_submit` and the
  ask/menu teardowns, which would clear the flag before `_submit` could read
  it. Note the existing `test_esc_hint_expires_back_to_tooltips` already set
  `_exit_confirm = False` by hand with the comment "user typed/state moved
  on" — the test assumed a clearing path the code never had.
  **Behavior change worth knowing:** answering the quit question by typing
  `y` no longer works, because typing now dismisses it. That path was
  undiscoverable anyway once R104 removed the hint; Esc-Esc → explicit menu
  is the documented gesture (R62).

Tests: `tests/test_tui.py::test_a_worker_menu_opened_over_an_esc_confirm_
still_gets_its_answer`, `::test_typing_dismisses_a_pending_exit_confirm`,
`::test_a_message_typed_after_a_stray_esc_is_actually_sent` — all three
verified to fail on the pre-fix code (the first by genuinely hanging).

### R143. An abandoned turn was recorded as if it had answered (2026-07-26)
Three findings from one pass, all the same shape: a turn that ended early —
by exception, by "stop", by a secret challenge — left `send()`'s
bookkeeping describing a turn that completed normally.

- **R143a — an exception escaping `run_turn` skipped the dangling-user
  cleanup.** `run_turn` deliberately lets two exceptions out: a
  `ProviderError` raised by the corrective retry *inside* `except
  MalformedToolCall` (the outer `except ProviderError` can't see it, being
  in the same try), and the bare re-raise when tools are already degraded.
  `Engine.send` had no `try` around the call, so those flew past the
  `if self.messages[-1] is user_msg: pop()` cleanup, leaving `messages`
  ending on a `user` entry. The next `send` appended a second one — two
  consecutive user messages, exactly what R44/R95e/R128 exist to prevent,
  and most APIs reject the request outright one turn away from the cause.
  Reachable: a 500 whose body says "parse" → `MalformedToolCall` → retry →
  a stale pooled keep-alive → `ProviderError`. Now caught, popped,
  re-raised. (`tests/test_core.py::test_malformed_retry_error_leaves_no_
  nudge_in_history` asserted this escape from the *agent* side and never
  looked at what the engine did with it.)
- **R143b — a `tool` message was logged as the assistant's reply.** All four
  early-return paths (iteration cap, "stop" at the approval gate, interrupt
  mid-round, secret-stop) end with `_flush()`, which appends tool results —
  so `messages[-1]` is a `tool` message, and `_assistant_text` happily
  returns its string content. The session recorded `assistant
  text="[skipped: user stopped the turn]"`: the skip marker as the model's
  answer, in the markdown export, and — worst — replayed as a genuine
  assistant message by `--continue` (`resume_from`). R95e's guard only asked
  *whether* history grew, never whether the tail was an assistant message.
  Now takes the last actual `role == "assistant"` message (on a normal turn
  that IS `messages[-1]`, so nothing changes). The record is still written:
  the tokens were really spent and `/cost` reads it.
- **R143c — the fourth abandon site R134g never swept.** R134g routed three
  abandon paths through `_skip()` so they reach `cb.on_tool_result` and get
  a session record; it was fixing three specific reported findings, not
  sweeping, and the secret-stop path still extended `round_out` directly.
  Same consequence it fixed elsewhere: the remaining calls produced no
  session record and `/context`'s 🔧 undercounted. Converted. The secret
  itself is not exposed by this — `_skip` passes only the `[skipped: …]`
  marker, never the withheld output, which is the property the challenge
  actually guarantees and which the updated test now asserts directly.

Tests: `tests/test_core.py::test_an_exception_escaping_run_turn_still_pops_
the_user_message`, `::test_a_turn_stopped_at_the_gate_does_not_log_a_skip_
as_the_answer`, and `::test_tool_output_stop_halts_the_turn` (rewritten from
asserting the bug's silence to asserting the secret's absence plus the skip
row's presence) — all three verified to fail on the pre-fix code.

### R144. Two ways a tool could kill the turn instead of returning (2026-07-26)
- **R144a — `run_command` raised on non-UTF-8 output.** `Popen(text=True)`
  decodes as STRICT UTF-8, so any command emitting a non-UTF-8 byte — a
  latin-1 log, a binary blob, a tool printing raw bytes, anything under
  `LANG=C` — raised `UnicodeDecodeError` and lost the WHOLE output,
  including the part that decoded fine. `read_file` and `grep` both already
  pass `errors="replace"` deliberately; `_run_command_once` was the one that
  didn't. It also matters more there: bash mode calls it directly, outside
  `run_tool`'s exception guard, so the exception was unhandled rather than
  merely converted into a lost result.
- **R144b — a non-`str` return from an extension killed the turn.**
  `run_tool`'s `try/except` wrapped only the runner call; the
  `len(out) > TOOL_OUTPUT_LIMIT` truncation check sat just outside it. A
  user-authored extension runner returning `None`, a `dict` or an `int` — an
  easy mistake, since `extensions.py` never states str as a hard contract —
  raised `TypeError` from `len()` and propagated into `agent.py`. That is
  precisely the failure the guard's own comment describes preventing: the
  assistant message already carries the `tool_use`, and a missing tool
  result invalidates every later request in the session. Coerced inside the
  guard instead.

Tests: `tests/test_core.py::test_run_command_survives_non_utf8_output`,
`::test_run_tool_does_not_die_on_an_extension_returning_a_non_string` —
both verified to fail on the pre-fix code, with `UnicodeDecodeError` and
`TypeError` respectively.

### R145. Two leaked OS resources (2026-07-26)
- **R145a — a failed MCP handshake leaked the child process, forever.**
  `MCPServer.__init__` spawns the child, then runs `_initialize()` /
  `_discover_tools()`. Only the TIMEOUT path (inside `_read_response`) called
  `_kill_unresponsive()`; every other failure — a JSON-RPC `error` reply (a
  protocol-version mismatch, the case
  `test_mcp_server_raises_on_a_json_rpc_error` already exercises), "closed
  the connection", a `BrokenPipeError` — propagated out of `__init__` with
  the process still running. And because the constructor raised,
  `MCPManager` never stored the object, so `close_all()` and the `atexit`
  hook could not reach it either. One live child plus its pipes leaked per
  misconfigured server, per session, for the life of the process. The
  handshake is now wrapped: any escape kills the child first.
- **R145b — a `/model` switch across providers stranded the old HTTP pool.**
  `_provider_for` rebuilds the provider and reassigns `self._provider`. The
  discarded one owns `self._http: dict[str, httpx.Client]`, one pooled
  client per endpoint, each holding live keep-alive sockets; nothing closed
  them. R96j already closes the losing racer one level down in
  `_client_for` for exactly this reason — the reasoning applies to the whole
  provider. New `_close_provider()` helper, deliberately exception-proof:
  failing to tidy up the old provider must never fail building the new one.

Tests: `tests/test_mcp.py::test_a_failed_handshake_does_not_leak_the_child_
process` (new fixture `tests/fixtures/refusing_mcp_server.py` — it refuses
`initialize` and then sleeps, so an exited process is proof it was killed
rather than orphaned); `tests/test_core.py::test_switching_provider_closes_
the_old_pooled_connections`, `::test_closing_a_provider_never_raises`. The
first two verified to fail on the pre-fix code.

### R146. Persistence durability: atomic writes, explicit UTF-8 (2026-07-26)
- **R146a — every YAML persist could truncate the live file.**
  `persist_runtime_value`, `persist_model_entry`, `remove_model_entries`,
  `save_state_values`, `approve.save` and `approve.save_deny` all did
  read → parse → dump → `write_text()` **over the live file**: truncate,
  then write. A crash, `^C`, `ENOSPC`, or a kill inside that window leaves
  `config.yaml` — the committed file holding every provider and model —
  empty or half-written, and `load_config` then fails at the next start.
  Not theoretical: `persist_runtime_value` fires mid-turn from
  `/redact allowlist`. New `paths.write_text_atomic()` writes a sibling
  temp file, `fsync`s it, and `os.replace`s it into place; all six call
  sites use it. A sibling, not `/tmp`, because `os.replace` is only atomic
  within one filesystem; the `fsync` is what makes it survive a power loss
  rather than merely a crashed process.
- **R146b — the session log used the locale encoding (hardening).** The
  JSONL was opened with no `encoding=`, i.e. the platform locale, while
  written with `ensure_ascii=False`. Under `LANG=C`/`POSIX` — cron, CI,
  minimal containers — the first non-ASCII prompt or tool output would raise
  `UnicodeEncodeError` from inside `Engine.send`; a log written on a UTF-8
  machine would also be unreadable on one that isn't, and `iter_records`
  catches only `JSONDecodeError`, not `UnicodeDecodeError`, so `/resume`,
  `/cost` and `list_sessions` would crash rather than skip. Now explicit
  UTF-8 on the write and UTF-8 + `errors="replace"` on both reads.
  **Honestly labelled**: this is not reproducible on macOS, where CPython
  forces UTF-8 for `open()` regardless of locale, so the accompanying test
  documents and locks the behavior but does NOT fail on the pre-fix code
  here. It is a real defect on Linux and correct by construction either way
  — every other file Aurora reads/writes should get the same treatment if
  this recurs.

Tests: `tests/test_core.py::test_a_crash_mid_persist_never_truncates_the_
live_config` (fails pre-fix with "DID NOT RAISE OSError" — the old path has
no atomic commit point to fail at), `::test_atomic_write_leaves_no_temp_
files_behind`, `::test_session_log_round_trips_non_ascii_under_a_c_locale`
(see the caveat above).

### R147. The 429 backoff ignored Esc for up to 30 seconds (2026-07-26)
`openai_compat.py` is scrupulous about cancellation everywhere else —
`cancellable_sse` polls every 0.15s specifically so "the caller always
unblocks within `poll` seconds of a cancel". The R99 rate-limit retry was
the one blocking point that didn't: a bare `time.sleep(wait)`, where `wait`
is a server-provided `Retry-After` clamped to `_RATE_LIMIT_BACKOFF_CAP`
(30s). So on a free-tier 429 answering `Retry-After: 120`, Aurora slept a
solid 30 seconds during which Esc-Esc → confirm cancel did **nothing** —
while the ticker thread kept the spinner animating, so it read as a hang
rather than as a wait. The cancel was only noticed on the next attempt.

`_sleep_unless_cancelled(wait, cancel)` waits in `_CANCEL_POLL_S` (0.15s)
hops — the same cadence `cancellable_sse` uses — and returns True the
moment `cancel()` goes true, at which point `turn()` returns a `cancelled`
result instead of retrying.

The five existing 429 tests asserted the backoff schedule by patching
`time.sleep` and comparing the recorded values; they now patch
`_sleep_unless_cancelled` and record its `wait` argument instead. That is a
better test regardless — it asserts the *schedule*, which is the actual
requirement, rather than the mechanism used to wait.

Tests: `tests/test_core.py::test_a_429_backoff_can_be_cancelled`,
`::test_sleep_unless_cancelled_waits_the_full_time_when_not_cancelled`.
Verified against the pre-fix code by observation rather than a quick assert:
the same test run there blocked for the full cap (repeatedly, past a 120s
command timeout) even though `cancel()` was already returning True — which
is precisely the reported symptom.

### R148. `_merge_char_runs` was quadratic in run length (2026-07-26)
`ANSI(...).__pt_formatted_text__()` emits one fragment per character, and
the merge loop rebuilt its accumulator with
`out[-1] = (f[0], out[-1][1] + f[1])` — a fresh string AND a fresh tuple
per character. Quadratic in run length, and CPython's in-place `+=` fast
path can't help because the target is a tuple slot, not a local. Each run
is now accumulated in a list and `"".join`ed once.

Measured here, not assumed: 1.088ms → 0.466ms (2.3x) on the 4096-char
`_MERGE_LIMIT` tail, and 6.18ms → 1.90ms (3.3x) at 16k — the ratio widening
with size, which is the quadratic showing itself. It matters because the
tail entry is re-parsed on every frame while streaming, so this is ~0.6ms
off every frame of every turn.

**Not R96l.** R96l measured and REJECTED two different things: lowering
`_MERGE_LIMIT`, and turning `Tui.append`'s `self._chat[-1] += s` into a list
buffer (rejected for touching six `isinstance(item, str/dict)` call sites).
This is a self-contained function with one call site and no change to
`_chat`'s shape.

An audit agent reported this as a 4.4x win; measuring it directly gave 2.3x
at the size that actually occurs. Recorded because the smaller honest number
is the one a future reader should compare against.

Tests: `tests/test_tui.py::test_merge_char_runs_matches_the_naive_
implementation` — differential against the previous implementation over 56
ANSI texts plus mouse-handler (3-tuple) fragments, since this rewrite is
only allowed to be faster, never different. Every downstream linkify pass
depends on its exact output.

### R149. `dd` and friends: "always allow" that generalizes over nothing (2026-07-26)
Raised by the user as "`dd` (also known as disk destroyer) should be treated
as dangerous as `rm -rf` and `sh`". Investigating it found the problem was
not a missing list entry but **the direction the two-token rule
generalizes in**. `_rule_for` stores the first two tokens of a command,
which for a destructive one is precisely the HARMLESS half, leaving the
target free to vary:

- "always allow" on `rm -rf ./build` stored `rm -rf` → thereafter
  auto-approved `rm -rf /`, `rm -rf ~`, `rm -rf /Users/<name>`, unprompted.
- "always allow" on `dd if=/dev/zero of=./img` stored `dd if=/dev/zero` →
  thereafter auto-approved `of=/dev/disk0`. The rule kept the *source* and
  generalized over the *destination* — the thing that gets destroyed.

The existing comment claimed two-token storage was "correct for anything
that can write/delete/execute, since a bare `rm` must never auto-approve
`rm -rf /`". True, and it only ever covered the SINGLE-token legacy case;
the two-token case walked straight into exactly that.

**R149a — `DANGEROUS_COMMANDS`, the exact mirror of `SAFE_COMMANDS`.** Where
a safe command generalizes across any args, these generalize across none:
`is_allowed` (via `strict`, the R141 mechanism) permits only a whole-command
match, and `_rule_for` stores the command whole so that match stays
reachable. "Always allow" still works and still means something — it just
cannot grow a different target. Four families, chosen with the user: disk/
device writers (`dd`, `mkfs`/`mkfs.*`, `fdisk`, `parted`, `shred`, `hdparm`,
`diskutil`, `newfs`/`newfs_*`), deleters (`rm`, `rmdir`, `unlink`, `srm`),
interpreters (`sh`, `bash`, `zsh`, `dash`, `ksh`, `fish`, `eval`, `source`,
`exec`, `python*`, `perl`, `ruby`, `node`, `osascript`), and privilege/fetch
(`sudo`, `su`, `doas`, `chown`, `chmod`, `chgrp`, `launchctl`, `systemctl`,
`curl`, `wget`).

`_is_dangerous` checks EVERY token by basename, not just the first —
`sudo dd …`, `/bin/rm -rf /`, `xargs rm -rf`, `env dd …` and
`find . -exec rm {} +` all place the name elsewhere, and a first-token test
would let the two-token rule generalize right over them (`xargs rm` covering
`xargs rm -rf /`). This over-triggers slightly (`grep dd .` counts as
dangerous), costing one re-approval when args change; a token that merely
*contains* the word does not fire, so `git commit -m "remove dd stuff"` is
unaffected.

**R149b — a single-token DENY rule denied almost nothing.** Found while
testing the above. A lone `dd` in `denylist.yaml` — the obvious way to
hard-block the disk destroyer — matched only the bare word `dd`, because the
"single-token rules are exact-match only" guard was applied to both
directions. That guard exists to stop a vague rule ALLOWING too much;
denying too much is the safe direction. A single-token deny rule now covers
any args, while the allowlist keeps the restriction.

**Supersedes** one case of `test_allowlist_matches_across_path_spellings`
(R95g): it asserted that approving `bash <script>` also covered
`bash <script> --verbose` as a prefix. `bash` is an interpreter, so the args
ARE the program and an extra flag changes what runs (`deploy.sh --prod`).
That case now asserts the re-prompt; the test's actual subject — path
spellings collapsing to one rule — is unchanged.

Docs: `README.md`'s approval section and `man.py`'s `allowlist.yaml` entry
now both state the three tiers, since this is user-visible approval
behavior.

Tests: `tests/test_core.py::test_a_dangerous_command_rule_never_generalizes_
over_its_target`, `::test_always_allow_on_a_dangerous_command_still_matches_
it_exactly`, `::test_dangerous_commands_are_detected_away_from_position_
zero`, `::test_safe_and_dangerous_command_sets_never_overlap`,
`::test_a_dangerous_command_is_still_denied_by_a_prefix_deny_rule` — all
five verified to fail on the pre-fix code.

### R150. The five small ones the R139–R149 audit deferred (2026-07-26)
Cheap, contained, each a real defect. Taken as one pass after the user asked
which of the deferred findings were worth fixing.

- **R150a — `aurora wipe` left MCP-server credentials in the OS keyring.**
  `mcp._resolve_env` resolves each `mcp_servers[].env` VALUE through
  `keystore.get_key()`, so an MCP server's `GITHUB_TOKEN` lives in the same
  `"aurora-agent"` keyring service as any provider key. But
  `_known_key_names()` — what `wipe` and `key clear --all` iterate —
  collected only providers' `api_key_env`. `wipe` therefore `rm -rf`'d
  AURORA_HOME while those credentials survived, which is precisely the
  "silently un-logging-out the user" hole ARCHITECTURE.md §5 says that
  function exists to close. Note it is the env mapping's VALUES that name
  keystore entries ({CHILD_VAR: AURORA_KEYSTORE_VAR}), never its keys.
- **R150b — the help overlay and `/nano` could render at once, with help
  undismissable.** Both occupy the chat area but their visibility filters are
  independent, and `open_nano`'s refusal list checked menu/question/secret/
  busy but not `_help_visible`. `?` then `/nano <file>` drew both, splitting
  the screen — and help could not then be closed, because `?` and its click
  target both require `self._editor is None` (via `_click_guard`) while
  Escape is `filter=_no_editor`. Help is a transient overlay, not a challenge
  that must be answered, so opening a file now just closes it.
- **R150c — an out-of-range digit leaked into the hidden input buffer.** With
  a menu open, a digit past its option count fell through to `insert_text` —
  into the buffer the menu is drawn on top of, invisibly, since
  `_input_height` collapses the input line to one hidden row. `5` on a
  2-option "Cancel this?" left a stray `5` in the prompt afterwards
  (`_open_ui_menu`, unlike `select_menu`, has no draft save/restore). The
  `Keys.Any` fallback exists to swallow exactly this, but a digit binding is
  more specific and wins.
- **R150d — Esc's bash-mode branch preempted the completion branch.**
  `_on_escape`'s docstring states the priority as "close an open completion/
  confirm menu; leave bash mode; …" but the code tested `_bash_mode` first.
  In bash mode with `cd Doc<Tab>`'s PathCompleter popup open, Esc armed the
  leave-bash gesture instead of dismissing the popup, and the `elif buf.text`
  clear-the-draft branch was unreachable in bash mode entirely. Reordered to
  match the documented intent — the narrower, more local action wins, the
  same reasoning that puts the menu check first — and bash mode now gets its
  own clear-the-draft step before arming.
- **R150e — a bare `models:` line crashed startup.** YAML parses it as None,
  and `load_config`'s `cfg.setdefault("models", [])` does NOT replace a None
  (the key exists, so setdefault is a no-op). `_default_model`'s
  `next(m for m in self.models …)` then raised TypeError during `Engine`
  construction. `remove_model_entries` already guarded this same None case,
  on one side only. **Correction to the audit's report on this one:** it also
  claimed `self.models` stops aliasing `cfg["models"]` when the key is
  absent, which would make `/model add` invisible until restart. That is NOT
  reachable — `load_config` setdefaults the key before `Engine` ever reads
  it. Only the None crash was real. The assignment now goes through
  `cfg["models"]` anyway so the aliasing is explicit rather than incidental,
  and the accompanying test is labelled an invariant guard, not a regression
  test, because it passes on the pre-fix code.

Tests: `tests/test_mcp.py::test_wipe_knows_about_mcp_server_keyring_
credentials`; `tests/test_tui.py::test_opening_the_editor_closes_the_help_
overlay`, `::test_an_out_of_range_digit_does_not_leak_into_the_hidden_
buffer`, `::test_esc_closes_a_completion_popup_before_arming_leave_bash`,
`::test_esc_in_bash_mode_clears_a_typed_command_before_arming`;
`tests/test_core.py::test_a_yaml_null_models_key_does_not_crash_startup`
(all six verified to fail on the pre-fix code), plus
`::test_engine_models_aliases_the_config_list` as the invariant guard noted
above.

### R151. Shadow-git checkpoints are bounded now (2026-07-26)
`checkpoint()` commits once per APPROVED MUTATION — every `write_file`,
`edit_file` and `run_command` — into `AURORA_HOME/checkpoints/<cwd-hash>/`,
forever. No retention, no `gc`, and no cleanup of per-directory repos for
projects that no longer exist. Growth tracks changed content rather than tree
size (git dedups blobs), but a daily driver accumulates thousands of commits
per project with no ceiling.

**The `undo-` tags were the worse half.** Every `/rewind` left a permanent
`undo-<hash>` tag so the pre-rewind state stays reachable after HEAD moves
back. That tag also *pins* the orphaned commits, so growth was
**irreducible** — not merely unbounded. Neither Aurora nor a manual
`git gc --prune=now` could reclaim any of it.

New `rewind.prune(cwd, keep)` — three steps, in order:
1. Drop all but the newest `UNDO_TAG_RETENTION` (5) `undo-*` tags. Until
   these go, nothing else can be collected.
2. Cut the ancestry chain by marking the `keep`-th commit as a **shallow
   root** — the same mechanism `git fetch --depth` uses. Chosen deliberately
   over rewriting history: a rewrite changes every hash, including ones
   `/rewind`'s own output has already printed for the user to type back.
3. `reflog expire --expire=now --all` then `gc --prune=now`, which is what
   actually reclaims the disk.

`RETENTION` is 200, comfortably above the 20 `entries()` ever displays, so
nothing visible changes. A snapshot past the cut is genuinely gone — the
trade is bounded footprint for bounded recovery depth, which is the policy
the user chose ("keep N most recent, prune the rest").

**Kept off the approval path.** `prune()` runs a `git gc`, and
`checkpoint()` sits directly in front of every approved mutation, where R47's
synchronous `git add -A` is already the acknowledged latency cost. So
`_prune_soon()` fires it on a daemon thread and only every `_PRUNE_EVERY`
(50) checkpoints — roughly once or twice a session. A prune failure returns 0
and is swallowed: retention is housekeeping, and it must never break the
safety net it is housekeeping for.

Verified against a real repo before writing the tests: 40 snapshots → 10,
`.git` size 48814 → 31753 bytes, `/rewind` still listing and restoring
correctly afterwards.

`ARCHITECTURE.md` §5 updated — the persistence table now notes the cap, and a
new paragraph records both why a shallow root rather than a rewrite and that
session JSONLs still grow forever by design (R20).

Tests: `tests/test_rewind.py::test_prune_bounds_the_history_and_rewind_still_
works`, `::test_prune_drops_the_undo_tags_that_pinned_orphaned_commits`,
`::test_prune_is_a_noop_for_a_project_with_no_checkpoints`,
`::test_prune_never_raises_and_never_breaks_checkpointing`,
`::test_checkpoint_schedules_a_prune_only_every_nth_call` — all five verified
to fail on the pre-fix code.

### R152. The chat scrollback is bounded now (2026-07-26)
`_chat` and its per-entry fragment `_cache` only ever grew — nothing but
bash-mode `clear` (R116) ever emptied them. R96b made *Aurora's* own
`_fragments()` flat in session length and that holds, but the fragment list
it returns is handed WHOLE to prompt_toolkit's `FormattedTextControl`, whose
`create_content` splits every line, copies each one to strip mouse handlers,
and hashes the whole tuple for its cache key — all **before** that cache is
consulted. So that half is unavoidably linear and no amount of caching on
Aurora's side can help. It runs every frame, and `append()` invalidates once
per streamed chunk.

Measured after R148 (post-merge, fragments ≈ lines):

| lines | `create_content` | | lines | `create_content` |
|---|---|---|---|---|
| 4.4k | 9 ms | | 35.4k | 44 ms |
| 8.8k | 13 ms | | 70.8k | 86 ms |
| 17.7k | 24 ms | | | |

A cap is the only available lever, and it is what every terminal emulator
does. `_SCROLLBACK_MAX_LINES` = 10000, trimmed down to
`_SCROLLBACK_KEEP_LINES` = 8000 so the re-flatten a trim forces is amortized
over ~2k lines rather than paid per append. End-to-end check: 18000 lines of
output in, 9844 retained, **14.5 ms/frame (≈69fps)** where it would otherwise
have been 44ms and climbing.

Three decisions worth recording:
- **Eviction runs only when a NEW entry is created**, never on a merge into
  the last one, so the O(entries) sum costs about one pass per `_MERGE_LIMIT`
  of output instead of one per streamed chunk. The total is recomputed from
  the entries each time rather than tracked in a running counter across the
  four append sites — no drift possible.
- **Selections are invalidated, not remapped.** `_sel`/`_sel_frozen` are
  absolute (line, col) coordinates, so a trim shifts them. Dropping a live
  drag or a frozen "copy selected" range is cheap for the user to redo and
  easy to get subtly wrong otherwise — a stale range would make the button
  copy whatever text now occupies those coordinates. `_text_cache`,
  `_offsets`, `_nlines` and `_dirty_from` are likewise reset wholesale rather
  than adjusted.
- **The newest entry is never evicted.** `think_chunk` and `append` hold a
  live reference to `_chat[-1]` and keep writing into it.

This was originally deferred as "needs a real eviction design touching
absolute selection coords". That was overcautious: invalidating instead of
remapping removes most of the difficulty. Recorded because the deferral
reasoning was wrong, not just conservative. The user reported never having
noticed the slowdown, so this ships as a backstop against the cliff rather
than as a fix for a felt problem — hence a cap generous enough that an
ordinary session never reaches it.

Tests: `tests/test_tui.py::test_scrollback_is_capped_and_keeps_the_newest_
content` (fails pre-fix with 20048 lines against a 12000 bound),
`::test_eviction_invalidates_a_selection_rather_than_misreporting_it` (also
fails pre-fix), plus `::test_eviction_leaves_a_consistent_renderable_
transcript`, `::test_no_eviction_for_an_ordinary_session` and
`::test_eviction_never_drops_the_entry_still_being_written` as consistency
guards — those three pass either way by design, since they assert the cap
does no HARM rather than that it exists.

### R153. A failing MCP server now says why (2026-07-26)
`MCPServer.__init__` spawned each child with `stderr=subprocess.DEVNULL`, so
everything the server said about its own failure was discarded. The
spawn-failure path never needed it — the `OSError` already carries
`failed to start 'npm': [Errno 2] No such file or directory`. The gap was
every failure *after* the process starts: a server that imports a missing
dependency and dies mid-handshake, one that refuses the protocol version,
one that wedges. Those surfaced as `connection closed` or `timed out
waiting for a response` — true, but not actionable. Diagnosing a
misconfigured server meant re-running its command by hand outside Aurora,
and since `MCPServer` is constructed during Engine startup
(`extensions.discover()` → `mcp_extension.register()`), the one line the
user sees is a startup warning they cannot drill into.

Now `stderr=PIPE` with a per-server **drain thread** (`_drain_stderr`)
keeping the last `_STDERR_KEEP_LINES` (50) lines in a `deque`, and every
post-spawn failure routed through one `_fail()` helper that appends a
bounded `(stderr: …)` tail — `_STDERR_QUOTE_LINES` (5) lines, truncated
from the LEFT at `_STDERR_QUOTE_CHARS` (400) so the newest output is what
survives. The existing message text is unchanged; the explanation is
appended to it.

**Why a thread and not a read at failure time** — the obvious cheap version
of this fix (`stderr=PIPE`, read it when something goes wrong) is worse
than the bug. An undrained pipe fills its OS buffer (~64KB) and then blocks
the child on its next `write`, so a merely chatty server would hang instead
of running. Verified, not assumed: with the drain thread stubbed out,
`test_a_server_that_floods_stderr_still_completes_the_handshake` fails with
`timed out waiting for a response` after the full 15s timeout. The thread
is a daemon, ends itself at EOF when the child exits, and closes the pipe;
`close()` joins it briefly. `_stderr_tail()` also joins (≤0.2s) when the
child has already exited, so the traceback still in flight in the drain
thread makes it into the message rather than losing a race with it.

Found in a review pass over `extensions.py`/`mcp.py`, alongside eight other
reported "bugs" that were checked and are not bugs — the extension
`SPEC`/`register()` merge, `set_extensions`' dedupe, `close()`'s
error handling, the `_write_all` "stdin can be None" race, and
`_mcp_manager`'s partial-init window are all correct as written.

Tests: `tests/test_mcp.py::test_a_dying_server_reports_its_own_stderr` (new
fixture `tests/fixtures/noisy_mcp_server.py` — starts fine, writes a
node-style "Cannot find module" error to stderr, exits 1),
`::test_captured_stderr_is_bounded_in_lines_and_in_quoted_length`,
`::test_closing_a_server_leaves_no_stderr_thread_behind` — all three
verified to fail on the pre-fix code. `::test_a_server_that_floods_stderr_
still_completes_the_handshake` (the fixture's `--flood` mode: ~100KB to
stderr before serving) passes pre-fix by design — DEVNULL never blocked —
and exists to guard the naive fix, as above.

### R154. The context gauge is live, and auto-compact happens mid-turn (2026-07-26)
Reported from a real session: a long tool-heavy task died on
`request (67244 tokens) exceeds the available context size (65536 tokens)`
with the status bar giving no warning beforehand. Two separate defects, one
failure.

**R154a — the gauge only moved when a turn ended.** `self._used` (what
`context_stats()` reports, what the status bar renders, what the ">80%"
hint and R118's auto-compact both test) was assigned in exactly ONE place:
after `run_turn` returned. For the whole duration of a turn — the case where
context actually grows fast, a dozen rounds of tool results — the bar showed
the size of the PREVIOUS turn's prompt. It was not a lagging number, it was
a stale one, and the first sign of trouble was the provider rejecting the
request.

Now `on_usage` is routed through the engine (`_live_usage`) instead of
straight to the frontend: each round's reported prompt+reply updates the
gauge as it arrives, on the same basis as the end-of-turn assignment (this
round's prompt + this round's reply, never the summed output — R90d), and
still forwards to the frontend's own `on_usage` unchanged. A round that
reports no usage keeps the last real value rather than blanking a correct
gauge.

Tool results are counted the moment they land (`_count_tool_output`, new
`tokens.estimate_tokens_from_chars`). A tool result enters history
immediately but is only MEASURED by the provider on the next round's
prompt — so a single 60KB result (`TOOL_OUTPUT_LIMIT`) was ~15k invisible
tokens, and the request carrying it was already built and rejected before
anything could see them. Estimated, not authoritative: the provider's real
count replaces it on the next round. Erring high is the useful direction —
it folds a round early rather than a round late. New
`Frontend.invalidate_status()` (no-op in the classic REPL) repaints on it.

**R154b — auto-compact ran only where it could not help.** R118's fold was
checked *after* `run_turn` returned, so the turn that overflows dies before
ever reaching it. The safety net only ever ran on turns that didn't need
one. It was also off by default and set to 90%.

New `AgentCallbacks.maybe_compact`, called by `run_turn` between rounds —
before building each request after the first, which is the one point where
every assistant `tool_calls` entry already has its matching `tool` results
and history can be rewritten without producing an invalid sequence. The
engine owns the policy (`_maybe_auto_compact_mid_turn`), the loop owns only
knowing when it is safe to ask. `auto_compact` now defaults **true** at
**80%**; R118's end-of-turn check stays for the session that creeps up over
many small turns.

**The subtle half: `compact_history` had to fold IN PLACE.** It rebound
`self.messages = [...]`, which was harmless while compaction could only
happen between turns. `run_turn` is handed `self.messages` and mutates that
same list object for the entire turn, so a mid-turn rebind splits them: the
engine would hold the folded history while the loop kept appending to — and
sending — the unfolded one. The compaction would have had no effect on the
very requests it exists to shrink, and nothing would have looked wrong.
`self.messages[:] = ...` instead.

**No repeat-fold guard, by construction.** After a successful fold
everything foldable has become the summary merged into `messages[0]`, so
`compact.cut_index` finds no older-than-the-tail region and
`compact_history` returns 0 — *before* spending a summarization request. A
turn whose own recent tail is what blew the window therefore pays for one
summarization at most, not one per round. Verified by test rather than
argued.

User decisions on this one: compact automatically with no prompt (not a
challenge at the boundary), any turn crossing 80% (no separate "is this a
big task" condition), threshold stays a `config.yaml` runtime knob.

Tests: `tests/test_core.py::test_maybe_compact_runs_between_rounds_and_
never_before_the_first`, `::test_a_mid_turn_fold_is_seen_by_the_rest_of_the_
turn` (the in-place fold — asserts round 2's request carries the summary),
`::test_mid_turn_auto_compact_fires_at_the_threshold_and_only_folds_once`,
`::test_mid_turn_auto_compact_respects_the_off_switch`, `::test_the_gauge_
moves_on_every_round_not_just_at_the_end`, `::test_a_round_reporting_no_
usage_does_not_blank_the_gauge`, `::test_a_tool_result_is_counted_when_it_
lands_not_a_round_later`, `::test_auto_compact_is_on_at_80_pct_by_default`
(replaces `test_auto_compact_off_by_default`), plus
`::test_auto_compact_can_still_be_turned_off` — all eight verified to fail
on the pre-change code.

### R155. One `copy` button on the status bar, with a picker (2026-07-26)
The bar carried four copy buttons — `session id`, `copy last`, `copy all`,
and a conditional `copy selected` — on a fixed-width row where every feature
that produces something copyable wanted another one. They are now a single
`copy` fragment opening a menu: `copy last`, `copy whole session
transcript`, `copy session id`, `copy selected` (still offered only when
there IS a selection), `cancel`.

Renaming came with it. `copy all` never said what "all" was, and it was
actively misleading twice over: the text comes from the session LOG
(`session.export_markdown`), not the chat pane, so since R152's scrollback
cap it can hold more than the pane still shows — and `all` means *all
sessions* elsewhere in Aurora's own vocabulary (`/cost all`) while
`/copy-all` has always meant this session only. The menu row has room for
the honest phrase. The `/copy-all` command name is deliberately unchanged:
muscle memory, and the ambiguity was only ever in the eight-character
button.

`_copy_session_id`, `_copy_raw_response`, `_copy_all_chat` and
`_copy_selected` are gone — their logic moved into `_resolve_copy_menu`,
which keeps each branch's original threading behavior. R129's point still
holds: the whole-session option's work is UNBOUNDED (parses a session JSONL
that only grows, then spawns a clipboard subprocess with a 5s timeout), so
it still queues `/copy-all` for the worker instead of running on the UI
event-loop thread. The other three stay inline, exactly as their buttons
were.

Three traps, all real, all hit while building this:
- **`select_menu()` cannot be used here.** A mouse handler runs on the UI
  event-loop thread and `select_menu()` raises there by design (it blocks
  waiting for an answer only that thread can deliver). `_open_ui_menu` — the
  non-blocking, callback-delivered path the Esc-Esc confirms already use —
  is the right mechanism. It gained a `default_index` parameter.
- **`_open_ui_menu` calls `input.buffer.reset()`, which clears the draft
  TEXT, not just the selection.** So the picker would have eaten a
  half-typed prompt on every click — a regression against the button it
  replaced, which only ever dropped the selection. The draft is saved at
  click time and restored on every outcome including `cancel`, guarded on
  "still empty" so it can never clobber something typed in between.
- **The selection must be captured at CLICK time.** That same `reset()`
  destroys a prompt selection, so reading it in the resolver would have made
  `copy selected` — the one option that can't survive being asked about —
  reliably copy nothing. Precedence is unchanged from the old button: a
  prompt selection wins over a frozen chat one. If it vanishes between click
  and pick anyway, the notice says so rather than copying silence.

Esc stays a no-op while a menu is open (R62 — the pick must be explicit), so
the menu carries an explicit `cancel` row; a mis-click needs a way out.

User decisions: fold the `session id` button in too, pre-highlight
`copy selected` when a selection exists while keeping row ORDER fixed (so
the digit shortcuts never change meaning between clicks), and label the
whole-session row `copy whole session transcript`.

`KNOWLEDGE/project/TuiLayout.md` updated — R56's status-bar section now
describes the one-button shape and records the three traps above.

Tests: `tests/test_tui.py::test_the_status_bar_shows_one_copy_button_not_
four`, `::test_copy_selected_row_is_offered_only_when_there_is_a_selection`,
`::test_opening_the_copy_menu_does_not_eat_the_draft`,
`::test_copy_menu_defaults_to_the_selection_when_there_is_one`,
`::test_copy_menu_reports_a_selection_that_vanished` — all five verified to
fail on the pre-change code. Every pre-existing copy test was repointed at
the shipped path via a `_copy_via_menu` helper that clicks the button and
resolves a row through `_resolve_menu`, rather than calling the (now
deleted) handlers directly.

### R156. Auto-compact could not rescue the turn it exists for (2026-07-26)

Reported from a live session: a turn that read three large source files died
on `local HTTP 400: request (68374 tokens) exceeds the available context size
(65536 tokens)`, with the gauge showing 110.9k. R154 had already made the
gauge live and added the mid-turn fold — but the fold itself was structurally
unable to fire, and would not have shrunk anything if it had. Two independent
defects, both on the path from "context is filling" to "context is smaller":

**a. `compact.cut_index` could not cut inside a turn.** After walking backward
to the recent-keep budget it walked *forward* to the nearest `user` message.
Inside a single turn there is no later user message — a turn is `user ->
assistant(+tool_calls) -> tool(s) -> ...` — so the walk ran off the end and
returned `0`, meaning "nothing old enough to fold". `compact_history` then
returned 0, `_maybe_auto_compact_mid_turn` skipped its `notify` (it only
reports a non-zero fold), and `agent.run_turn` sent the oversized request.
The failure was therefore *silent*: the only symptom was the provider's 400.

Fixed by naming the real invariant — `compact.is_cut_boundary(m)`, true for a
`user` message **or** an `assistant` message carrying `tool_calls`. The
second is a legal sequence start for the same reason the first is: it OPENS a
round, so no `tool` message is left answering a `tool_calls` entry that was
folded away. Round boundaries inside a turn are now cut points, which is what
lets a runaway turn shrink itself. `engine.compact_history` gained the
matching case: when the cut lands on such an assistant, the summary is
inserted as its own preceding `user` message rather than merged into the
assistant's `content` — merging would put the summary in the model's mouth
and corrupt the `tool_calls` message its results answer. The original
alternation fix (no two consecutive `user` turns) is unchanged for the
`user`-boundary case; `user -> assistant -> tool` is alternating too.

**b. The fold could not shrink even when it ran.** `compact_history` sent the
*entire* flattened region as the summarization request — but the case
auto-compact exists for is a history that is already bigger than the window,
so that request was rejected with the same `exceed_context_size_error` that
triggered the fold. It was caught by the bare `except: pass`, which fell back
to `flattened_as_user_message` — a **verbatim** copy of the region. The fold
therefore carried back exactly as many tokens as it removed. Discovered by
the R156a regression test, which folded successfully and still measured
90085 tokens against a 90004-token starting point.

Fixed with `compact.clip_transcript(text, max_tokens)` — head+tail clip
(60/40, middle dropped with a count) applied to **both** the summarization
request and the fallback body. Head and tail rather than a plain head because
the start carries what the task is and the end carries where it got to, while
the middle of a runaway turn is the bulk file dumps least worth keeping. The
budget is `_COMPACT_INPUT_FRACTION` (0.5) of the real window, or
`_COMPACT_INPUT_TOKENS_FALLBACK` (16k) while `_context_limit_nonblocking`
still returns 0. One `_provider_for` call serves both the limit lookup and
the request, so a fold still costs at most one summarization (R154's
self-limiting property, which `test_mid_turn_auto_compact_fires_at_the_
threshold_and_only_folds_once` measures by counting that call).

`tokens.CHARS_PER_TOKEN` was extracted for this: `clip_transcript` converts a
token budget back into a character budget, and an unnamed `* 4` there would
have been the silent inverse of the three unnamed `// 4`s in `tokens.py`.

Tests: `tests/test_core.py::test_cut_index_can_fold_inside_a_single_runaway_
turn`, `::test_runaway_turn_actually_shrinks_to_fit_the_window` (both verified
to fail pre-fix with `assert 0 > 0` — the "nothing foldable" answer),
`::test_cut_index_never_cuts_at_a_tool_or_bare_assistant` (keeps the safety
property the user-only walk was buying), `::test_summary_is_not_merged_into_
an_assistants_own_words`. `::test_cut_index_never_splits_a_tool_call_from_its_
result` still passes unchanged — the forward walk only ever moves toward the
tail, so it cannot reach the round-opening assistant that precedes its
starting point.

Still open, deliberately not fixed here: `_maybe_auto_compact_mid_turn` stays
silent when `compact_history` returns 0, so a history that genuinely cannot
be folded still produces a provider error with no explanation. Worth a
"couldn't free space" notice, but that is a UI-surface change, not this bug.

### R157. `web_search`/`web_fetch` move to a bundled extension (2026-07-26)

`aurora/websearch.py` was the last built-in tool module that was already
extension-shaped: module-level `SPEC` + `RUNNERS`, no engine state, no
approval coupling, nothing depending on it beyond one config flag. It is now
`aurora/extensions_bundled/web_extension.py`, loaded by the same `discover()`
pass as the MCP and lint extensions — **still installed and enabled by
default**; living in `extensions_bundled/` rather than `~/.aurora/extensions/`
is the only thing that makes a bundled extension "built in".

Registered via the dynamic `register(engine)` style, not static
`SPEC`/`RUNNERS`, and that is the load-bearing detail: `runtime.web_search`
used to be enforced engine-side by `tools.specs(include_web)`, and a static
extension loads unconditionally — the move would have silently turned the
flag into a no-op. `register()` reads `engine.web` and contributes nothing
when it is false. It defaults to enabled for an engine object without the
attribute, matching `runtime.web_search`'s own default.

Engine-side special-casing removed with it:
- `tools.specs(include_web)` → `tools.specs()`. The parameter had no meaning
  left once the flag moved, and `agent.run_turn` no longer threads a `web`
  argument through from `Engine.send` to reach it.
- `tools.set_extensions`'s builtin-shadowing check and `run_tool`'s lookup
  table no longer name a `websearch` module. `PARALLEL_SAFE` still lists both
  tools by name, unchanged — that set is about what may run concurrently, not
  about where a tool is defined.
- `/extensions` now LISTS `web_search`/`web_fetch` instead of filtering them
  out, so the count goes 2 → 4. That is correct rather than cosmetic: the
  command reports what is actually loaded, and these are now loaded exactly
  the way the MCP tools always have been.

Verified in the built wheel, not just in a checkout — `python -m build
--wheel` produces `aurora/extensions_bundled/web_extension.py` alongside the
other three, so `pip install` really does ship it. (There is no `__init__.py`
in `extensions_bundled/`; the files are collected as package data under
`include = ["aurora*"]`, the same as the three that preceded it.)

Tests: `tests/test_web_extension.py` — `::test_web_tools_are_loaded_by_
default`, `::test_web_tools_are_runnable_through_the_extension_path` (the
runner must resolve through `_EXTENSION_RUNNERS`, not the builtin table it
left), `::test_web_search_false_contributes_no_tools` and
`::test_register_honours_the_flag_directly` (the toggle, the thing most
likely to break in this move), `::test_register_defaults_to_on_for_an_engine_
without_the_attribute`, `::test_engine_half_no_longer_imports_websearch`,
`::test_specs_takes_no_include_web_argument`. The last one caught real doc
rot on the first run: `extensions.py` and `mcp.py` still pointed at
`aurora/websearch.py` as the worked example of the static style.
`tests/test_mcp.py::test_lint_check_always_bundled_by_default` updated 2 → 4.

### R158. The two holes R156 left (2026-07-26)

Same session, same error, hours after R156 shipped: `request (66344 tokens)
exceeds the available context size (65536 tokens)` on a turn that read two
large source files. R156 was necessary and not sufficient — three defects,
found by reproducing the reported shape rather than by re-reading the fix.

**a. `cut_index` still returned 0 when the oversized message was LAST.**
R156 taught the forward walk to accept a round-opening `assistant` as a
boundary, but the walk only ever moves toward the tail. Reading one large
file produces a single tool result bigger than `keep_recent_tokens` all by
itself, so the backward walk stops ON that result with nothing but the end of
the list ahead of it — no boundary found, `return 0`, "nothing foldable",
exactly the pre-R156 symptom via a different route. This is the common case,
not a corner one: any `read_file` of a >80KB file hits it. Fixed by falling
back to the last boundary BEHIND the stop point. The kept tail is then bigger
than the budget asked for — that one huge message is unsplittable, so no cut
can do better — but everything in front of it folds instead of nothing.

**b. `_used` dropped the system prompt after a fold.** Everywhere else
`_used` comes from the provider's billed `input_tokens`, which INCLUDES the
system prompt; `compact_history` re-estimated it from `self.messages` alone.
On a bootstrapped `.agentic_context` session that is ~12k tokens of AGENTS.md
+ three INDEX.md + every `[CORE]` doc, silently missing. The gauge read low,
`_maybe_auto_compact_mid_turn` saw headroom that did not exist, and the fold
either fired late or not at all. The two sources of `_used` now measure the
same thing.

**c. The summary budget ignored everything sitting beside it.** R156 sized it
at a flat `limit * 0.5`, which is a statement about the window, not about the
request — the system prompt and the kept raw tail also have to fit. Half the
window plus a 12k system prompt plus a 20k tail is over a 65k window. The
budget is now the room genuinely left over (`limit - system - kept tail -
_COMPACT_HEADROOM_TOKENS`), with the old fraction kept as a ceiling so a huge
window doesn't produce an absurd summary, and `_COMPACT_MIN_SUMMARY_TOKENS`
as a floor so a nearly-full tail still gets a usable summary rather than no
fold at all.

Two existing tests changed, both deliberately, both verified as intended
rather than as breakage: `::test_cut_index_never_splits_a_tool_call_from_its_
result` asserted `idx == 0`, which encoded a pre-R156 assumption — index 1 IS
the `tool_calls` message, so cutting there keeps its result with it. It now
asserts the invariant it is named for (no `tool` message separated from its
call) instead of one particular index. `::test_compact_logs_the_drop_it_
causes` gained the system prompt in its expected `used_after`.

Tests: `::test_cut_index_folds_when_the_last_message_alone_blows_the_budget`,
`::test_used_counts_the_system_prompt_after_a_fold`,
`::test_fold_fits_the_window_including_the_system_prompt` (end to end, the
reported session's exact shape: bootstrapped system prompt + long history +
two big file reads; asserts the PROPERTY — system prompt plus folded history
fits the window — not a number) — all three verified failing pre-fix.
`::test_cut_index_returns_zero_when_there_is_genuinely_no_boundary` guards
the new fallback against inventing an unsafe cut.

Honest limit, unchanged from R156: this is not a proof. `TOOL_OUTPUT_LIMIT`
caps one result at ~15k tokens, so a kept tail of several unsplittable rounds
plus a large system prompt can still approach a 65k window. The remaining
guarantee would be recovery ON the provider's context error — catch it, fold
harder, retry the round — rather than only predicting it beforehand. Not
done here; it changes the agent loop's error path, not compaction.

### R160. `/extensions new <name>` scaffolds a SPEC/RUNNERS template (2026-07-26)

Writing an extension meant reading `EXTENSIONS.md` and copying the shape by
hand. `extensions.scaffold(name)` (aurora/extensions.py) slugifies the given
name, writes `AURORA_HOME/extensions/<slug>.py` from a template with a
placeholder `SPEC`/`RUNNERS` pair already wired together and loadable as-is
(`discover()` picks it up immediately, tool returns `"not implemented"`
until edited), and refuses to run if the file already exists (same caution
`/model add` uses before touching config.yaml) or if the name slugifies to
nothing. Wired into the REPL as `/extensions new <name>` (`ui.py`,
`man.py`), sitting next to the existing `/extensions` listing command it
scaffolds for.

Tests: `test_scaffold_writes_a_loadable_spec_runners_file`,
`test_scaffold_refuses_to_overwrite_an_existing_file`,
`test_scaffold_rejects_a_name_that_slugs_to_nothing`,
`test_extensions_new_command_scaffolds_and_reports_the_path`,
`test_extensions_new_command_requires_a_name` (tests/test_mcp.py).

### R161. `/search <text>` finds a past session by content (2026-07-26)

`/resume` only ever offered the last 20 sessions, so finding an older one
meant remembering its date or scrolling session files by hand.
`session.search_sessions(query, limit=20)` (aurora/session.py) streams every
`.jsonl` under `sessions_dir()` newest-first, case-insensitive
substring-matches `text` (user/assistant records) or `output` (tool
records), and returns one hit per session with a ~120-char snippet around
the match — same streaming-not-load-into-memory shape `list_sessions`/
`usage_all_sessions` already use, since a long-lived session's log is
unbounded (R20/R90g). Wired into the REPL as `/search <text>` (`ui.py`,
`man.py`), listing matches and then reusing `/resume`'s pick-a-number-to-
continue flow.

Tests: `test_search_sessions_finds_a_match_in_user_and_assistant_text`,
`test_search_sessions_is_case_insensitive_and_matches_tool_output`,
`test_search_sessions_returns_nothing_for_no_match` (tests/test_core.py),
`test_search_command_requires_text` (tests/test_mcp.py).

### R162. `/fallback on|off` — retry a failed turn on the next model (2026-07-26)

Endpoint failover (`providers/base.py`) already retries across URLs for ONE
provider; nothing retried across configured MODELS when a provider itself
was down. Off by default (persisted, `runtime.model_fallback`) — silently
switching models mid-session is a real behavior change some setups don't
want. When on, `Engine._run_turn_with_fallback` (aurora/engine.py, called
from `send`) wraps `agent.run_turn` and retries on two failure shapes:

1. A `ProviderError` that escapes `run_turn` itself (R143a's
  corrective-retry path — a second failure inside the `MalformedToolCall`
  handler).
2. The far more common shape: `run_turn` catches the error internally,
  notifies the user, and returns a turn that produced NOTHING
  (`len(self.messages)` unchanged from before the call) — this is what a
  rate limit, timeout, or unreachable host looks like from `send`'s side,
  and it never raises.

Either way, the next candidate comes from `Engine._fallback_models()` —
`valid_models()` (only models with a usable key, no interactive prompt)
minus whichever is current, in `config.yaml`'s `models:` order, the same
order `/model`'s picker walks. A model that DOES make progress (produces at
least one message, even mid-turn) is left alone — switching underneath a
turn that already got a tool call or partial reply back would be the
surprising move, not the safe one. A successful fallback calls
`switch_model()` for real: `self.current` stays on the model that actually
answered, logged as an ordinary `model_switch` event, so `/context` shows
where the turn ran. `/fallback` (no arg) shows current state + the resolved
chain from the current model; `on`/`off` sets and persists it.

Tests (tests/test_core.py): `test_fallback_off_by_default_lets_the_error_
through`, `test_fallback_on_retries_the_same_turn_on_the_next_model`,
`test_fallback_skips_a_model_without_a_usable_key`, `test_fallback_
exhausted_leaves_no_reply` (every candidate's error is swallowed by
`run_turn`, same as `send()`'s existing single-model contract — no
exception, just nothing produced).
`test_fallback_command_toggles_and_persists` (tests/test_mcp.py).

### R163. `$` cost badge moves live, mid-turn, not just at the end (2026-07-26)

`self._cost` was incremented in exactly one place — once `send()`'s call to
`agent.run_turn` returned — the same shape R154 already fixed for the
context gauge (`self._used`). A tool-heavy multi-round turn ran the whole
request before the status bar's `$` badge (`tui.py`, `ui.py`, both already
read `context_stats().cost_usd` continuously) moved at all. Moved the
accrual into `Engine._live_usage` (fires once per round via the existing
`on_usage` callback) using `self._provider.cost(self.current.get("model"),
input_tokens, output_tokens)` for THAT round, and removed the end-of-turn
`self._cost += provider.cost(model, turn.billed_input, turn.output_tokens)`
line it replaces. The two are the exact same total, not just close:
`billed_input`/`turn.output_tokens` are themselves sums over every round,
and `openai_compat.py`'s `cost()` is linear in tokens
(`price_in_per_mtok`/`price_out_per_mtok`), so summing per-round costs
equals costing the summed tokens once.

Tests: `test_cost_accrues_live_during_a_multi_round_turn` (checks the total
matches summing each round separately, not one end-of-turn calculation that
happens to land on the same number), `test_cost_visible_mid_turn_before_
the_turn_finishes` (tests/test_core.py).

### R164. Syntax highlighting inside rendered code fences (2026-07-26)

`mdrender.LineRenderer` (aurora/mdrender.py, shared by the classic REPL and
the TUI — both go through `TerminalFrontend`, R117) rendered every fenced
code line uniformly dim, with no distinction between a keyword, a string, a
comment, and everything else. `LineRenderer` now tracks the fence's
language tag too (the text after ` ``` `, e.g. ` ```python `) and, for a
curated set it recognizes — python, javascript (covers js/jsx/ts/tsx via
alias), bash (sh/shell/zsh), go, rust, ruby — highlights keywords (magenta),
strings (green), numbers (yellow) via one combined regex per language
(`_build_token_re`, compiled once at import, not per line), leaving
everything else dim as before. An untagged fence or a language outside that
set falls back to the exact old behavior — the whole line dim, nothing
more — so this is additive, never a regression for what it doesn't know.
`NO_COLOR`/non-tty stays fully byte-faithful, same contract as the rest of
the module (`render()`'s existing `if not RESET: return line` guard covers
the new code path too, untouched).

Deliberately NOT pygments or any highlighting library: the module's whole
premise is "small, dependency-free, line-based so it works mid-stream"
(its own docstring) — a real tokenizer needs the whole file/AST, which this
renderer structurally doesn't have (one line at a time, streaming).

Tests (tests/test_core.py, `_colours_on` helper forces the normally-disabled
non-tty colours on): `test_a_recognized_fence_language_highlights_keyword_
string_and_number`, `test_a_python_comment_is_dim_not_miscoloured`,
`test_an_unrecognized_fence_language_falls_back_to_plain_dim`, `test_a_
bare_fence_with_no_language_falls_back_to_plain_dim`, `test_a_fence_
language_alias_resolves_to_its_canonical_keyword_set`, `test_fence_
language_resets_when_a_new_fence_opens`, `test_highlighting_is_a_noop_
outside_a_fence`, `test_colours_disabled_stays_byte_faithful_inside_a_fence`.

### R165. `/cost` drops its repeated explainer footer (2026-07-26)

`_cost_report` (aurora/ui.py) appended three `dim()` lines to EVERY `/cost`
call — "in = billed prompt tokens…", "estimate only — an UPPER bound…", and
(when any model was unpriced) "\"no price\" = local, or a model with no
entry…". Useful once, noise on the second call and every one after: the
actual per-model numbers the command exists to show got buried under the
same three sentences every time. Removed; `/cost` now prints only the
per-model rows and the total.

Test: `test_cost_report_has_no_explainer_footer` (tests/test_core.py).

### R166. `/cost` narrows to all-sessions; `/context <id>` gets the per-model breakdown (2026-07-26)

`/cost [all]` and `/context [all] [id]` overlapped in a confusing way: both
had an `all` argument that meant something DIFFERENT in each command — for
`/cost` it meant "every session on this machine," for `/context` it meant
"every turn of one session, don't truncate." A user asking "why does /cost
show comments on every call" (R165) also asked whether the two commands
should just be one; they shouldn't (the two `all`s can't merge without
becoming ambiguous), but the overlap they actually share — per-model $
breakdown — is now shared code, not two commands duplicating the same
arithmetic:

- **`ctxtree.model_breakdown_lines(usage_rows)`** (aurora/ctxtree.py) is the
  per-model `turns · in · out · $ · cached` formatter, extracted from what
  used to be `/cost`'s only body. Takes a dict shaped like `session.
  usage_by_model()`/`usage_all_sessions()`, returns `(lines, priced total)`.
- **`/cost` drops its `[all]` argument** — it now ALWAYS reads `session.
  usage_all_sessions()` (aurora/ui.py `_cost_report`). A single session's
  breakdown is `/context <id>`'s job now, not a second command reading the
  same data a different way.
- **`/context <id>`** (`ctxtree.render`) now also prints the per-model
  breakdown when a session used MORE than one model — the case R162's
  `/fallback` and a manual `/model` switch mid-session both create, and the
  one place the session's cost tree previously stayed silent about which
  model actually ran which turn. A single-model session still shows only
  the head line's total, unchanged — the breakdown would be pure repetition
  there.

Tests: `test_cost_command_with_no_sessions_at_all`, `test_cost_command_
prices_known_models_and_flags_the_rest` (updated for the new no-arg
signature), `test_context_tree_shows_a_per_model_breakdown_when_multiple_
models_ran`, `test_context_tree_per_model_breakdown_uses_the_shared_cost_
helper` (tests/test_core.py).

### R167. `/context` drops emoji badges for plain-word labels (2026-07-26)

The per-turn badge line in `/context`'s tree (`ctxtree.py`'s `_badges`) used
📥/📤/🔧/💭/⚡ as field labels. Replaced with `in:`/`out:`/`tools:`/
`think:`/`cache:` — same fields, same values, same `│`-separated layout,
just plain words instead of pictographic glyphs. Per-tool-call status
glyphs (✓/✗/⊘) and approval-decision glyphs (✅/⛔/✎/⏹) are UNCHANGED — those
are single-color outcome markers with their own established meaning
throughout the tree, not the colorful multi-glyph badges this was about.
`session.export_markdown`'s tool-call marker (`aurora/session.py`) got the
same treatment (`🔧` → `tool:`) for the same reason.

Per-turn $ cost was already there before this — `_badges` has priced each
turn individually since R92/R134 (the `price = price_for(...)` block), this
just renamed the labels around it; nothing about the cost figure itself
changed.

Tests: all pre-existing `/context`-badge assertions in tests/test_core.py
updated to the new label text (`in:`/`out:`/`tools:`/`think:`/`cache:`) —
no new test needed since coverage was already there, only the literal
strings it checked for changed.

### R168. The status bar's `$` price is clickable → `/cost` (2026-07-26)

The `ctx` gauge next to it has been the `/context` link since R135c; the
price beside it (rendered only when `cost_known`, R135e) was bare text with
no handler. It's now its own fragment — `class:status.id`, same as the ctx
gauge and every other status-bar link, which is what renders it underlined
— with a click handler (`_cost_report_click`, aurora/tui.py) that runs
`/cost` through the inbox exactly like `_cost_tree_click` already does for
`/context`: a mouse handler runs on the UI event-loop thread, and `/cost`
reads every session log on the machine (R20's logs are unbounded), so the
command itself has to go through the worker thread, not run inline. The
status-bar building code (`status()`'s `frags` list) had to split into
"model name" / "conditional price fragment" / "the rest," since the old
single string interpolation (`f"{cost} │ "`) couldn't carry a click target.

Tests: `test_cost_report_click_queues_the_command_instead_of_working_inline`
(proves it goes through the inbox, not the UI thread — the same proof shape
`test_cost_tree_click_queues_the_command_instead_of_working_inline` already
used for `/context`), `test_status_bar_price_is_clickable_and_runs_cost`
(tests/test_tui.py).

### R169. `/context` badge labels go UPPERCASE (2026-07-26)

R167 replaced the emoji field labels with lowercase words (`in:`/`out:`/
`think:`/`tools:`/`cache:`). Now uppercase (`IN:`/`OUT:`/`THINK:`/`TOOLS:`/
`CACHE:`) — same fields, same values, same `│`-separated layout, purely a
capitalization change in `ctxtree.py`'s `_badges`. `/cost`'s per-model
breakdown (`model_breakdown_lines`, R166) is untouched — its `turns · in ·
out · $ · cached` line was never part of R167/this change, only the
per-turn badge row in `/context`'s tree.

Tests: all `/context`-badge assertions in tests/test_core.py updated to the
uppercase label text — same tests as R167, only the literal strings changed.

### R159. Status bar shows a live `⤵N` compaction counter, clickable in the TUI (2026-07-26)

`/context` already showed fold history in the tree, but there was no
at-a-glance signal that a session had been folded at all — a user watching
the status bar had no idea compaction had happened unless they opened
`/context`. `Engine.compactions` now counts folds this session, incremented
in `compact_history` beside the log record it must agree with (`engine.py`).
On resume, it's recomputed from the session log's `compact` records rather
than carried in process state, since a `--continue`'d session appending to
the same log needs "this session" to mean everything logged, not just what
happened since the current process started.

Rendered only when non-zero (`⤵0` on the overwhelmingly common
never-folded path would just be noise, and the status bar has an 80-col
budget, R134g). In the TUI it's a clickable status-bar fragment
(`_compactions_click`) that asks "compact again?" and runs `/compact`
through the inbox on yes — same worker-thread rule as `/context` and
`/copy-all`, since `compact_history` calls `provider.turn`. It follows the
same three R155 draft-preservation traps as the other status-bar menus:
`_open_ui_menu` (never `select_menu()`, since a mouse handler runs on the
UI thread), the typed draft saved before the menu opens and restored on
every outcome including cancel, and the restore guarded on "still empty"
so it can never clobber something typed between the click and the pick. In
the classic REPL (`ui.py`) it's a plain readout beside the context gauge —
no click target there, so `/compact` is typed as normal.

Tests: `tests/test_tui.py` — `_Stats`/`_FakeEngine` carry a `compactions`
field so the status-bar render tests exercise both the zero (hidden) and
non-zero (shown) cases.

### R170. Deep-dive review fixes (2026-07-26)

A pass over `agent.py`/`approve.py`/`secrets.py`/`engine.py`/`tui.py`/
`tools.py`/`session.py`/`mcp.py`/`keystore.py`/`providers/openai_compat.py`
turned up several correctness, security, and performance issues. Each
sub-letter below is independently testable; they share one heading because
they came from one review pass.

**R170a. A corrupt `denylist.yaml` now fails CLOSED instead of silently
disabling deny enforcement — `approve.py`, `agent.py`.**
`approve._load` used `yaml.safe_load(text) or {}`, so a present-but-empty
or otherwise-corrupt denylist file collapsed to `{}` exactly like a missing
one — and `is_denied`'s `if not data: return False` guard then silently
returned "not denied" for everything. R120's "deny always wins" guarantee
never actually fired in that case, with no visible error. `_load` now
raises `ApproveLoadError` when the file exists but fails to parse as a
YAML mapping (bad YAML, or valid YAML that isn't a dict — a list/string/
etc.). `run_turn` in `agent.py` catches it at both call sites (initial
load and the post-"always-deny" reload), notifies the user, and sets a
`deny_broken` flag that blocks every gated tool call until the file is
fixed — fail closed, not fail open. A genuinely missing or truly empty
file is unchanged (still "no deny rules yet", not an error).

Tests: `test_denylist_corrupt_file_raises_instead_of_silently_disabling_deny`,
`test_denylist_wrong_shape_raises`, `test_agent_fails_closed_when_denylist_is_corrupt`
(tests/test_core.py) — all three fail on the pre-fix code (the first two
with an uncaught YAML/AttributeError instead of raising `ApproveLoadError`,
the third because the tool call would have gone through unblocked).

**R170b. A secret glued onto a PATTERNS match by a hyphen is no longer
silently dropped — `secrets.py`.**
`scan()`'s entropy fallback skipped a whole `_CANDIDATE_RE` candidate the
moment it overlapped ANY already-claimed span, even partially. `_CANDIDATE_RE`
includes `-`/`+` in its charset, which aren't `\w` for regex `\b` purposes —
so a fixed-length PATTERNS match (the AWS access key's exact 20 chars, e.g.)
can end well inside a longer candidate span if a hyphen glues more
secret-looking text directly onto it with no whitespace separator. The
glued-on tail was a real, independently-qualifying high-entropy token that
never got reported. `scan()` now splits a partially-overlapping candidate at
claim boundaries (`_unclaimed_subspans`) and judges each still-unclaimed run
on its own merits — a fully unclaimed candidate (the common case) is
unaffected and still evaluated in one pass.

Tests: `test_a_secret_glued_onto_a_pattern_match_is_still_reported`
(tests/test_secrets.py) — fails on the pre-fix code (the tail secret is
silently absent from `scan()`'s results).

**R170c. `resume_from` drops a trailing dangling `user` record instead of
letting `send()` duplicate it — `engine.py`.**
A session that crashed mid-turn (provider error, `kill -9`, power loss)
logs its `user` record and never reaches the `assistant` reply, so the
log's last record is a dangling `user`. R128's same-role merge only
collapses CONSECUTIVE records within the replay — it has nothing to merge
here, since there's no second `user` record yet to merge WITH. Left alone,
the next `send()` appends its own new `user` message unconditionally
(`self.messages.append(user_msg)`, no last-role check), producing two
adjacent `user` turns that strict-alternation providers (Anthropic-family,
via OpenRouter) reject on the very first request after `--continue`.
`resume_from` now drops a trailing `user` record before restoring — the
prompt is still in the JSONL for `/context` or a human reading the log,
just not replayed into live history where it would never get an answer
anyway. If that dangling record was the ONLY thing ever logged, the
session still resumes (same log id, empty history) rather than silently
forking a new session id.

Tests: `test_resume_drops_a_trailing_dangling_user_message`,
`test_resume_drops_dangling_user_message_when_it_is_the_only_record`
(tests/test_core.py) — both fail on the pre-fix code (the dangling prompt
survives into live history, or the session id gets orphaned). The
pre-existing `test_resume_gauge_matches_exact_char_count` was adjusted to
end on an `assistant` record so it keeps testing char-count summation
rather than incidentally exercising the new drop behavior.

**R170d. The cursor lands at the end of a restored draft after an approval
challenge, not at the start — `tui.py`.**
`ask()`/`select_menu()` teardown assigns `self.input.buffer.text =
self._saved_draft` directly (not `insert_text()`) to avoid triggering the
async completer — but the `.text` property setter only clamps
`cursor_position` if it now exceeds the new text's length; it never moves
it. `buffer.reset()` at challenge-open time had already put it at 0, so
after every approval prompt or menu, the cursor silently jumped to the
start of whatever the user had been typing instead of staying where they
left off. Both teardown paths now set `cursor_position = len(saved_draft)`
right after restoring the text.

Tests: `test_ask_preserves_and_restores_input_draft` and
`test_select_menu_preserves_and_restores_input_draft` (tests/test_tui.py)
gained a cursor-position assertion each — both fail on the pre-fix code
(cursor stuck at 0).

**R170e. A timed-out command that leaves an escaped grandchild now says so
instead of pretending cleanup finished — `tools.py`.**
`_kill_group` can only reach processes still in the SIGKILL'd process
group; a grandchild that double-forked into its own session (`setsid`, a
daemonizing tool) escapes it, can keep holding the stdout pipe open, and
the final `proc.wait(timeout=5)` cleanup can itself time out. That was
swallowed by a bare `except TimeoutExpired: pass` — the tool call
"succeeded" with `[timeout after Ns]` and no sign anything was left
running. This is still a best-effort situation (the escaped process can't
be reached from here, full stop — that's the nature of the escape), but
`_run_command_once` now appends a `[warning: a grandchild process may have
escaped cleanup and could still be running]` line to the returned output
instead of staying silent about it.

Tests: `test_run_command_warns_when_final_reap_itself_times_out`
(tests/test_core.py) — fails on the pre-fix code (no warning in the
output).

**R170f. `session.log()` serializes concurrent writes with an advisory
lock — `session.py`.**
O_APPEND makes a single `write()` syscall atomic, but only within one
process's own file descriptor — two Aurora processes resumed onto the
SAME session id (`aurora --resume abc123` twice) had no coordination
whatsoever, so a `json.dumps()` + `write()` in one could interleave with
another's at the OS scheduler's whim, corrupting a line or duplicating a
turn index. `log()` now wraps the write in `fcntl.flock(f, LOCK_EX)` —
every writer takes the same advisory lock on the log file itself, no new
lock/state file needed. POSIX only (`fcntl` doesn't exist on Windows);
falls back to the previous best-effort behavior there, same as before
this fix.

Tests: `test_session_log_serializes_concurrent_writes_no_interleaving`
(tests/test_core.py) — forces the race by making `json.dumps` sleep mid
critical-section, then asserts every writer's timed interval is fully
disjoint from every other's; fails on the pre-fix code (overlapping
intervals, proving two writers could have interleaved on disk).

**R170g. The "possible secret in this command" notice now says it's a
raw-text scan, not a guarantee — `agent.py`.**
R58's command-parameter notice only ever scans the literal `command`
string — it can't decode or execute anything, so a secret piped through
`base64 -d`, hex, or built up via `$(...)` substitution never appears in
the text it scans and is invisible to it. That's an inherent limit of a
static text scan (not something to fix here), but the wording — "possible
secret in this command" — implied more assurance than the check can back
up: a clean scan reads as "we checked, it's fine" when it only means
"nothing suspicious in the LITERAL string." The notice now says so:
"(raw-text scan, easy to evade via encoding — not a guarantee)".

Tests: `test_run_command_param_secret_is_notice_only_and_still_runs`
(tests/test_core.py) gained an assertion for the new wording — fails on
the pre-fix code (old wording had no such caveat).

**R170h. MCP servers no longer inherit Aurora's full environment —
`mcp.py`.**
`MCPServer.__init__` built the child's environment as `{**os.environ,
**env} if env else None` — when a server's config had no explicit `env:`
block, `Popen(env=None)` made it inherit Aurora's ENTIRE `os.environ`,
including Aurora's own provider API keys if the user set them as env vars
(`OPENROUTER_API_KEY` etc.). A compromised or merely misconfigured server
— MCP server config is just YAML the user edits, and could be templated
from an untrusted source — got them for free. A new `_child_env` builds
from a minimal allowlist instead (`PATH`, `HOME`, `LANG`, `LC_ALL`,
`TMPDIR`, `USER`, `SHELL`, `TERM` — just enough to run a normal program),
then layers the server's own already-keystore-resolved `env:` config on
top. (Named `_child_env`, not `_resolve_env` — that name was already
taken by the existing function that resolves a config's `env:
{CHILD_VAR: AURORA_KEYSTORE_VAR}` mapping through the keystore; `_child_env`
is the later step that turns ITS output into what actually reaches
`Popen`.) A server that genuinely needs something else still declares it
in its own config, same as always.

Tests: `test_mcp_server_does_not_inherit_auroras_own_secrets`,
`test_mcp_server_still_gets_its_own_configured_env` (tests/test_mcp.py),
against a new dedicated fixture `tests/fixtures/env_mcp_server.py` (a
`getenv` tool reporting the child process's own environment — kept
separate from `fake_mcp_server.py` so its extra tool doesn't shift
tool-count assertions elsewhere). Both fail on the pre-fix code (the
monkeypatched `OPENROUTER_API_KEY` leaks straight through).

**R170i. `keystore.forget_passphrase()` — a real way to drop the cached
encrypted-file passphrase — `keystore.py`.**
`_passphrase_cache["pw"]` holds the decrypted passphrase for the entire
process lifetime once entered, with no timeout — a core dump or `ptrace`
of a long-running Aurora session exposes it, and every key in the
encrypted file with it. There was no way to clear it at all short of
exiting the process: `clear_key()` (the existing, similarly-named function)
clears a STORED KEY, a different operation — it in fact POPULATES the
cache on success, to avoid re-prompting right after. `forget_passphrase()`
is the actual primitive: pops the cache entry, returns whether there was
anything to pop. Best effort, documented as such — `bytes` are immutable
in Python, so this drops the dict's only reference rather than
overwriting memory in place; it closes the easy access path, not a
guarantee against every form of memory inspection. Not wired to a UI
trigger yet (a `/key forget` command, an inactivity timeout) — that's a
new feature surface, out of scope for this fix; the primitive existing
and being tested is the actual gap this closes.

Tests: `test_forget_passphrase_drops_the_cached_passphrase`
(tests/test_core.py).

**R170j. The `/model` picker no longer blocks on a live `/props` probe
against a dead local server — `ui.py`, `engine.py`.**
`_pick_model` called `provider.live_model_name()` directly and
synchronously to label the "local" entry — a live `/props` GET (4s
timeout) behind an endpoint pick, run before the menu could even render.
`context_stats()` had already been fixed for the exact same class of
freeze on `live_context_limit` (R95i's `_context_limit_nonblocking`:
serve a cache immediately, refresh on a daemon thread), but that fix
never covered `live_model_name`. A new `Engine._live_model_name_nonblocking`
mirrors `_context_limit_nonblocking`'s cache/pending/lock shape (120s TTL,
one refresh thread per key even under a burst of calls) and `_pick_model`
now calls that instead — `None` on the very first call (picker just shows
"local" with no live name that one time, same as any other cache-miss
path in this codebase), populated in the background after.

Tests: `test_model_picker_does_not_block_on_a_slow_live_model_name_probe`
(tests/test_core.py) — a fake provider whose `live_model_name()` raises if
called synchronously; fails on the pre-fix code (`_pick_model` calls it
directly).

**R170k. `_ChatWriter` coalesces a `print()` statement's two writes into
one append — `tui.py`.**
`print("hello")` calls `.write()` TWICE — once for the joined args, once
more for `end` (`"\n"` by default) — and each used to be its own
`self._tui.append()` call (lock acquire + `app.invalidate()`), so one
visible line of output cost two full round trips. `write()` now buffers
until it sees a newline in what it was just given, then flushes the whole
buffer as ONE `append()` call — a multi-line blob (a subprocess's captured
output, already written in one call) still flushes immediately, since it
already contains its own newline(s); only the common print()
content/end split is now one round trip instead of two. `flush()` was a
total no-op before this fix and nothing in Aurora's own code called it —
it now actually pushes whatever's buffered, so a `print(x, end="")`
progress-indicator style (none exists in Aurora's own code today, but a
user's own extension could reasonably write that way) still reaches the
screen the moment it's explicitly flushed, same contract a real terminal
gives a partially-buffered stream.

Tests: `test_chat_writer_coalesces_a_print_statements_two_writes`,
`test_chat_writer_flushes_immediately_on_embedded_newline`,
`test_chat_writer_flush_pushes_a_trailing_partial_line`
(tests/test_tui.py) — the first and third fail on the pre-fix code (every
write appends immediately, so buffering assertions see appends that
shouldn't have happened yet).

**R170l. Bash-mode output is stripped of OSC/DCS/APC/PM/SOS control
sequences before it reaches the transcript — `tui.py`.**
`ANSI(text).__pt_formatted_text__()` (how bash output is rendered, in
`_entry_fragments`) only recognizes CSI (`\x1b[...`) sequences for
styling — anything else after an ESC falls into its "not `[` → continue"
branch, which drops the ESC and the ONE character read after it but then
resumes parsing the rest of that sequence's body as ordinary text. So a
malicious or compromised command's OSC 52 (clipboard write), a
window-title/resize DCS payload, or similar didn't get forwarded to the
real terminal (prompt_toolkit fully owns rendering and never blindly
passes raw bytes through) — but it DID show up as garbled literal
characters in the transcript, and a crafted payload with its own embedded
CSI sequence inside that fallthrough text could still inject real style
codes. A new `_strip_dangerous_escapes` (OSC: BEL- or ST-terminated;
DCS/SOS/PM/APC: ST-terminated) removes these entirely before
`append_bash_output` stores the text — so neither the display nor
anything copied out of it (`/copy-all`, session export) carries the raw
sequence. Plain CSI color codes are deliberately left alone; other
`append*` paths (`append`, `append_bash_output`'s sibling for LLM
prose/tool text) are untouched since they don't carry raw, uncontrolled
subprocess output.

Tests: `test_strip_dangerous_escapes_removes_osc52_clipboard_hijack`,
`test_strip_dangerous_escapes_removes_st_terminated_osc`,
`test_strip_dangerous_escapes_removes_dcs`,
`test_strip_dangerous_escapes_leaves_plain_csi_color_codes_alone`,
`test_append_bash_output_strips_dangerous_escapes` (tests/test_tui.py) —
all fail on the pre-fix code (`_strip_dangerous_escapes` didn't exist /
the raw sequence survived into stored bash output).

### R171. External review pass: bugs, security, performance, hardening (2026-07-27)

A batch from one external code-review pass over `aurora/`, each item
verified against the actual code before fixing (one claim, B3 on
`_fallback_models()`'s `cur=None` filter, was checked and found to already
be correct behavior — no fix needed, no entry). Two findings (P2, P4) were
confirmed as real but explicitly non-bugs ("fine today", "no functional
problem") and got no code change, only this note. I7 (`cancellable_sse`'s
private-httpcore `abort()`) was already defensive (falls back to
`resp.close()`) and is left as accepted risk.

**R171a. A user cancel no longer triggers model fallback — `agent.py`,
`engine.py`.** `Turn` gained a `cancelled: bool` field. `run_turn` has
THREE separate cancellation exits — the top-of-loop check before a request
is even sent, `result.stop_reason == "cancelled"` for a cancel that lands
mid-stream, and the per-tool-call loop's own `cb.cancelled()` check — and
the first pass at this fix only set the flag on the first of the three. A
follow-up review (self-caught, same session) traced all three and found
the mid-stream path unguarded: it returns before `messages.append(...)`,
so `len(self.messages) > before` is False there too, and
`_run_turn_with_fallback` still fell through to a live fallback request on
a mid-stream cancel. All three now set `turn.cancelled = True`.
`_run_turn_with_fallback`'s only "did this make progress" test was
`len(self.messages) > before` — a cancelled turn also produces zero new
messages (in two of the three shapes), so it fell straight into
`switch_model` and fired a fresh request against the next configured model,
the opposite of what cancel means. Now checked before the no-progress
fallback path and returned immediately.
Tests: `test_cancelled_turn_does_not_fall_back_to_another_model` (the
top-of-loop shape), `test_cancelled_mid_stream_also_skips_fallback` (the
shape the first pass missed — verified to fail without the fix),
`test_cancelled_mid_tool_loop_also_skips_fallback` (companion coverage for
the third shape, kept even though `len(self.messages) > before` already
happened to catch it) (tests/test_core.py).

**R171b. `resume_from` counts TURNS, not messages — `engine.py`.**
`restored` incremented once per restored message (both user and
assistant), but `__main__`'s `"({n} turns)"` print means one user+assistant
exchange — a 10-turn (20-message) session reported "20 turns". Now only
counted on the `user` side.
Tests: `test_resume_from_counts_turns_not_messages` (tests/test_core.py);
updated `test_resume_restores_the_context_gauge`,
`test_resume_gauge_matches_exact_char_count`,
`test_resume_drops_a_trailing_dangling_user_message`
(tests/test_core.py), `test_resume_rebuilds_history` (tests/test_finish.py)
to the corrected counts.

**R171c. `_open_ui_menu` no longer eats a half-typed draft — `tui.py`.**
R155/R159 fixed this per-caller (the copy menu, the compactions click);
`_open_ui_menu` itself still did an unconditional `buffer.reset()`, so
every Esc-Esc confirm (cancel busy work / leave bash mode / quit) opened
through it with no draft save — the 2s arm window lets the user type after
arming, and the second Esc ate it. `_open_ui_menu` now saves the draft into
`self._menu_draft` before resetting; `_resolve_menu` restores it on every
outcome, cancel included, same "restore only if the box is still empty"
guard the copy menu already used.
Tests: `test_open_ui_menu_preserves_a_half_typed_draft_generically`
(tests/test_tui.py).

**R171d. `_input_height` now accounts for WORD wrap, not just a char-count
ceil-div — `tui.py`.** `wrap_lines=True` breaks at word boundaries, which
can need more rows than `ceil(len(line)/cols)` predicts whenever a word
wouldn't fit whole — undercounting on a narrow terminal clips the cursor
below the input box (no scrollback there, min==max==preferred). Row
counting now runs each line through `textwrap.wrap(..., break_long_words=
True, drop_whitespace=False)` to mirror the real wrap behavior.
Tests: `test_input_height_accounts_for_word_wrap_not_just_char_count`
(tests/test_tui.py).

**R171e. `_ChatWriter` is now a real `io.TextIOBase` — `tui.py`.** Its
`write()` return value (chars accepted, not chars already flushed to
screen — R170k buffers until a newline) is the standard buffered-stream
contract, same as a real `io.BufferedWriter`, but nothing made that
explicit or gave `isinstance(sys.stdout, io.IOBase)` a reason to hold for
an extension that checks it.
Tests: `test_chat_writer_is_a_real_io_textiobase` (tests/test_tui.py).

**R171f. Resumed sessions seed the live `$` gauge from their own log —
`engine.py`.** `resume_from` rebuilt `self.messages` but left `self._cost`
at its constructor default (0.0), so the status-bar badge and `/cost`
disagreed with the same session's `/context <id>` view after
`--continue` — the log has the true total, the live gauge didn't. Now
seeded via `ctxtree.model_breakdown_lines(session.usage_by_model(...))`,
the same pricing basis `/context` itself uses; `self._cost_priced` (R171u)
is seeded alongside it.
Tests: `test_resume_from_seeds_cost_from_the_sessions_own_log`
(tests/test_core.py).

**R171g. The final reap-wait after a command timeout shrank from 5s to 1s
— `tools.py`.** `_kill_group` already SIGKILLs the direct child before this
`proc.wait(...)` runs — reaping it is normally instant, and the wait exists
only to avoid a zombie, not to give an escaped grandchild (R170e) time to
die. A 5s bound blocked the WORKER THREAD (no Esc polling happens here) up
to 5s past the command's own timeout, on a case the wait can't even fix —
the "may have escaped cleanup" warning still fires either way.
Tests: `test_run_command_warns_when_final_reap_itself_times_out`
(tests/test_core.py) now pins the timeout value at 1.

**R171h. `_strip_dangerous_escapes` now strips an UNTERMINATED OSC/DCS too,
and `append()` (not just `append_bash_output`) is covered — `tui.py`.**
The existing regex required a BEL/ST terminator for both OSC and DCS/SOS/
PM/APC; a crashed binary, a truncated capture, or a deliberately malformed
payload emitting one with no terminator at all (`"\x1b]52;c;PAYLOAD"`, no
trailing BEL/ST) passed straight through into stored bash output. Two new
alternatives strip from the introducer to the end of the string when no
terminator appears anywhere in the (fully-captured, never streamed)
output. Separately, R170l scoped the strip to `append_bash_output` only,
reasoning that model output was "trusted-ish" — but a compromised or
prompt-injected model can put an OSC 52 clipboard write in its own reply
just as easily as a subprocess can, and `append()` is the path that reply
renders through, so it now strips too.
Tests: `test_strip_dangerous_escapes_removes_unterminated_osc52`,
`test_strip_dangerous_escapes_removes_unterminated_dcs`,
`test_append_strips_dangerous_escapes_from_llm_text` (tests/test_tui.py).

**R171i. Effective `extra_body` keys are logged per assistant turn —
`engine.py`.** `provider.extra_body` is a plain mutable attribute on a
provider object the engine hands to extension code holding a reference to
`engine` — a `tools.set_extensions` runner could mutate it for a LATER
turn with no trace anywhere. Not closed (extension code is arbitrary
Python, accepted risk), but now visible: `session.log("assistant", ...,
extra_body_keys=sorted(self._provider.extra_body))` (keys only, values
could carry secrets).
Tests: `test_send_logs_effective_extra_body_keys` (tests/test_core.py).

**R171j. `/bootstrap` fetches now require explicit review before they're
saved/persisted — `bootstrap.py`, `ui.py`.** The first-ever download of a
`/bootstrap set <url>` had no integrity check at all (no pinning/hash,
`follow_redirects=True`), and the cached copy it produces later gets
offered — and can be run with tools enabled — at every startup. A
compromised URL or a redirect attack on that first fetch reached a
tool-enabled turn with nothing between fetch and execution.
`refresh_from_source` gained a `confirm(old_text, new_text) -> bool`
parameter (default `None` keeps the old unconditional-overwrite behavior
for non-interactive callers) and now skips the rewrite entirely when the
fetched content is byte-identical to the cache — no diff, no confirm, no
write. `ui.py`'s redownload flow and both `/bootstrap set <url>` fetch
paths (typed arg and pasted-URL) now print a preview and ask before
saving/persisting.
Tests: `test_bootstrap_refresh_from_source_rejects_when_confirm_declines`,
`test_bootstrap_refresh_from_source_persists_when_confirm_accepts`,
`test_bootstrap_refresh_from_source_skips_confirm_when_unchanged`
(tests/test_core.py); updated `test_set_from_real_url_then_refresh`
(tests/test_bootstrap_network.py) for the new unchanged-content no-op.

**R171k. Clipboard-write policy documented (no code change) —
`clipboard.py`.** `copy()` is reachable from the model's own output (copy
picker, `/copy-all`) with no size/rate gate beyond `_osc52`'s 100KB
truncation — the symmetric direction of R171h's OSC-strip policy. Accepted
risk, same trust boundary as the rest of the model's output; documented in
a docstring rather than rate-limited, since a legitimate large copy (a
full session export) is normal use.

**R171l. MCP child-env allowlist policy documented (no code change) —
`mcp.py`.** `HOME`/`USER`/`TMPDIR` still pass through verbatim to MCP
servers (R170h only closed the secret-key leak) — a compromised server
still learns the username and temp-dir path. Not closed: an MCP server is
TRUSTED code the user configured, same level as a shell command or
extension Aurora runs; env-filtering was never meant to be a sandbox
boundary. Documented so that assumption isn't silently implied.

**R171m. A connection retry now notifies and jitters its backoff —
`providers/base.py`, `providers/openai_compat.py`, `engine.py`.** Each
retry re-sends the whole prompt (a real cost on a remote model) with
nothing telling the user why the request seemed to restart, and the flat
`0.3 * attempt` delay had no jitter (a provider-side outage hitting many
Aurora instances at once would retry in lockstep). `Provider` gained a
`notify` attribute (same per-turn-set pattern as `on_think`), wired from
`engine.py`'s `send()`/`_run_turn_with_fallback` alongside it; the retry
path calls it and adds `random.uniform(0, 0.2)` to the delay.
A follow-up review found one call site the first pass missed:
`compact_history()` (the `/compact` and auto-compact summarization
request) builds its own provider via `_provider_for` and never set
`.notify`, so a connection retry during a fold stayed silent — the
provider's safe `None` default meant this didn't crash, just under-covered
the stated goal. `compact_history` gained a `notify=None` parameter, wired
onto the provider before its request; both `_maybe_auto_compact`/
`_maybe_auto_compact_mid_turn` and `ui.py`'s manual `/compact` now pass
`fe.notify` through.
Tests: `test_turn_retry_notifies_before_resending` (tests/test_core.py);
`test_auto_compact_fires_only_past_threshold` (tests/test_core.py, updated
— its fake config's summarization request now genuinely retries against
an unreachable URL, so the retry notices legitimately appear in `notes`
before the auto-compact one).

**R171n. `_live_clock_key` no longer scans the whole scrollback per render
— `tui.py`.** Called from `_fragments()` every render (0.5s ticker,
every keystroke, every mouse move) while any think row is open, to find
rows that are almost always 0 or 1. `_open_think_items` (a list of dict
refs, not indices — `_evict_locked` drops from the FRONT of `_chat`, which
would shift a stored index) is now maintained at `begin_think`/
`think_chunk` create time and cleared in `_close_think_locked`, so the key
function iterates only the actually-open rows.
Tests: `test_live_clock_key_uses_tracked_open_rows_not_a_full_scan`
(tests/test_tui.py).

**R171o. `separators=(", ", ": ")` pinned explicitly on every session-log
write — `session.py`.** `iter_records`'s prefilter depends on
`'"event": "<name>"'` being an exact substring of every line — true only
because `json.dumps`'s DEFAULT happens to produce it. Pinning the
separators explicitly turns that from an implicit assumption riding on a
stdlib default into a stated contract.

**R171p. `/cost <id>` is now an alias for `/context <id>` — `ui.py`.**
Two commands both named around "cost" with different scopes (`/cost` = all
sessions, `/context <id>` = one session's tree) meant remembering which one
takes an id. `/cost` with an argument now dispatches to the same
`ctxtree.report`.

**R171q. The iteration-cap prompt says "request rounds", not "tool
iterations" — `ui.py`.** `Turn.iterations` counts REQUEST rounds
(`agent.py` increments once per `provider.turn` call), including a round
with zero tool calls and the R5 corrective-retry round — "tool iterations"
overclaimed what was actually being counted.

**R171r. `/remember`'s redraft loop notifies when it gives up —
`memory.py`.** The `for _attempt in range(3)` loop (y/n/s or two redrafts
max) fell out silently when the user kept picking "c" on the 3rd attempt,
reading as if the finding just vanished. Now notifies "redraft limit
reached — skipped" via a `for/else`.
Tests: `test_remember_notifies_when_redraft_limit_reached`
(tests/test_memory.py).

**R171s. `skills.run` now goes through `_run_command_once` — `skills.py`.**
A bare `subprocess.run(cmd, timeout=300)` is the exact shape R125c fixed
for TUI bash mode: on timeout it kills only the direct child, so a skill
that forks/backgrounds something (a dev server, a build) leaves it running
forever, reparented to init. Routed through the same process-group-safe
helper `run_command` uses; the 300s cap is now `skills._SKILL_TIMEOUT`
(module-level, was inline) so it can be overridden.
Tests: `test_skill_run_kills_the_whole_process_group_on_timeout`,
`test_skill_run_reports_exit_code_and_output` (tests/test_core.py);
updated `test_skill_run_survives_exec_format_error` (tests/test_finish.py)
— the shell now reports a bad interpreter as a normal non-zero exit rather
than Aurora catching an OSError, same safe outcome, different failure
shape.

**R171t. A scaffolded-but-unedited extension now raises instead of lying
— `extensions.py`.** The template's `return "not implemented"` was a
loadable, discoverable tool that looked like a successful call returning
that string. Now `raise NotImplementedError(...)`, which surfaces as
`[tool error: ...]` — honest about "this was never finished".
Tests: `test_scaffolded_extension_raises_not_implemented`
(tests/test_core.py); updated `test_scaffold_writes_a_loadable_spec_
runners_file` (tests/test_mcp.py) to expect the raise.

**R171u. `write_text_atomic` now fsyncs the DIRECTORY too — `paths.py`.**
The temp file's own fsync (R146a) only makes ITS contents durable — the
`os.replace` rename that makes it visible AS the target path is a separate
directory-entry write, which can still be lost on a power loss even though
the temp file's data was safely flushed (surviving file is the OLD
contents, new inode unlinked). POSIX only, best-effort (no directory fd on
Windows), same fallback shape as `session.py`'s POSIX-only `flock`.
Tests: `test_atomic_write_fsyncs_the_directory` (tests/test_core.py).

**R171v. `cost_known` no longer hides a real accrued `$` figure after a
`/model` switch — `engine.py`.** `context_stats()` asked only whether the
CURRENT model has pricing; switching mid-session from a priced model
(with real accrued `self._cost`) to an unpriced one made the `$` badge
disappear entirely, reading as "this session cost $0" instead of "the
current model is unpriced, on top of $X already spent". A new
`self._cost_priced` flag, set the first time `_live_usage` accrues cost
from a round with real pricing (and seeded on resume, R171f), keeps
`cost_known` true once anything real has been priced.

**R171w. `tui_crash.log` is capped at 1MB — `tui.py`.** An accident log
(crash tracebacks), never meant to fall under R20's "keep everything
forever" policy (that's the session JSONL's job) — grew unbounded on a
machine that crashes often. Truncated to its last 1MB before each append,
same "keep the recent tail" shape used elsewhere.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line and `ARCHITECTURE.md`'s two
`R1–R__+` spans, all R170 → R171.

### R172. A 24h-scope re-audit: two cleanups, no bugs found (2026-07-27)

A second, wider deep-dive review (this time spanning every commit in the
prior 24h, R139 through the R171 follow-up — 53 commits) re-verified each
fix against the CURRENT code rather than trusting past AURORA.md prose.
Verdict: no live bugs anywhere in that range. Two non-bugs were worth
cleaning up while already in that code.

**R172a. Collapsed the copy-menu and compact-menu's own draft-save/restore
into R171c's generic one — `tui.py`.** R155 (`_copy_menu_draft`) and R159
(`_compact_menu_draft`) each independently saved/restored the input draft
around their own `_open_ui_menu` call, predating R171c's later fix that
made `_open_ui_menu`/`_resolve_menu` do this generically for every caller.
Both mechanisms were firing on every click, but harmlessly — R171c's
restore ran first (inside `_resolve_menu`, before the per-menu callback),
so each menu's own restore always found a non-empty buffer and no-opped
under its "only if still empty" guard. No double-write, just two
mechanisms doing one job. `_copy_menu_click`/`_resolve_copy_menu` and
`_compactions_click`/`_resolve_compact_menu` now rely solely on the
generic path; `_copy_menu_draft`/`_compact_menu_draft` are gone.
Tests: existing draft-preservation tests
(`test_opening_the_copy_menu_does_not_eat_the_draft`,
`test_open_ui_menu_preserves_a_half_typed_draft_generically`, and the
compact-menu equivalents in tests/test_tui.py) still pass unchanged,
confirming the generic path alone is sufficient.

**R172b. `engine.compactions` (R159's live counter) gets direct test
coverage — `tests/test_core.py`.** The counter's increment-on-fold and
recompute-on-resume logic had no test asserting the counter itself; the
only related test changes were fixture attribute additions needed so
OTHER tests wouldn't break on the new field. Three tests added: it
increments once per actual fold, it does NOT increment on any of
`compact_history`'s early `return 0` no-op paths, and `resume_from`
recomputes it from the session's own `compact` log records rather than
inheriting the fresh Engine's constructor default.

### R173. Two review findings fixed: a false loop-repeat nudge, and a config-typo code-injection path into MCP children (2026-07-27)

**R173a. The agent loop's repeat-nudge is now keyed on the RESULT, not just
the call — `agent.py`.** `run_turn` used to flag a repeated tool call as
identical to last round's purely by (name, args), then told the model "you
already ran this exact call with this exact result" unconditionally — even
when the underlying file/command had changed between rounds (the ordinary
write → test → fix → re-test pattern). A model that trusts the note stops
re-running a test it just fixed. `last_calls` (a set of call keys) is
replaced with `last_results` (call key → prior round's output string); the
nudge now only fires when this round's output for that key matches the
prior round's exactly. Tests:
`test_agent_loop_nudge_fires_on_a_true_repeat`,
`test_agent_loop_nudge_does_not_fire_when_the_result_changed`.

**R173b. `_child_env` now strips dynamic-loader/interpreter-startup vars
from an MCP server's `env:` config, unconditionally — `mcp.py`.** R170h/S5
guard the LEAK direction (Aurora's own secrets never reach a server
uninvited); nothing guarded the INJECTION direction. A config's `env:` map
is user-authored YAML, hand-edited and typo-prone — `env: {LD_PRELOAD:
SOME_KEY}` (deliberate or a fat-fingered literal a keystore lookup happens
to resolve) landed verbatim in the child's real environment, which is code
injection into that process's next dynamic-link, not an identity leak like
HOME/USER. A new `_ENV_DENYLIST` (`LD_PRELOAD`, `LD_LIBRARY_PATH`,
`LD_AUDIT`, `DYLD_INSERT_LIBRARIES` and its siblings, `BASH_ENV`, `ENV`,
`PYTHONSTARTUP`, `NODE_OPTIONS`, `PERL5LIB`, `RUBYOPT`, `GCONV_PATH`) is
popped from the merged env after the config's `env:` is applied — always
stripped, not just filtered by allowlist membership, so no config value can
reintroduce it. Test:
`test_mcp_server_config_cannot_inject_a_loader_var_into_the_child`.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R172 → R173.

### R174. Five requested features: /diff, wait_until's `then`, session log rotation, a maskable secret challenge, cached model latency (2026-07-27)

**R174a. `/diff` shows what the last turn actually changed — `rewind.py`,
`engine.py`, `ui.py`.** R47's shadow-repo checkpoints already have both
endpoints of "what changed"; there was no read-only way to ask for the
diff. `rewind.head(cwd)` returns the shadow repo's current checkpoint HEAD
(or `None` before any checkpoint exists); `engine.send()` now calls it
BEFORE the turn runs and stores it as `self.last_turn_diff_base`.
`rewind.diff_since(ref, cwd)` diffs the working tree against that ref (or
against git's empty-tree hash when `ref` is `None`, so a project's very
first mutation still shows a real diff). `/diff` reads
`engine.last_turn_diff_base` and prints the result via the same
`colour_diff` `/commit` uses. Tests: `test_head_is_none_before_any_checkpoint`,
`test_head_returns_the_current_checkpoint`,
`test_diff_since_shows_changes_made_after_the_given_checkpoint`,
`test_diff_since_none_diffs_against_the_empty_tree`,
`test_diff_since_bad_ref_is_a_message_not_a_crash`,
`test_diff_since_is_empty_when_nothing_changed` (tests/test_rewind.py),
`test_send_records_the_checkpoint_head_before_the_turn_runs`
(tests/test_core.py).

**R174b. `wait_until` gets a `then` command, run once right after success — `tools.py`, `agent.py`, `ui.py`.** The natural next step after "wait for the
server to be listening" is "then curl it" — without `then` that was a
second, separate tool call, which re-races the exact condition wait_until
just polled for (a listener that accepts the TCP connection a moment before
it can actually answer). `then` runs in the SAME gated call, immediately
after `command` first exits 0; its own exit code and output are appended
to the result. The approval-prompt display bug this exposed is fixed too:
`wait_until` used to fall through to the generic `path` branch (empty for
it), so neither the polled `command` nor now `then` ever appeared at the
approval prompt at all — both frontends now show them like `run_command`.
R58's param-secret notice now scans `command` and `then` together, so a
credential in the follow-up isn't invisible just because it's in the newer
field. Tests: `test_wait_until_runs_then_once_after_success`,
`test_wait_until_then_output_is_included_in_the_result`,
`test_wait_until_then_never_runs_if_the_poll_never_succeeds`,
`test_wait_until_without_then_is_unchanged`,
`test_wait_until_then_reports_a_nonzero_exit`,
`test_wait_until_then_secret_gets_the_same_notice_as_command`
(tests/test_core.py),
`test_approve_shows_wait_until_command_and_then` (tests/test_tui.py).

**R174c. Session logs rotate by size, never delete — `session.py`.** R20's
"nothing is ever auto-deleted" made a daily driver's session JSONL
unbounded in SIZE as well as time — multi-MB logs that every `/cost`/
`/search` walked in full, eroding R96e's own prefilter as a single file
grew. Past `SESSION_LOG_MAX_BYTES` (5MB), a session's log continues in
`<id>.2.jsonl`, `<id>.3.jsonl`, etc — the guarantee is unchanged, only which
FILE a record lives in. Every reader (`iter_records`, `list_sessions`,
`usage_all_sessions`, `search_sessions`, `latest_session_id`) now walks a
session id's full part sequence via new `_parts_for`/`_base_session_ids`/
`_latest_mtime` helpers, so a rotated session is still exactly one entry
everywhere it used to be one entry, with its display mtime following
whichever part is currently being written. Tests:
`test_log_rotates_to_a_new_part_once_the_cap_is_exceeded`,
`test_rotated_parts_are_never_deleted_and_all_records_readable`,
`test_write_target_recomputes_from_disk_not_cached_state`,
`test_list_sessions_counts_a_rotated_session_once_and_shows_fresh_mtime`,
`test_usage_all_sessions_does_not_double_count_a_rotated_session`,
`test_search_sessions_finds_a_hit_in_a_rotated_part`,
`test_latest_session_id_follows_rotated_activity` (tests/test_finish.py).

**R174d. The secret challenge can mask the token — `secrets.py`, `ui.py`.**
`format_matches` printed the matched token in bold, unconditionally — the
one flow in Aurora that shows a detected secret in full no matter what the
user decides, worse than the transcript itself (which at least redacts on
"redact"). A new "v" choice in `secret_challenge` toggles `mask=True`,
which shows the kind and surrounding context but replaces the token with
`<kind hidden>`; picking "v" re-renders and re-asks rather than answering,
same loop shape as the approval gate's "e"xplain. Off by default — existing
behavior is unchanged unless asked for. Tests:
`test_format_matches_mask_hides_the_token_but_keeps_context_and_kind`
(tests/test_secrets.py),
`test_secret_challenge_v_toggles_masking_then_reasks` (tests/test_tui.py).

**R174e. `/model`'s picker shows cached last-request latency — `agent.py`,
`engine.py`, `session.py`, `ui.py`.** Deciding which model to pick had no
signal for "how fast is this one responding right now" short of guessing.
A live per-entry probe would add real latency/complexity to opening the
picker itself, so this is cache-only: `agent.run_turn` times each
successful `provider.turn()` call (`Turn.last_request_latency`, the LAST
completed round, not a sum); `engine.py` logs it as `latency_s` on the
`assistant` session record; `session.last_latency_by_model()` reads the
most recent `latency_s` per model across the newest sessions; the picker
shows it (`250ms` / `1.2s`) next to context/price. Silently absent for a
model with no recent record — never a stale guess. Tests:
`test_turn_records_the_last_successful_request_latency`,
`test_turn_latency_is_the_last_round_not_the_sum`,
`test_last_latency_by_model_reads_the_most_recent_record`,
`test_last_latency_by_model_ignores_records_missing_the_field`,
`test_last_latency_by_model_prefers_the_newer_session`,
`test_model_picker_shows_cached_latency_for_a_recently_used_model`
(tests/test_core.py).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R173 → R174.

### R175. R58's secret detection closes two coverage gaps: the assistant's own reply text, and write_file/edit_file/apply_patch arguments (2026-07-27)

A direct question ("does this actually prevent secrets from reaching
context/the provider?") turned up that R58's challenge only ever covered
the user's prompt and tool OUTPUT — three real paths bypassed it entirely
and put a secret straight into `messages` (and so into every subsequent
request to the provider) with no notice and no choice: the model's own
generated reply text, and the write_file/edit_file/apply_patch arguments
that write NEW content to disk. (A fourth, run_command/wait_until's
`command` argument, is a deliberate notice-only exception per R131/R170g,
documented there — not touched here.)

**R175a. The assistant's own reply text is now scanned — `agent.py`.**
Before `messages.append(provider.assistant_message(result))`, a non-empty
`result.text` is scanned the same way tool output is; a match triggers the
same keep/redact/stop challenge (`secret_challenge("reply", ...)`). Can't
un-stream text that already reached the screen via `cb.on_text` inside
`provider.turn()` — the fix's real value is keeping "redact" from
persisting the secret into history/the session log (so it isn't re-sent to
the provider on later rounds) and letting "stop" abort the turn before any
tool calls attached to that same reply run. `ui.py`'s `secret_challenge`
gained a `context == "reply"` branch ("the assistant's reply"). Tests:
`test_agent_scans_the_assistant_reply_and_redacts_it_in_history`,
`test_agent_reply_secret_stop_drops_the_reply_and_any_tool_calls`,
`test_agent_reply_secret_keep_still_sends_it`,
`test_agent_reply_without_a_secret_is_unaffected`,
`test_reply_scan_is_skipped_when_the_feature_is_off` (tests/test_core.py).

**R175b. write_file/edit_file/apply_patch's written-content argument is now
scanned — `agent.py`.** Same challenge, same placement (before
`messages.append`, so a redact mutates `call.arguments[field]` in place —
both the historical assistant message AND the write that runs later this
round see the redacted text). One field per tool: `content` (write_file),
`new` (edit_file), `diff` (apply_patch) — `edit_file`'s `old` is
deliberately NOT scanned, since it mirrors what's already in the file and
scanning it would just be a redundant re-challenge on text that isn't new
exposure. `ui.py` gained a `context.startswith("write:")` branch. Tests:
`test_agent_scans_write_file_content_and_redacts_before_the_write`,
`test_agent_write_file_secret_stop_never_writes`,
`test_agent_edit_file_new_field_is_scanned`,
`test_agent_edit_file_old_field_is_not_scanned`,
`test_agent_write_file_without_a_secret_is_unaffected`,
`test_write_arg_scan_is_skipped_when_the_feature_is_off` (tests/test_core.py).

**Still open, by design, not by oversight:** `run_command`/`wait_until`'s
`command` argument stays notice-only (R131/R170g) — the command needs its
real value to actually run, and blocking/rewriting it would either break
the call or duplicate the approval gate it already passed. A secret
embedded there is still visible to the user via the existing notice, just
not redacted from history.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R174 → R175.

### R176. New `find_files` tool; /cost's status-bar-vs-report cost discrepancy explained (2026-07-27)

**R176a. `find_files` — glob-based filename search — `tools.py`.** A gap
found comparing Aurora's tool set against another IDE assistant's: Aurora
had `grep` (content search) but nothing for "find a file by name pattern"
short of the model shelling out via `run_command`. `find_files(pattern,
path=".")` walks with `os.walk`, pruning the SAME directories `grep` does
(`GREP_PRUNE`) mid-walk — unlike `Path.rglob`, which has no way to skip a
subtree, so an excluded directory would still be fully listed even though
nothing in it could ever match. Matches against both the bare filename (a
plain `*.py` matches at any depth) and the path relative to `path` (so
`tests/test_*.py` also works). Capped at `MAX_FIND_RESULTS` (500) with a
truncation notice, same shape as `grep`/`read_file`'s own caps. Read-only,
no approval needed, added to `PARALLEL_SAFE` alongside `grep`/`read_file`.
Tests: `test_find_files_matches_by_bare_filename_at_any_depth`,
`test_find_files_matches_a_pattern_with_a_slash_against_the_relative_path`,
`test_find_files_prunes_the_same_dirs_grep_does`, `test_find_files_no_matches`,
`test_find_files_rejects_a_non_directory`,
`test_find_files_truncates_past_the_cap`,
`test_find_files_is_registered_read_only_and_parallel_safe` (tests/test_core.py).

**R176b. `/cost`'s all-sessions total now states the current session's own
figure too — `ui.py`.** A user reported clicking the status bar's `$3.31`
price (R168 made it a `/cost` link) and landing on a report whose bottom
line said `$8.6359`, read as a bug. It wasn't one: the status bar shows
`engine.context_stats().cost_usd` — THIS session's own accrued cost — while
`/cost` has always been the cross-session aggregate (R166), landed in a
separate change with no line connecting the two numbers. `_cost_report` now
appends `(this session so far: $X.XXXX — the number on the status bar;
already included in the total above)` whenever `cost_known` is true, so the
two figures visibly reconcile instead of just disagreeing. Omitted entirely
when `cost_known` is false (an unpriced/local-only session), matching the
status bar's own rule for hiding the `$` badge. Tests:
`test_cost_report_shows_this_session_cost_next_to_the_all_sessions_total`,
`test_cost_report_omits_the_session_line_when_cost_is_unknown`
(tests/test_core.py).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R175 → R176.

### R177. Three findings from an external code review, fixed — `agent.py`, `rewind.py`, `colors.py` (2026-07-27)

**R177a. A "stop" on a reply-embedded secret left history ending on the
caller's own user turn — `agent.py`'s R58 reply-scan.** The scan returned
`turn` immediately on `decision == "stop"`, before
`messages.append(provider.assistant_message(result))` — so `messages` was
left exactly as `Engine.send` had it BEFORE calling `run_turn` (ending on
the user message it appends before the turn starts), and any tool_calls
that round carried were silently dropped with no `_skip()`/`_flush()` (the
mechanism every other abandon path already uses to keep history valid).
The next `send()` then appended a second, consecutive user message — the
exact R44/R128 invalid sequence every other stop path was fixed to avoid.
Fix: redact and append a (tool-call-free) assistant message before
returning, so the turn closes validly and the raw secret still never lands
in history either way. Test:
`test_agent_reply_secret_stop_drops_the_reply_and_any_tool_calls` (updated
to assert the fixed behavior — it previously asserted the bug,
tests/test_core.py).

**R177b. `/diff` showed nothing for a turn whose only/last mutation was a
brand-new file — `rewind.diff_since`.** `checkpoint()` snapshots BEFORE a
mutation runs, not after, so a turn that ends on a `write_file` of a new
path leaves that file UNTRACKED relative to the "before this turn" ref —
and plain `git diff <ref> --` never shows untracked files at all, only
changes to files git already knows about. `/diff` then read as "the turn
did nothing" exactly when the user most wanted to see the new file (a
mid-session new file; the very-first-mutation-of-a-project case already
worked, since that diffs against the empty tree). Fix: `diff_since` now
runs `git add -A` (the same call `checkpoint()` itself makes, respecting
the same `info/exclude`) before diffing, then diffs against the INDEX
(`git diff --cached <ref> --`) instead of the working tree directly — the
next `checkpoint()` re-runs `git add -A` regardless, so leaving this
staged has no lasting effect. Test:
`test_diff_since_shows_a_new_untracked_file` (tests/test_rewind.py).

**R177c. A URL containing a raw ESC byte could smuggle an arbitrary
terminal escape into a hyperlink — `colors.URL_RE`/`linkify`.** The bare-URL
regex excluded `\s` and a handful of punctuation from a matched URL's body,
but `\s` doesn't cover ESC (0x1b) or other C0 control bytes — so a "URL"
copied verbatim out of fetched content (e.g. quoted back by the model in
its own reply) could carry one straight through the match. `linkify()`
splices the matched text verbatim into an OSC-8 hyperlink escape
(`\033]8;;<url>\033\\...`); an embedded ESC there lets the "URL" terminate
that escape early and open a NEW one the terminal will actually interpret
(an OSC-52 clipboard write, say) — classic REPL only (`ui.py`), since the
TUI parses its own ANSI and makes `linkify()` a no-op via `IN_TUI`. Fix:
`URL_RE` now excludes `\x00`–`\x1f`/`\x7f` from both the URL body and its
final character, closing it at the one shared regex both front ends'
URL-matching paths depend on. Tests: `test_url_re_excludes_escape_byte`,
`test_linkify_never_embeds_a_control_byte_in_its_output`
(new tests/test_colors.py).

**Findings evaluated and NOT changed** (external review flagged 10; 3
above were real, the rest didn't hold up on inspection — logged here so a
future pass doesn't re-litigate them from scratch): `wait_until`'s `then`
truncation claim was backwards (the success header is prepended at the
START of the capped output, not appended — truncation drops from the
`then` command's own tail, the less critical end); the R174e latency gap
on an escaped `ProviderError` during the R5 retry is real but already
absorbed by `engine._run_turn_with_fallback` (R143a) — a missing log entry
on an already-crashed turn, not corruption; the write-arg-scan ordering
concern was withdrawn by the reviewer on re-trace; MCP's `env:` PATH
override is user-authored `config.yaml` — the same trust level as the
`command`/`args` fields right next to it, so it adds no new privilege an
attacker who could already edit that file wouldn't already have; the three
performance findings (session log `stat()` calls, `search_sessions`'
missing prefilter, `secrets.scan`'s per-call allocation) are real shapes
but `secrets.scan` in particular never actually sees more than
`TOOL_OUTPUT_LIMIT` (60KB) — every call site truncates before scanning —
so the claimed 2MB worst case doesn't occur; worth a dedicated pass, not
folded into this bugfix commit.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R176 → R177.

### R178. `/undo` (precision revert) + a status-bar button for it; `/context`'s cost/ctx projection line (2026-07-27)

**R178a. `/undo` — revert just the LAST mutation, not the whole tree —
`rewind.py`.** `/rewind` is coarse: whole-tree reset to a snapshot, losing
everything after it. The common want is narrower — "undo just that last
`edit_file`" — and the shadow repo already has the data for it, since
`checkpoint()` runs before every approved mutation. Two cases, because that
means the LAST mutation is usually still just an uncommitted working-tree
change relative to HEAD, not its own commit: (1) **uncommitted** — `git add
-A` (to see untracked additions at all) then `git reset --hard HEAD`
discards exactly that, since R47's invariant (a checkpoint before EVERY
approved mutation) means nothing else could be dirty; (2) **already sealed
into HEAD** by a LATER mutation's own checkpoint call — reverted file-by-
file against HEAD~1 (added paths deleted, modified/deleted paths restored)
and committed as a new checkpoint, so every OTHER already-checkpointed
mutation survives, unlike resetting to an older ref. Wired as `/undo`
(`ui.py`) alongside `/rewind`. Tests: `test_undo_reverts_uncommitted_new_file`,
`test_undo_reverts_uncommitted_modification`,
`test_undo_reverts_only_the_last_sealed_mutation_keeping_earlier_ones`,
`test_undo_with_nothing_to_undo`, `test_undo_no_checkpoints_at_all`,
`test_undo_is_itself_recorded_as_a_new_checkpoint` (tests/test_rewind.py).

**R178b. A status-bar `undo` button, shown once the first file changes —
`tui.py`.** Feature request: don't make `/undo` type-only. The button
appears the first time `TuiFrontend.on_tool_result` sees a mutating tool
(`tools.NEEDS_APPROVAL`) succeed this session (`Tui._has_checkpoints`,
checked via one `rewind.head()` call — cheap, and only while still False,
so it costs at most once per session, never once per 0.5s status-bar
render) and never hides again. Clicking it opens a Yes/No confirm
(`_open_ui_menu`, the same non-blocking UI-thread pattern `_compactions_click`
uses) before queuing `/undo` — a button has no natural "which one" step the
way `/rewind`'s picker does, so a stray click must not silently revert
work. Tests: `test_status_bar_undo_button_hidden_until_a_checkpoint_exists`,
`test_status_bar_undo_click_confirms_before_queuing`,
`test_status_bar_undo_click_no_op_on_decline`,
`test_on_tool_result_flips_has_checkpoints_only_for_mutating_tools`
(tests/test_tui.py).

**R178c. `/context`'s "at this rate…" cost/ctx projection line —
`ctxtree.py`.** Feature request: the natural extension of R163's live `$`
badge from "here's the number now" to "here's where it's headed". Every
input already exists — per-turn `input_tokens` IS the running context size,
already summed for the tree's own `ctx`/`in` figures. `_projection()` takes
the session's own turn-over-turn ctx growth (linear, first-to-last over
every priced turn) and the live context limit (`engine.context_stats().limit`
— only known for the CURRENT session, so a past/other `/context <id>` never
shows it) and reports `~N more turn(s) until context fills`, plus the
extrapolated `$` total when pricing is known. Needs ≥3 priced turns and a
positive growth rate — a flat trend (all cache hits, or just past a
`/compact` fold) has no "fills up" point to project, so it prints nothing
rather than a misleading number. Tests:
`test_context_tree_projects_when_context_is_growing`,
`test_context_tree_no_projection_without_growth_or_live_limit`
(tests/test_core.py).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R177 → R178.

### R179. `/undo` incident, attempt 1 (incomplete — see R180): a blind confirm let it revert unrelated, already-sealed work instead of the file the user meant — `rewind.py`, `ui.py`, `tui.py` (2026-07-27)

**Correction, same day:** this entry's fix only made the wrong behavior
visible before confirming (named the files, required a yes/no) — it did
NOT stop `undo()` from being ABLE to silently pick an older, unrelated,
already-sealed mutation in the first place. Minutes after this shipped,
the exact same incident happened again on a different out-of-tree file
(`fibonacci.py` on the Desktop), with the confirm now correctly naming the
about-to-be-reverted files — and the user still had to catch it manually,
because the underlying "committed" fallback was still there, still wrong,
just no longer invisible. **R180 removes that fallback entirely** rather
than warning about it. Left in place as the historical record of what was
tried first and why it wasn't enough.

**What happened.** Minutes after R178 shipped, the user asked Aurora to
write `Weather Lisbon Today.txt` to their Desktop, then clicked the new
status-bar `undo` button expecting it to revert THAT file. Instead it
reverted the entirety of the R177/R178 session's own work in the Aurora
project (agent.py, colors.py, rewind.py, tools.py, tui.py, ui.py, tests,
the CHANGELOG entries) back to an hour-old state — silently, with no
indication anything but the weather file was in scope.

**Root cause.** The Desktop path is OUTSIDE the Aurora project's
checkpointed tree (`rewind.covers()`'s own long-documented gap — R47's
`git add -A` only ever sees paths inside `cwd`). `checkpoint()` still ran
before the write (R47 fires unconditionally) but found nothing to commit,
so by the time `/undo` ran there was NOTHING in the shadow repo
representing the weather-file write. `undo()`'s fallback — "tree is clean
against HEAD, so revert HEAD's own diff against HEAD~1 instead" — picked
up the PREVIOUS, unrelated, already-sealed mutation (this session's own
bugfix/feature commits) and reverted that instead. The button's confirm
("undo the last mutation?") named no file, so there was nothing in the UI
for the user to catch this on before confirming. The real project's own
`.git` history was never touched — only the working-tree files were
overwritten with older content — so recovery was a plain `git checkout --
<paths>` back to the last real commit; still, several minutes of surprise
and risk that should never have been possible.

**Fix — every caller previews before confirming, and shows what:**
- `rewind.undo_preview(cwd) -> (kind, paths)` — the exact detection logic
  `undo()` already had, split out so it can run WITHOUT mutating anything.
  `kind` is `"uncommitted"` (the common case), `"committed"` (the
  fallback branch — reverting an EARLIER, already-sealed mutation, not
  necessarily the one just run), `"none"`, or `"error"`. `undo()` now
  calls this internally instead of duplicating the logic.
- `ui._undo_cmd()` — the `/undo` command previews first and always shows
  the affected paths plus which branch fired (`"already sealed by a LATER
  checkpoint — an EARLIER mutation, not necessarily the one you just did"`
  for the committed case) before asking to confirm (default NO). It never
  calls `rewind.undo()` blind again.
- `tui.py`'s status-bar `undo` button no longer opens its own vague
  confirm menu (a mouse handler can't safely do the git plumbing
  `undo_preview` needs) — it just queues `/undo` through the inbox, same
  as `_cost_tree_click`/`_agentic_report_click` do for their own commands,
  so the ONE real confirmation (with real file names) happens in
  `_undo_cmd` on the worker thread.
- A second, unrelated bug found while fixing this: the "committed" branch's
  added/modified status lookup had its dict built backwards
  (`{status: path}` instead of `{path: status}`, from mis-ordering
  `git diff --name-status`'s `STATUS\tPATH` columns) — every lookup missed,
  so `undo()` fell through to `checkout HEAD~1 -- path` even for newly
  ADDED paths, which silently failed (the path doesn't exist in HEAD~1) and
  left them un-reverted. Caught by
  `test_undo_reverts_only_the_last_sealed_mutation_keeping_earlier_ones`
  failing after the `undo_preview` refactor — never shipped to a user
  (caught in this same session, before the fix was committed).

Tests: `test_undo_preview_reports_uncommitted_paths_without_touching_anything`,
`test_undo_preview_reports_committed_paths_without_touching_anything`,
`test_undo_preview_none_when_nothing_to_undo`,
`test_undo_after_an_out_of_tree_mutation_reverts_the_prior_in_tree_change_not_the_new_file`
(tests/test_rewind.py — the last one pins the exact incident scenario),
`test_status_bar_undo_click_queues_the_command` (tests/test_tui.py, replacing
the old menu-based test).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R178 → R179.

### Defaults
- Aurora **starts on whichever model is first in `config.yaml`'s
  `models:` list** — no longer necessarily the free local one; the user's
  own ordering decides.

### R180. `/undo`'s "committed" fallback removed entirely — it could never have been correct — `rewind.py`, `ui.py` (2026-07-27)

**Why R179 wasn't enough.** R179 made `undo()`'s dangerous fallback
branch VISIBLE before confirming; it didn't ask whether that branch
should exist at all. It shouldn't have. The invariant that proves it:
`checkpoint()` runs BEFORE a mutation, never after — so the mutation that
just ran, if it touched the checkpointed tree at all, is ALWAYS still
sitting as an uncommitted diff against HEAD the moment `/undo` runs.
Nothing seals it until the NEXT approved mutation's own pre-checkpoint
call fires. If the tree is clean against HEAD instead, that can only mean
the last mutation left no trace in this tree — its target was outside it
(`covers()`'s documented gap), or it was a genuine no-op — full stop.
`HEAD`'s own diff against `HEAD~1` in that state is always some OLDER,
already-reviewed, unrelated mutation; it can never legitimately BE "the
last mutation". The fallback branch was reachable specifically whenever
that clean-tree state occurred, which is exactly the condition an
out-of-tree write produces — so it fired on the very first thing anyone
would naturally try it on.

**The fix.** `rewind.undo_preview()` and `rewind.undo()` now only ever
report/act on the uncommitted diff against HEAD. A clean tree returns
`("none", [])` — "nothing to undo — the last action left no change in
this tracked directory" — and `undo()` never looks at `HEAD~1` again.
Wanting to revert something older/already-sealed is what `/rewind`
already does (list checkpoints, restore one explicitly — whole-tree, not
scoped to a single mutation, but explicit about which snapshot, unlike
guessing "the last one"). `ui._undo_cmd()` and the status-bar button
carry no fallback-specific messaging anymore since there's only one
outcome shape left to explain. Tests: rewrote
`test_undo_reverts_only_the_last_sealed_mutation_keeping_earlier_ones` into
`test_undo_never_reaches_into_already_sealed_history` (asserts the OLDER
mutation survives untouched and `/undo` reports nothing to undo, the
opposite of the old assertion), and
`test_undo_after_an_out_of_tree_mutation_reports_nothing_to_undo` (renamed
from `..._reverts_the_prior_in_tree_change...` — same incident scenario,
now pinning the SAFE outcome) (tests/test_rewind.py).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R179 → R180.

### R181. `/undo` now actually works on files outside the checkpointed tree — `rewind.py`, `agent.py`, `engine.py`, `tui.py` (2026-07-27)

**Why R180 wasn't enough either.** R180 made `/undo` SAFE (it stopped
reverting the wrong thing), but for the actual case that triggered both
incidents — editing a file outside the project directory — "safe" meant
"nothing to undo", not "undo the thing I just changed". The user tried it
a third time (`edit_file` on `/Users/alice/Desktop/notes.txt`)
and got exactly that: a correct but useless answer, with the status-bar
button still showing (from an earlier in-tree mutation) even though it
had nothing to act on for THIS edit. Two options were on the table — hide
the button when there's nothing to undo, or actually track the file and
undo it. This does the second, since it's strictly more useful.

**The fix: a location-independent per-file snapshot, alongside the
whole-tree checkpoint.** `rewind.snapshot_before_write(path)` captures a
file's content right before `write_file`/`edit_file`/`apply_patch` touches
it — no git, no `cwd` scoping, works no matter where `path` lives.
`agent.py`'s `cb.checkpoint(call.name)` call now also passes
`call.arguments` (`AgentCallbacks.checkpoint`'s signature grew a second
parameter), and `engine.py`'s callback snapshots `args["path"]` when the
call has one, or calls `rewind.clear_last_mutation()` when it doesn't
(`run_command`/`wait_until` — no single unambiguous target, and a stale
snapshot from an EARLIER write must not be mistaken for "the last
mutation" once something else has run since). `undo_preview()`/`undo()`
check this snapshot FIRST, before falling back to R180's tree-diff check
— covers `"file"` as a third kind alongside `"uncommitted"`/`"none"`.
The status-bar button's visibility check (`tui.py`) now reads
`undo_preview()` instead of `rewind.head()` alone, so it reflects this too
— `head()` stays `None` forever for a project whose only mutations were
ever out-of-tree, which used to hide the button even once something WAS
undoable. `ui._undo_cmd()`'s confirm now leads with the filename plainly
for the common single-file case, per feedback mid-fix, instead of a
path list formatted the same way as the multi-file tree-diff case.

Tests: `test_agent_passes_call_arguments_to_the_checkpoint_callback`,
`test_snapshot_before_write_then_undo_restores_an_out_of_tree_file`,
`test_snapshot_before_write_of_a_brand_new_file_deletes_it_on_undo`,
`test_a_non_file_mutation_invalidates_a_stale_file_snapshot`,
`test_undo_button_visibility_covers_out_of_tree_snapshots`
(tests/test_rewind.py; `test_on_tool_result_flips_has_checkpoints_only_for_mutating_tools`
in tests/test_tui.py updated for the new visibility check).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R180 → R181.

### R182. `/undo`'s confirm now shows the actual diff, not just the filename — `rewind.py`, `ui.py` (2026-07-27)

Feedback after R181: naming the file wasn't quite enough either — showing
the real change is what lets it be judged at a glance instead of just
trusted. `rewind.undo_diff(cwd)` returns a unified diff of exactly what
`undo()` would revert, same before→now direction `/diff` already uses so
it reads the same way: for the R181 per-file snapshot, a `difflib.
unified_diff` between the snapshot's old content and the file's current
content (handles the brand-new-file case as a pure addition, using `""`
for "didn't exist"); for the tree-diff fallback, the same staged-vs-HEAD
git diff `undo_preview` already computes internally, just not truncated
to `--name-status`. `ui._undo_cmd()` prints it (via the existing
`colour_diff`) right above the confirm prompt. Empty string (nothing
printed) when there's nothing to undo, or on any failure — a diff
problem must not block the confirm it's decorating. Tests:
`test_undo_diff_for_an_uncommitted_tree_change`,
`test_undo_diff_for_an_out_of_tree_file_snapshot`,
`test_undo_diff_for_a_brand_new_file_shows_a_pure_addition`,
`test_undo_diff_empty_when_nothing_to_undo` (tests/test_rewind.py).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R181 → R182.

### R183. `/<command> help` (alias `man`) for every command — `man.py`, `ui.py` (2026-07-27)

Feature request: typing `/undo help` should print a clear, full
description of what `/undo` does — not the one-line `COMMAND_INFO` blurb
autocomplete already shows, the same depth of detail `--man` gives it.
`man` and `help` are synonyms; either works.

**One source of truth, not three.** `man.py`'s new `COMMAND_MAN` dict
(via `_entries()`, built from the colour constants) holds one thorough,
multi-paragraph entry per command — covering all 35 keys in `ui.
COMMAND_INFO`, including four (`undo`, `diff`, `nano`, `exit`) that
`--man`'s old hand-maintained text block had silently never covered at
all. `man.command_man(cmd)` returns one entry; `man.man_page()`'s
COMMANDS section renders ALL of them, in `COMMAND_ORDER`, from the exact
same dict — so the full manual and the per-command lookup can no longer
drift apart the way the old block already had (missing 4 commands with
no test catching it).

**The lookup itself** is one generic intercept in `ui._handle_command`,
checked once right after parsing `cmd`/`arg`, before every other branch
(including the exit/quit short-circuit, so `/exit help` explains rather
than quitting): `arg.lower() in ("help", "man")` prints
`man.command_man(cmd)` (falling back to the `COMMAND_INFO` one-liner if
somehow missing) and returns without touching that command's real logic.
No per-command wiring needed — a new command gets `/cmd help` for free
the moment it's added to `COMMAND_INFO` (and gets caught by the
parity test below if its man entry is forgotten).

**Trade-off, accepted:** a command whose own argument could legitimately
BE the literal word "help"/"man" (e.g. `/search help`, searching logs for
that word) can't reach that argument this way. Narrow enough not to
block on — `/search "the word help"` or similar phrasing still works.

**Rendering.** `/cmd help`'s standalone output prints an entry exactly as
authored (assumes a left margin, matching how a user reads it inline in
chat). `man_page()`'s COMMANDS section, when nesting the same entries
under a `{C}/cmd{R}` header, normalizes each line's indentation (strips
whatever leading whitespace an entry happened to be hand-typed with, then
reapplies a flat 4-space indent) rather than layering a second indent on
top of hand-aligned text — the first version of this doubled every
entry's own indentation, which read as ragged, inconsistently wrapped
text once nested.

Tests: `test_every_command_info_key_has_a_man_entry` (parity — every
`COMMAND_INFO` key must have a real, non-trivial man entry),
`test_command_help_prints_the_man_entry_without_running_the_command`,
`test_command_man_is_a_synonym_for_help`,
`test_exit_help_explains_instead_of_quitting`,
`test_command_help_falls_back_to_the_short_blurb_if_man_entry_missing`
(tests/test_core.py).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R182 → R183.

### R184. `/context`'s per-turn badge line clarified + coloured — `ctxtree.py` (2026-07-27)

Feedback on the turn-stats line (`IN: 8.5k→25.7k billed │ OUT: 941
(THINK: 0 of it) │ TOOLS: 11 │ CACHE: 66%`): the `→` read as growth
("input went from 8.5k to 25.7k") when `billed` is actually a SUM across
every round of a multi-tool turn (R37), not a later value — nothing in
the arrow said "sum". Fixed by naming both figures: `8.5k last · 25.7k
billed`. `CACHE:` moved from a disconnected trailing badge to its own
`│`-separated segment right after `IN` — briefly folded into the `IN`
clause itself with a comma first, but that read as part of the billed
NUMBER rather than its own fact ("only 30.9k was billed?" — no, 30.9k
IS the real billed total; the comma just made "78% cached" look like a
modifier of it rather than a sibling fact). `THINK: 0 of it` — permanent
noise on every non-reasoning-model turn — is now omitted entirely when
zero, shown only when there's real (or estimated) thinking to report.
Each badge (`IN`/`OUT`/`TOOLS`, plus the folded-in cache clause) also got
its own colour — cyan/magenta/yellow, `GREEN` kept for cache since that
already means "this saved you money" everywhere else in Aurora — chosen
as a standard, easily-told-apart, easy-on-the-eye set rather than the
previous single uncoloured wall of text. Each segment is self-contained
(`{colour}...{RESET}`), never nested inside another's span, since ANSI
has no colour stack to restore to. Tests updated:
`test_context_tree_shows_billed_only_when_it_differs` (new wording),
`test_context_tree_hides_cache_badge_below_ten_percent` /
`test_context_tree_cache_badge_clamps_at_100_percent` (new `"NN%
cached"` wording) (tests/test_core.py).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R183 → R184.

### R185. The badge line's numbers had no unit below 1000 — `tokens.py`, `ctxtree.py` (2026-07-27)

`fmt_token_count` drops its "k" suffix entirely below 1000 (950 stays
"950") — reasonable in isolation, but it meant `OUT: 845` on the badge
line was a bare number with NOTHING anywhere saying what it counted.
Fixed by spelling out `tok` on each clause's leading figure — `IN: 15.3k
tok last · 35.4k billed`, `OUT: 845 tok` — once per clause, not
repeated on every related number (`billed`/`last` are already
self-explanatory once the unit is established for that clause). Tests
updated: `test_context_tree_never_presents_reasoning_as_additive`,
`test_context_tree_estimates_thinking_when_unreported`,
`test_context_tree_shows_billed_only_when_it_differs`
(tests/test_core.py — new `"... tok ..."` wording).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R184 → R185.

### R186. `/context` kept showing the ctx figure from BEFORE a `/clear` — `ctxtree.py` (2026-07-27)

Reported live: `/clear` then `/context` right after still showed `ctx
15.5k` (the last turn's pre-clear size) while the status bar correctly
showed `ctx 0`. Cause: `engine.clear()` DOES log a `"clear"` event (and
`engine.reset()` logs `"reset"` right after calling `clear()`), but
R96e's event prefilter (`_collect()`'s `wanted` set) never included
either — they were silently skipped without even a `json.loads`, so the
ctx-tracking loop in `render()` never saw one and just kept whatever the
last turn (or `/compact` fold) had reported. Fixed: both added to
`wanted`; a `"clear"`/`"reset"` row now resets the running `ctx` to 0,
same as a `/compact` fold updates it from its own before/after fields —
and both get their own tree marker (`/clear ── context dropped to 0`),
matching how `/compact`/a model switch already show up in place. Tests:
`test_context_tree_ctx_resets_to_zero_after_clear`,
`test_context_tree_ctx_resets_to_zero_after_reset` (tests/test_core.py).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R185 → R186.

### R187. Deep-dive review of the MCP client and extension loading — `mcp.py`, `extensions.py`, `tools.py`, `engine.py` (2026-07-29)

A read-through of `aurora/mcp.py` + `aurora/extensions.py` prompted by pairing
Aurora with the `agentic_context_mcp` server. Removing the built-in
`.agentic_context` integration (release 1.1.304) made MCP the *only* route to
that context system, so the client's failure modes now matter more than they
did when MCP was purely additive. Every item below was reproduced against a
real fixture server before being fixed.

**R187a. One server's malformed `tools/list` silently disabled EVERY server's
tools — `mcp.MCPServer._discover_tools`.** `tools/list` output is the
server's data and was stored verbatim, so `_to_aurora_spec`'s
`mcp_tool['name']` raised `KeyError` for an entry without a name. The raise
happens inside `specs()`/`runners()`, which run in
`mcp_extension.register()` — *after* `MCPManager.__init__`'s per-server
`try/except` has already returned. So the "one bad server must never take
down the others, or Aurora itself" guarantee that constructor exists to
provide did not hold at all: `extensions.discover()` caught the error one
level up, the whole extension failed to register, and every healthy server's
tools went with it. The only warning was `extension mcp_extension.py failed
to register: KeyError: 'name'`, naming no server — so the user could not tell
which `mcp_servers:` entry was at fault. Verified: one healthy `echo` server
plus one nameless-tool server yielded **zero** mcp tools. Fixed by validating
each entry where the data arrives: non-dict entries, and names that aren't a
non-blank string, are dropped into `self.tool_warnings` (bounded by `_brief`,
same reasoning as `_STDERR_QUOTE_CHARS`) and collected by `MCPManager` into
its existing per-server `errors` surface, so a bad entry costs exactly that
entry and is attributed to the server that sent it. A `tools/list` that isn't
a list at all is a protocol breach rather than one bad entry, so it still
fails that server — via `_fail`, which routes through the same
`__init__` cleanup that keeps R145a's no-leaked-child guarantee. Tests:
`test_malformed_tool_entries_are_dropped_not_raised`,
`test_one_malformed_server_does_not_lose_a_healthy_servers_tools`,
`test_tools_list_that_isnt_a_list_fails_that_server_only`,
`test_a_malformed_server_leaves_no_orphaned_child`, plus fixture
`tests/fixtures/malformed_tools_mcp_server.py` (the first three fail without
the fix).

**R187b. A duplicate `mcp_servers:` name orphaned a live child process for the
whole session — `mcp.MCPManager.__init__`.** `self._servers[name] =
MCPServer(...)` overwrote the earlier entry, dropping the only reference to a
child that had *already* been spawned and handshaked. `close_all()` iterates
the dict's values, so neither it nor the `atexit` hook could reach the
displaced server — it outlived the session, holding its pipes. Same leak class
as R145a, reached from the other side: there the object was never stored
because `__init__` raised; here it was stored and then displaced. Verified
with a control — two entries with *unique* names left 0 processes alive after
`close_all()`, two with the same name left 1. Fixed by rejecting a repeated
name *before* spawning anything, which also keeps the tool namespace honest:
two servers sharing a name generate colliding `mcp_<name>_<tool>` specs, so
one set was unreachable regardless of which server won the dict slot — making
"skip the duplicate" the same outcome the model already saw, minus the leak.
Test: `test_duplicate_server_name_is_refused_before_spawning` (asserts the
refused entry is never started *and* that nothing survives `close_all()`;
fails without the fix).

**R187c. A server's `timeout:` never left `config.yaml` — `mcp.MCPManager`.**
`MCPServer.__init__` accepted `timeout` from the start, but the manager called
`MCPServer(name, command, c.get("args"), env)` and never passed it, so the
15s default was unreachable and `config.yaml.example` documented no such key.
That matters more than a normal unconfigurable default, because crossing the
timeout is **unrecoverable**: `_read_response` calls `_kill_unresponsive`, and
there is no reconnect anywhere in `MCPManager` — the next call gets
`connection closed: Broken pipe`, permanently. Verified end to end: one slow
call, then `child still alive? False`, then every subsequent call broken. And
it collides with a real server — `agentic_context_mcp` sets
`_SCRIPT_TIMEOUT_SECONDS = 30` as its own designed ceiling, so any script run
between 15s and 30s (a `validate.sh`/`rebuild-index.sh` over a large
instance; ~2s on this repo's 37-file one, but the framework advertises
instances up to 214k tokens) killed the context server mid-session with no
way for the user to raise the client's limit. Fixed: `timeout:` is forwarded,
validated (non-numeric/zero/negative warns and falls back rather than
producing a ceiling that kills every server on its first call), documented in
`config.yaml.example`, and the agentic-context example there now sets 45 —
above the server's own 30 — with the reasoning inline. The 15s default moved
to a named `_DEFAULT_TIMEOUT` so the client and the docs can't drift.
**Not fixed here:** the missing reconnect. Killing an unresponsive child is
R126's deliberate behavior and re-establishing one mid-turn needs its own
design (re-handshake, re-discover tools, decide whether a retry is safe when
the tool may have already run) — the configurable ceiling removes the forced
failure, which is the part that made this a live production hazard. Tests:
`test_config_timeout_is_forwarded_to_the_server`,
`test_timeout_defaults_when_not_configured`,
`test_an_invalid_timeout_warns_and_falls_back_to_the_default` (5 cases);
6 of the 7 fail without the fix.

**R187d. One malformed extension file stopped Aurora from starting at all —
`tools.set_extensions`, `engine.py`.** `extensions.py`'s docstring promises it
"never lets one broken extension take the rest of the session down", and
`discover()` does guard both the import and `register()`. But the merge step
after it — `tools.set_extensions(ext_specs, ext_runners)` at `engine.py`'s
construction — ran bare, and it calls `spec.get("name")`. A user extension
whose static `SPEC` isn't a list of dicts (`SPEC = ["oops"]`, or a dict, whose
`extend` yields its keys as strings) therefore raised `AttributeError: 'str'
object has no attribute 'get'` straight out of `Engine.__init__`: Aurora
refused to start, with a raw traceback, over one file in
`~/.aurora/extensions/`. Fixed on both levels — `set_extensions` skips a
non-dict spec and one whose name isn't a non-blank string (each with a
warning), and the call site catches anything else, records it, and continues
with no extension tools rather than failing construction. The nameless case
was its own smaller bug: `None` is in neither `builtin_names` nor `seen`, so
such a spec was KEPT and shipped to the model, while `None in runners` left it
without a runner — a tool advertised and permanently uncallable. Tests:
`test_set_extensions_skips_a_spec_that_isnt_an_object`,
`test_set_extensions_skips_a_nameless_spec`,
`test_a_broken_extension_file_does_not_prevent_engine_construction` (all
three fail without the fix; the last one constructs a real `Engine` with a
deliberately broken file on disk and asserts the healthy bundled tools still
load).

**R187e. Three smaller client-lifecycle and protocol gaps — `mcp.MCPServer`.**

*The per-read deadline reset had no absolute ceiling.* `_read_response` resets
its deadline on every chunk that arrives, which is R126's deliberate "don't
kill a server that's still talking" rule — but with no upper bound, a server
emitting `notifications/progress` in a loop and never the matching reply
blocked forever, on the turn's own thread. Verified: with the cap removed the
new test does not finish in 25s (its own bound is 6.2s). Now capped at
`timeout * _MAX_TOTAL_WAIT_MULT` (8x), which leaves a genuinely busy server
its full per-read allowance and only fires on a stream that is alive but never
answering.

*`close()` could leave a zombie.* Its fallback path called `kill()` with no
following `wait()`, so a child ignoring SIGTERM was killed and never reaped,
holding a process-table slot until Aurora exited. `_kill_unresponsive` always
did kill-then-wait correctly; only this path was wrong.

*stdin/stdout were never closed.* Only stderr was, by the drain thread — so
two descriptors per server stayed open until the `Popen` object was garbage
collected. A slow fd leak for a long session with several servers. Both
teardown paths now end in `_close_pipes()`.

*The handshake result was discarded.* `_initialize` sent `protocolVersion` and
threw the reply away, so a server answering with a different version was
indistinguishable from one that agreed, and the mismatch surfaced later as
whatever unrelated-looking symptom it caused. Now recorded in
`MCPServer.warnings` (renamed from `tool_warnings` in this commit, since it
now carries more than tool complaints) and surfaced through `MCPManager.errors`
— **recorded, not enforced**: the spec expects a client to accept a server's
version or fail, but Aurora can't know which differences matter, and refusing a
server that would have worked is a worse regression than the ambiguity.

Tests: `test_an_endlessly_chattering_server_still_hits_an_absolute_ceiling`,
`test_close_reaps_the_child_and_closes_its_pipes`,
`test_a_killed_child_also_has_its_pipes_closed`,
`test_a_protocol_version_mismatch_is_recorded_but_not_fatal`,
`test_a_matching_protocol_version_produces_no_warning`, plus fixture
`tests/fixtures/babbling_mcp_server.py`. Each of the first four fails (or, for
the ceiling, hangs) with its own fix reverted — verified one at a time, since
reverting all three at once hangs the run.

Cross-references bumped in the R187a commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line and `documents/ARCHITECTURE.md`'s
two `R1–R__+` spans, R186 → R187.

### R188. Menu rows are clickable, like the status bar's buttons — `tui.py` (2026-07-29)

The arrow-key menu (`select_menu` / `_open_ui_menu` — `/model`, the Esc-Esc
confirms, the copy picker) was keyboard-only: `↑/↓`, digit-jump, Enter. The
status bar's buttons have been clickable since R134g/R155, so a user who taps
a status-bar item and then gets a menu had no reason to expect the mouse to
stop working there.

`_menu_fragments()` now attaches a `_menu_row_click(i)` handler as each option
row's third fragment element — the same `(style, text, handler)` shape the
status bar's clickable fragments already use. A click on `MOUSE_UP` sets
`_menu_index` and calls `_resolve_menu(index)`, so it commits to the row the
way Enter does (there is no hover-only state in a terminal to justify
click-to-highlight-then-confirm). Routing through `_resolve_menu` is what
makes it work for BOTH menu paths for free: the blocking worker-thread
`select_menu()` (answers queue) and the UI-thread `_open_ui_menu()`
(callback) already converge there. Non-`MOUSE_UP` events are ignored, as in
every other handler in this file, so a drag across the pane can't answer a
menu. The hint reads `↑/↓ move · Enter/click select · number to jump`.

**Process note:** the implementation landed in R187a's commit
(`9a02fd6`) unintentionally — `git add -A aurora` swept up the
already-edited `tui.py` while committing an unrelated MCP fix, and that
commit message doesn't mention it. Recorded here rather than rewritten out of
five commits of history; this entry and its tests are the missing halves of
`ChangeWorkflow.md`'s three-part rule for it. Tests:
`test_menu_rows_carry_a_mouse_handler` (fails without the fragment change),
`test_clicking_a_menu_row_picks_that_row`,
`test_a_menu_row_ignores_everything_but_mouse_up` (tests/test_tui.py).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R187 → R188, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R189. Auto-compact threshold/keep-tail are tunable, not just on/off — `engine.py`, `ui.py`, `config.yaml` (2026-08-02)

`auto_compact_threshold_pct` (default 80) and `compact_keep_recent_tokens`
(default 20,000) were read from `runtime:` in `config.yaml` but had no
setter and no `/set`-style command — the only way to change them was hand-
editing the file, and there was no way at all to change them from inside a
running session. On a small context window (64K), the gap between the 80%
trigger and the hard limit is ~13K tokens, thin enough that a single
reasoning-heavy round (Qwen3/DeepSeek-R1-style `<think>` output routinely
5-20K tokens) can overflow the window in one generation — before the
mid-turn compact check (`_maybe_auto_compact_mid_turn`, which only runs
*before building each request after the first*) ever gets a chance to fold
anything, since it can't interrupt a request that's already in flight.

`Engine.set_auto_compact_threshold_pct(pct)` and
`Engine.set_compact_keep_recent_tokens(n)` persist through the existing
`persist_runtime_value` path (same as `set_auto_compact`). `/autocompact`
now parses three forms: `on`/`off` (unchanged), a bare number (sets the
threshold pct), and `keep=<tokens>` (sets the keep-tail size) — `ui.py`'s
`cmd == "autocompact"` branch. `config.yaml`'s `runtime:` section now ships
`auto_compact_threshold_pct: 65` and `compact_keep_recent_tokens: 8000` as
this machine's defaults, sized for 64K local models (more headroom before
the danger zone, and a fold that actually frees enough room to matter on a
small window).

Tests: `test_set_auto_compact_threshold_pct_persists`,
`test_set_compact_keep_recent_tokens_persists` (`tests/test_core.py`).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R188 → R189, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R190. `find` is not read-only: a SAFE_COMMANDS rule could auto-approve `find / -delete` — `approve.py` (2026-08-06)

`SAFE_COMMANDS` generalizes in the widest way the allowlist offers:
`_rule_for` stores the BARE command name and `_matches` lets that
single-token rule prefix-match any args. That is deliberate and useful —
"always allow" on `find /path/A` should cover `find /path/B` next session
instead of re-prompting per path.

But the list's premise ("nothing here writes, deletes, or executes
arbitrary code") is a claim about the command NAME, and for `find` it only
holds for how `find` is *usually* invoked. The binary ships `-delete`,
`-fprintf`, `-fls`, `-exec` and `-execdir`. So one "always allow" on a
routine `find . -name '*.log'` stored the rule `find`, and from then on
**`find / -delete` was auto-approved, unprompted, forever.** Verified
against the real matcher before the fix.

This is R149's bug arriving through the opposite door. R149 stopped a
command *known* to be destructive from generalizing over its target; R190
stops a command *mis-classified as safe* from generalizing at all. Note
`find . -exec rm {} +` was already caught — but only because `rm` is a
token, so `-exec truncate`, `-exec tee` and `-exec ./script.sh` slipped
through.

`_UNSAFE_FLAGS` maps a safe-listed command to the flags that make it
mutate; `_is_dangerous` now returns True when both the command and one of
its own unsafe flags are present. Both halves are required, so a stray
`-delete` belonging to some other tool doesn't trip it. A plain `find`
still generalizes across paths — the useful behaviour is untouched.

The same commit closes the smaller version of R149's gap: `mv`, `cp`, `ln`,
`install`, `truncate`, `tee`, `docker`, `podman` and the code-executing
package managers (`pip`, `npm`, `yarn`, `pnpm`, `gem`, `cargo`, `make`,
`apt`, `brew`) were absent from `DANGEROUS_COMMANDS`, so their two-token
rule stored the harmless half and left the target free: `mv ./notes.md`
auto-approved `mv ./notes.md ~/.bashrc`, and `pip install` auto-approved
any package — where the package *is* the payload, since installers run
setup.py/install scripts. `git` is deliberately NOT added: `git status`
cannot become `git clean`, so the two-token rule is already sound there.

Tests: `test_a_safe_command_with_a_mutating_flag_never_generalizes`,
`test_always_allow_on_a_mutating_find_stores_it_whole`,
`test_unsafe_flag_needs_its_own_command_present`,
`test_writers_and_installers_never_generalize_over_their_target`,
`test_denylist_still_catches_writers_by_prefix` (`tests/test_core.py`).

### R191. A background prune could silently destroy the checkpoint it was protecting — `rewind.py` (2026-08-06)

`prune()` (R151) runs `reflog expire` + `gc --prune=now` on a **daemon
thread**, while `checkpoint()` may be running `git add -A` + `git commit`
against the same shadow repo. Nothing serialized the two, and both swallow
their errors by design ("a failed prune must leave checkpointing working"),
so the collision was completely silent.

Measured before the fix, arming the counter so a prune fires mid-loop:
**1 in 6 runs of 30 back-to-back checkpoints lost one, and one run left
HEAD unreadable, reporting 0 commits.** A lost checkpoint is exactly the
failure R47/R151 exist to prevent — the mutation is approved and applied,
but `/rewind` has nothing to restore. It also surfaced as an order-dependent
test failure (`test_prune_bounds_the_history_and_rewind_still_works` passed
alone, failed after `test_core.py`), which is how it was found.

Two fixes, both keying previously-global state per shadow repo:

- **`_repo_lock(wt)`** — a per-repo `threading.Lock` held across
  checkpoint's add/commit/rev-parse and across prune's whole body, so a gc
  can never land mid-commit. `_prune_soon` is dispatched *outside* the lock;
  dispatching while holding it would just make the new thread wait.
- **`_since_prune` is now a dict keyed by shadow-repo path**, not one
  process-wide int. The cadence was shared across every project touched in
  one process: 49 checkpoints in project A made project B's *first*
  checkpoint trigger a gc of B.

Tests: `test_a_background_prune_never_loses_a_concurrent_checkpoint`,
`test_the_prune_cadence_is_per_repo_not_process_wide`
(`tests/test_rewind.py`); the existing rate-limit test now monkeypatches
`_since_prune` as `{}`.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R189 → R191.

### R192. The $ figure ignored prompt-cache hits and read ~4x the real bill — `openai_compat.py`, `engine.py`, `agent.py` (2026-08-12)

`OpenAICompatProvider.cost()` priced every prompt token at the full input
rate. Aurora has read `usage.prompt_tokens_details.cached_tokens` since R91
and logs it as `cached_input`, but the number was only ever *displayed* —
never priced. Cache reads bill at a fraction of the input rate, so the badge
overstated by the size of the discount times the hit rate.

An agentic loop is the worst case for this, and the error is invisible in the
shape of the bug: each iteration resends the whole prefix, so the hit rate
climbs with turn length and so does the overstatement. A real session
(`moonshotai/kimi-k3`, 2026-08-09) billed 10,675,848 prompt tokens of which
10,283,008 — **96.3%** — were cache hits. Aurora showed **$32.50**;
OpenRouter charged **$4.74**. Reconstructed: 392,840 fresh × $3.00/M = $1.18,
31,741 output × $15.00/M = $0.48, 10,283,008 hits × $0.30/M = $3.08. Sums to
$4.74 exactly, which is also what pins the cache-read rate at 0.1x input.

- `cost()` takes a fourth `cached` argument and splits the prompt into fresh
  and hit halves. `cached` is clamped into `[0, inp]`: the two counts come
  from different fields of one usage block, and a provider reporting them
  inconsistently must not produce a negative fresh count (and so a negative
  price).
- `_model_info_from_catalog_entry()` now reads OpenRouter's
  `pricing.input_cache_read` into a new `price_cache_read_per_mtok`, and
  `_merge_model_entry()`'s explicit key allowlist carries it through to
  `remote_context_limits.json`. A catalog silent on the rate yields `None`,
  not `0` — "free cache reads" is a different and wrong claim.
- `_CACHE_READ_FALLBACK = 0.1` covers entries written before the field
  existed. It is a fraction OF the input rate, not an absolute $, and 0.1 is
  what every provider Aurora talks to charges. Erring toward a discount is
  the right direction: assuming none is the bug.
- Plumbing: `AgentCallbacks.on_usage` widened to
  `(input, output, cached)` at the one call site in `agent.py`, and
  `Engine._live_usage` takes `cached_tokens` and forwards it to `cost()`.
  The frontend's own `on_usage` is UNCHANGED at two arguments — the cached
  count is accounting, not display, so only the agent→engine hop widened.

R163's per-round accrual invariant survives: cache reads add a third *linear*
term, so summing per-round costs still equals the whole-turn figure. The
split must be applied per round rather than to the turn total, because the
first round is typically a miss and later ones hits — only the per-round
numbers know which was which.

Scope: NEW sessions only, by decision. `price_for()` (which `/cost` uses to
price *past* sessions from their logs) is untouched, so historical reports
keep reading high even though the logs carry `cached_input` and could be
recomputed. Deliberate, not an oversight — revisit if the old numbers ever
need to reconcile against a provider invoice.

Tests (tests/test_core.py, all six fail without the fix):
`test_cost_prices_cache_hits_at_the_cache_rate_not_the_input_rate`,
`test_cost_falls_back_to_a_tenth_of_input_when_no_cache_rate_is_listed`,
`test_cost_survives_a_provider_reporting_more_cached_than_prompt_tokens`,
`test_catalog_entry_carries_the_cache_read_price`,
`test_save_remote_model_info_persists_the_cache_read_price`,
`test_cached_tokens_reach_the_cost_call_round_by_round`.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R191 → R192, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R193. Deep-dive review: writes that could not survive a failure — `rewind.py`, `providers/openai_compat.py` (2026-08-12)

One pass over the write paths, following `PerformanceReview.md`'s method
(hypothesis → reproduce against real code → fix → measure). Theme: three
places wrote a file in a way where an interruption, a wrong locale, or a
second writer left the file WORSE than before the write started. Two were
reproduced end-to-end before any code changed.

#### R193a. `/undo` silently corrupted any file that wasn't valid UTF-8 — `rewind.py`

`snapshot_before_write` stored `target.read_text(errors="replace")` and
`undo()` wrote it back with `target.write_text(...)`. Three defects stacked
on the one path, all on the command whose entire job is to restore a file
faithfully:

- **Lossy decode.** `errors="replace"` turns every undecodable byte into
  U+FFFD, and `undo` then wrote those replacement characters back as the
  file's "original" content — reporting `undone: reverted <path>`.
  Reproduced: `caf\xe9 …` came back as `caf\xef\xbf\xbd …`.
- **Locale-dependent encoding.** Neither call pinned an encoding, so both
  used the locale's — the same defect R146b fixed in `session.py`. Under
  `LANG=C` that is ASCII, so the snapshot mangled every non-ASCII character
  on the way IN as well.
- **Truncate-then-fail.** `write_text` opens mode `"w"` — it truncates
  first and encodes after. Under `LANG=C` the encode raised
  `UnicodeEncodeError` *after* truncation, so `undo` reported "undo failed"
  and left the user's file EMPTY. Reproduced end to end: a 21-byte UTF-8
  file became 0 bytes, with the failure reported as if nothing had happened.

Fixed by snapshotting BYTES (`content_b64` in the marker) and restoring
through `_atomic_write_bytes` — sibling temp file + `os.replace`, so the
target is either the old content or the new one, never a partial write. The
original's permission bits are copied onto the temp before the swap, since
`mkstemp` creates 0600 and `os.replace` takes the temp's mode (an undone
executable would otherwise come back non-executable and owner-only).
`_snapshot_bytes` still reads the legacy `content` field, so markers written
before this in users' checkpoint dirs keep working.

Two further defects found in the same function while fixing it:

- **Unbounded read on the approval path.** The whole target was held in
  memory and JSON-encoded into the marker, for a file chosen by whatever the
  model decided to edit. Capped at `MAX_SNAPSHOT_BYTES` (20MB); past it no
  snapshot is taken and `/undo` falls back to the tree diff, exactly as it
  already does for a file outside the checkpointed tree.
- **A failed snapshot left the PREVIOUS marker in place.** `undo_preview`
  would then report that older file as "the last mutation" and `/undo` would
  revert the wrong file — the same shape as the two incidents `undo_preview`'s
  docstring already documents. The marker is now invalidated on any failure
  or skip, matching what `clear_last_mutation` does for the tools with no
  single target path.

`undo_diff` still decodes lossily, deliberately: the stored bytes are
authoritative for the restore, and that side only renders text for a human
to read in the confirm prompt.

#### R193b. The price table could be corrupted or silently lose an entry — `providers/openai_compat.py`

`remote_context_limits.json` was written with `write_text` and with no lock,
by two writers that R188 put in genuine competition: the background price
refresh runs on its own thread while the main thread can be doing
`/model add`.

- **Non-atomic.** A crash, Ctrl+C or full disk mid-write leaves invalid
  JSON. That failure is silent and TOTAL — `_load_remote_context_limits`
  catches `JSONDecodeError` and returns `{}`, so every model loses its
  context limit and price at once, and the ctx gauge quietly drops to the
  128k default. The file also ships inside the package, so nothing
  regenerates it.
- **Unsynchronized read-modify-write.** Whichever writer finished second
  wrote back a table built from a snapshot taken before the other's edit,
  dropping it outright.

Both fixed with `_write_entries_atomically` (temp + `os.replace`, cleaning
up the temp on failure) under a module-level `_SAVE_LOCK` taken by both save
paths. `save_remote_model_infos` keeps R136's one-read-one-write contract;
its guard test now counts the new mechanism rather than a `Path.write_text`
call that no longer happens.

#### Measured, NOT changed

`session.py`'s read paths were benchmarked against the real store (7.1MB,
193 sessions) before assuming anything: `search_sessions` 0.04s,
`usage_all_sessions` 0.14s, `list_sessions` and `last_latency_by_model`
0.02s each. `search_sessions` bypasses `iter_records`' R96e prefilter and
`json.loads`-es every line, which looked like the obvious win — it isn't one
at this scale, and a raw-line prefilter would need an escape-aware guard to
avoid false NEGATIVES on any needle containing `"`, `\`, a newline or a tab.
Not worth it until a real profile says otherwise. Recorded here so the next
pass doesn't re-try it, per `PerformanceReview.md`'s dead-ends section.

#### Found, NOT fixed — needs a decision

`tools._run_command_once` reads a command's output via `communicate()`, which
is unbounded in memory: `run_command("yes")` accumulates until
`COMMAND_TIMEOUT` (300s) rather than being cut off. This is the same class
R96m fixed for `grep` ("bound the PRODUCER, not the consumer"), and the model
is again the actor most likely to issue the over-broad command. Not fixed
here because the right cap depends on a product decision this review
shouldn't make alone: `run_command`'s own output is already truncated to
`TOOL_OUTPUT_LIMIT` (60k) downstream, but `_run_command_once` is ALSO the
TUI's bash-mode path, where the user is the one who typed the command and may
well want all of it.

Tests: `tests/test_rewind.py` —
`test_undo_restores_a_non_utf8_file_byte_for_byte`,
`test_undo_restores_non_ascii_under_a_non_utf8_locale` (runs in a subprocess
under `LC_ALL=C`; `open()` resolves its default encoding in C at interpreter
start, so an in-process `locale` monkeypatch reproduces nothing and would
pass with or without the fix),
`test_a_failed_snapshot_invalidates_the_previous_one`,
`test_an_oversized_file_is_not_snapshotted`, plus
`test_undo_preserves_the_files_permission_bits` which guards the new
mechanism rather than an old bug (the old `write_text` reused the inode and
kept the mode for free). `tests/test_core.py` —
`test_a_failed_price_table_write_leaves_the_old_table_intact`,
`test_price_table_writes_do_not_leave_temp_files_behind`,
`test_concurrent_price_table_writes_do_not_lose_an_entry`. All except the
permissions test verified failing without their fix.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R192 → R193, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R194. A runaway command could exhaust memory — `tools.py` (2026-08-12)

`_run_command_once` read output via `communicate()`, which buffers the
command's COMPLETE stdout+stderr in memory before any caller-side truncation
runs. Flagged as "found, not fixed" in R193 pending a decision on bash-mode
semantics; measured, decided and closed here.

Measured on the old path: `run_command("yes ABCDEFGHIJKLMNOP")` captured
**12.4GB and grew peak RSS by 9.5GB inside a SIX SECOND timeout**. That is
enough to take the machine down, and — exactly as R96m argued for `grep` —
the model is the actor most likely to issue the runaway command.

Replaced with the same incremental read discipline R96m/R127 established for
`grep`: binary pipes decoded once at the end (text mode wraps each pipe in a
TextIOWrapper whose buffered read would block past the deadline), `select`
with the remaining budget so a stall BETWEEN chunks is caught and not just
total runtime, and raw `os.read` rather than a buffered `.read(n)`.

Two decisions worth stating, since neither is forced by the mechanism:

- **The cap is 5MB, not `grep`'s 200k.** This function is also the TUI's
  bash-mode path, where the user typed the command themselves and the output
  is theirs to read. The tool path truncates to `TOOL_OUTPUT_LIMIT` (60k)
  downstream regardless, so the cap is invisible there and generous here.
- **A verbose command is NOT killed.** Past the cap the reads still happen,
  they just stop being accumulated — draining keeps the pipe empty so the
  command runs to completion and its exit code and side effects survive.
  Stopping the reads instead would fill the 64KB pipe buffer and block the
  child on its next write: the R153 deadlock. Both pipes are watched for the
  same reason.

`proc.wait()` after both pipes hit EOF is now bounded by the remaining
deadline: EOF is not the same as "the process exited" — a double-forked
grandchild can close the pipes and leave the shell alive, which is the R170e
case, and an unbounded wait there would hang the worker thread.

Tests: `test_run_command_output_is_bounded_for_a_runaway_command`,
`test_run_command_keeps_draining_past_the_cap` (a command that prints past
the cap and then exits 7 — proves it was never blocked AND that its exit code
survived). `test_run_command_warns_when_final_reap_itself_times_out` was
updated: its fake process now carries real pipes instead of a `communicate()`
stub, which models the escaped grandchild more closely than the old stub did
(pipes at EOF, process unreapable).

### R195. A file allowlist rule could be escaped with `..` — `approve.py` (2026-08-12)

`fnmatch`'s `*` crosses `/` — it is not `glob` — so a stored rule of
`~/project/*` matched the signature `~/project/../../etc/passwd`: the
traversal segments were just more characters for `*` to swallow. `_norm_path`
expanded `~` but never normalized `..`, and `tools._resolve` only expands `~`
too, so the write really did land outside the approved directory.

Verified end-to-end before the fix: with a rule of `/tmp/x/project/*`,
`is_allowed` returned True for `/tmp/x/project/../secret/keys.txt` and
`write_file` then overwrote `/tmp/x/secret/keys.txt` — **no prompt**. A user
who approved "always allow writes under my project" was silently
auto-approving writes anywhere on the filesystem.

This is the same class as R141 (`ls && rm -rf ~` prefix-matching a stored
`ls` rule): a matcher advertising a boundary guarantee that quietly stops
holding on a spelling nobody tested. Both were found by asking what the
matcher does with input shaped to look like something it isn't.

Fixed by adding `os.path.normpath` to `_norm_path`. Purely LEXICAL — it
collapses `..` without touching the disk, so the deliberate "expanded but
NOT resolved" property is preserved and a glob rule stays a glob.
`_matches` runs rules through the same function, so both sides normalize and
a traversal that resolves back INSIDE an approved directory still matches.
It applies to the denylist too, without which a deny rule would be
side-steppable by spelling the path with `..`, breaking R120's "deny always
wins".

**Residual, deliberately open:** a SYMLINK inside the approved directory
pointing outside it still matches. Catching that needs a real `resolve()`,
and a glob rule has no filesystem identity to resolve — closing it means
changing what a rule *is*, not just how it is spelled. Lexical traversal was
the reachable half and is closed; this is recorded rather than silently left.

Tests: `test_a_file_rule_cannot_be_escaped_with_dot_dot` (four escape
spellings, plus the legitimate nested path that must still match),
`test_a_dot_dot_path_still_matches_the_rule_it_really_lands_in`,
`test_a_denied_path_cannot_be_reached_by_dot_dot_either`. All fail without
the fix except the middle one, which guards against over-correcting.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R193 → R195, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R196. A corrupt allowlist.yaml killed the turn instead of failing closed — `agent.py` (2026-08-12)

R170a made a corrupt `denylist.yaml` fail CLOSED: caught, reported, and every
gated call blocked until it's fixed. `allowlist.yaml` got no such treatment —
`approve.load()` was called bare at the top of `run_turn` (and again after an
"always allow" answer), and only `ProviderError` is caught around `run_turn`,
so `ApproveLoadError` propagated out of the agent loop and took the whole turn
with it. The file is YAML that Aurora explicitly invites the user to
hand-edit, so a typo is a normal event, not a corruption scenario.

Both call sites now go through `_load_allow_or_empty`, which reports and
returns `{}`. Empty IS the fail-closed answer for this direction: no rule
matches, so every gated call is prompted for — the pre-allowlist behaviour.
Failing closed on the deny side means blocking; on the allow side it means
asking. Both refuse to act on rules they cannot read, which is the property
R170a was really after.

Test: `test_agent_asks_for_approval_when_allowlist_is_corrupt` — asserts the
gate is ASKED (not pre-approved, not crashed) and that the turn still
completes. Fails without the fix with `ApproveLoadError`.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R195 → R196, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R197. An MCP server could grow the read buffer without limit — `mcp.py` (2026-08-12)

R187e added two time ceilings to `_read_response` (the per-read deadline reset
and `_MAX_TOTAL_WAIT_MULT`), and both bound how LONG a server may take. Nothing
bounded how MUCH it may send. `_read_response` accumulates 64KB chunks into
`self._buf` until it finds a newline, so a server streaming an unterminated
line grows that buffer at pipe speed for the entire
`timeout * _MAX_TOTAL_WAIT_MULT` window.

Measured on the old code with `timeout=0.5` (a 4s window): the buffer reached
**125MB**, peak RSS +127MB. At the default `_DEFAULT_TIMEOUT` of 15s the
window is 120s, i.e. roughly 3.75GB on the same hardware.

Not an exotic input: ONE JSON-RPC line is how a large MCP tool result
legitimately arrives, so this is the degenerate end of ordinary behaviour
rather than a hostile special case. Same unbounded-producer shape as R194 and
R96m — this pass has now found it three times in three different modules,
which is worth noting as a pattern rather than three coincidences.

Capped at `_MAX_RESPONSE_BYTES` (64MB): far above any legitimate response
(`run_tool` truncates the parsed text to `TOOL_OUTPUT_LIMIT` = 60k regardless)
while bounding the damage. Crossing it kills the child and reports the real
cause, rather than letting the time ceiling eventually trip and blame a
timeout.

Test: `test_a_server_sending_an_endless_line_hits_a_byte_ceiling`, with a new
`tests/fixtures/firehose_mcp_server.py` (handshakes normally, then answers a
tool call with bytes forever and no newline). The cap is monkeypatched down so
the test costs megabytes rather than the real 64MB. Note the honest limit of
that test: without the fix it fails on the missing constant, not on the
overrun — the 125MB measurement above is the actual evidence, taken directly
against the pre-fix code.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R196 → R197, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R198. An ESC inside an OSC payload smuggled the sequence past the sanitizer — `tui.py` (2026-08-12)

`_DANGEROUS_ESCAPES` gave every alternative a body that excluded ESC
(`[^\x1b]*`, or `[^\x07\x1b]*` for the BEL-terminated OSC) — including the two
R171 added for UNTERMINATED sequences, which therefore could never reach their
`\Z` anchor once another ESC intervened. With no alternative matching at the
introducer, `re.sub` advanced past it, stripped only the INNER sequence, and
left the outer one live:

    "\x1b]52;c;PAY" + "\x1b]0;title\x07" + "LOAD"  ->  "\x1b]52;c;PAYLOAD"

A still-open OSC 52 — a clipboard write — survived the sanitizer whose entire
purpose is removing it, and `append()`/`append_bash_output()` then STORED it,
so it also reached `/copy-all` and session export. Three further payloads leak
the same way: an embedded CSI (`\x1b]52;c;PAY\x1b[0mLOAD`), two consecutive
unterminated OSCs, and a DCS with an inner CSI.

R171's stated reasoning was right — "no terminator anywhere in the rest of the
text is unambiguous, so strip from the introducer to the end" — the character
class just couldn't express it. The unterminated alternatives now use
`[\s\S]*\Z`, consuming anything to end-of-text including ESC.

Alternative ORDER is now load-bearing and is commented as such: Python's `re`
tries alternatives left to right and takes the first match, so a properly
terminated sequence still matches the earlier, narrower alternative and only
IT is removed, leaving following text intact. Only a genuinely unterminated
introducer reaches the greedy pair and takes the rest of the text with it —
already R171's accepted trade, since a real terminal swallows that text as OSC
payload regardless. Verified unchanged: OSC 8 hyperlink pairs, an OSC title
followed by text, and plain CSI colour codes.

Tests: `test_an_esc_inside_an_osc_payload_does_not_smuggle_it_through` (four
payloads, fails without the fix) and
`test_r195_fix_does_not_over_strip_terminated_sequences` (guards the
over-correction the greedy alternatives could otherwise cause).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R197 → R198, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R199. Aurora could not read back a config it had written itself — `config.py`, `approve.py`, `paths.py`, `providers/openai_compat.py`, `rewind.py` (2026-08-12)

An asymmetry, found by sweeping for the class R193a had just turned up rather
than by reading the file: every YAML/JSON Aurora persists is WRITTEN as
explicit UTF-8 (`paths.write_text_atomic`, R146a) — and `persist_model_entry`
/ `persist_runtime_value` pass `allow_unicode=True`, so real non-ASCII lands
in the file — while every READ used `read_text()` or `open()` with **no
encoding**, i.e. the locale's.

Under `LANG=C` (cron, CI, a minimal container, a Docker image with no locale
set) that is ASCII, so `load_config` raised `UnicodeDecodeError` at STARTUP
and Aurora would not run at all. Reproduced with a one-line config:

    description: "Kimi K3 — a 2.8T model"
    -> UnicodeDecodeError: 'ascii' codec can't decode byte 0xe2 in position 79

Not a corner case. `/model add` writes OpenRouter's own model descriptions
verbatim into config.yaml, and those are full of em dashes — so the app
wrote the file that then stopped it starting. This is the third appearance of
the locale-encoding defect (R146b in `session.py`, R193a in `rewind.py`, here)
and the second bug this pass found by pattern-matching an earlier one, which
argues for pinning the encoding as a house rule rather than per incident.

Pinned `encoding="utf-8"` on every read of a file Aurora itself writes:
`config.load_config` (the `open()`), `load_state`, `persist_runtime_value`,
`persist_model_entry`, `remove_model_entries`, `approve._load`,
`paths.aurora_home`'s marker file, `_load_remote_context_limits` and both
price-table read-modify-writes, and `rewind._read_last_mutation`.

Two of those were already safe by accident and are pinned anyway, so the
property holds by construction rather than by luck: `json.dumps` and
`yaml.safe_dump` both default to escaping non-ASCII, so the shipped price
table and a tool-written allowlist happen to be pure ASCII today. The
allowlist is a file Aurora explicitly invites the user to hand-edit, and a
hand-edited one carries whatever the editor saved.

Test: `test_config_round_trips_non_ascii_under_a_non_utf8_locale` — a
subprocess under `LC_ALL=C` (as R193a established, `open()` resolves its
default encoding in C at interpreter start, so an in-process `locale` patch
reproduces nothing), covering read, write, and read-back. Fails without the
fix at the first `load_config`.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R198 → R199, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R200. `/autocompact` accepted and persisted a threshold that broke it — `engine.py`, `ui.py` (2026-08-12)

R189 made the auto-compact threshold and keep-tail settable from a running
session, but the setters took any number and wrote it straight to
`config.yaml`. The value survives restarts, so a single typo disabled or
inverted auto-compact until the user noticed and hand-edited the file back.

The gate is `if not stats.limit or stats.pct < self.auto_compact_threshold_pct`:

- **`/autocompact 0`** (or negative) — never returns early, so auto-compact
  folds history on EVERY turn, at 5% context as readily as at 95%, spending a
  summarization request each time and throwing away fidelity the setting
  exists to preserve.
- **`/autocompact 500`** — `pct` cannot reach it, so auto-compact never fires
  again while `/autocompact`'s own output keeps reporting it ON, "once
  context hits 500%". A safety mechanism silently off is worse than one
  visibly off: the failure only shows up as a rejected oversized request much
  later, which is the exact symptom R156/R158 exist to prevent.
- **`keep=0`** — `compact_history` reads `keep_recent_tokens=0` as the MANUAL
  `/compact` sentinel meaning "fold the ENTIRE history". As a persisted auto
  value that makes every auto-compact discard the current turn as well.

Both setters now range-check (`0 < pct <= 100`, `keep > 0`) and raise
`ValueError`. Validation sits in the Engine rather than the command handler
because that is where the invariant belongs and it covers any future caller —
`/autocompact` is currently REPL-only, with no TUI equivalent.

`ValueError` specifically because `/autocompact`'s handler already catches it
for a non-numeric argument; the handler now prints the setter's reason when
there is one, instead of a bare "usage:" line that would imply the input was
unparseable when it parsed fine and was merely out of range.

Tests: `test_an_out_of_range_autocompact_threshold_is_refused` (0, -5, 101,
500 all refused; the live AND persisted values unmoved; the legal edges 100
and 0.5 still accepted) and `test_a_non_positive_keep_tail_is_refused`. Both
fail without the fix. R189's two persistence tests still pass unchanged.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R199 → R200, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R201. A mistyped passphrase silently destroyed every stored API key — `keystore.py`, `paths.py`, `ui.py`, `__main__.py` (2026-08-12)

`store_key` did this:

    data = {}
    try:
        data = _encfile_load(pw.decode())
    except Exception:
        pass
    data[env_var] = value
    _encfile_save(pw.decode(), data)

A decrypt failure left `data` as `{}`, and the save then replaced the WHOLE
store with the single key being added. The commonest way to reach that branch
is not corruption but a **mistyped passphrase**.

Reproduced end-to-end (isolated per
`MEMORY/bugs/20260712_000000_never-smoke-test-against-real-keystore`: tmp
AURORA_HOME **and** a fake `keyring` in the same process): a store holding
`OPENROUTER_API_KEY` and `ANTHROPIC_API_KEY`, plus one typo while adding a
third key, left the file containing only `LLAMA_API_KEY`, encrypted under the
typo. The correct passphrase then raised `InvalidToken`. Both real keys were
gone, with no other copy of the plaintext anywhere — `store_key` returned
`"encrypted file"` and the UI printed success.

Three fixes:

- **Do not treat "can't decrypt" as "empty".** "No file yet" is the only case
  where `{}` is genuinely right, and it is distinguishable without decrypting
  anything, so the two are split apart instead of sharing one `except`. A
  failure now raises the new `KeystoreError`, drops the bad passphrase from
  the cache so the next attempt re-prompts, and writes nothing. Both call
  sites (`ui._prompt_and_store_key`, `__main__`) report it as a refusal — a
  mistyped passphrase is a user event, not a traceback.
- **Atomic, private writes.** `_encfile_save` was `write_bytes` then
  `chmod(0o600)` — two windows in one line: a crash between truncate and
  write leaves an undecryptable blob (every key gone), and between write and
  chmod the file briefly carries the umask's permissions. New
  `paths.write_bytes_atomic(path, data, mode=)` sets the mode on the temp
  file so the name only ever points at a complete, correctly-permissioned
  file. The salt file gets the same treatment: not secret, but a half-written
  salt derives a different key and makes an existing store undecryptable.
- **Stop asking the wrong question.** The prompt said "Choose a key-store
  passphrase" even when a store already existed — inviting exactly the typo
  that used to wipe it, since it reads as "set one" rather than "recall one".
  "Choose" is now used only when there is no store yet.

Tests: `test_a_mistyped_passphrase_does_not_destroy_the_existing_key_store`
and `test_the_key_store_is_written_atomically_and_private` (mode bits on both
files, and the live store never opened for writing). Both fail without the
fix. Both scope `_prompter` and `_passphrase_cache` with `monkeypatch` rather
than `set_prompter` — they are module globals, and a leaked prompter breaks
an unrelated test later in the run, which is how the first draft of these
tests was caught.

**Unrelated flake, not from this change:**
`tests/test_secrets.py::test_scan_is_faster_with_literal_guards_on_ordinary_text`
compares wall-clock times and tripped once during a full run, then passed
alone and in two consecutive clean full runs (849 passed). It is load-
sensitive by construction, and this pass added several subprocess-spawning
tests that make the machine busier. Recorded rather than "fixed" — it is a
pre-existing fragility in a timing assertion, not a regression.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R200 → R201, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R202. R200's range check was on the setters only; a hand edit walked past it — `engine.py` (2026-08-12)

Found by re-reading R200 rather than new code: R200 validated
`set_auto_compact_threshold_pct` / `set_compact_keep_recent_tokens`, so
`/autocompact 0` is refused — but `config.yaml` is a file Aurora explicitly
invites the user to hand-edit, and `Engine.__init__` read those values
straight into attributes:

    self.auto_compact_threshold_pct = float(
        self.runtime.get("auto_compact_threshold_pct", 80))

So every state R200 refused was still reachable by typing it into the file,
which is also where R200's own setter had just persisted it. A guard on one
door only.

Two failure shapes, both verified against the pre-fix code:

- **Out of range** — `0`/negative reinstates "auto-compact folds history on
  every turn"; above 100 reinstates "never fires while the UI reports it ON".
- **Not a number** — the bare `float()`/`int()` raised `ValueError` out of
  `Engine.__init__`, so `auto_compact_threshold_pct: eighty` made Aurora
  refuse to start at all. An unstartable app is a worse answer to a bad
  setting than a corrected one, and this is a config the user is told to edit.

`_runtime_number(key, default, cast, valid, hint)` now does the read: casts,
validates, and on either failure falls back to the default and reports
through `extension_warnings` — the surface both frontends already print at
startup — rather than raising or inventing a second channel. It never raises.

The general point, which is the reason this is its own entry rather than an
amendment to R200: a validated setter and an unvalidated load path are the
same defect seen from two ends, exactly like R199's write-UTF-8/read-locale
asymmetry. Validation belongs wherever a value ENTERS the process, and a
config file is an entry point.

Tests: `test_a_hand_edited_bad_runtime_number_is_corrected_not_obeyed` (0,
-5, 500 each corrected AND reported) and
`test_a_non_numeric_runtime_value_does_not_stop_aurora_starting`. Both fail
without the fix, the second with the original `ValueError`.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R201 → R202, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R203. One session reported two different costs for itself — `providers/openai_compat.py`, `ctxtree.py` (2026-08-12)

R192 taught `OpenAICompatProvider.cost` about cache reads, but four other
places computed `billed * price_in + out * price_out` from `price_for()` and
were left behind: `/context`'s per-turn badge, its session total
(`_session_cost`), the per-model breakdown (`model_breakdown_lines`), and —
through that last one — the cost a RESUMED session is seeded with.

So a live session disagreed with itself. On the real 2026-08-09 kimi-k3
session: **$32.50 in `/context` against $4.74 in the status bar**, where
$4.74 is what OpenRouter actually charged. Same log, same records, 6.9x
apart, both on screen.

This is R192's own fault and the same shape as R202: a rule applied at one of
its entry points. The fix is therefore not "add the cache term in four more
places" but `cost_for(model, inp, out, cached)` — one function every priced
surface calls, with `OpenAICompatProvider.cost` delegating to it. Four copies
of an arithmetic expression that must agree will eventually not.

`_session_cost` previously documented its number as a deliberate UPPER bound:
"cached reads bill cheaper but the discount isn't reported uniformly, so
nothing is subtracted". R192 retired that premise — `cached_input` is on every
assistant record, and the rate reconciled to the cent against a real invoice.
The note is kept rather than deleted: the reasoning was sound when written,
and it was the facts that changed, which is worth being able to see.

Consequence worth stating plainly: **historical figures move down.** `/cost`
and `/context` on old sessions now report what those sessions really cost
rather than an upper bound. R192 deliberately left history alone on the
grounds that it was out of scope; that stops being defensible once the same
session shows two numbers, so the divergence is what forced this and history
is corrected as a side effect, not as a re-pricing project.

`test_cost_command_prices_known_models_and_flags_the_rest` asserted `$2` — the
all-fresh figure. Its fixture always declared 500k of that 1M input a cache
hit, and the report has always PRINTED a "cached" column for it; it simply
never priced it. Now $1.55. The expectation encoded the bug.

Tests: `test_context_badge_and_the_live_cost_agree_on_the_same_turn` (the real
kimi-k3 numbers, badge against `cost_for`) and
`test_every_priced_surface_uses_one_rule` (badge, session total and per-model
breakdown must land on one number for one usage). Both fail without the fix.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R202 → R203, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R204. The reasoning stream was never escape-sanitized — `tui.py` (2026-08-12)

R171 added `_strip_dangerous_escapes` to `append()`, reasoning that "a
compromised or prompt-injected model can put an OSC 52 clipboard write in its
own reply just as easily as a subprocess can, and this is the path that reply
renders through". That argument is about the MODEL, not about which of its
two output channels carried the payload — and `think_chunk`, the
`reasoning_content` stream, was left raw.

So the same payload through the same model was stripped via `on_text` and
stored intact via `on_think`. `_chat` kept it, the render used it, and
`/copy-all` and session export carried it out — the exact three consequences
the sanitizer's own comment lists. Reasoning models emit large free-form
streams, so this is the wider channel of the two, not the narrower one.

Fifth instance this pass of one rule guarded at one of its entry points
(R199 write/read encoding, R202 setter/load, R203 one priced surface of four,
this). It is now the most productive thing to grep for in this codebase.

Sanitized per chunk, matching `append()`, plus one full-text pass when the row
closes. The close-time pass is defence in depth rather than the mechanism, and
the comment says so: a sequence split across two chunks is already handled,
because R198's unterminated alternative is greedy to end-of-text, so a chunk
ending mid-sequence loses its partial introducer as it arrives and the
continuation lands as ordinary text. The pass exists so that stops being
load-bearing on another rule's greediness. Once per request, never per chunk —
re-scanning accumulated text on every chunk would be O(n²) over a 20k-token
reasoning stream.

Tests: `test_think_chunks_are_sanitized_like_the_reply`,
`test_a_think_payload_split_across_chunks_still_does_not_survive`,
`test_closing_a_think_row_re_sanitizes_the_whole_text` (writes into the row
directly, so it tests the close pass and not the per-chunk strip). All three
fail without the fix.

### R205. Scrollback eviction rescanned the whole transcript per entry — `tui.py` (2026-08-12)

`_evict_locked` opened with `sum(self._entry_lines(e) for e in self._chat)`,
and `_entry_lines` does `text.count("\n")` over an entry's whole text. So
every NEW entry rescanned every byte of the transcript: quadratic in total
bytes, on the path that runs whenever output does not merge into the last
entry — tool results, notices, bash output, think rows.

Measured (entries just over `_MERGE_LIMIT`, so each append creates one):

| new entries | before | per append | after | per append |
|---|---|---|---|---|
| 2,000  | 4.3 s  | 2,140 µs | 0.07 s | 35 µs |
| 6,000  | 36.3 s | 6,054 µs | 0.52 s | 87 µs |
| 12,000 | **96.6 s** | **8,051 µs** | **1.36 s** | **114 µs** |

71x at 12k entries. The streaming path — many small appends merging into one
entry — is unchanged at 24.2 ms per 20k appends (was 24.5), which is the case
that had to not regress.

The counts are memoised per entry in `_line_counts`, parallel to `_chat` and
`_cache` and cleared by the same `_dirty(i)` that invalidates the render
cache. That preserves what the original comment was protecting: the total is
still DERIVED from the entries rather than maintained as a running counter
across four append sites — it is just not re-derived where nothing changed.
A stale count is unreachable without also leaving a stale rendering, which
the render path surfaces loudly.

`PerformanceReview.md` attributed felt slowdown in long sessions to render
cost, and its own numbers (9ms/frame at 4.4k lines) predate R96b's caching:
re-measured today the render path is ~0.00 ms at 10k lines and
`_live_clock_key` is 1 µs. The render half was already fixed; this was the
half still there, in a function whose docstring described the sum as running
"about once per `_MERGE_LIMIT` of output" — true for streamed text, and not
true at all for the entry-creating paths.

Tests: `test_the_line_count_memo_never_drifts_from_a_fresh_recompute` (memo
vs from-scratch after every mutation kind the TUI supports) and
`test_the_memo_stays_parallel_to_chat_across_eviction`.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R203 → R205, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R206. The classic REPL emitted terminal escapes straight to the terminal — `colors.py`, `ui.py`, `tui.py` (2026-08-12)

R170l, R171 and R204 stripped OSC/DCS/APC/PM/SOS payloads out of everything
the TUI stores. All three live in `tui.py`, and the sanitizer's own comment
explains why that placement felt sufficient: "prompt_toolkit fully owns
rendering and never blindly passes raw bytes through", so the concern there
was what got STORED and later copied out by `/copy-all` or session export.

`aurora --classic` has no such intermediary. `TerminalFrontend` writes model
and tool output to `sys.stdout` directly, so an OSC 52 in a reply is not
merely stored — the emulator interprets it and the user's clipboard is
overwritten. The frontend WITHOUT the guard was the more exposed of the two,
and `--classic` is not obscure: it is also the automatic fallback whenever
stdout is not a tty.

Four model- or tool-controlled paths were unfiltered, all verified emitting a
raw OSC 52 before the fix: `on_text` (the reply), `on_think` (R204's channel,
here for the same reason), `on_tool_result` (subprocess output — precisely
what R170l stripped for the TUI), and `on_tool_start`, which echoes tool
ARGUMENTS, model-authored text that nothing had ever treated as such.

Sixth instance this pass of one rule guarded at one of its entry points
(R199, R202, R203, R204, and R200/R202 as a pair). The fix is therefore
structural rather than four more call sites: `_DANGEROUS_ESCAPES` and the
strip function moved to `colors.py` — already the ANSI module, and already
security-aware about this exact class, since its `URL_RE` comment describes
an OSC-52 smuggling route through `linkify()`. `tui.py` keeps an alias, so
nothing referencing it there changed. Two frontends can no longer drift apart
on this rule because there is now one rule.

Tests: `test_the_classic_repl_does_not_emit_escapes_to_the_terminal` (all
four paths), `test_the_classic_repl_still_shows_the_real_text_and_colours`
(CSI colours must survive — they are how the REPL renders at all), and
`test_both_frontends_share_one_escape_rule`, which asserts the identity
rather than the behaviour, so a future copy-paste back into `tui.py` fails
here loudly.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R205 → R206, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R207. Two write paths still used the locale's encoding — `ui.py`, `extensions.py`, `rewind.py` (2026-08-12)

R199 pinned `encoding="utf-8"` on every READ of a file Aurora writes, after
finding it could not load back a config it had written itself. The sweep that
found it was `grep read_text | grep -v encoding=`. Re-run for WRITES, two
were still open — and both compound the encoding failure with a
truncate-then-fail, because `open(..., "w")` and `write_text` truncate before
they encode:

- **`/export`** — `open(out, "w")` then `f.write(export_markdown(...))`.
  A transcript containing an em dash, a non-English reply, or unicode inside a
  code block raised `UnicodeEncodeError` under `LANG=C`, leaving a **0-byte
  .md**. Reproduced: 108 bytes of transcript, 2 em dashes, export size 0.
  Now `write_text_atomic`, which pins UTF-8 and lands the file whole or not at
  all — so a re-export that fails no longer destroys the previous one either.
- **`extensions.scaffold`** — `_TEMPLATE.format(name=name.strip(), ...)`.
  `tool_name` is slugified to ASCII but `name` is the user's raw text, so
  `/extensions new café` hit the same wall. The 0-byte `.py` left behind is
  worse than a lost file: `extensions.discover()` tries to load it at the
  next startup.

Two more writes are ASCII by content (`info/exclude` from the `EXCLUDES`
constant, `shallow` from a git SHA) and were never broken. Pinned anyway, so
the property holds by construction rather than by what the data happens to
contain today — the same reasoning R199 gave for pinning the price table and
the allowlist, both of which were also safe only by accident.

Tests: `test_export_and_scaffold_survive_a_non_utf8_locale` (subprocess under
`LC_ALL=C`, both paths) and `test_a_failed_export_does_not_leave_a_truncated_file`.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R206 → R207, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R208. `/copy-all` and `/export` carried the escapes the sanitizer promised to remove — `ui.py` (2026-08-12)

The sanitizer's comment has said since R170l that payloads are "stripped
entirely before storage, so neither the display NOR anything copied out of it
(`/copy-all`, session export) carries the raw sequence." Only the first half
was ever true, and the comment named the two commands it was wrong about.

The TUI sanitizes its own `_chat` DISPLAY buffer. `/copy-all` and `/export`
read `session.export_markdown()`, which walks the session JSONL — written by
the engine from the RAW model text, which passes through none of that.
`/copy` and `/copy-last` are the same: `nth_response` reads
`engine.messages`, and `_last_copyable_text`'s other branch is the TUI's
captured shell output.

Verified: an OSC 52 in a reply comes back intact from `export_markdown`, so
it reached the user's **clipboard** — where pasting into a terminal fires it —
and the exported `.md`, where `cat` does. The clipboard path is the sharper
one, because the payload survives Aurora entirely and goes off to whatever
the user pastes into.

Seventh instance this pass of a rule guarded at one of its entry points, and
the first where the codebase's own comment already asserted the coverage that
did not exist. A stale claim in a comment is worse than no claim: it is the
reason nobody looked.

Fixed with `_outbound()`, applied at the four points where text leaves for a
clipboard or a file. It lives in `ui.py` on purpose and by necessity: an
escape is inert inside the log and dangerous only when it reaches a terminal,
so the UI boundary is the right place — and `session.py` is engine-side,
where `test_engine_never_imports_concrete_ui_module` forbids reaching for it.
`/export` now goes through `_all_chat_text` rather than reading the session a
second time, so there is one path, not two that must agree.

Tests: `test_copy_and_export_do_not_carry_terminal_escapes` and
`test_copy_last_sanitizes_both_of_its_sources` (both of that helper's
sources — raw reply and captured shell output — since both go to the
clipboard). Both fail without the fix.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R207 → R208, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R209. A symlink out of an approved directory is no longer auto-approved — `approve.py` (2026-08-12)

R195 closed the `..` half of the file-rule escape lexically and recorded the
symlink half as deliberately open: "catching that needs a real `resolve()`,
and a glob rule has no filesystem identity to resolve — closing it means
changing what a rule *is*." That was true of the rule as a whole and false of
its LITERAL PREFIX, which is an ordinary path.

`_resolved_rule` splits a rule at its first glob character, backs up to the
last whole path segment (so `/a/b*` resolves `/a`, never `/a/b`), resolves
that, and re-attaches the tail verbatim. An allowlist match then requires the
lexical AND the resolved pair to agree: a symlink inside an approved directory
pointing outside satisfies the first — the path really is under the rule —
and fails the second.

Resolving BOTH sides is what keeps this from over-prompting, and is the
reason a naive "resolve the signature" fix would have been worse than the
bug. On macOS `/tmp` is itself a symlink to `/private/tmp`; comparing a
resolved signature against an unresolved rule breaks every rule underneath it
and re-prompts forever. Tested explicitly, including an approved directory
reached THROUGH a symlink, which must still match — and must still refuse an
escape reached through that same link.

`is_denied` keeps matching on EITHER form. Requiring both there would let a
symlink spelling slip past a deny rule, and R120's "deny always wins" must not
be narrowed by the change that tightened the allow side — the two directions
fail toward different answers, as R141/R149 already established.

Tests: `test_a_symlink_out_of_an_approved_directory_is_refused`,
`test_an_approved_directory_reached_through_a_symlink_still_matches`,
`test_the_denylist_is_not_narrowed_by_the_resolved_check`. The first two fail
without the fix; the third guards against over-correcting.

### R210. A timing assertion was measuring the machine — `tests/test_secrets.py` (2026-08-12)

`test_scan_is_faster_with_literal_guards_on_ordinary_text` compared wall-clock
times (`guarded < unguarded * 0.8`). It failed twice during full-suite runs
while passing alone and in repeated clean runs — this pass added several
subprocess-spawning tests, and a ratio between two timings measures whatever
else the machine is doing.

Recorded as a known flake under R201; two failures is enough. A test that
fails randomly does not just cost a rerun, it teaches you to ignore red.

Rewritten to count which patterns reach `finditer`, which is exactly the
mechanism R96g introduced — exact, unaffected by load. The invariant is
stated precisely: a pattern runs only if it is guard-exempt or one of its
literals really is in the text. That distinction matters, and the test says so:
`tui.py` contains `_live_clock_key`, so the Stripe guard `_live_` legitimately
fires and that regex runs and finds nothing. The guard is a SUPERSET filter,
not a predictor of matches — an earlier draft of this test asserted the
stricter thing and was wrong about the code.

Verified it still detects a broken guard: with `_LITERAL_GUARD` neutered, all
9 patterns run and the test fails on "no pattern was skipped at all". Timing
is what motivated R96g; it is not what R96g promises, so it is not what the
test should assert.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R208 → R210, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R211. Editing keys leaked past the menu's key swallow — `tui.py` (2026-08-12)

The `Keys.Any` binding that guards an open `select()` menu documents itself
as swallowing "every other key (letters, space, backspace, paste) … so
nothing leaks into the buffer rendered underneath the menu". **Two of the
four keys it names leaked.**

Its own comment states the mechanism correctly — "Keys.Any is a fallback, it
only fires when no more-specific binding matched" — and that is exactly why
it does not hold. prompt_toolkit's `get_bindings_for_keys` sorts bindings
with more `Keys.Any` occurrences FIRST, and `KeyProcessor` calls
`matches[-1]`, so every more specific binding beats the swallow. `space`,
`c-j` and `backspace` were each bound specifically with only `_no_editor` on
them.

Measured against the real resolver rather than guessed: of the single-key
bindings that outrank the swallow, all but three are deliberate — enter,
arrows, escape and digits DRIVE the menu, and `!` already carries an explicit
body guard. The three genuine leaks, both effects reproduced:

- **space** — appended to the draft (`"my draft"` → `"my draft "`)
- **c-j** — added a newline to it
- **backspace** — edited it and, on an empty `$` prompt, **silently left bash
  mode**. That is the sharper one: a stray backspace while answering
  "Approve this command?" changed a mode the user never touched.

Fixed with a `_no_menu` filter on those three rather than an early `return`
in each body. Filtering is what actually hands the keystroke to `Keys.Any` —
it uses prompt_toolkit's resolution order instead of fighting it, and a body
guard would leave the specific binding still winning and merely doing
nothing, which is not the same thing for a key that should fall through.

Eighth instance this pass of a guarantee asserted in a comment that the code
did not provide, and the second (after R208) where the comment named the very
cases it was wrong about.

Tests: `test_editing_keys_do_not_leak_into_the_draft_during_a_menu`,
`test_backspace_during_a_menu_cannot_drop_bash_mode`,
`test_menu_driving_keys_still_beat_the_swallow` (over-correction guard) and
`test_editing_keys_are_unaffected_with_no_menu_open`. They press keys through
a `_resolved_press` helper that picks among matching bindings the way
`KeyProcessor` does — the existing `_press` helper calls a handler directly
and so cannot see precedence, which is the entire subject here.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R210 → R211, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R212. A status-render failure was silent — `tui.py` (2026-08-12)

The status bar's construction is wrapped in `except Exception`, which is
right: the bar repaints on a timer, so an exception there would take the
session down. The fallback was the problem — it replaced the whole bar with
the string `" aurora"` and did nothing else. No log, no marker, no record. A
bug anywhere in that block presented as a status bar that had mysteriously
lost the model, the ctx gauge, the cost and the session id, with nothing to
go on.

Not hypothetical: `MEMORY/bugs/20260715_120000_tui_status_token_int_crash`
records exactly such a crash — `live_token_tag()` handing a character count
to a function expecting a string — and this handler is what a repeat of it
would hide.

The bar now renders `" aurora ⚠"` and records the error, ONCE per distinct
failure. Once matters: the bar repaints several times a second, so logging
every frame would bury the session log under duplicates of a single bug. The
logging is itself wrapped — diagnostics must not become the failure — and the
handler still never propagates, which was the original point and is pinned by
its own test.

`status()` is a closure over `_build_app`'s locals, so it was unreachable
from a test; it is now also bound to `self._status_render`. That is what let
the failure path be tested at all rather than reasoned about.

This is a defect in the debugging surface rather than in behaviour, and it is
the eighth time this pass that a silent fallback or an unverified comment was
the reason a bug could sit unnoticed. Worth stating as a pattern: `except
Exception: <substitute a default>` on a path that repaints continuously is
indistinguishable from working, so it needs a mark on the screen and one line
in the log, not one or the other.

Tests: `test_a_status_render_failure_is_reported_once` (marker, recorded
error, and exactly one log record across five renders),
`test_a_healthy_status_bar_is_unmarked` (the marker has to mean something),
and `test_a_status_render_failure_never_propagates`, which fails the render
AND the logging of that failure and still expects no exception.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R211 → R212, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R213. `aurora --classic` could not read piped input at all — `ui.py` (2026-08-13)

`__main__.py` documents the inline REPL as the fallback for "pipes, CI", and
falls back to it automatically whenever stdout is not a tty. Piped input never
worked.

From a TERMINAL, Enter arrives as `\r` — `c-m`, which the REPL binds to
`validate_and_handle()`. From a PIPE, every line ends with `\n`, and that IS
`c-j` — which the REPL bound to "insert a newline". So each piped line was
appended to the buffer and never accepted; EOF then discarded the whole
buffer. `echo hello | aurora --classic cfg.yaml` **exited 0 having printed
nothing, logged nothing, and run no turn.** No session file was even created.

Found by running the thing rather than reading it. Nothing about the binding
looks wrong on the page — `c-j` is a legitimate, documented editing key, and
the REPL's own tooltip row advertises it. It is only wrong in the one context
where `\n` is an input terminator rather than a keystroke, and that context
cannot be reached from a unit test that fakes the input.

Fixed by registering the binding only when `sys.stdin.isatty()`. NOT
registering it (rather than filtering it) is deliberate: it hands `c-j` back
to prompt_toolkit's own default, which accepts the line. A filter would leave
our binding winning and doing nothing, which is the same distinction R211
turned on.

Verified end to end against an unreachable provider: the turn now runs,
retries twice, reports `local backend unreachable`, and writes its `user`
record — the behaviour the engine always had and the REPL never let it reach.

Tests: `test_piped_input_actually_runs_a_turn`, driven as a real subprocess
with `input="hello\n"` because the bug only exists when stdin is genuinely
not a tty; and `test_ctrl_j_still_inserts_a_newline_for_an_interactive_user`,
which pins that the binding survives for a real terminal.

**Method note.** Every earlier finding this pass came from reading code or
sweeping a class. This one came from executing the program, and it was
invisible to both other techniques: the code is correct in isolation, no
comment claims otherwise, and no class-sweep pattern matches it. Worth
budgeting a run-it pass, not only a read-it pass.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R212 → R213, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R214. EOF at a menu killed the session instead of answering safely — `ui.py` (2026-08-13)

`select()` read with a bare `input()`, so `EOFError` propagated straight out
of whatever it was asked. Found the same way as R213 — by running the thing:
`printf '/model\n' | aurora --classic` printed a traceback and exited 1, from
`ui.py:332`, having escaped `run()` entirely.

Two ordinary ways in, and the second is the serious one:

- Piped or CI input running out mid-menu.
- **Ctrl+D at a prompt.** That is a normal gesture, and `select()` backs the
  APPROVAL gate (`"Approve?"`), the iteration-cap prompt, and the secret
  challenge. So Ctrl+D while deciding whether to approve `rm -rf /` did not
  cancel the menu — it killed the session with a traceback, mid-turn.

It cannot be handled by looping the way a blank Enter is. `select()`
deliberately re-prompts on an empty line, reasoning that "an accidental Enter
must never silently pick 'yes' on an approval challenge" — but EOF repeats
instantly, so re-prompting spins forever. The answer has to be a VALUE.

`eof_key` is therefore a required decision rather than a default, for the same
reason blank-Enter re-prompts: the safe answer differs per menu and only the
caller knows it. Each caller now states its own, and every one fails safe:

| menu | EOF answers |
|---|---|
| `Approve?` | `s` — stop the agent, never `y`/`a` |
| iteration cap | `n` — stop |
| secret challenge | `stop` — never `keep` an unredacted value |
| `confirm()` | `n`, whatever `default_yes` says — each caller guards something that writes or spends |

A caller passing nothing gets the `EOFError` re-raised, and `run()`'s command
dispatch now catches it and ends the session the same way EOF at the main
prompt already did — so an unguarded prompt anywhere below (a guidance
comment, a paste) degrades to a clean "bye" instead of a traceback. That is
the backstop, not the mechanism.

Ninth instance this pass of a rule applied at one entry point: `run()`'s
bootstrap ask already wrapped its prompt in `except (EOFError,
KeyboardInterrupt)`, so the codebase knew this was needed — in one caller,
while the shared primitive left every other caller exposed.

`test_confirm_is_a_numbered_menu_with_default_first`'s fake `select` had a
fixed signature and broke on the new argument. Widened to `**kw` — that test
is about option ORDER — and it now also asserts `eof_key == "n"`, so the
tolerance does not cost coverage.

Tests: `test_eof_at_the_approval_gate_stops_instead_of_approving`,
`test_eof_at_the_other_gates_fails_safe`,
`test_select_without_an_eof_key_still_raises`, and
`test_a_menu_command_over_a_pipe_exits_cleanly` (the original reproduction,
as a subprocess). All fail without the fix.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R213 → R214, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R215. The startup health probe crashed with no model configured — `engine.py`, `ui.py` (2026-08-13)

`_provider_for` returns `None` when no model is configured. `context_stats`
guards that explicitly, with a comment naming how it happens: "possible after
`/model remove` of the last entry, R81". `_provider_health_uncached` did not,
and went straight to `provider.api_key`.

The probe runs fire-and-forget on a daemon thread, so nothing caught it and
nothing was meant to. The result: `AttributeError: 'NoneType' object has no
attribute 'api_key'` dumped as a raw thread traceback to stderr, BEFORE the
banner — while the process still exited 0. Loud, alarming, and costing the
health check that was supposed to tell the user what was wrong.

Found by running Aurora against a `models: []` config, which is also what a
`/model remove` of the last entry leaves behind.

Tenth instance this pass of a guard applied at one of its entry points, and
the third where the codebase demonstrably knew about the case — `context_stats`
carries the explanation, in a comment, six hundred lines away.

The probe now returns `{"ok": False, "detail": "no model configured"}`. The
banner also rendered `engine.current.get('model')` directly, printing the
literal word "None" next to "✘ no model configured"; it now shows `(none)`.
The turn path already told the user what to do ("no model selected — /model
to pick one") and is unchanged.

Tests: `test_the_health_probe_survives_having_no_model_configured` (asserts
the precondition too, so it fails loudly if `_provider_for` ever stops
returning None here) and `test_startup_with_no_model_is_clean_and_says_so`,
a subprocess checking stderr carries no traceback and the banner names the
state. Both fail without the fix.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R214 → R215, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R216. A YAML typo tracebacked; R215's health message contradicted itself — `__main__.py`, `engine.py` (2026-08-13)

Two findings from running Aurora against four deliberately degenerate configs
(dangling provider, missing provider, wrong type, malformed YAML). The other
two behaved correctly.

**A YAML typo in config.yaml surfaced as a raw `yaml.parser.ParserError`
traceback** out of `main()`. config.yaml is hand-edited — R199 and R202 both
turned on that fact — so a typo is a normal event, not a corruption scenario.
The allowlist has had `ApproveLoadError` for exactly this since R170a; the
config, which users edit far more often, had nothing. Now `not valid YAML`
plus the parser's own message, which already names the file, line and column,
and exit 1. The traceback was the noise; the location was the useful part and
is kept.

**R215's own message was wrong in a case R215 did not consider.**
`_provider_for` returns `None` for two DIFFERENT reasons, and R215 reported
both as "no model configured" — so a config whose model names no provider
rendered as:

    model    v/m  ✘ no model configured

a line that contradicts itself. Split apart: no model at all keeps "no model
configured"; a model with no provider set says so instead. Recorded as its own
entry rather than an edit to R215 because it is a real behaviour change and
because the shape is worth naming — R215 fixed a crash by adding a branch, and
the branch inherited the crash's assumption about why it had been reached.

A third case is distinct again and was already correct: a model naming a
provider the config does not DEFINE gets a real but keyless provider from
`make_provider`, never reaches the None branch, is already reported unhealthy,
and fails the request with a readable "missing an 'http'" error. My first
draft of the test asserted it took the None path; the code was right and the
test was wrong, so the test now pins all three cases apart.

Tests: `test_a_yaml_typo_in_the_config_is_a_message_not_a_traceback`
(subprocess: exit 1, no traceback, `line 3` preserved) and
`test_health_distinguishes_no_model_from_no_provider`. Both fail without the
fix.

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R215 → R216, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.

### R217. An unusable AURORA_HOME crashed with a pathlib traceback — `paths.py`, `__main__.py` (2026-08-13)

Everything persistent goes through `aurora_home()` — sessions, allowlist,
denylist, key store, checkpoints — and both it and `sessions_dir()` did a bare
`mkdir(parents=True, exist_ok=True)`. So an unusable AURORA_HOME surfaced as a
raw traceback out of whichever caller touched it first, which is arbitrary.

Two causes, both reproduced by running Aurora rather than reading it, and both
ordinary environment mistakes rather than corruption:

- **AURORA_HOME points at a file** — a stale path, a typo in a shell rc.
  `FileExistsError: [Errno 17] File exists`.
- **AURORA_HOME is not writable** — a read-only mount, wrong ownership.
  `PermissionError: [Errno 13] Permission denied: .../sessions`.

`_ensure_dir` now wraps both call sites and raises `AuroraHomeError` with a
sentence that names the variable and says which of the two it is; `main()`
exits 1 on it, beside R216's YAML handler. The failure is fatal either way —
Aurora genuinely cannot run without a writable home — so this changes nothing
about the outcome, only about whether the user can tell what to fix.

Third entry in a row (R215, R216, R217) where a legitimate user-facing
condition arrived as a traceback. The pattern is narrower than "add error
handling": in each case the failing operation was a one-liner nobody thought
of as a failure point — `provider.api_key`, `yaml.safe_load`, `mkdir` — sitting
under a function whose job was described as something else.

Tests: `test_an_unusable_aurora_home_is_explained_not_tracebacked` (both
causes), `test_a_usable_aurora_home_still_just_works` (the wrapper must be
invisible on the normal path — it still creates nested directories and
returns them), and `test_startup_with_an_unusable_home_exits_cleanly`
(subprocess: exit 1, no traceback, names AURORA_HOME).

Cross-references bumped in this commit (per `ChangeWorkflow.md`):
`README.md`'s "currently through R__" line, R216 → R217, and
`documents/ARCHITECTURE.md`'s two `R1–R__+` spans.
