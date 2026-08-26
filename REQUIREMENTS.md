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
