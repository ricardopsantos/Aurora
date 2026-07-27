# Changelog

Aurora's version is `1.1.<commit-count>` (the `1.0.` prefix was used through
release 1.0.185; R114 bumped it to `1.1.`), pinned at the point each release
was published — check `aurora --man` or `python3 -c "import aurora;
print(aurora.__version__)"` for what you're actually running. For the full
numbered requirements record, see `CHANGELOG_TECHNICAL.md` (formerly
`AURORA.md`).

## 1.1.303 (2026-07-27)

### Added
- `/nano <file>`, a built-in text editor: open, edit, save, close, with line
  numbers, a cursor position/"modified" indicator, and click-to-open for any
  filename mentioned in bash-mode output. Restricted to plain text-ish files
  (`.txt/.md/.json/.yml/.yaml/.xml/.sh`, ≤1MB) and never auto-creates a
  missing file. Closing a dirty file now asks once before discarding your
  edits.
- Extensions: drop a small Python file into your extensions folder and its
  tools become callable by the model — see `/extensions` for what's loaded,
  and `/extensions new <name>` to scaffold a starting template. Bundled with
  Aurora out of the box: an MCP client (connect to any Model Context
  Protocol server listed in `config.yaml`), `lint_check` (runs `ruff` on a
  Python file, or a syntax-only check if `ruff` isn't installed),
  `refresh_model_prices` (re-pulls context size/pricing for every configured
  OpenRouter model in one batch), `find_files` (find files by name pattern,
  the filename counterpart to `grep`), and `web_search`/`web_fetch`. Every
  extension-provided tool call still goes through the approval gate — an
  extension is never silently trusted.
- A denylist alongside the existing allowlist: at any approval prompt,
  "Always DENY this" blocks a command or path permanently without asking
  again, and `/denylist` shows what's blocked.
- `apply_patch`: apply a multi-hunk unified diff to a file in one atomic,
  one-approval call instead of several separate edits, each needing its own
  approval. All hunks apply, or none do — the file is never left
  half-patched.
- `wait_until`: repeatedly runs a shell command until it succeeds or times
  out, approved once for the whole call — for "wait until the server is
  listening" instead of guessing a `sleep` duration. It can also run a
  follow-up command (`then`) once the wait succeeds, in the same approval.
- `/commit [message]`: stages, drafts a commit message in your repo's own
  style (or uses the one you give it), shows you the diff and message for
  review, and commits — without leaving Aurora. Never stages anything
  without asking first.
- `/diff` shows exactly what your last turn changed on disk, including a
  brand-new file.
- `/undo`, a precision revert: undoes just the last file change (in the
  project or anywhere else on disk), rather than resetting the whole
  project the way `/rewind` does. Always shows the actual diff of what
  would be reverted before asking to confirm. A status-bar button offers it
  once something has changed.
- `/search <text>` finds a past session by what was actually said or done in
  it, not just by date — useful once you have more sessions than `/resume`'s
  recent list shows.
- `/fallback on|off`: if a model/provider is down or rate-limited mid-turn,
  automatically retry the same request on the next configured model instead
  of just failing. Off by default.
- Auto-compact: once a session's context usage gets high, older history is
  automatically folded into a summary — including, now, mid-turn, so a
  single tool-heavy turn that would otherwise blow the context window can
  shrink itself and keep going instead of dying with a context-limit error.
  On by default at 80% usage; `/autocompact off` disables it, and
  `/compact` still folds everything manually at any time.
- Syntax highlighting inside rendered code fences for Python, JavaScript/
  TypeScript, bash, Go, Rust, and Ruby (keywords, strings, numbers).
- `/<command> help` (or `/<command> man`) prints a full explanation of any
  command — not just its one-line autocomplete blurb.
- The status bar's `$` price is now clickable (→ `/cost`), and a single
  `copy` button replaces the previous four separate copy buttons, opening a
  menu (copy last reply, copy the whole session, copy the session id, copy
  a selection). A small `⤵N` counter shows how many times the session has
  auto-compacted, clickable to compact again.
- A secret detected in a shell command or tool output can now be viewed with
  the token itself masked (`v` at the challenge prompt), instead of only
  full-reveal-or-nothing.
- Session logs now rotate once they pass 5MB instead of growing as one
  ever-larger file — nothing is deleted, `/cost`, `/search`, and `/resume`
  all still see every record.
- `/context`'s per-turn view now also shows: a live "at this rate, ~N more
  turns until context fills" projection with an estimated cost, a per-model
  cost breakdown for sessions that used more than one model, and a visible
  drop to zero right after `/clear`/`/reset`.
- The status-bar mode label (`prompt mode`/`bash mode`) is now clickable to
  toggle modes; double-clicking the prompt selects the whole draft; bash
  mode now Tab-completes file paths (including for `cd`) and `cd` persists
  across bash-mode commands; `clear`/`cls` in bash mode now clears the
  actual on-screen scrollback rather than dumping raw escape codes into the
  transcript.
- `/model`'s picker now shows each model's last-seen response latency when
  known, and no longer blocks the picker on a live probe of a possibly-dead
  local server.

### Changed
- "Copy last" now includes the prompt that started the turn, not just the
  thinking and the reply, with a clearer visual separator between them.
- `/cost` no longer repeats its explainer footer on every call, and now
  states your current session's own cost alongside the all-sessions total
  so the two numbers visibly reconcile instead of just looking like they
  disagree. `/cost <id>` is now an alias for `/context <id>`.
- `/context`'s per-turn line uses plain, coloured, uppercase labels
  (`IN:`/`OUT:`/`TOOLS:`/`CACHE:`) instead of emoji, states units, and no
  longer implies growth where a figure is actually a multi-round sum.
- `/think` is removed — the TUI's existing click-to-expand on any "thought
  for Ns" row already does the same thing, for any past turn, not just the
  latest. Classic (non-TUI) sessions lose the ability to review past
  reasoning after the fact as a result.
- The LlamaDesk integration is removed — it required a `llamadesk:` config
  block nobody had configured. Aurora's local-model support is unaffected.
- `/model` add/remove, prompt caching, per-session `/cost`, concurrent
  read-only tool calls, and a 429 rate limit getting its own automatic
  retry (previously covered) are joined by: connection retries now notify
  you and jitter their delay, and a 429 retry now honours the server's own
  requested wait time (capped) and can be cancelled instead of blocking for
  up to 30 seconds.
- Shadow checkpoints (what `/rewind` and `/undo` restore from) and the chat
  scrollback are both bounded now, so a long-running project or a long
  session no longer grows either one without limit.

### Fixed
- **A serious approval bypass**: an "always allow" rule for a safe,
  read-only command (like `ls` or `cat`) could be silently reused to
  auto-run a chained destructive command (`ls && rm -rf ~`, and several
  other shell-operator forms) with no approval prompt at all. Fixed — a
  compound command now always requires an exact, whole-command match.
  Relatedly, "always allow" on a destructive command like `rm -rf` or `dd`
  no longer generalizes to a different, more dangerous target than the one
  you actually approved.
- A backspace on a selected (double-clicked) draft in the prompt deleted
  only one character instead of the whole selection.
- MCP servers no longer inherit Aurora's own environment variables (so a
  misconfigured or compromised server can't read your provider API keys),
  can no longer have loader/interpreter environment variables injected into
  them via a typo'd config, and a failing MCP server now reports why
  (its own error output), instead of just "connection closed" or "timed
  out". A hung or wedged MCP server no longer leaks its process, and a
  large request to one is no longer silently truncated.
- A crafted URL or terminal escape sequence in fetched content, bash
  output, or a model's own reply could no longer smuggle a hidden terminal
  command (e.g. a clipboard hijack) into what gets displayed or copied —
  these sequences are now stripped.
- The first-ever fetch of a `/bootstrap set <url>` now shows a preview and
  asks for confirmation before it's saved, rather than trusting it sight
  unseen.
- Several turn-ending edge cases (a cancelled turn, an approval denial, an
  interrupted round, hitting the iteration cap, a secret-detection stop) no
  longer leave incorrect or missing session-log records — previously some
  of these could log a "skipped" placeholder as if it were the model's real
  answer, undercount tool calls in `/context`, or (rarer) leave the session
  history in a shape that made the *next* message to certain models fail.
  `--continue` also no longer restores two consecutive user messages from a
  turn that crashed mid-flight.
- Secret detection now also scans a model's own reply text and the new
  content written by `write_file`/`edit_file`/`apply_patch`, not just your
  prompt and tool output — closing two paths where a credential could reach
  the model/provider with no notice or chance to redact.
- Several TUI/menu edge cases fixed: a menu opened while an Esc-Esc confirm
  was already open could permanently freeze the session; a stray Esc could
  silently swallow the next message you typed; an out-of-range digit press
  during a menu could leak into the hidden input; opening the help overlay
  and `/nano` at the same time could leave help stuck open; Esc in bash
  mode with a path-completion popup open now closes the popup instead of
  arming "leave bash mode".
- `/undo` itself went through several corrections in quick succession after
  a real incident: an early version could, in one specific case, silently
  revert an unrelated, already-sealed piece of work instead of the file you
  meant. It's since been rebuilt to only ever act on the change your last
  action actually made, to show you a real diff before confirming, and to
  work correctly on files outside the project directory too.
- A crash killed between two file saves (config, allowlist/denylist rules,
  etc.) could previously leave that file empty or half-written on the next
  start; writes to these files are now atomic.
- A hung/silent subprocess (bash-mode command, `grep`, an MCP server) is now
  reliably caught by its timeout instead of occasionally blocking far past
  it; a heavily-chattered stderr no longer causes `grep` to falsely report
  a timeout.
- `run_command` no longer crashes on non-UTF-8 command output, and a
  misbehaving extension returning something other than text no longer kills
  the whole turn.
- Assorted smaller fixes: `cd` with spaces in bash mode; a chat crash right
  after a bash command; the approval gate now tells you when `/rewind`
  can't undo a write that landed outside the checkpointed project (or in a
  git-ignored path inside it); a corrupt denylist file now blocks tool use
  instead of silently letting everything through; a bare `models:` line in
  `config.yaml` no longer crashes startup; `aurora wipe` now also clears
  MCP-server credentials from the keyring.

### Performance
- Fixed a case where the context/cost status-bar numbers could sit stale
  for the entire duration of a long, tool-heavy turn instead of updating as
  it ran.
- The "copy all"/"copy whole session" action now runs off the UI thread, so
  clicking it on a large session no longer freezes the app.
- A text-merging routine used while streaming output was quadratic in the
  size of the text; it's now linear, saving real time on every frame of
  every turn.
- Old checkpoints are now pruned periodically instead of growing forever —
  verified to shrink a real project's checkpoint storage by roughly a
  third with no loss of `/rewind`'s recent history.

## 1.0.182 (2026-07-23)

### Added
- `/model add <openrouter-url-or-id>` (R80): paste an OpenRouter model page
  URL and Aurora appends the model to config.yaml, fetches its context
  size/pricing/description from the OpenRouter catalog, prompts for the key
  if it isn't stored yet, and switches to it. OpenRouter-only for now. A
  model the catalog doesn't list is refused at the add ("not found on
  OpenRouter"), not discovered broken on the first send; an unreachable
  catalog adds it unverified with a warning.
- `/model remove <url-or-name>` (R81): drop a configured model (any
  provider) from config.yaml; removing the current one falls back to the
  first remaining model with a usable key.
- `bootstrap.example.md` (R83): a recommended start prompt that orients
  Aurora in the project at boot — install with
  `/bootstrap set bootstrap.example.md`. The README/site hero image now
  shows it.
- Full-screen TUI status bar now shows live input/output token counts during
  a turn and a final token tag after the response.
- TUI now persists full tracebacks of uncaught event-loop exceptions to
  `$AURORA_HOME/tui_crash.log` for easier debugging behind the alternate screen.
- `Frontend.on_usage()` callback so providers can report per-request token usage.
- Prompt caching (R91): the system prompt is sent as a `cache_control`
  breakpoint, so a long bootstrap preamble isn't re-billed on every tool
  iteration of every turn — where a multi-step task quietly spends most of
  its money. On for remote models, off for the local one (llama.cpp caches
  its own prefix), `runtime.prompt_cache` / `/cache on|off` to control.
  Cache hits come back in the usage stats and are reported by /cost.
- `/cost [all]` (R92): per-model turns/tokens/$ for this session or every
  session on the machine — a pure read over the session logs, so it works
  on sessions that ended weeks ago. An upper bound, not an invoice.
- `todo_write` tool + `/todo` (R93): on multi-step work the model keeps its
  own task list instead of drifting three tools deep — the structural
  counterpart to the loop nudge and iteration cap, which are only brakes.
  `runtime.todo_tool` turns it off.
- Read-only tool calls in a round (reads/greps/fetches) now run
  concurrently (R94) — approvals, secret challenges, transcript order and
  history are unchanged, only the waiting overlaps. `runtime.parallel_tools`
  turns it off.
- `read_file` takes an optional `offset`/`limit` line range (R90b) — the
  truncation notice already told the model to "read a specific range", but
  there was no parameter to do it with.
- `run_command` takes an optional `cwd` and honours `runtime.timeout`
  instead of a hardcoded 300s; `edit_file` takes `replace_all` (R90f).
- `apply_patch` (R97): apply a multi-hunk unified diff to a file in one
  atomic, one-approval call, instead of several separate `edit_file` calls
  each needing its own unique anchor. Hunks are matched by their surrounding
  text, not the diff's own line numbers, so a slightly stale patch still
  applies correctly; the approval prompt always shows the real computed
  result, never the raw diff text.
- `wait_until` (R100): repeatedly run a shell command until it succeeds or a
  timeout passes, approved once for the whole call — for "wait until the
  dev server is listening" instead of guessing a `sleep` duration.
- `/commit [message]` (R101): stage, draft a commit message from the diff
  with the current model (style-matched to your recent commits), review,
  and commit — without leaving Aurora. Never stages anything without
  asking first; an explicit message skips drafting entirely.
- A 429 rate limit now gets its own backoff-and-retry (R99) instead of
  failing the turn immediately — a shared free-tier quota often clears in
  a few seconds, and Aurora now just waits instead of asking you to.

### Fixed
- TUI status-bar token-tag crash (`TypeError: object of type 'int' has no
  len()`).
- `tests/test_rewind.py` import failure — `tests/` is now an importable package
  via `tests/__init__.py`.
- Select-menu and challenge-prompt layout edge cases (narrow-terminal clipping
  and input-line height glitches).
- The first Esc of the double-tap gesture now shows its "Esc again to …"
  status-bar hint (R78a — documented since R62, never rendered).
- A provider error during the malformed-tool-call retry no longer leaves the
  corrective nudge in history as a stray user message (R78b).
- `/redact` appears in `/`-autocomplete again (R78c).
- `read_file` reads only the returned head of a file instead of slurping the
  whole file into memory first (R78d).
- Drag-select in the TUI copied lines offset by the top pad on a short
  transcript (R79a).
- A write/edit approval aimed at a binary or unreadable file no longer
  crashes the turn building its diff preview (R79b).
- One garbled SSE line from a provider no longer kills the whole stream
  (R79c).
- `web_fetch` streams with a 2MB cap instead of downloading unbounded
  bodies into memory (R79d); `/resume`'s session listing streams logs
  instead of reading each one whole (R79e).

- `grep` searches with extended regex (`-E`) instead of BRE (R90b) —
  a model's `(foo|bar)`/`a+` patterns used to match nothing SILENTLY, which
  reads as "the code isn't there".
- The context folder is found by its CONTENTS everywhere, walking up from
  the cwd (R90c): the bootstrap still hardcoded `.agentic_context` and only
  looked in the cwd, so a differently-named folder was found by `/remember`
  but never bootstrapped, and running Aurora from a subdirectory
  bootstrapped nothing. `/agentic_report`'s autocomplete/`/help` gate also
  keyed on the Aurora checkout instead of your project.
- The context gauge no longer overstates usage on multi-tool turns (R90d) —
  it summed every iteration's output, but each earlier reply is already
  inside the next round's prompt. Cost accounting is unchanged.
- `secrets.scan` is linear again on match-dense text (R90e) — overlap
  tracking was O(matches²) on the worker thread, on by default.
- The engine/UI architecture guard now catches relative imports (R90a); it
  had been passing while `engine.py` imported `ui.estimate_tokens`, which
  pulled prompt_toolkit into the engine half. Those token helpers moved to
  `aurora/tokens.py`.
- Session logs stream on resume/export instead of being read whole (R90f);
  a resumed session's context gauge is restored instead of reading 0.
- Requirements reconciled with the code: R15 (Anthropic prompt caching)
  retired — it died with the Anthropic provider (R74a) and nothing
  implemented it; R13's footer description rewritten to the OpenAI-compat
  reality.

- Race fixes (R82): a status-bar endpoint probe can no longer redirect an
  in-flight request's retries; the per-endpoint HTTP client pool is
  lock-guarded; Happy-Eyeballs winner selection is atomic (no leaked
  socket when both address families connect near-simultaneously); sending
  with no model configured notifies instead of erroring through a blank
  provider.

- Up/down arrow in the TUI's input line only recalls command history from a
  genuinely empty prompt now (R98) — navigating within a multi-line draft no
  longer risks jumping into history just because the cursor lands back on
  the first or last line.
- `grep` no longer buffers a search's entire output in memory before
  truncating it (R96m) — an over-broad pattern over a large tree could
  exhaust memory well within the timeout; output is now bounded as it
  streams in, and the process is stopped the moment enough has arrived.
- The approval diff for a replace-all edit now shows every line that will
  actually change, not a fixed count of one (R95a).
- `grep` reports a real error (bad regex, missing directory) as an error
  instead of silently saying "no matches" (R95b) — the model was concluding
  code didn't exist rather than fixing its own broken pattern.
- `run_command` kills the whole process group on timeout, not just the
  shell (R95c) — a timed-out build or dev server no longer keeps running
  in the background for the rest of the session.
- Secret detection now catches the most common `.env` shape — a bare
  `API_KEY=`/`SECRET=`/`TOKEN=`/`PASSWORD=` line with no name prefix — which
  previously matched nothing (R95d).
- `read_file`'s byte cap is honoured while streaming a ranged read, not
  just checked at the end (R95f) — a huge file opened with `offset` no
  longer accumulates the whole remainder in memory first.
- "Always allow" file rules now match regardless of `~` vs. absolute-path
  spelling (R95g).
- The context-limit gauge and endpoint health checks no longer force an
  extra network round trip on every tool iteration of a turn (R95h/R95i) —
  the status bar's live numbers never block the UI on a socket.
- A turn that produced nothing (a provider error, an interrupt) no longer
  gets logged as if it had (R95e) — it was inflating `/cost`'s turn counts
  with answers that never happened.
- The TUI's live "thinking…" clock only re-renders the transcript once a
  second, instead of on every keystroke, mouse move, and status update
  (R95j).

### Performance
A full audit of the render path, the tool loop, and the provider layer
(R96a–m) found and fixed several real bottlenecks, all measured before and
after:
- The `/command` autocomplete no longer touches the filesystem on every
  keystroke (23× faster) — it used to walk the skills directory and read
  every skill file in full on each character typed.
- The chat transcript now renders in time independent of session length —
  previously a long session got measurably slower to render with every
  message, since the whole transcript was re-flattened on every frame.
- Drag-select-to-copy no longer rebuilds the whole transcript on every
  mouse-move during a drag.
- Secret redaction (38× faster on dense text) and the `/cost` log scan
  (up to 3× faster) both had unnecessary full-text rescans removed.
- A redundant background thread in the TUI's turn-handling was removed —
  it existed only to support a Ctrl+C path that could never actually fire
  in the full-screen UI.
- The "always allow" allowlist no longer re-parses the same command rule
  on every single tool call in a turn.
- Two rare concurrency bugs were fixed: a duplicate background probe under
  a race, and a leaked network connection when two threads raced to open
  the same endpoint.
- Two further ideas — shrinking a render buffer size, and restructuring how
  streamed text accumulates — were investigated and deliberately NOT
  applied, having been measured to make things worse under realistic
  conditions rather than better.

### Changed
- Draft token estimate in the TUI status bar is now shown inline with context
  usage.
- Docs reconciled with the code (R78f): `/max` marked removed, Esc-in-menu
  behavior corrected, stale module/test listings refreshed, a corrupted
  AURORA.md heading restored.

## 1.0 — first stable release (2026-07-15)

The first versioned release. Aurora is a micro terminal coding agent: a
tool loop (read/write/edit/run/search) with an approval gate in front of
every write or command, running against a local llama.cpp server or
OpenRouter.

**Highlights:**
- Full-screen TUI with a pinned prompt, live status bar (model, context
  used, cost), and a classic inline REPL fallback for pipes/CI.
- Steer instead of deny-and-redo: at any approval prompt, drop a comment
  and Aurora folds it into the same request instead of a flat no.
- Every prompt and tool output scanned for secrets before it's sent or
  logged — redact, allow, or stop.
- Sessions are durably logged and resumable; checkpoints let you
  `/rewind` any approved change.
- One model picker for local (free) and OpenRouter (paid, with live
  pricing) models, click-to-open from the status bar.

**Removed this release:** the Anthropic provider (OpenRouter-compatible
models only, for now), the `/max-wait` time-based challenge, and the
startup logo.
