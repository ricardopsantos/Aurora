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
- **R3.** Optional local model **library switching via LlamaDesk** (a
  companion loading agent, `/api/models`, `/api/status`, `/api/switch`): the
  picker shows Anthropic models ($), the currently-loaded local model (free,
  ready), and the LlamaDesk library (free, needs a ~1–2 min load). Loading a
  library model requires an explicit **eviction confirm** (a switch is
  global — it unloads the model for every other consumer of that server) and
  checks for in-flight work first. Not configured? The picker just shows the
  local/Anthropic/OpenRouter models from `config.yaml` — LlamaDesk is
  entirely optional (see the commented block in `config.yaml`).
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
- **R12.** **agentic_context protocol** (see `~/repositories/AgenticContext`):
  when the cwd has `.agentic_context/`, Aurora bootstraps it at start —
  `AGENTS.md` (rules AND personality; the user shapes who Aurora is through
  the context system, not code), the three `INDEX.md`s, `[CORE]` docs,
  `rebuild-index.sh` self-heal. The per-task protocol (consult MEMORY before,
  write qualifying findings + rebuild after, flag `[PROMOTE?]`) is embedded in
  the system prompt so Aurora runs it herself; her writes pass the normal
  approval gate. `open_context_doc` lazy-loads docs on summary match.
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
  llamadesk.py     # optional local library: models(+native ctx/size), status, switch, wait
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
  websearch.py     # web_search (ddgs) + web_fetch
  paths.py         # AURORA_HOME resolution
  tokens.py        # local token estimate/format (engine-side, R90a)
  todo.py          # the model's own task list (R93) + todo_write tool
install.sh         # venv + editable install + PATH symlink + data-dir marker
config.yaml        # committed: providers, models(+extra_body), runtime, llamadesk, key_fetch
tests/             # test_core, test_finish, test_architecture, test_memory,
                   #   test_rewind, test_secrets, test_tui,
                   #   test_expand_newlines, test_bootstrap_network (263 tests)
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

Automated — **263 tests passing** (`tests/test_core.py`, `tests/test_finish.py`,
`tests/test_architecture.py`, `tests/test_memory.py`, `tests/test_rewind.py`,
`tests/test_secrets.py`, `tests/test_tui.py`, `tests/test_expand_newlines.py`,
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
- **R52. `/remember`** (`memory.py`): the bootstrap READS `.agentic_context`;
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
  engine-side: all output goes through `fe.notify`, never print().

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
  **`copy last`** copies the last turn's RAW response — the model's
  reasoning (`fe.think_buffer`, the same buffer `/think` prints), if any,
  followed by its final answer — unlike `/copy`, which copies only the
  answer text (`engine.last_response()`); same text as the `/copy-last`
  command (shared logic: `ui._raw_last_response_text`). **`copy all`**
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

### Context-size picker on a LlamaDesk library load (2026-07-13, R68; actually implemented 2026-07-15)
- **R68.** *(Spec'd 2026-07-13 and fully tested in `test_core.py` — a
  7-rung ladder, `native` always offered even off-ladder, a free-text
  `custom…` entry. That version was never actually wired into `ui.py`; the
  real code kept silently doing `ctx = min(config, native)` with no prompt
  at all until this was noticed and fixed 2026-07-15 — see git history for
  the original spec text.)* **Simplified and actually implemented
  2026-07-15**, local models only (LlamaDesk library loads — remote/
  OpenRouter models have no "load at ctx N" step, their limit is fixed by
  the provider): loading a LlamaDesk library model (R3) now genuinely asks
  which context size to load it at, instead of silently picking one.
  `ui._pick_ctx(default_ctx, native)` — called from `_pick_model`'s
  library-load branch, after the eviction confirm, before `desk.switch()`
  — offers just **64k / 128k / 256k** (`_CTX_OPTIONS`), not the original
  7-rung ladder: small enough to glance at, big enough range for daily use.
  Options above `native` (the gguf's `ctx_native`, from
  `LlamaDesk.models_detail()`) are dropped entirely, never just disabled —
  Aurora never rope-extends a model past what it was trained for. If
  `native` itself is under 64k (a tiny model), it's offered alone instead
  of an empty menu. No free-text custom entry — three options is exactly
  the point. Pre-selects the largest offered size `<= default_ctx`
  (`config.yaml`'s `llamadesk.ctx`); the chosen value is used for that one
  load only, never written back to config (RAM headroom is per-machine,
  per-model — not something to sync).

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
- **R87. `/remember [all|last [k]]`** controls how much of the session
  `memory.py` checks before saving: no argument or `last` checks just the
  last question/reply pair, `last k` the last `k` pairs, `all` the whole
  session (the original R52 scope, still what "save everything" means
  when explicitly asked for). `memory._last_k_messages` slices `engine.
  messages` at the k-th-last user-role message and keeps everything after
  it, so a multi-iteration tool-call reply is kept whole. A malformed
  argument (e.g. `last abc`) prints usage instead of guessing. Restored to
  `/` autocomplete and the README table (superseding the 2026-07-12
  hide-while-reworking note above).
- **`~/AURORA_PFCS/MEMORY/` fallback when there's no real `.agentic_context`
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
  `.agentic_context`) — the notify message says so explicitly.

### `find_context_root` detects by contents, never by folder name (2026-07-22, R88)
- **R88. `memory.find_context_root` no longer hardcodes `.agentic_context`
  as a literal path segment.** It walks up from cwd and, at each ancestor,
  checks every immediate subfolder for BOTH a `KNOWLEDGE/SKILL.md` and a
  `MEMORY/SKILL.md` — whichever subfolder has both, regardless of its own
  name, is the context root. `.agentic_context` remains the convention
  (and what this repo itself uses), but a differently-named folder with
  the same shape is now found too. Requiring the `SKILL.md` files (not
  just the `KNOWLEDGE`/`MEMORY` dirs) rules out an unrelated folder that
  happens to have similarly-named subfolders with no actual content.

### `/agentic_report` command + status-bar link (2026-07-22, R89)
- **R89. `/agentic_report`** (`ui._agentic_report_cmd`): asks "Stats" or
  "Index" via the normal `select()` menu. **Stats** runs the context
  folder's own `scripts/stats.sh` (size/count stats for
  KNOWLEDGE/MEMORY/SKILLS — `memory.run_stats`) as-is. **Index**
  pretty-prints `KNOWLEDGE/INDEX.md` and `MEMORY/INDEX.md` through
  `mdrender.LineRenderer` (the same markdown→ANSI renderer chat replies
  use) instead of dumping raw markdown.
- **Only exists as far as the user is concerned when a context protocol
  folder is detected** (`memory.find_context_root(".")`, by contents —
  R88): hidden from `/` autocomplete (`SlashCompleter.__init__` computes
  `self._has_agentic_context` once per completer lifetime, not per
  keystroke) and from `/help`/`?` (`ui.help_text(has_agentic_context)`
  appends the `/agentic_report` line only when true). Typing it manually
  when nothing is detected still works and just says so — same
  discoverability-only pattern as `/remember`'s 2026-07-12 hide (R52's
  note above).
- **The TUI's line-1 status bar shows a clickable, underlined "agentic
  report" link** — same `class:status.id` style as "session id"/"copy
  last"/"copy all" — under the same detection, cached once as
  `self._agentic_root` in `Tui.__init__` (not re-walked on every render
  tick). Clicking it (`_agentic_report_click`) queues `/agentic_report`
  onto the worker's inbox exactly like the model-picker click queues
  `/model` — the Stats/Index choice is a blocking `select()`, which must
  never run on the UI thread.

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
- **R90c. ONE context-root detector, shared by every surface.** R88 made
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
    offered in every project and then reported "nothing detected" when run.
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
- **R93. The model can keep a visible task list** (`aurora/todo.py`,
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
    no shared state", which `todo_write` (R93) fails despite being ungated.
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

### Defaults
- Aurora **starts on whichever model is first in `config.yaml`'s
  `models:` list** — no longer necessarily the free local one; the user's
  own ordering decides.
