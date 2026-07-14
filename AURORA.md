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
- **R1.** Anthropic provider — default model `claude-sonnet-5`; opus/haiku in
  the picker. API key never stored in plaintext (see R22).
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
  asks approval to continue. `/max N` changes it live and persists to config.
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
  `model │ tokens used/max (%) │ $cost │ session-id`. Anthropic: exact `usage`
  from responses + static limit table + price table. Local: `usage` from
  llama.cpp + live `n_ctx` from `/props`. No cost shown for local (free).
- **R14.** **`/compact`** summarizes history into one message and continues;
  **`/clear`** starts fresh; automatic warning at ~80% context.
- **R15.** Anthropic **prompt caching** (`cache_control` on the system prompt)
  — the bootstrap docs are resent every turn; caching cuts that cost ~90%.

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
  installs, symlinks `aurora` into PATH. Mostly pure-python deps (`httpx`,
  `PyYAML`, `prompt_toolkit`, `keyring`, `cryptography`, `ddgs`) plus Pillow
  for the startup logo, so macOS and
  Linux behave identically. Machine sync = `git pull`; nothing
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
  providers/       # base, anthropic, openai_compat (streaming, tool use,
                   #   caching, thinking channel, extra_body, bulk tool results)
  engine.py        # ENGINE FACADE: all conversation state; the API a UI drives
  frontend.py      # the engine ⇄ UI Protocol (streaming, approvals, secrets)
  agent.py         # tool loop: cap w/ continue-blocks, s/c/n-reason approvals,
                   #   loop-nudge on repeated calls, Esc cancel
  tools.py         # the nine tools (R6) + 60k-char output cap
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
  session.py       # JSONL logging + --continue/resume/export
  websearch.py     # web_search (ddgs) + web_fetch
  paths.py         # AURORA_HOME resolution
install.sh         # venv + editable install + PATH symlink + data-dir marker
config.yaml        # committed: providers, models(+extra_body), runtime, llamadesk, key_fetch
tests/             # test_core, test_finish, test_architecture, test_memory,
                   #   test_rewind, test_secrets, test_tui (165 tests)
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

Automated — **165 tests passing** (`tests/test_core.py`, `tests/test_finish.py`,
`tests/test_architecture.py`, `tests/test_memory.py`, `tests/test_rewind.py`,
`tests/test_secrets.py`, `tests/test_tui.py`; pytest, no network — providers
mocked):
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
  menu — ↑/↓ move, Enter confirms, digits 1–9 jump-select, **Esc == "No"
  whenever a No is offered** (else the safest fallback: an explicit Stop,
  otherwise the last option). While the menu owns the area it is
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
  - **NOT part of this rule, deliberately**: a menu/approval challenge still
    resolves on a SINGLE Esc (to the safest option, R54) — it already has its
    own explicit choice mechanism; clearing typed text on a non-empty input
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

### `/remember` temporarily hidden from discovery (2026-07-12)
- **`/remember` (R52) is being reworked** and is hidden from `/` autocomplete
  and the README's command table while that's in progress — removed from
  `COMMAND_INFO` in `ui.py`. The command ITSELF still works if typed manually
  (`elif cmd == "remember":` in `_handle_command` is untouched) — only
  discovery is disabled, not the feature. Re-add its `COMMAND_INFO` entry
  (and the README row) once the rework lands.

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

### `/remember` temporarily hidden from discovery (2026-07-12)
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
  **how `config.yaml` is deployed**: on m7, `~/scripts/misc/llama/aurora-gateway.py`
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

### Context-size picker on a LlamaDesk library load (2026-07-13, R68)
- **R68. Loading a LlamaDesk library model (R3) asks which context size to
  load it at, instead of silently using `config.yaml`'s fixed
  `llamadesk.ctx`.** `ui._pick_ctx(default_ctx, native)` — called from
  `_pick_model`'s library-load branch, after the eviction confirm, before
  `desk.switch()` — shows a numbered/arrow-key menu (same `select()`
  primitive as everywhere else) built from a fixed ladder (`_CTX_LADDER =
  [4096, 8192, 16384, 32768, 65536, 131072, 262144]`), FILTERED to `<=
  native` — `native` (the gguf's `ctx_native`, from
  `LlamaDesk.models_detail()`) is the hard ceiling; Aurora never rope-extends
  a model past what it was trained for. `native` itself is always added as
  an explicit "(native max)" option even when it isn't a round ladder value
  (e.g. a model reporting `100000` exactly). A `custom…` entry drops into a
  free-text prompt validated `0 < ctx <= native`. The default-selected rung
  is `min(config llamadesk.ctx, native)` — the same value the old fixed-ctx
  logic computed — so a blank Enter reproduces pre-R68 behavior exactly; the
  chosen value is used for that one load only, never written back to
  `config.yaml` (RAM headroom is per-machine, per-model — not something to
  sync). `native=None` (an older LlamaDesk server with no `ctx_native`
  field) drops the ceiling entirely: the full ladder shows, and `custom…`
  accepts any positive integer, matching the old fully-permissive behavior.

### Defaults
- Aurora **starts on the free local model**; pinned pair
  `[local, claude-sonnet-5]`.
