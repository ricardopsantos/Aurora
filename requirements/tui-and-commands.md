# Aurora — Requirements: TUI & commands

The terminal UI, Esc/cancel behaviour, slash commands, clipboard, input box, one-shot mode.
Part of the index in [`../REQUIREMENTS.md`](../REQUIREMENTS.md); numbers are shared across all files.

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
    1003.** Reported bug: over an SSH session (Mac → the server), moving or
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

73. **R311 — `aurora -t "task"` runs one turn without ever prompting.** For
   cron/CI and agent-to-agent delegation. `HeadlessFrontend` answers every
   prompt itself: `approve` denies (reason fed back to the model) unless
   `--yes`; `ask_continue` stops at the step cap (`--max-steps N` overrides
   `runtime.max_iterations`); `secret_challenge` stops; `ask_secret` skips.
   The allowlist is honoured before the front end is asked, and the denylist
   wins over `--yes`. stdout = assistant text only; progress → stderr. Exit 0
   when nothing went wrong, 1 on any denial (including denylist), step cap,
   secret stop, interrupt or provider error; 2 on a usage or config error
   (bad flag, empty task, missing/invalid config, unknown `--resume` id), so
   a script can tell "never ran" from "ran but did not finish". The `·`
   status lines (extension warnings, resume/continue) go to stderr too.
   `-t` is handled before the TUI/classic branch, so it never opens a REPL;
   `--yes`/`--max-steps` without `-t` and an empty task are usage errors. Idea from Green-Agent.

   **Test:** `tests/test_headless.py`.

74. **R312 — A line starting with `/` whose first word contains another `/` is a prompt, not a command.**
   Pasting an absolute path (`/Users/x/photo.jpg make a copy`) used to hit the
   skill fallback and print `[unknown skill: ...]`. Skill names never contain
   `/`, so such a line is sent to the model as a normal turn; a plain typo
   like `/skils` still reports unknown skill.

   **Test:** `tests/test_skills.py::test_path_like_slash_line_is_a_prompt`.

75. **R313 — Elapsed time in the thinking row and the busy status bar is `Ns` under a minute, `m:ss` from 60s on.**
   `709s` after a long local-model run was unreadable; it now shows `11:49`.
   Applies to "thinking… ", "thought for " and the "generating… " status bar.

   **Test:** `tests/test_tui.py::test_fmt_secs`.

76. **R314 — With `/auto-approve` ON, the "N request rounds — continue?" prompt is not shown.**
   Backstop: the prompt returns once the turn reaches 50 rounds or 60
   minutes, whichever first. The cap still advances by `max_iterations` (so
   switching auto-approve off mid-turn asks at the next block), and ESC-ESC
   cancel still works. Headless `--yes` is excluded: an explicit `--max-steps`
   stays binding.

   **Test:** `tests/test_core.py::test_auto_approve_skips_the_iteration_cap_prompt`.

77. **R317 — every copy says what it contains, and the "whole" copies carry
    commands and full output.** "copy last (full)" took the prompt, thinking
    and replies but showed a tool call as `→ run_command`: neither the command
    nor what it printed. The whole-session copy dropped the command and
    clipped output to 200 chars. Now:
    - **Picker rows, least to most:** "copy last turn: answer only"
      (`/copy-response`), "copy last turn: prompt + replies" (`/copy-last`,
      unchanged behaviour — R256's no-tool-output rule still holds there),
      "copy last turn: everything" (**new `/copy-turn`**: the same plus each
      tool's command — or JSON arguments — and its output in full, no cap),
      "copy whole session" (`/copy-all`), then session id / selected.
    - **`/copy-all` carries each tool's command and output.** The session
      log now records a tool's `args` (string values clipped at 4000 chars)
      beside its output. The log has always kept only the first 4000 chars of
      an output (R133b); a longer one ends in
      `[… N more chars not stored in the session log]` rather than being cut
      silently. `/copy-turn` reads live history, so it is complete.
    - Old labels are not kept as aliases.

    **Test:** `tests/test_core.py::test_copy_turn_shows_the_command_and_the_whole_output`,
    `::test_copy_last_still_leaves_tool_output_out`,
    `::test_session_export_carries_the_command_and_flags_unstored_output`,
    `tests/test_tui.py::test_the_copy_menu_names_what_each_copy_contains`,
    `::test_the_copy_menu_turn_row_copies_commands_and_output`.

78. **R318 — slash autocomplete also matches inside a command name.** Typing
    `/<text>` offers every command or skill whose name starts with `<text>`
    (first, alphabetical) and then every one that merely contains it,
    case-insensitively (`/ert` offers `qwertyuio`). Picking one replaces the
    typed text with the full name.

    **Test:** `tests/test_core.py::test_completer_offers_substring_matches_after_prefix_matches`.


79. **R330 — a model that failed its last probe is flagged on the status bar.**
    At startup with `local` down only the scrolled-away header said so; the
    status bar showed a plain `local │ ctx 0/128k`. `Engine.provider_health()`
    now remembers its last result (`last_health`, with the model it was for); while
    that result is a failure for the CURRENT model the bar shows `✘ <model>` in
    the warning style, and the header adds `/model to switch`. A later
    successful `/status` or model switch clears it.

    **Test:** `tests/test_deepdive_20261022.py::test_provider_health_is_remembered_for_the_status_bar`.

80. **R331 — narrow terminals get ellipses, not mid-word breaks.** The `cwd`
    header line is middle-ellipsized to the pane width (`/tmp/claude…name/with/
    deep/nesting`), the `(/extensions for details)` tail is dropped under 70
    columns, and the status bar is clipped per row to the pane width: a plain row
    ends in `…`, the hint row loses whole hints from the right (` …`), and click
    handlers stay on what survives. `Tui._cols()` is the single width source.

    **Test:** `tests/test_deepdive_20261022.py` (R331 group).

81. **R332 — the completion menu is opaque.** It was drawn only where it had
    text, so transcript fragments (and the prompt's rule) showed to its left and
    right (`Auror compact…`). The float now starts at column 0, the menu window
    extends to the full width, and every row is padded with the menu style.

    **Test:** `tests/test_deepdive_20261022.py::test_completion_menu_fills_its_row`
    (visual check: tmux at 100 and 45 columns).

82. **R333 — help text matches the commands and its columns line up.** `?` help
    described `/copy-last` as "RAW response, thinking included" while the
    autocomplete said "prompt, thinking, every reply"; both now use the
    autocomplete wording. The description column of `Alt+M`, `?`, `/redact`,
    `/cache`, `/thinking` and `/nano` was off by one or two; all start at
    column 22.

    **Test:** `tests/test_deepdive_20261022.py::test_help_columns_line_up_and_copy_last_wording_agrees`.

83. **R337 — `aurora -t … --json`.** Cron jobs and other agents had to scrape stdout and
    parse progress lines. With `--json`, stdout is exactly one JSON object —
    `ok`, `exit_code`, `reply`, `reasons`, `model`, `session`, `cost_usd`
    (null for a free/local model), `context_tokens` and `tool_calls`
    (`name`, `args`, `result` clipped to 2000 chars) — and progress still goes
    to stderr. Exit codes are unchanged (0 / 1 / 2). `--json` without `-t` is a
    usage error.

    **Test:** `tests/test_headless.py` (`test_json_*`).

84. **R342 — `/try <task>`: a trial in a throwaway worktree.** `/rewind` undoes
    after the fact and only covers the cwd tree. `/try` creates a `git worktree`
    under `AURORA_HOME/try/` that is a copy of HEAD plus the user's uncommitted
    and untracked changes, commits that as a private baseline, runs the turn with
    the process cwd inside it (relative paths only — an absolute path into the
    original is not sandboxed, and the task says so), then shows `baseline..now`
    and asks apply / discard / keep. Apply copies only the trial's own changes
    (the user's pre-existing edits are not re-applied); the real tree is untouched
    until then. Needs a git repo with a commit; otherwise a clear error.

    **Test:** `tests/test_deepdive_20261022.py` (R342 group).

85. **R347 — Enter on a command fragment with one suggestion chooses it.**
    Typing `/mod` shows one suggestion, `/model`; Enter used to submit the
    fragment `/mod` as typed. Now, when exactly one command or skill name
    completes the bare `/word` (no space yet, not bash mode, no question open),
    Enter chooses it: a command that needs no parameters (`/model`, `/status`,
    `/cost`, …) is run; a command that takes parameters (`/auto-approve`,
    `/plan`, `/budget`, `/try`, `/redact`, `/cache`, `/autocompact`, `/fallback`,
    `/search`, `/nano`, `/commit`), one that destroys or leaves (`/clear`,
    `/reset`, `/undo`, `/rewind`, `/quit`, `/exit`), and any skill is only typed
    in, with a trailing space, and waits for the user (`ui.TYPED_ONLY_COMMANDS`).
    With several suggestions, or a name that is already complete, Enter behaves
    as before (a highlighted row is still accepted, as before).

    **Test:** `tests/test_tui.py` (R347 group).

## R348 — Type while the model works (2026-10-23)
- Rule: a `/command` that only sets a flag applies at once mid-turn (`ui.LIVE_TOGGLES`, `LIVE_DISPLAY_TOGGLES`, plus
  `/auto-approve`); the approval menu offers "Yes, and turn auto-approve ON". Plain Enter queues the line as the next
  prompt; Ctrl+Enter / Ctrl+Space (`_CTRL_ENTER_KEY`, CSI-u and xterm forms mapped onto it) injects it into the running turn,
  delivered on the round's last tool result (`Engine.add_steer`/`take_steer`, `AgentCallbacks.steer`). Undelivered lines
  become the next prompt; a cancelled turn drops them. The old quick-ask is `/ask <text>`.
- Covered by `tests/test_deepdive_20261022.py::test_r348_*` and `tests/test_tui.py::test_plain_enter_queues_*`.

## R349 — "stop" typed mid-turn cancels the turn (2026-10-24)
- Rule: while a turn is running, a submitted line that is just `stop` or `/stop` (any case) sets the turn's cancel (and the
  side channel's) at once instead of queueing as the next prompt. Idle, it is an ordinary prompt.
- Covered by `tests/test_tui.py::test_stop_typed_while_busy_cancels_instead_of_queueing`.

## R351 — Ctrl+Enter on an empty line makes the queue enter now (2026-10-24)
- Rule: while a turn runs, Ctrl+Enter / Ctrl+Space with an empty input line acts on everything queued behind it:
  plain prompts are injected into the running turn (`Engine.add_steer`); side-safe `/commands` and `!` lines run on the
  side channel; if the queue holds a command that rewrites the session (`/clear`, `/compact`, …), the turn is cancelled
  and the whole queue runs next, in order. Nothing queued: the key does what it did before.
- Covered by `tests/test_tui.py::test_ctrl_enter_on_empty_line_*`.

## R352 — Average tokens/second on the status bar (2026-10-24)
- Rule: line 1 of the status bar shows the session-average generation speed (`NN t/s`) after the ctx gauge, once at
  least one second of streaming has been measured. Seconds run from the first streamed token to the end of each request,
  so prompt processing is not counted; requests with no reported output tokens are skipped (`agent._note_gen`,
  `Engine.tokens_per_second`, `ContextStats.tok_per_s`).
- Covered by `tests/test_core.py::test_gen_speed_*` and `test_engine_tokens_per_second_*`.

- **R353 — Aurora Lite: `aurora --web` is a small web chat (Tasks card `20261017-124302`).** A third Frontend beside the TUI and headless, in `aurora/web/` (standard library only; the engine never imports it). One engine, one session, one turn at a time (a second send is 409); workspace pinned by `--workspace` (default `~/claude_code`); the model is the `local` entry (whatever the local server has loaded), set without `switch_model` so the TUI's remembered model is untouched. Every `/api/*` call needs `Authorization: Bearer $AURORA_WEB_TOKEN`; with no token set the server refuses to start. Any message starting with `/` is refused (400). Approvals offer only yes, no and stop: no "always allow/deny", no explain, no steer; no answer within 10 minutes is a denial, and Stop ends a waiting approval. Saved allowlist/denylist still apply, the denylist wins. Events are kept in memory so a reconnect (`/api/stream?after=N`) replays what it missed. Binds `127.0.0.1` by default; exposed only behind a login proxy. Design language flavour 1 Desktop, layout page. Tests: `tests/test_web.py`.
- **R354 — Aurora Lite shows the loaded model and how long it thought (Tasks card `20261029-155432`).** `GET /api/state` carries `loaded` (true when the local server has a model behind the `local` entry; the engine caches that fact up to 2 minutes, so it is a recent view, not a live probe), and the page polls it every 10 s: green `model: <name>` or red `no model loaded (wakes on send)`. Every event carries `t` (server seconds); the page counts `thinking… 12s` live and settles on `thought for 12s` when the first text or tool call follows, computed from event times so a reconnect replay still shows the true duration. Tests: `tests/test_web.py::test_r354_*`.
- **R355 — Aurora Lite can render markdown replies (Tasks card `20261029-160619`).** A header checkbox "Markdown" (on by default, remembered in the browser) shows each reply as headings, bold/italic, inline and fenced code, lists, quotes, rules and tables instead of raw text; switching it re-renders the replies already on screen. The renderer builds DOM nodes with `textContent` only (no `innerHTML`), so nothing the model writes becomes HTML or script, and only `http(s)` links become links. Tests: `tests/test_web.py::test_r355_*` (page checks; the rendering itself was checked once in headless Chromium on a phone-width viewport with a script/`javascript:` sample).
- **R356 — Aurora Lite has a collapsible command sidebar, a context meter and a visible session id (Tasks card `20261029-161837-aurora-lite-command-sidebar-context-mete`, 2026-10-29).** (1) `GET /api/state` carries `session_id`, and a `session` event (id) is the first event of the log and follows every clear; the header shows `session <first 6>` (full id in the tooltip, click copies it). (2) The header `ctx used / limit (pct%)` and a sidebar bar turn amber from 70% and red from 90%, with a hint to Clear or Compact. (3) A left sidebar (button "Sidebar" in the header; open on a desktop and closed on a phone until asked; remembered as `aurora_web_sidebar`) holds the meter, three command buttons and the recent sessions. (4) `POST /api/command {name}` runs only `clear` (the same as `/api/new`; the page asks first in a native dialog), `compact` (folds older messages into a summary, holding the turn slot) and `context` (a note with used / limit); anything else is a 400 `unknown command`, a busy turn is a 409, and typed `/...` is still refused by `/api/send`. (5) "Clear on open" (on by default, `aurora_web_clear_open`): a page that opens while idle runs Clear before it replays the log. (6) A turn that ends at 90% or more of the context wipes the engine's history (`AUTO_CLEAR_RATIO`) without clearing what the page shows, and a note says why. (7) `GET /api/sessions` lists the 15 most recent sessions (short id, when, first message, `current`), read-only. Tests: `tests/test_web.py::test_session_id_*`, `test_clear_announces_*`, `test_commands_are_an_allowlist`, `test_context_command_*`, `test_compact_*`, `test_commands_are_refused_while_a_turn_runs`, `test_a_turn_*_90_percent_*`, `test_sessions_list_*`, `test_page_has_the_sidebar_*`. The layout was checked in headless Chromium at 390 px and desktop width.
- **R357 — Aurora Lite starts in auto-approve mode (Tasks card `20261029-163210-aurora-lite-start-in-auto-approve-mode`).** Every `aurora --web` session begins with the engine's auto-approve ON: tool calls that would ask run without a prompt, exactly as `/auto-approve on` does in the TUI, so a saved denylist rule still refuses the call (deny always wins, R120). A header checkbox "Auto-approve" (amber when on) shows the state and switches it for the running server via `POST /api/auto {on: bool}` (token required, 400 for a non-bool); the state is `auto_approve` in `GET /api/state`. It is not remembered: a service restart turns it back on. With it OFF the R353 dialog (yes / no / stop only) applies again. This replaces R353's "approvals are asked" default only; the Authelia login and the bearer token are unchanged and are now the only gate in front of a shell. Tests: `tests/test_web.py::test_r357_*`.
- **R358 — Aurora Lite has a "Load context" button (Tasks card `20261029-164651-aurora-lite-load-context-button`).** `GET /api/state` carries `context_file`: the absolute path of `context.md` in the workspace (the server's working directory), or `""` when there is none. The button beside Send fills the message box with `load <that path>` and focuses it; it never sends, so the user can edit or just press Send, and Aurora then reads the file with its normal tools. It is disabled while `context_file` is empty. Tests: `tests/test_web.py::test_r358_*`.
- **R359 — Aurora Lite recent sessions can be opened to read (Tasks card `20261029-164801-aurora-lite-tap-a-recent-session-to-read`).** Each row in the sidebar's "Recent sessions" is a button (tap, Enter or Space) that opens a read-only viewer with that session's transcript (user, assistant and tool turns) rendered as markdown by the safe renderer (R355). `GET /api/sessions/<id>` returns `{id, markdown}` for an exact session id only (404 for a prefix, an unknown id or anything path-like, so a crafted id never reaches the filesystem; 401 without the token); the transcript is cut at 300,000 characters with the cut stated. Reading does not resume or change the live session. Tests: `tests/test_web.py::test_r359_*`.
- **R360 — Aurora Lite page fixes from the 2026-10-29 audit (Tasks card `20261029-165840-ui-bug-perf-audit-tasks-pi-tabs-aurora-l`).** (1) "Recent sessions" lists each session once: `loadSessions()` draws only the newest call's result and swaps the rows in with one `replaceChildren`, so overlapping calls cannot append twice. (2) The access token field and Markdown switch moved from the header to the sidebar; the header keeps only the brand, model, ctx, session, status and Auto-approve, so it fits at 390 px (no sideways scroll) and a 401 opens the sidebar so the token can be entered. (3) The sidebar is sticky below the header (`--hh`, set from the header's height) so its top is never scrolled under it. (4) A tool result is shown inside the call it belongs to: calls without a result wait in a list and a result takes the oldest of the same name, so parallel calls no longer leave bare "read_file result" rows. (5) A disabled Send uses the plain disabled button style (dim text on the button fill). (6) On touch, checkboxes are 20 px and checkbox labels, the session chip, tool `summary` rows and session rows are at least 44 px; the token input is 16 px; list gaps use `--sp-half`. Tests: `tests/test_web.py::test_r360_*` (page checks; layout re-measured in headless Chromium at 390 and 1280 px).

- **R361 — Aurora Lite's token field never triggers the browser's password popup (owner request, 2026-10-29, screenshot of a stray dark frame under the header).** The access-token box is a plain text input masked with `-webkit-text-security: disc` (`autocomplete=off`, password-manager ignore hints), not `type=password`, so Safari/Chrome do not open their saved-password suggestion popover over the page. Tests: `tests/test_web.py::test_r361_*`.
- **R362 — Aurora Lite: no stray frame, and a "Hide tool calls" switch (owner request, 2026-10-29).** (1) The read-only session viewer `<dialog id="view">` has `display:flex`, which overrode the browser's `display:none` for a closed dialog, so an empty bordered frame hung under the header at top centre; `#view:not([open])` is now `display:none`. (2) A sidebar checkbox "Hide tool calls" (on by default since the owner's answer, remembered as `aurora_web_hide_tools`) hides every tool-call row (`details.tool`: the call with its result) and leaves the user messages, replies, notes and the thinking rows; it works on rows already on screen and on new ones, and a reply still streams as before. Tests: `tests/test_web.py::test_r362_*`; behaviour checked in headless Chromium.
- **R363 — Aurora Lite's favicon follows the Aurora logo (owner request, 2026-10-29).** *Replaced by R398/R399 (2026-11-02): the tab and home-screen icons are now the sidebar's logo.* The tab icon is an inline SVG (no extra request): the logo's dashed cream line-art face with the bow and two upright eyes on the house dark rounded square, replacing the "Au" letters. Source logo: the owner's site (`base_logo-192.webp`). Tests: `tests/test_web.py::test_r363_*`.
- **R364 — Aurora Lite always shows what it is doing (owner request, 2026-10-29).** While a turn runs, a pulsing line under the log (and the header status tag) says `thinking… 12s`, `running <tool>… 3s`, `writing… 5s` or `working… 40s` (the counter restarts with each state and keeps ticking, so a long silent wait never looks frozen), and disappears (status back to `idle`) when the turn ends. It does not depend on the thinking or tool rows being visible, so it also works with "Hide tool calls" on, and a page opened mid-turn picks it up from the replayed events. The pulse stops under `prefers-reduced-motion`. Tests: `tests/test_web.py::test_r364_*`; behaviour checked in headless Chromium.
- **R365 — Tap the status to see the thinking and the commands (owner request, 2026-10-29).** While a turn runs, tapping (or Enter/Space on) the line under the log or the header status tag ("working…", "thinking… 12s", …) expands that line downwards into a panel with this turn's THINKING text and a COMMANDS list (`$ <tool> <first argument>`), updating live; tapping again folds it. It works whatever "Hide tool calls" says, so the commands stay reachable while the chat shows only results. The text resets with each new message, and the panel exists only while a turn runs. Tests: `tests/test_web.py::test_r365_*`; behaviour checked in headless Chromium.
- **R366 — The token field fits the sidebar (found while checking R365, 2026-10-29).** In the sidebar's "Access token" row the field takes the remaining width and Save keeps its own width (`aside .btn{width:100%}` no longer applies there); before, the field was squeezed to about 18 px and Save overflowed the sidebar. Tests: `tests/test_web.py::test_r366_*`; checked in headless Chromium.
- **R367 — Aurora Lite's sidebar is a stack of foldable sections (owner request, 2026-10-29: "recent sessions should hide when I tap it … the sidebar sucks").** The sidebar has five sections separated by hairlines: Context (meter), Commands (Clear, Compact, Context in one row), Options (Clear on open, Markdown, Hide tool calls as plain checkbox rows), Recent sessions and Access token. Each caption is a button with the house triangle (down open, turned -90° folded; Enter/Space work) that hides or shows the section's body, and the choice is remembered per section (`aurora_web_folds`). All start open except Access token; a 401 opens the sidebar and unfolds it. On touch, captions and option rows are 44 px high. Tests: `tests/test_web.py::test_r367_*`; layout checked in headless Chromium at 1100 and 390 px.
- **R368 — Aurora Lite behind the reverse proxy (:9593) has no Authelia login, only its bearer token (owner request, 2026-10-29).** The Caddy block for `:9593` no longer uses `forward_auth`; the page loads for anyone on the private network and every `/api/*` call still needs `Authorization: Bearer $AURORA_WEB_TOKEN` (401 without or with a wrong one, R353; no token configured = the server refuses to start). The proxy keeps private-network-only access (no LAN rule). This lowers R357's "Authelia login and bearer token are the only gate" to the token alone: anyone holding the token can run shell commands through Aurora (auto-approve is on by default), so the token must stay out of git and chat. The i3's `:9594` block is unchanged (Authelia). Config: the reverse proxy's Caddyfile. Verified with curl: page 200, API 401 / 401 / 200 for no / wrong / right token.
- **R369 — Aurora Lite has an alternative page at `/v2`, in the Paper flavour (owner request, 2026-10-29: "do a path /v2 with the alternative UI", wide column 720 px on computers).** `GET /v2` (and `/v2/`) serves `aurora/web/index_v2.html`; `/` keeps the Desktop page unchanged. Both pages load the same `GET /app.js` (the script moved out of `index.html`; no API change; neither page nor script needs the token, every `/api/*` still does). `/v2` follows Web App Design Language flavour 2 (Paper), wide column: title block with the accent dot and a mono subtitle (the model), a tag line (context, session, status, Auto-approve), the chat as Paper panels in one centred 720 px column (gutters 32 px, 16 px on a phone), a sticky input panel with a full-width Send (Stop beside it while a turn runs), and a right-hand **drawer** (`min(560px, 100%)`, Esc / scrim / Close fold it) opened by the Menu button that holds the foldable sections (Context, Commands incl. Load context, Options, Recent sessions, Access token). The drawer always starts closed (`aurora_web_sidebar_v2` is cleared on load). Every element id the shared script uses exists in both pages. Pending: deciding whether `/v2` replaces `/`, and the i3 copy. Tests: `tests/test_web.py::test_r369_*`; layout checked in headless Chromium at 1280 and 390 px (light and dark), no sideways scroll.
- **R370 — The last chunk of a reply is never lost (found while building R369).** The page paints a reply on the next animation frame, so a `done`, a tool call, a new message or a reset that arrived in the same frame cleared the reply before its last chunk was drawn (an empty or short reply bubble). Each of those events now paints the reply first (`flushCur()`). Tests: `tests/test_web.py::test_r370_*`.

- **R371 — `/v2`: Enter sends like in the terminal, and the context bar sits under the header (owner request, 2026-10-29).** (1) In the message box of `/v2`, Enter sends (Ctrl/Cmd+Enter too); Shift+Enter and Alt/Option+Enter insert a new line at the caret; Enter during IME composition does nothing; on a touch-only device Enter stays a new line and the Send button sends; Enter does nothing while a turn runs (Send disabled). `/` keeps Ctrl/Cmd+Enter. The switch is `window.AURORA_ENTER_SENDS`, set only by `/v2`. (2) The context meter and its "getting full" hint are part of the `/v2` header, under the tag line and above the first message, no longer inside the drawer (which keeps the used / limit text). Tests: `tests/test_web.py::test_r371_*`; keys checked in headless Chromium.
- **R372 — `/v2`: the chat is a scroll box, a Theme choice, and the two versions in the menu (owner request, 2026-10-29).** (1) `/v2` is an app shell (`height: 100dvh`): the title block, tag line and context bar stay at the top and the message box with the Send button stays at the bottom while only the conversation (`#log`, a bordered box) scrolls; the page itself never scrolls. The log follows new text only while the reader is at its end (within 80 px) and always after the reader sends; scrolling up to read is never pulled back. The top space is 32 px (the title stays on screen) and on a phone the context and session tags are left out of the tag line (the bar and the menu carry them). (2) Menu > Theme: Automatic (follows the system), Light, Dark, as a button group with `aria-pressed`; remembered as `aurora_web_theme` and applied before first paint (`data-theme` on the root; Automatic removes it). (3) The menu's header shows `Code <version> · UI <version> (Paper)`: the code version is `aurora.__version__` (shared with the terminal version; `GET /api/state` carries it as `version`), the UI version is `AURORA_UI_VERSION` in the page (2.2; bump it with every `/v2` change). Tests: `tests/test_web.py::test_r372_*`; behaviour checked in headless Chromium at 1100 and 390 px.
- **R373 — Several messages can be sent without waiting; they run one at a time, in order (owner request, 2026-10-29: "send several messages without the last ones being processed (serial)").** `POST /api/send` while a turn (or a compact) runs no longer answers 409: the message is queued (at most 20; the 21st is a 409 "queue is full"), a `queued` event (`text`, `position`) is emitted, and `GET /api/state` carries `queued` (count). When a turn ends the server emits `done`, then takes the oldest queued message, emits its `user` event and runs it: never two turns at once, strict send order, each answer is a normal turn with its own context. Stop ends the running turn and drops the whole queue (`queue_cleared` event with `count`, a note on the page); later sends still run. Clear, Compact and Context commands stay refused while a turn runs. Both pages keep Send usable while busy (the button reads "Queue" then) and show a dashed "Queued (n)" strip above the status line with each waiting message (one line, ellipsised) until its turn starts; a page opened mid-run rebuilds the strip from the replayed events. Tests: `tests/test_web.py::test_r373_*`.
- **R374 — The Paper page is the default web page (owner request, 2026-10-29: "v2 can be the default version, is the better ui looking").** `GET /` serves the Paper page (`index_v2.html`); `/v2` and `/v2/` keep serving it so bookmarks work; the original Desktop page moved to `/v1` (and `/index.html`). UI version 2.3. Tests: `tests/test_web.py::test_r374_*`.
- **R375 — /v2 bug and performance pass (owner request, 2026-10-29).** (1) The v2 drawer key `aurora_web_sidebar_v2` is real code, not swallowed by the preceding line comment, so v2 never inherits v1's open drawer. (2) Send no longer marks the page busy itself: busy comes from the `user`/`queued` event, so a turn that fails at once (its `done` arriving before the send answer) can't leave the page stuck on "working…". (3) A fresh page (`after=0`) replays from the newest `reset`, not from event 1; event lookup uses the contiguous ids instead of scanning the 20 000-event buffer. (4) A page whose `after` is beyond the newest id (the server restarted under it) gets a `restart` marker plus the whole new log, and clears its view, instead of hearing nothing until the counter catches up. (5) Thinking text is appended to one text node, not re-set with `textContent +=` (quadratic on long thoughts). Tests: `tests/test_web.py::test_r375_*`.
- **R376 — Resume, Diff and Undo in the web page (owner request, 2026-10-29).** A past session opened from Recent sessions has a Resume button (hidden for the current one); after a confirm, `POST /api/resume {id}` (exact ids only, 409 while a turn runs) makes it the live session via `engine.resume_from`, emits `reset`, `session`, a `history` event (last 200 user/assistant messages, each clipped) that the page draws as the chat, a note with the turn count, and `done`. The Commands section has Diff (`GET /api/diff`: what the last turn changed against its pre-turn checkpoint, read-only, with a note when there is nothing) and Undo (`GET /api/undo` previews the kind, paths and diff; the page shows them in a confirm; `POST /api/undo` reverts just the last mutation like the terminal's /undo, 409 while a turn runs). Both buttons are disabled while busy. Tests: `tests/test_web.py::test_r376_*`.
- **R377 — /v2 is a Chat page in the shape of Open WebUI, on trial (owner request, 2026-10-29: "change to look like OpenWebUI … keeping our features"; "live as v2 and if I like it, turns to be the default").** `/v2` serves `index_v2.html`, Design Language flavour 3 (Chat); `/` keeps the Paper page (`index_paper.html`, R369–R376) and `/v1` the Desktop page until the owner approves the switch. Layout: a left sidebar (Aurora logo, New Chat, Search, the chats grouped Today / Yesterday / Previous 7 days / Previous 30 days / month, each with its first message and an age like `5m`, `2d`, `1w`; the current one highlighted), a borderless chat area (user messages as right-aligned bubbles, replies as plain text with the logo as avatar, notes centred and dim, thinking and tool calls as small pill disclosures), a "How can I help?" empty state, a jump-to-latest button when scrolled up, and a rounded composer with the model tag, an Auto (auto-approve) toggle and round Stop / Send buttons (Send turns amber and its label says Queue while a turn runs). A "…" menu by the title holds Context (meter, used/limit, session id), Commands (Compact, Context, Diff, Undo, Load context, Clear), Theme (Automatic / Light / Dark), Options (New chat on open, Markdown, Hide tool calls), the access token and the versions. Clicking a chat resumes it (R376, no confirm; the current one does nothing); New Chat starts a fresh chat at once. Server side: `GET /api/sessions?limit=N` (≤ 100), `GET /api/search?q=` (≥ 2 characters, one snippet per session, R161's search), and New Chat after at least one turn starts a new session id, so the old chat stays in the list and can be resumed (a chat with no turns keeps its id; no file is written until the first message). Phone (≤ 768 px): the page opens on the chat, never on the list; the sidebar slides over with a scrim and closes after a pick; the menu is a bottom sheet; every control is at least 44 px; the composer respects the safe area and the on-screen keyboard (`interactive-widget=resizes-content`). Tests: `tests/test_web.py::test_r377_*`.
- **R378 — The logo smiles below its eyes (owner, 2026-10-29: "the smile is over the eyes, not below").** In the favicon, the sidebar logo and the reply avatar of all three pages, the face reads as a two-year-old with a bow on her head: the eyes are two short strokes a little above the middle (y 13.8–16.2), the smile one solid soft curve under them (y ≈ 20.2–21.1, not dashed, so it never looks like a smirk), clear of the dashed outline; the bow stays top-left (owner, 2026-10-29: "push the eyes a bit up … a baby, 2 years old, with a bow on the head"). Test: `tests/test_web.py::test_r378_*`.
- **R379 — In a plain browser tab, /v2 shows the owner's site logo in the sidebar (owner, 2026-10-29).** `base_logo-192.webp` from the owner's site, scaled to 96px (half its size, aspect kept, embedded as a data URI), replaces the Aurora face on `.brand .logo` inside `@media not (display-mode: standalone)`; the installed app keeps the face. Test: `tests/test_web.py::test_r379_*`.
- **R380 — The reply avatar in a plain browser tab is the site logo, and files can be dropped on the message bar (owner, 2026-10-29).** Inside `@media not (display-mode: standalone)` the `/v2` reply avatar (`.msg.reply::before`) uses the same embedded the owner's site logo as the sidebar (R379). Dropping files on the message bar (all three pages, `app.js`) appends each text file to the draft as `name:` + a fenced block (fence longer than any backtick run inside); a file with a NUL byte, or one that would take the draft past 150 KB, is refused with an error note and the rest still go in. The API stays text-only. Tests: `tests/test_web.py::test_r380_*`.
- **R381 — The web chat takes attachments: documents as text, pictures for vision models (owner, 2026-10-29: "support main file formats on upload/attachment").** A paperclip, a file picker and drops on the message bar send each file raw to `POST /api/attach` (bearer token, `X-Filename`, ≤ 20 MB). Text comes back from PDF (pypdf), DOCX, PPTX, XLSX, ODT/ODS/ODP, RTF, HTML and any plain-text or code file (standard library, zip parts capped), at most 150,000 characters (the cut is said), and the page puts it into the draft as a fenced block; unreadable, empty, encrypted or binary files are refused with the reason. PNG/JPEG/GIF/WebP come back as a data URL, longest side shrunk to 1568 px (small ones untouched, EXIF rotation applied); they ride on `/api/send` as `images` (≤ 4), only when the model entry in the config has `vision: true` (else refused, 415/400). The engine keeps them on the in-memory user message (`images`), the OpenAI-compatible provider turns them into `image_url` parts in the outgoing copy only, the session log records a count, never the pixels, and compaction counts a flat 1500 tokens each. The original file on the user's machine is never touched. In a plain browser tab (`display-mode: browser`) the sidebar logo and the reply avatar are the owner's site logo, declared after the face rule so it wins (R379/R380). Tests: `tests/test_web.py::test_r381_*`.
- **R382 — A model served with a vision projector needs no `vision: true` (owner, 2026-10-29: "the current model (local) cannot see pictures").** When the model entry has no `vision` flag, an attached picture is accepted if the server's `GET /props` says `modalities.vision` (llama-server started with `--mmproj`; the gateway only passes /props, its /v1/models drops the capability), falling back to a `multimodal` capability in `/models`; any probe error means no. The probe runs only on attach/send, never from the status poll (it could wake an idle-unloaded model), and is cached 10 s. Test: `tests/test_web.py::test_r382_*`.
- **R383 — The chat page can delete chats and file them in folders, and it is now the default page (owner request, 2026-10-29: "an option to delete a chat and to create folders to drag and drop chats inside").** `/` and `/v2` serve the Chat page; the Paper page moved to `/paper`, `/v1` is unchanged. Each chat row has a delete button (a trash icon on hover or focus; always shown on a phone) that asks first, then **hides** the chat from the web list and search: the session log stays on disk, so the terminal version still has it (`POST /api/chat/delete {id}`; the open chat is refused with 400, the page starts a new chat first). "New Folder" in the sidebar adds a flat folder (no nesting, 60-char name, at most 100) typed in place (a form, so a phone keyboard's Enter submits it; tapping away keeps a typed name); a folder renames in place, folds and unfolds (the state is stored on the server and survives reloads) and deleting it asks first and hides it together with its chats. A chat is in at most one folder; folders list above the date groups and their chats appear only inside them. Dragging a chat row onto a folder files it there, dragging it onto any other part of the list takes it out (`POST /api/chat/move {chat, folder|""}`); drag and drop exists only with a mouse (`(hover:hover) and (pointer:fine)`), never on a touch device and never in search results. Folders and hidden chats live server-side in `$AURORA_HOME/web-chats.json` (`aurora/web/folders.py`; a missing or corrupt file reads as empty); the other calls are `/api/folder/create {name}`, `/rename {id, name}`, `/open {id, open}`, `/delete {id}`, and `GET /api/sessions` carries `folders` plus each chat's `folder`. Every call needs the bearer token. Tests: `test_r383_*`.
- **R384 — After a turn ends, the model chip shows what is loaded now, not a cache up to 2 minutes old (Tasks card `20261031-122549`, owner, 2026-10-31).** A first message that wakes an idle-unloaded model finished with the chip still red `no model loaded (wakes on send)`: the engine's live-name cache held the pre-wake answer. At turn end the web session calls `Engine.refresh_live_state()` (blocking, local model only, off the render path) before emitting `done`, so `done` carries the fresh `live_model`. Tests: `tests/test_web.py::test_r384_*`.
- **R385 — `/v2` is gone (the Chat page is `/`), and each sidebar chat shows how many messages were sent and can be renamed (owner, 2026-10-31).** (1) `GET /v2` and `/v2/` answer 404; `/` serves the Chat page, `/paper` and `/v1` are unchanged. (2) Every chat row in `GET /api/sessions` and `/api/search` carries `prompts`, the number of messages the user sent in that session (the bootstrap turn does not count; cached by the log's mtime), shown as a dim number before the age (`title` says "N messages sent") as a measure of the chat's size. (3) A chat can be renamed in place (pencil icon on hover or focus, always on a phone; Enter or tapping away keeps it, Esc cancels, an empty name restores the first message): `POST /api/chat/rename {id, name}` (60 chars, whitespace collapsed, bearer token, 400 for an unknown id). The name lives in `web-chats.json` (`titles`), so the session log is never touched and the terminal version keeps the first message; rows carry `renamed`. UI version 3.1. Tests: `tests/test_web.py::test_r385_*`, `test_r377_the_chat_page_is_the_default`.
- **R386 — the Chat page always shows the context bar, token count and how many times the chat was compacted (owner, 2026-10-31).** The header carries a bar (amber from 70%, red from 90%), `used / limit · N%` in k/M tokens, and `⤵N` (title "Compacted N times this session") once N ≥ 1; tapping it is the same as before. `GET /api/state` `context` gains `compactions` (the engine's R159 count, auto-compactions included, restored on resume). The Menu's meter stays. Phone: the bar shrinks to 32px. Tests: `tests/test_web.py::test_r386_*`.
- **R387 — in aurora-web, tapping the model name opens a menu of the configured models and picking one switches to it (owner, 2026-11-02).** `GET /api/models` lists the `config.yaml` models (`i`, `provider`, `model`, `current`, `ready`; `ready` is false when the provider needs an API key this machine lacks, and that entry is disabled in the menu). `POST /api/model {i}` switches: 400 for a bad index or a missing key, 409 while a turn runs, 401 without the token. The pick is remembered in `state.yaml` as `web_last_model`/`web_last_provider` (never the TUI's `last_model`) and `aurora --web` restores it at start when its key is still there, else it falls back to the pinned `local` model (R353). `/api/state` gains `provider`; the tag shows `model: <name>` for a non-`local` model, and for a `model: local` entry of another provider (a llama-server on another machine) the loaded model's name prefixed with the provider; such an entry's menu row is the provider name, never a probe, so listing the menu cannot wake a sleeping server. Tests: `tests/test_web.py::test_r387_*`.
- **R388 — aurora-web can open on the last chat instead of a new one (owner, 2026-11-02).** The Menu's "New chat on open" checkbox (browser setting, on by default, as before) decides what a freshly opened page shows. On: a new chat (unchanged). Off: the page resumes the most recent chat that has at least one prompt (`/api/resume`); if the server's live chat already has prompts (a reload mid-conversation) it is kept, and nothing is resumed while a turn is running. Tests: `tests/test_web.py::test_r388_*`.
- **R389 — aurora-web has a Notes section: plain-text notes kept on the server (owner, 2026-11-02; redesigned the same day: "looks poor and amateur").** The Chat page's sidebar has a Notes button that opens a large dialog: a search box and a list (newest first; each row shows the title, or "Untitled", the time or day, and a one-line preview), and a writing area with a big title line, a roomy proportional-font text area, and a word and character count. There is no Save button: the note autosaves 700 ms after typing stops, on switching note, on closing and on leaving the page, one request at a time, and shows "Saved" or a red "Not saved: …" (closing then asks, since the text would be lost). Copy and Delete (which asks) are icon buttons. On a phone the dialog is full screen and shows the list first, then the note with a Back button. Notes live as `AURORA_HOME/web-notes/<id>.txt` plus an `index.json` (title, preview, updated), so every device sees the same ones; title ≤ 80 characters (blank allowed), text ≤ 100,000, ≤ 500 notes; ids are 12 hex characters, anything else is a 404/400. `GET /api/notes[?q=]` (q: title or text, case-insensitive), `GET /api/notes/<id>`, `POST /api/notes/create|save|delete`, all behind the token. The notes belong to the web page only (the TUI and the engine never read them). Tests: `tests/test_web.py::test_r389_*`.
- **R390 — the Chat page Menu uses dropdowns, and Commands sit beside Send (owner, 2026-11-02).** Theme is one select (Automatic / Light / Dark). Options stays a plain list of checkboxes (New chat on open, Markdown, Hide tool calls), always visible, never behind a dropdown (owner, 2026-11-02). The Commands (Compact, Context, Diff, Undo, Load context, Clear) are removed from the Menu and open from a button between the model tag and Send, as a popup that closes after a pick; Clear still asks first. Tests: `tests/test_web.py::test_r390_*`.


- **R391 — the Chat page's Auto-approve switch lives in the Menu's Options, not next to the model name (owner, 2026-11-02).** The checkbox ("Auto-approve tool calls", amber while on) is the first item under Options; the row beside Send holds only the model tag, the Commands button, Stop and Send. Behaviour is unchanged (`POST /api/auto`, on by default). Test: `tests/test_web.py::test_r391_*`.
- **R392 — Aurora Web review fixes (owner, 2026-11-02).** Each line is testable; tests in `tests/test_web_r392.py` (server) and `tests/test_web_mobile.py` (Playwright, 320×568 and 360×330; skipped without Playwright).
  - *Reliability.* A Stop never cancels the message queued after it (bug 1). A body over the limit is answered `413 "too large…"` and the connection is closed, never parsed as the next request (bugs 2, 3); `MAX_BODY` is 1 MB so a 100,000-character note fits in any UTF-8. The server warns at start when pypdf/Pillow are missing (bug 4). A fresh page never opens inside a reply (bug 7). New chat, Undo and a model switch take the turn slot (bug 8). A picture with an EXIF rotation is sent upright, even when small (bug 10). The stream carries a per-run `boot` id; a page from another run gets a `restart` marker and the log from the newest reset, never a splice (bug 11, P10). A damaged `web-notes/index.json` or `web-chats.json` is moved aside as `*.corrupt-<ts>` and never written over; the notes index is rebuilt from the `.txt` files (bug 12). Note saves carry `base` (the `updated` the page loaded): a newer note is `409 changed on another device` and the page asks Overwrite / load theirs (bug 13); a note deleted elsewhere is saved as a new note (bug 14). Auto-approve is remembered in `state.yaml` `web_auto_approve` (bug 15). `done` is sent before the model probe, which follows as a `state` event (bug 16). The vision probe is cached 10 min (bug 17). The listen backlog is 128 (P8).
  - *Speed.* Text/think chunks are sent every 50 ms or 1 KB (P1); the streaming reply redraws only its open last block (P2); a replayed log fires no requests (P3); session first-prompts and search hits are cached by log mtime (P4, P5, P9); a hidden tab does not poll (P6); the replay buffer is capped at 32 MB (P7); app.js is served as `/app.js?v=<hash>` with a year-long immutable cache (P11); at most 20 streams, then 429 (P12). HTML pages send `frame-ancestors 'none'` and `X-Frame-Options: DENY`; sidebar rows carry epoch `ts`.
  - *Web approval policy.* The engine never auto-approves for the web; the page's policy decides: a web denylist (`AURORA_HOME/web-denylist.txt`, one regex per line; default blocks `rm -rf`, `sudo`, `git push`, `mkfs`, `dd if=`, shutdown/reboot, `curl|sh`, `chmod -R 777`, fork bombs) refuses without asking even with auto-approve on; auto-approve on answers yes; off asks the page. Reads are never gated, so off = reads only.
  - *Features.* Plan mode toggle in Options (`POST /api/plan {on}`, a PLAN tag in the header). Cost and usage (`GET /api/usage`: per-model turns, tokens, cost; chat and today totals; `POST /api/budget {which: session|daily, usd}`), opened from the Menu or a tap on the context pill (bug 18). Regenerate / Edit under the last reply (`POST /api/retry {text?}`: cuts the history back to before the last user message; files are not reverted). Voice input: a mic button when `AURORA_WEB_STT_URL` (a whisper.cpp-compatible `/inference`) is set; `POST /api/transcribe` converts with ffmpeg. `GET /health` (no token): version, packages, stores readable, model port reachable (TCP only, never wakes a model); 503 when a package or store is missing. "New chat on open" is skipped while another page is connected or the chat was used in the last 10 minutes (bug 9).
  - *Small phones (U1–U17).* No sideways scroll at 320 px busy or with Notes open; Menu, Send and Stop stay on screen with the keyboard up and the live panel open; popups stay inside the visible area; inputs are 16 px on touch; inline code wraps; the approval dialog shows the arguments first and folds the diff, focus on Deny; one ⋯ per chat row on touch; slim header with the keyboard up; English dates in Notes; the empty meter is hidden; opening Notes closes the drawer.

- **R394 — Front ends reach the core only through `aurora/service.py` (owner, 2026-11-02).** `aurora/service.py` is the
  front-end contract: version, `aurora_home`, sessions (ids, exists, list, search, mtime, prompt count, markdown export,
  usage, new session), remembered state (load/save), the key-store prompter, checkpoints/undo, and a re-export of
  `ProviderError`. Modules under `aurora/web/` import nothing else from the core (the engine and the Frontend protocol
  are passed in). Each facade function delegates at call time, so patching `aurora.session` still works. Pure refactor:
  no behaviour change. Documented in `documents/ARCHITECTURE.md` §1.

  **Test:** `tests/test_architecture.py::test_web_imports_core_only_via_service` (absolute and relative imports, R90a).

- **R395 — Facade deep dive: one-scan session listing, exact-id moves (owner, 2026-11-02).** Listing, searching,
  `latest_session_id` and model latency get every session's freshest mtime from ONE directory scan
  (`session._session_mtimes`), never by probing each session's parts (2000 sessions on the Pi: warm sidebar 120 → 11 ms,
  warm search miss 231 → 15 ms). `service.session_exists` checks a plain id (`[A-Za-z0-9_-]{1,64}`) with one stat of its
  base log, other ids against the full list; prefixes, rotated part names and paths are never sessions.
  `/api/chat/move` refuses an id that is not a session (`no such session`), like delete and rename already did; before,
  any text was stored in `folders.json`. The web may not reach a core module with `importlib`/`__import__` either.

  **Test:** `tests/test_service.py` (contract, call-time delegation, exact ids, one-scan ordering with rotated parts),
  `tests/test_web_r395.py` (crafted ids on move/delete/rename/resume, dynamic-import guard).

- **R396 — Aurora Web offline support (owner, 2026-11-02).**
  - *Errors.* A request that gets no answer (no network) says `offline: not sent …` on the page instead of failing silently.
  - *Indicator.* A header tag shows `offline`, `reconnecting…` and how many messages are waiting; it hides when the link is
    back. A dropped stream is retried at once on the browser's `online` event or when the page is shown again, not after
    the backoff; a stream silent for 40 s (the server's heartbeat is 15 s) counts as dead.
  - *Draft.* The text being typed is kept in localStorage and restored on reload; it is cleared when sent. The box is
    cleared after a send only if it still holds the sent text (text typed while the request was out stays).
  - *Outbox.* A message sent with no connection goes into an outbox (localStorage, max 2 MB a message, else it stays in the
    box), drawn as a dashed "waiting" bubble, and is sent in order when the link is back (stream reconnect, `online`,
    page shown, end of a turn). Each carries a `client_id`; the server accepts an id once (last 500 remembered), so a
    resend after a lost answer never runs a message twice. A refused id (e.g. a command) is not burned.
  - *Service worker + manifest.* `/sw.js`, `/manifest.webmanifest` (icons: R399); every page links the manifest (installable).
    Network first; when offline the page shell, `/api/sessions`, `/api/sessions/<id>` and `/api/notes` are answered from the
    last good copy, marked `X-Aurora-Offline: 1`. `/api/stream`, `/api/state` and every POST are never cached.
    Needs https (Caddy) or localhost.

  **Test:** `tests/test_web_r396.py` (server: dedupe, caps, served files, cache allowlist), `tests/test_web_offline.py`
  (Playwright: tag, outbox and order across a reload, resend after a lost answer runs once, draft, offline errors,
  page and chats opened offline from the service worker, typing during a send).

- **R397 — Aurora Web shows and refreshes OpenRouter prices (owner, 2026-11-02).** `GET /api/models` gives each
  OpenRouter model its cached catalog price (`price: {in, out, cache_read, at}`, $ per million tokens, `at` = last
  refresh) and starts the terminal `/model`'s 24 h background refresh (the page redraws the open menu on a `prices`
  event). The model menu shows `$in / $out per M` (or `free`) per OpenRouter row and a **Refresh prices** button with
  "updated N ago"; the Usage view has the same button. `POST /api/prices/refresh` fetches the catalog once for every
  configured OpenRouter model and stores the prices: 200 `{refreshed, changed: [{model, before, after}], missing}` plus a
  notify line; 502 when the catalog is unreachable (old prices kept); 429 while one runs or within 20 s of the last;
  400 with no OpenRouter models. Usage costs use the new price at once. All of it goes through `aurora.service`
  (`model_price`, `refresh_prices`, `refresh_prices_in_background`).

  **Test:** `tests/test_web_r397.py` (server, fake catalog, no network, no write to the repo's price file),
  `tests/test_web_prices_ui.py` (Playwright, 1280 and 360 wide).

- **R398 — The tab icon is the sidebar's logo (owner, 2026-11-02).** Every page (`/`, `/v1`, `/paper`) links
  `/favicon.png` (also answered at `/favicon.ico`, cached a day): the same picture the sidebar shows in a browser tab
  (the WebP logo in `index_v2.html`), padded to a square on black, pixels unchanged. The home-screen icon follows in R399.

  **Test:** `tests/test_web_r396.py::test_r398_favicon_is_the_sidebar_logo` (pixel-for-pixel against the sidebar's logo).

- **R399 — The home-screen icon is the logo too (owner, 2026-11-02).** The manifest's icons are `/icon-192.png`,
  `/icon-512.png` (purpose `any`) and `/icon-maskable-512.png` (purpose `maskable`, the face kept inside Android's
  80 % safe circle), and every page links `/apple-touch-icon.png` (180 px, iPhone/iPad). All are made from
  `images/base_logo.png` (213×234, the original of the sidebar logo), squared on the logo's own background
  (`#0d0d0e`, also the manifest's theme and background colour) so no box shows. The SVG face (`/icon.svg`) is gone.
  A phone that already installed the app may need it removed and added again to pick up the new icon.

  **Test:** `tests/test_web_r396.py::test_r399_home_screen_icons_are_the_logo` (sizes, purposes, no box, line art present,
  safe zone, `/icon.svg` 404).

- **R400 — One Language setting for voice in and voice out, English by default (owner, 2026-11-02).** Menu → Options →
  **Language** (English UK/US, Português PT/BR, Español, Français, Deutsch, Italiano; per browser, default `en-GB`).
  The microphone sends it (`/api/transcribe?lang=en`; the server passes it to whisper from an allowlist, anything
  else is English). Each finished reply gets a 🔊 button that reads it in that language with the device's voices
  (`speechSynthesis`; tap again to stop; code blocks, tables and URLs are skipped), and **Read replies aloud** speaks
  each new reply when it finishes (never a replayed log).

  **Test:** `tests/test_web_r400.py` (server language allowlist), `tests/test_web_speech_ui.py` (Playwright, 1280 and 360).

- **R401 — A note can be sent to Aurora as context with one message (owner, 2026-11-02).** In Notes, the **Send to chat**
  button (beside Copy) saves the open note, closes Notes and puts a 📝 chip with its title above the message box.
  Typing `#` in the message box opens a picker of matching notes (title or text, up to 8; arrows, Enter or Tab pick,
  Esc closes; on a phone, tap); the pick becomes a chip and the `#word` is removed. A chip can be removed with ×;
  up to 5 notes per message, and they go with that message only (never pinned to the chat). `POST /api/send` takes
  `note_ids`; the server reads the notes itself (an unknown or bad id is a 400, nothing is sent) and gives the model
  `[Note: <title>]` … `[End of note]` blocks in front of the message, while the chat shows only `📝 <titles>` and the
  message. Notes that would not fit in 90 % of the context left are refused with the reason, never cut. Edit keeps the
  notes the message carried; Regenerate and a queued message do too. The text is copied when sent: editing the note
  later does not change a past chat.

  **Test:** `tests/test_web_r401.py` (server), `tests/test_web_r401_ui.py` (Playwright, 1280 and 360).

- **R402 — The sidebar's Chats list folds, like the Menu sections (owner, 2026-11-02).** On the Chat page a **Chats**
  header with a chevron sits above the list of past chats; a tap (or Enter/Space) folds or unfolds the whole list,
  and the choice is remembered per browser (`aurora_web_folds.chats`, the same store as the Menu sections). Typing in
  the sidebar search unfolds it so the results show. New chat, New folder, Notes and the search stay visible.

  **Test:** `tests/test_web_r402_ui.py` (Playwright, 1280 and 360: fold, remembered after reload, search unfolds, keyboard, no sideways scroll).

- **R403 — Notes are Markdown, with an Edit / View switch (owner, 2026-11-02).** The note's header has an **Edit / View**
  button (Ctrl/Cmd+E): Edit shows the Markdown source in the text area, View shows it formatted with the same renderer
  and styles as the chat replies (headings, bold/italic, code and code blocks, lists, quotes, rules, tables, http(s)
  links; built from DOM nodes only, never HTML). One pane at a time, so it works the same on a phone. A note with text
  opens in View; a new or empty one opens in Edit. Task items `- [ ] text` / `- [x] text` show as checkboxes: in View a
  tap ticks it, which rewrites only that line of the source and autosaves (R389); in chat replies the boxes are
  read-only. The file on disk stays plain text.

  **Test:** `tests/test_web_r403_ui.py` (Playwright, 1280 and 360: opening mode, rendering, a tick saves, switching, Ctrl+E).

- **R404 — Notes keep the order you give them by dragging (owner, 2026-11-02).** Each row in the Notes list has a grip
  (⠿) on the right: drag it up or down with the mouse or a finger (the list scrolls near its edges), or focus a row and
  press Alt+↑/↓. A drag never opens the note. The order is stored on the server (`pos` in `web-notes/index.json`), so
  every device sees it: new notes go on top, and an edit no longer moves a note. `POST /api/notes/order {ids}` takes the
  new order of all notes or of the ones a search shows; those swap places among the positions they already held, the
  rest stay put; unknown, bad or repeated ids change nothing (404/400); `updated` is untouched (no save conflict). Notes
  from before R404 keep their old newest-first order. An order that fails to save is said and the list reloads.

  **Test:** `tests/test_web_r404.py` (server), `tests/test_web_r404_ui.py` (Playwright: mouse at 1280, touch at 360, Alt+↑).

- **R405 — An attached file goes to Aurora raw, as a 📎 chip, never into the message box (owner, 2026-11-02).** A text or
  document file attached with the paperclip, dropped on the message bar or dropped anywhere on the page (an overlay
  says "Drop files to attach") becomes a 📎 chip with its name, like the R401 note chips (× removes it; hover shows its
  size). On Send its text goes to the model in front of the message as `[File: <name>]` … `[End of file]`, after any
  notes; the chat shows `📎 <names>` and the message. A plain text file is sent exactly as it is (line endings and the
  last newline kept; this replaces R381's trimming for text files); PDF/office files are sent as their extracted text.
  Up to 10 files per message, each up to `attach.MAX_TEXT` characters (longer ones are cut, and the page says so); a
  bracket or newline in a name cannot end the block early. Edit, Regenerate and a queued message keep the files; the
  R401 context check covers them too. Pictures stay as before (R381).

  **Test:** `tests/test_web_r405.py` (server), `tests/test_web_r405_ui.py` (Playwright: paperclip at 1280 and 360, drop on the page and on the bar).
- **R406 — An attached picture is also a file the agent can open (owner, 2026-11-03: "convert the attached image to black and white" got "I don't have a file path").** Aurora Web saves each uploaded picture, byte for byte (EXIF kept), to `AURORA_HOME/web-uploads/` (mode 600, removed after 30 days) and the message carries `[Attached picture saved at: <path>]`, so tools can read or convert it. The server accepts only paths it saved itself. Tests: `tests/test_web_r406.py`.
- **R407 — Files are dropped on the chat, not anywhere on the page (owner, 2026-11-03).** Only the chat column (`.main`: the log and the message bar) takes dropped files and shows the overlay; a drop on the sidebar, a dialog or elsewhere does nothing, and the browser does not open the file. Tests: `test_r407_*` in `tests/test_web_r407_r408.py`.
- **R408 — An Uploads section lists the saved uploads and deletes them (owner, 2026-11-03).** A sidebar button opens the Uploads dialog: each picture saved by R406 with its name, size and date and a Delete button (house confirm). `GET /api/uploads`, `POST /api/uploads/delete {name}`; a name that is not a file in the uploads folder is 404. Tests: `test_r408_*` in `tests/test_web_r407_r408.py`.
- **R409 — Attached documents are saved and listed too; the drop overlay goes away after any drop (owner, 2026-11-03).** Every accepted upload, not only pictures, is saved byte for byte under `AURORA_HOME/web-uploads/` and shown in Uploads; a file chip carries its path and the message gets `[Attached file saved at: <path>]` (the server accepts only paths it saved). Bug: a file dropped on the message bar attached but the "Drop files to attach" overlay stayed, because the bar stopped the event before the chat column saw it; the column now hides the overlay on any drop inside it. Tests: `tests/test_web_r409.py`.
- **R410 — Uploads shows thumbnails, downloads again and deletes all (owner, 2026-11-03).** Each picture has a 96 px preview made on the fly (`GET /api/uploads/file?name=…&thumb=1`; the saved file is never changed), other files show their extension; Download fetches the file (`GET /api/uploads/file?name=…`) under its original name; Delete all (house confirm) calls `POST /api/uploads/delete_all`. Video is not an accepted attachment, so it has no preview. Tests: `tests/test_web_r410.py`.
- **R411 — A tap outside Uploads or Notes closes it; New Folder sits on the Chats header (owner, 2026-11-03).** A click on the backdrop of the Uploads or Notes dialog closes it the same way its Close button does (Notes still saves first); on a phone Notes is full screen, so there is no outside. New Folder makes a chat folder, so it is a folder icon on the Chats header row instead of a sidebar entry, and it unfolds Chats when they are folded. Tests: `tests/test_web_r411.py`.
- **R413 — The user's files live in `AURORA_HOME/workspace/`, uploads and outputs, both browsable; replies show outputs (owner, 2026-11-03: "i want aurora web to be able to show images on the ui to see and download"; "files related to user inside ~/.aurora/ with sub-folders"; "both uploads and outputs folders should be browsable").** Uploads are saved to `workspace/uploads/` (supersedes R406/R409's `web-uploads/`, whose files move there on first use, still pruned after 30 days); `workspace/outputs/` holds what the agent makes for the user and is never pruned. The web session's system prompt tells the agent to save files it creates there and name the full path. The Uploads dialog has Uploads | Outputs underline tabs, each listing thumbnails, Download, Delete and Delete all; a thumbnail opens the picture full size with Download. A reply naming a `.aurora/workspace/{uploads,outputs}/<file>` path shows pictures inline (click = full size) and a Download button for any file. Files are served only by name from those two folders (no `/`, `..`, or symlink out, token required), unchanged; `view=1` serves png/jpg/gif/webp with their image type, everything else as a download. Tests: `tests/test_web_r413.py`.
- **R414 — "Uploads" is now Files, with friendlier rows and a preview for every common file type (owner, 2026-11-03: "rename the upload section to Files … preview of the md files and the pdf files … other famous file types").** The sidebar entry (folder icon) and dialog are named Files; its Uploads | Outputs tabs stay. Each row is a type icon or real thumbnail (pictures, and a PDF's first page via poppler's `pdftoppm` when installed), the name, then type · size · date ("today 10:37"); tapping it opens the preview. Download and Delete are icon-plus-label buttons, icon only at ≤768 px. The preview shows pictures full size, PDFs in a frame, audio and video in a player, markdown rendered, text/code/csv/json/yaml/log/html/svg as plain source text, and docx/xlsx/pptx/odt/ods/odp/rtf as their extracted text (`?text=1`, first 200k characters); anything else says there is no preview. svg and html are never served as their own type (they could run script in this origin). A reply naming a non-picture output shows a file card that opens the preview; message-bar chips carry the type icon and open the preview. Previews never change the file. Tests: `tests/test_web_r414.py`, `tests/test_web_r414_ui.py` (1280 and 360 px).
- **R415 — Web chat bugs from the deep-dive card (owner, 2026-11-03: "fix all those issues - confirm them first").** A resend of one outbox id is checked and recorded in the same step that accepts it, so two copies racing in run once. A message sent while a model switch, resume, undo or new chat holds the turn slot starts when that ends, not after the next turn. Regenerate and Edit send the message's pictures again. Edit keeps only the notes and files still named in the titles line; removing the line detaches them all; Regenerate keeps them all. The diff is refused while a turn runs. Regenerate after an automatic clear (full context) says the history was cleared. The live chat read during a turn ends with an "in progress" line. A note save refused as a conflict while the page is closing is kept as a new "(conflict copy)" note. Tests: `tests/test_web_r415.py`.
- **R416 — Web performance (same card).** Each event's size is measured once, outside the lock, and the buffer trim uses the stored sizes. Chat search reads at most the newest 4 MB of each chat, newest file first, and a full search cache drops its oldest half instead of everything. Tests: `tests/test_web_r415.py`.
- **R417 — Web QA (same card).** The offline copy of `/api/notes?q=` is kept under its query, never as the full list. `/api/folder/open` gives no error text when it worked. The R377 layout test reads every phone CSS block. Tests: `tests/test_web_r415.py`, `tests/test_web.py`.
- **R418 — The chat page is the official page; Notes and Files fill the chat area; ten features from the deep-dive card (owner, 2026-11-03: "page v2 layout is now the official layout"; "tapping on Notes and Files should not be shown on the overlay, should use all the part that we have now on aurora-web text and input field … the New chat button turns into Back to chat"; "save it when we tap back to chat"; "do F1–F5, I1–I5").** `/` serves the chat page (there is no `/v2`). On that page Notes and Files replace the chat and message bar in the main area (no overlay, the header title says Notes or Files), and the sidebar's New Chat button reads **Back to chat** until you leave them. Back to chat, tapping a chat, or switching between Notes and Files saves the open note first. The Paper and `/v1` pages keep the dialogs. Features: **F1** a chat row's Share link makes a read-only link `/shared/<token>` (random, one per chat, in `web-shares.json`), served without the bearer token as escaped text with a script-free CSP and `noindex`, revoked with Stop sharing; it works only for whoever can reach the server. **F2** each reply ends with a dim line: tokens in and out for the turn, its cost when the provider charges, and the time it took (`turn` on the `done` event). **F3** the approval dialog counts down to the automatic denial; `AURORA_WEB_APPROVE_TIMEOUT` sets the timeout in seconds; a timeout says so in the chat. **F4** a file that appears or changes in `workspace/outputs` during a turn is sent as an `output` event and shown as a file card under the reply, even when the reply does not name it. **F5** Menu > Options > Temperature (0 to 1.5) is sent with each message from this page only and applied to that turn alone; off means the model's own setting. **I1** search rows show how many records matched (and say when only the newest 4 MB of a long chat was searched), and opening a found chat marks the words and scrolls to the first. **I2** each queued message has a remove button (`/api/queue/cancel {position, text}`; the text must still match). **I3** Save as note keeps a reply as a note that remembers its chat; the note shows Open its chat. **I4** Regenerate and Edit say when pictures or notes went again. **I5** the last 50 messages are kept on the device and shown, marked read only, when the page opens offline. Tests: `tests/test_web_r418.py`, `tests/test_web_r418_ui.py` (1280 and 360 px).

- **R419 — File preview Prev/Next, and Esc steps back one layer (owner, 2026-11-03: "alongside the Download and Close button … a Next and Prev button"; "wrap around end and arrow keys work. also ESC key closes the preview, and ESC again goes back to chat window again. esc key will also go back from notes window to chat window").** The preview has Prev and Next, which step through the Files list of the same kind (Uploads or Outputs) in its order and wrap at both ends; ← and → do the same unless a player, iframe or text field has focus. They are hidden when that list has fewer than two files. Esc closes only the top layer: an open menu or modal dialog (e.g. the preview) first, then, with Notes or Files open on the chat page, it goes back to the chat as Back to chat does (the open note is saved first). An inline rename box keeps its own Esc. Test: `tests/test_web_r419_ui.py` (1280 and 360 px).

- **R420 — Music and video uploads are stored, not read (owner, 2026-11-03: "When i try to upload a mp3 file to aurora web i got error … this looks like a binary file"; "i want to upload them to help me tag my music, only that. just uploaded, let it there and when i can change the files").** The web chat accepts audio (`.mp3 .m4a .aac .wav .flac .ogg .oga .opus .wma .aiff .aif`) and video (`.mp4 .m4v .mov .webm .mkv .avi`) up to 200 MB (documents stay at 20 MB). The bytes are saved unchanged in `workspace/uploads` and are not transcribed or otherwise read. The model gets a short line with the file's name, size and path, and is told to use its tools on that path (e.g. to read or write tags) and to save changed copies in `workspace/outputs`. Other binary files are still refused. Test: `tests/test_web_r420.py`.

- **R421 — Download a chat as HTML, brief or full (owner, 2026-11-03: "I want the option to download a session (html format). Before download i can pick if i want just the prompts and final responses, or prompts, commands, thinking and final responses"; tool output: "full").** A chat row has Download chat. It asks for **Prompts and final answers** or **Everything** (prompts, each round's thinking, every tool call with its output, answers), then saves `aurora-<title>[-full].html`: one file, all text escaped, no script, nothing loaded from outside, light and dark. Endpoint: `GET /api/sessions/<id>/export?level=brief|full`. The session log now keeps the thinking of each model round as a `thinking` record, written before that round's tool results (at most 20,000 characters per record; `chars` holds the real size). Tool output is as complete as the log keeps it (4,000 characters, R133b), and the file says how much was cut. Chats from before R421 have no thinking to show. Tests: `tests/test_web_r421.py`, `tests/test_web_r421_ui.py` (1280 and 360 px).

- **R422 — Notes come in three types: Personal, Work, Skills (owner, 2026-11-03: "On the notes, i want 3 types of notes - Personal … Work … Skills"; "existing notes became personal. i can also change notes from one section to other. about the /name thing, no need").** The Notes list has house tabs Personal, Work and Skills. Each shows only its own notes, and the device remembers which tab was open. A new note takes the open tab's type. Notes from before R422 are Personal. In the open note, a selector moves it to another type: the typed text is saved first, and the list follows the note to its new tab. Moving changes neither `updated` nor the order. API: `GET /api/notes?type=`, `POST /api/notes/create {…, type}`, `POST /api/notes/type {id, type}`. Skills notes are plain notes, not `/name` commands. Tests: `tests/test_web_r422.py`, `tests/test_web_r422_ui.py` (1280 and 360 px).

- **R423 — Edit the models list from the page; config.yaml's comments are kept (owner, 2026-11-03: "can we have a better way to add/delete/update them using the UI? The ui would read the yml file, and changing on the aurora web ui, would update the yml file"; on losing comments: "why will lose the comments? i dont want. think on way").** Menu > Models… lists the entries of `models:` in the config file Aurora was started with: model, provider, tools, vision, other settings, in use. Add model, Edit and Delete change that file. The new module `aurora/modelcfg.py` edits it as text, not with a YAML load and dump (a dump is what used to drop every comment). Only the lines of the entry being added or deleted are written, or, for a change, the lines of each key whose value changed; a changed one-line scalar keeps its inline comment. Every other line of the file stays byte for byte. After each edit the new text is parsed and must give exactly the asked-for list with every other top-level key unchanged; otherwise nothing is written and the page says to edit by hand (also for inline `models: [...]` lists or tabs). `/model add` and `/model remove` in the terminal use the same editor now. Edits name the entry by index plus its expected provider/model, so a file changed meanwhile is refused (409). The model in use cannot be deleted, and nothing is edited while a reply runs. If `config.local.yaml` has its own `models:`, the list is read-only and the page says why. API: `GET /api/models/config`, `POST /api/models/config/add|save|delete`. Tests: `tests/test_modelcfg_r423.py`, `tests/test_web_r423.py`, `tests/test_web_r423_ui.py` (1280 and 360 px).

- **R424 — Every screen keeps the house spacing and one height per row (owner, 2026-11-03: "review the ui rules and if all is ok and same ui rules and margins on all the screens of the aurora web").** Checked with the DesignLanguage `tools/check.py` and a per-screen audit at 1280 and 390 px: chat, sidebar, menu, Notes (list and open note), Files, preview, Models (list and form), Download chat, and the confirm dialog. Vertical gaps between stacked elements are on the 0/8/16/32/64 scale: the sidebar's 1, 2 and 10 px and the composer's 4 px were fixed. Controls sharing a row have one height: the header and composer tags match the 36 px buttons (44 on touch), the paperclip is a round 36 px icon button like the others, and the note-type selector and New note are 36 px. Every dialog and panel uses one header bar (title 16 px from the left edge and vertically centred in a 36 px row, 44 on touch, buttons 36 px), and the Notes header now sits at the same height as Files and Models. Confirm and Download chat share one width. The Send button's fill is flavour 3's rule, not a gap. Files rows are list rows, taller than their action buttons on purpose. Test: `tests/test_web_r424_ui.py`.

- **R425 — Deep-dive fixes in R419–R423 (owner, 2026-11-03: "do a deep dive on the features you build now and find bugs and performance issues. fix them immediately").** (1) Opening a preview resets Prev/Next until that file's own Files list has loaded, and a slower answer for an earlier preview is ignored; before, Next pressed straight away stepped through the previous preview's list. (2) Chat search skips `thinking` records, so a hit always points at text the chat shows. (3) A cancelled turn's thinking is still logged. (4) A Notes search looks in all three types, and each hit names its type; before, a note in another tab could not be found. (5) Renaming the model in use from Models also moves the page's remembered pick (`web_last_model`), so the next start does not fall back to the default. No performance problems found: the preview list is one stat-only listing per opened file, thinking is appended per chunk with a cap, export and Models read the file once per request. Tests: `tests/test_web_r421.py`, `tests/test_web_r422_ui.py`, `tests/test_web_r423.py`.

- **R426 — Wake a sleeping model on request; Save as note also downloads the chat (owner, 2026-11-03: "if i re-select a model that is sleeping, i should awake the model. tapping idle on the top of screen, should also awake the model"; "when i tap Save as a note, nothing happens! that should CREATE and DOWNLOAD the chat content as HTML").** Picking a model in the model menu, including the one already in use, and tapping the **idle** status in the top bar both call `POST /api/wake`. For the `local` model, when it is not loaded, that sends a one-token completion: an idle manager wakes llama-server only on a completion, not on `/props` or `/v1/models`. The status then shows the loaded model at once, without waiting for its 2-minute refresh. The chat says "waking the model…", then "awake: <model>" or why it failed. A remote model, a loaded one, or a running reply sends nothing. **Save as note** under a reply keeps the reply as a note (as R418 I3), downloads the chat as HTML (prompts and final answers, R421), and says both in the chat. Before, the only sign was the button changing to "Saved". Note: on 2026-11-03 the Pi's aurora-web had kept running the code from before R419, while it served the new page from disk, so the new page called routes the old server lacked (note tabs showed every note, Add model did nothing). A pull restarts the service through the post-merge hook; a commit made in a deployed clone does not, so restart it with `deploy/restart-web.sh`. Tests: `tests/test_web_r426.py`, `tests/test_web_r426_ui.py` (1280 and 360 px).

- **R427 — Raw editor for config.yaml and config.local.yaml in Models (owner, 2026-11-03: "the menu should have an option to see the yml file content"; "whole" file, "raw editor also", config.local.yaml "yes"; "validate that the yml is valid before we save. after we save the models menu should be reloaded"; the rest "your call").** The Models dialog has the house tabs **List** (the R423 form), **config.yaml**, and **config.local.yaml** (only when that file exists). A file tab shows the whole file in a monospace editor with line numbers (no wrapping; it scrolls inside the editor; Tab inserts two spaces). Its buttons are Revert, Validate and Save.
  - **Validate** checks on the server, without saving: that the text is valid YAML (with the line and column of an error; the cursor jumps there), the shape Aurora needs (`providers` is a mapping of mappings, `models` is a list of entries each with a `model` and a provider listed under `providers`, `runtime`/`skills` are mappings, checked on the file merged with the other one), and that the model in use is still listed.
  - **Save** validates the same way and writes nothing on any error. It refuses when the file changed on disk since it was loaded (409, Revert reloads it), keeps a copy `<file>.aurora-bak-<time>` (the newest 5; hand-made `.bak-` copies are never touched) and writes atomically.
  - After a save, the models and the providers reload at once, keeping the model in use, and the List and model menu follow. A `runtime:` change applies after a restart, and the page says so.
  - **Secrets never reach the page:** a value whose key looks like a secret (`api_key`, `token`, `secret`, `password`, …, but not `*_env`, `*_fetch`, `*_file`, `*_path`, `*_url`), in a `key: value` line or inside a one-line `{…}` mapping, is shown as `«hidden»`. On save, the server puts the original back at the same key path. A value typed over `«hidden»` is saved. A `«hidden»` whose key was moved or renamed refuses the save, so a key is never written as the text `«hidden»` and never lost.
  - Esc leaves an edited file only after "Discard changes?", then goes back to List, then closes the dialog.
  - API: `GET /api/config/files`, `POST /api/config/validate|save {name, text, base}`.
  - Tests: `tests/test_configedit_r427.py`, `tests/test_web_r427.py`, `tests/test_web_r427_ui.py` (1280 and 360 px), plus the R424 audit of the raw tab.

- **R428 — The top bar keeps its pills whole (owner, 2026-11-04, card aurora-web-v2: "if i change chat, the context view on the top and the server status, both break ui").** On the chat page (`/`), a long chat title, such as one that is a pasted line of `df` output, used to take the space of the status pill and the context pill: the status showed "i…" and the context was cut mid-number. Now only the title shrinks and ends in "…". The status and context pills keep their natural width. The phone rules of R392 still apply (at 400 px and below, the context pill drops its meter and the status shows only while busy). Tests: `tests/test_web_r428_r433.py`, `tests/test_web_r428_r433_ui.py` (1280 and 360 px).

- **R429 — Models are edited as YAML only (owner, 2026-11-04, card aurora-web-v2: "The settings model modal, should only display the yml from now on, and we can validate the config and save it"; "only the yml way to edit on the ui; not the input fields").** The Models dialog (now titled **Edit models**) is the R427 raw editor alone, with the tabs **config.yaml** and **config.local.yaml** (only when that file exists). It opens on config.yaml. The R423 List tab, its Add/Edit/Delete form and their fields are gone from the page. The server routes `/api/models/config*` stay for scripts and the TUI's `/model add|remove`. Esc or Close asks "Discard changes?" first when the file was edited. Tests: `tests/test_web_r428_r433.py`, `tests/test_web_r427_ui.py`.

- **R430 — Edit models and Refresh prices live in the model menu (owner, 2026-11-04, card aurora-web-v2: "should disappear from the settings and be moved to the models pickers. along side with option \"Refresh prices\" and a option \"Edit models\"").** Tapping the model name under the message box lists the models as before, then **Refresh prices** (when an OpenRouter model is configured, R397), then **Edit models**, which opens R429's editor. Models… is no longer in the settings. Tests: `tests/test_web_r428_r433.py`, `tests/test_web_r428_r433_ui.py`.

- **R431 — Settings fill the right side, opened from the bottom of the sidebar (owner, 2026-11-04, card aurora-web-v2: "The overall ui/ux of the settings menu is super bad. review it and redo the settings"; "maybe on the bottom to left side pane to open settings, and then full right view is the settings space to exist, like the files and notes").** The ••• button and its drop-down are gone. **Settings** is the last item of the sidebar. It replaces the chat the way Notes and Files do (R418): the top bar reads "Settings", New Chat reads "Back to chat", and Esc goes back. The page is one column of at most 560 px (Design Language, Settings view), with one heading per group and nothing folded:
  - **Appearance:** Theme (System, Light, Dark), formatted replies (Markdown), Hide tool calls, and the voice Language (used by the microphone and Read aloud).
  - **Chat:** Auto-approve tool calls, Plan mode, New chat on open, Temperature (R418 F5).
  - **Context and usage:** the context meter and its numbers, the session id (tap to copy), and Cost and usage….
  - **Access token**, with one line on what it is for.
  - The code and page versions, and a link to the Paper page.
  
  A 401 from the server opens Settings with the token field focused. The context pill in the top bar still opens Cost and usage. The `/paper` and `/v1` pages are unchanged. Tests: `tests/test_web_r428_r433.py`, `tests/test_web_r428_r433_ui.py`, and the R424 spacing audit of the panel.

- **R432 — Uploads is now Input, in the page, on disk and in the API (owner, 2026-11-04, card aurora-web-v2: "The upload folder should be renamed instead Input folder"; "all deep. ui, folders, all").** What the user attaches is kept in `AURORA_HOME/workspace/input` (was `workspace/uploads`, R413). The first use moves every file from `workspace/uploads`, and from R406's `web-uploads`, into `input`, then leaves `workspace/uploads` as a symbolic link to `input`, so a path named in an older chat still opens. Files' tabs read **Input** and **Outputs**. The API is `GET /api/files?kind=input|outputs` (reply `{files, kind}`), `GET /api/files/file`, `POST /api/files/delete`, and `POST /api/files/delete_all` (all were `/api/uploads…`; `kind=uploads` is refused). The model is told an audio or video file "was saved unchanged in the input folder". A reply that names an old `workspace/uploads/…` path still shows the file. In code: `attach.input_dir/files/file_path/delete_file/delete_all_files/is_input/max_attach`, `INPUT_KEEP_DAYS` (30, as before), and DOM ids `files`, `files-btn`, `fl-*`. Tests: `tests/test_web_r428_r433.py`, plus the R406–R420 tests moved to the new names.

- **R433 — A note tab always shows a note (owner, 2026-11-04, card aurora-web-v2: "if im on note X, change tab, the note should be closed, and the first note on that tab should be open. if no notes exist on that tab, create a new one. if we change tab and we created an empty note, should be deleted"; tabs are Personal, Work, Skills).** Changing tab in Notes saves and closes the open note, clears the search, and opens the first note in the new tab's list. When that tab has no notes, a new empty note of that type is created and opened. On leaving a tab, an open note with no title and no text is deleted, whoever created it. A blank note the page made for an empty tab is also deleted when you press Back or close Notes. Moving a note with the type select (R422) is unchanged. Tests: `tests/test_web_r428_r433.py`, `tests/test_web_r428_r433_ui.py` (1280 and 360 px), `tests/test_web_r422_ui.py`.

- **R434 — Edit models line numbers cover the whole file (owner, 2026-11-04: "line number are not ok", screenshot: numbers stopped at 16 while the text went on).** The page's general `dialog pre` rule (45vh cap, border, wrapping) also applied to the editor's line-number gutter, which is a `<pre>`. So the numbers stopped part-way down, sat in their own rounded box, and drifted away from their lines on scroll. The gutter now fills the editor's height, has no own border or wrapping, and scrolls with the text: the last number sits beside the last line. Test: `tests/test_web_r434_ui.py` (1280 and 360 px).

- **R435 — A clearer chat download (owner, 2026-11-04: "the html download format of the conversations with aurora - they are not great. improve the UI to be more clear. review it").** The R421 HTML file was reviewed on a real chat. It showed answers as raw Markdown, gave a pasted document as the prompt the whole page, used a title that kept `#` and backticks, and labelled each tool call only "→ run_command" (or the whole call, fences included). Now:
  - **Header:** the title is the first prompt's first words, without Markdown marks. Pills under it show the date and time span, the number of prompts, the models used, and the level.
  - **Turns:** each prompt starts a numbered turn ("You #n", time) in a bubble on the right. The answer below it carries a label (Aurora, model, time).
  - **Markdown:** prompts and answers are rendered: headings, lists (nested, with ☐/☑ tasks), tables, code blocks, quotes, rules, bold, italic, inline code, and links. Only http(s) links are made; everything else is escaped text, so the file still runs no script and loads nothing (CSP `default-src 'none'`).
  - **Long prompts:** a prompt over 15 lines or 1200 characters folds to its first lines, with "Show all N lines".
  - **Full level:** a turn's thinking and tool calls sit in one "Worked: N steps (k tool calls, thinking)" block before its answer. Each step's title is the tool and its target (the command, path, URL or first value, one line). Opening a step shows the full arguments, then the output (scrolls past 480 px).
  - **Small screens and print:** one column on a phone; for print, light colours and outputs not cut.
  
  Tests: `tests/test_web_r435.py`, `tests/test_web_r421.py`.

- **R436 — Reply actions are one row of house icon buttons (owner, 2026-11-04: "do a deep dive on aurora-web project and find bugs and correct them. also review ALL the UI/UX, still looks fishy on some sections").** Under the last reply, Regenerate, Edit, Save as note and Read aloud are `.btn` buttons with the house line icons (no ↻ ✎ 📝 🔊 glyphs). A phone shows the icons only, at 44 px, with each button's label as its name. Read aloud used to float alone in the middle of the column; it now sits in that row. It shows Stop while reading. An older reply keeps its own Read aloud, aligned with its text. The `/paper` and `/v1` pages keep their glyph button. Tests: `tests/test_web_r436_r437.py`, `tests/test_web_r436_r437_ui.py` (1280 and 360 px).

- **R437 — Deep-dive fixes, 2026-11-04 (owner, 2026-11-04: "do a deep dive on aurora-web project and find bugs and correct them. also review ALL the UI/UX, still looks fishy on some sections").** Every screen of `/` was photographed at 1280 and 390 px, and the newer code was read for bugs.
  - **UI**
    1. Notes and Files no longer repeat their title in a second bar under the top bar. New note sits beside the note search. Delete all sits at the end of the Input/Outputs tab row. On a phone, the note's Back arrow is the first item of the note toolbar, not alone on its own row.
    2. Opening a panel (Settings, Notes, Files) focuses no field, so Settings no longer opens with Theme highlighted and a phone keyboard stays down.
    3. The note toolbar's Edit/View toggle is drawn like the other buttons. The type select is never cut ("Persona"). On a narrow phone the toolbar wraps, with 8 px between rows.
    4. Controls sharing a row have one height: note search and New note are 36 px, or 44 px on a phone.
    5. A chat row's hover actions also take the room of the message count, so the title keeps more space.
    6. On a phone, the file preview puts the file name on its own line above Prev/Next/Download/Close.
    7. Delete all says which folder it means ("Delete every file in Input?").
    8. On a desktop, opening Notes opens the tab's first note, as a tab change does (R433).
  - **Bugs**
    1. A tab clicked while Notes was still loading its first note got that note's text in the new tab's editor, so a title typed there renamed the wrong note and moved it. Opening Notes and every tab change now run one after another.
    2. Two quick tab clicks could create two blank notes.
    3. The download's list parser kept its position in a value shared between calls. Two chats downloaded at the same moment, which the threaded server allows, could garble each other's lists. Each call now keeps its own.
    
  Tests: `tests/test_web_r436_r437.py`, `tests/test_web_r436_r437_ui.py`, and the R424 spacing audit (which caught two height mismatches on the way).

- **R438 — A table right under a line of text is a table (owner, 2026-11-04: "the table is valid but is not well formatted on the ui", screenshot: "**Audio details:**" followed on the next line by a Markdown table, shown as one paragraph of pipes).** The page's Markdown renderer started a table only after a blank line: a paragraph took every following line until a blank one. Models often put a bold label straight above a table. A paragraph now also ends where a table starts (a row with `|` over a `---` row) or where a horizontal rule starts. Found on the way: a line like `  # x` (a heading indented by spaces) matched neither the heading rule nor the paragraph's stop test and looped forever, freezing the page; the paragraph now always takes at least one line. The chat download (R435) already checked every line and was not affected. Test: `tests/test_web_r438_ui.py`.

- **R439 — Markdown: a CommonMark/GFM subset, safe and unbreakable (owner, 2026-11-04: "improve the overall md support compatibility and resilience on the aurora-web app and on the notes section").** The page's one renderer (chat replies, Notes View, file previews, the `/paper` and `/v1` pages) was rewritten. It is still built from DOM nodes and `textContent` only: no text ever becomes HTML.
  - **Blocks:**
    - Headings: ATX `#`–`######` (up to 3 leading spaces, closing `#`s dropped), and `===` under a line.
    - Code: fenced with ``` or `~~~` of any length (a longer fence holds a shorter one, the language becomes `language-x`, an unclosed fence runs to the end), and indented code.
    - Quotes: can hold any block (lists, code, quotes) and take lazy continuation lines. Task ticks are not editable inside a quote.
    - Lists:
      - nested by indentation, including under-indented nesting ("1. a" then "  - b");
      - ordered lists keep their start number (`3.` stays 3);
      - a change of marker starts a new list;
      - loose lists get paragraph spacing; lazy continuation lines join their item;
      - task items `[ ]`/`[x]` at any depth, each still mapped to its source line, so a tick in Notes View edits the right line.
    - Tables (GFM): `:--` `:-:` `--:` alignment; `\|` and `|` inside `code` stay in the cell; short rows are padded. A table needs one delimiter per header cell, so "x | y" over `---` is text and a rule.
    - Rules: `---`, `***`, `___`, spaced forms. A line of text followed by `---` gives text and a rule.
  - **Inline:**
    - Code spans of any backtick run (``` `` a`b `` ```).
    - `***strong em***`, `**`/`__`, `*`/`_` (no emphasis inside snake_case), and `~~strike~~`.
    - Links: `[text](url "title")` with balanced parentheses in the URL, `<https://…>` autolinks, and bare `https://` or `www.` URLs, with trailing punctuation left out of the link.
    - Images become a link "🖼 alt", so nothing loads. Only `http(s)` and `mailto` links are made; any other link shows its words without a target.
    - `<br>`; a hard break from two trailing spaces or a trailing backslash; backslash escapes; and the entities `&amp; &lt; &gt; &quot; &apos; &nbsp;` and numeric ones. Other HTML stays visible text.
  - **Resilience:** an exception shows the raw text instead of a blank reply. Nesting is capped at 16. Every loop always advances. A 4,000-line reply ending in 20,000 asterisks renders in well under 3 s.
  - **Streaming:** the commit point that freezes finished blocks also knows `~~~` fences. It never cuts before an indented or list line, so a loose list is not split while it streams.
  - **CSS:** h2/h3 now differ from h4, and h5/h6, strikethrough, nested and loose lists, and image links have styles. Task items look the same in chat replies and in Notes.
  - Tests: `tests/test_web_r439_ui.py` (26 block and inline cases, links, a no-HTML check, task source lines, a 400-input random stress test that checks no throw, no hang and no lost words, streaming cut points).

- **R440 — Writing Markdown in Notes (owner, 2026-11-04: "improve the overall md support compatibility and resilience on the aurora-web app and on the notes section").** In a note's Edit mode:
  - Enter on a list line continues the list: `- ` stays `- `, `3. ` becomes `4. `, and `- [x] ` becomes `- [ ] `, with the same indent.
  - Enter on an empty item removes its marker, which ends the list.
  - Tab and Shift+Tab indent or outdent a list line by two spaces. Elsewhere Tab still moves the focus.
  - The note list's one-line previews drop the Markdown marks: "- [ ] milk **now**" reads "☐ milk now".
  
  Tests: `tests/test_web_r440_ui.py`.

## R441 - Version 1.3

Asked for 2026-11-04: the scheme moves from `1.2.<n>` to `1.3.<n>`. `VERSION_BASELINE.json` `major_minor` becomes `1.3`
and `baseline_commit_count` stays 393 (the last published release), so `<n>` keeps counting commits since it: 216 at
609 commits, 217 with this commit. `documents/CHANGELOG.md` names the new scheme.

Tests: `tests/test_version_scheme.py`.

## R442 - The /model picker names the provider when a model name repeats

Asked for 2026-11-04 (a screenshot of `/model` with three identical `local` rows): when two or more configured providers
serve the same model name, typically several `local` servers, those rows read `provider/model`, for example
`server-a/local → <loaded model>`. A name that appears once keeps its plain label.

Tests: `tests/test_picker_provider_r442.py`.

## R443 - The web sidebar slides

Asked for 2026-11-04 (with the same change in the Tasks sidebar): in all three web layouts the sidebar or drawer animates
instead of appearing and vanishing. The chat layout (`/`) and the classic one (`/v1`) slide the sidebar out with a
negative left margin, so the page widens as it goes (0.22 s); on a phone the chat sidebar slides over the page and the
scrim fades in. The paper layout's menu drawer slides from the right edge (0.25 s). Once closed, the pane is
`visibility:hidden`, so keyboard focus and screen readers cannot reach it. Users who prefer reduced motion get none.

Tests: `tests/test_web_r443_slide.py`.

## R444 — Copy by default in the chat pane (owner, 2026-11-04, card "Aurora - Double Tap on word")
Releasing a drag-selection in the chat copies it to the clipboard at once: the highlight stays, and the status bar says "copied N chars — <method>". A double-click on a word highlights just that word and copies it; the second click is swallowed, so a per-fragment handler (the thinking toggle) does not fire twice, and a third quick click starts over. Both work on SGR terminals (release reports LEFT) and legacy ones (release reports no button); a right or middle release is ignored. Nothing is copied for a selection of blanks. The "copy selected" button and the `/copy` menu still copy later, so they replace what the selection copied. Text selected in the prompt line is still copied only on request. Tests: `tests/test_tui.py` (`test_drag_select_*`, `test_double_click_*`).

## R445 — The `/model` picker names what each backend has loaded, and reads it fresh (owner, 2026-11-04: the model the picker says is loaded on a remote machine was wrong)
The loaded-model name cache is keyed per provider. It used to be keyed by the current provider, so every `local` row (one per backend) showed the current backend's model. Opening the picker also re-reads every `local` row's `/props` (each in its own thread, inside the picker's health deadline) instead of serving a name up to 120 s old; a dead backend shows no name and cannot hold the picker open. Tests: `tests/test_core.py::test_model_picker_labels_each_local_row_with_its_own_loaded_model`.
- **R451 — A phone's closed sidebar is closed from the first paint (card `20261114-203412-aurora-issues`, B4, 2026-11-14).** The sidebar used to start open and the script then closed it, so on a phone it slid shut over 0.22 s on every page load, and a tap in that moment missed (the six mobile UI tests, R414/R418/R419/R421/R422/R428, timed out on "element is not stable"). The remembered state is now applied with `body.sb-init` set, which switches the slide off, and the class is removed two frames later, so opening and closing by hand still slide (R443). Tests: `tests/test_web_r451_ui.py` (360 px, needs Playwright).
- **R452 — Bash mode is a real, cancellable, live shell (card `20261123-183314-aurora-bash-mode-weaknesses-deep-dive`, part 1, 2026-11-23).** Commands typed in bash mode (`!` / `$`), and the model's `run_command` (foreground and `background=True`) and classic-mode `!cmd`, run in **bash** when it is installed (`tools._shell_executable`), not `/bin/sh` (dash on Debian: no brace expansion, `[[ ]]`, arrays, `source`). In bash mode: Esc-Esc on an empty prompt while a command runs opens "Cancel this?" (main) or offers "Cancel the side command" (side channel, which now polls `_side_cancel`), instead of "Leave bash mode?"; output appears while the command runs, in one growing entry (at most ~10 redraws a second, a 256 KB live window); a non-zero exit shows a dim `[exit N]`, a cancel `[cancelled]`, a timeout `[timeout after Ns]`; typed commands time out after 3600 s (`bashmode.BASH_MODE_TIMEOUT`), not the tools' 300 s. Each command's final directory and environment carry over to the next (an EXIT trap writes them out), so `cd a && make`, `cd -`, `cd $X`, `export` and `source .venv/bin/activate` work; aliases and shell functions do not carry over; a killed command or one with its own EXIT trap keeps the previous state; the side channel reads the state and never writes it. A plain `cd <path>` is still resolved without a shell (an unquoted path with spaces works). Prompt mode and bash mode keep separate up-arrow histories (`input_history`, `bash_history` in `AURORA_HOME`). When the output goes straight to the screen (no `|`, `>`, `$(`, backtick), colour is forced on (`CLICOLOR_FORCE`, `FORCE_COLOR`, git `color.ui`, GNU `ls`/`grep`/`diff --color=always`); clickable file names keep their colour and are underlined. Editors, full-screen programs, argument-less REPLs, `ssh host` without a command and `git commit` without a message are refused with a one-line "needs a terminal" hint instead of failing silently; sudo failing for want of a password gets a hint. "Copy last" records side-channel output too and copies it without colour codes. Bash-mode output is never added to the model's context. Tests: `tests/test_bashmode_r452.py`, `tests/test_tui.py`.
- **R453 — Existing commands follow one rule each (card `20261123-183314-aurora-bash-mode-weaknesses-deep-dive`, part 2, #9–#12, 2026-11-23).** (a) A line starting with `/` whose first word contains another `/` (`/etc/hosts has a bad entry`) or names an existing path (`/tmp is full`) is a message, not a command (`paths.starts_with_path`): the web UI sends it to the model instead of answering "commands are disabled in the web version", and the TUI and classic REPL send it instead of `[unknown skill: …]` when no skill has that name. Real commands are still refused on the web. (b) Every on/off command takes `on|off`: `on` never turns a setting off. A bare `/multiline`, `/thinking` or `/markdown` (display toggles) flips it; a bare `/cache`, `/fallback` or `/redact` (settings) only shows it, so checking `/redact` never switches redaction off; any other argument prints the command's usage and changes nothing. `/thinking` and `/markdown` are saved to `runtime:` (`show_thinking`, `render_markdown`) like `/multiline`. (c) The five copy commands are one: `/copy` (the last turn's final reply), `/copy N`, `/copy turn`, `/copy full`, `/copy all`, `/copy bash`, `/copy last` (whichever is newer, turn or bash output, and the message says which). `/copy-response`, `/copy-last`, `/copy-turn`, `/copy-all` still work as aliases but are not listed in help, completion or the manual. `/copy <anything else>` prints the usage and copies nothing. Tests: `tests/test_r453_commands.py`.
- **R454 — The web page's share links, policy, token checks and start-up (card `20261123-183314-aurora-bash-mode-weaknesses-deep-dive`, part 3, #17–#20, 2026-11-23).** (a) A share link is a snapshot: `shares.create` stores the session's record count (`upto`) and an expiry (`expires`, 7 days by default; `days=0` never expires). `/shared/<token>` renders `export.render(sid, "brief", upto=…)`: the prompts and final answers up to that point, never tool output or thinking, with a footer saying it is a read-only copy and until when. Sharing the same chat again keeps the token and moves the snapshot and expiry to now. An expired, revoked or unknown link is 404 ("does not exist, was revoked or has expired"). A link stored before R454 (no `upto`) is frozen at its first read and given the default expiry. (b) Every app page (`/`, `/paper`, `/v1`) sends `Content-Security-Policy` with `default-src 'self'`, `script-src 'self'` plus one `'sha256-…'` per inline `<script>` (computed from the page served, never `'unsafe-inline'`), `connect-src 'self'`, `img-src 'self' blob: data:`, `media-src 'self' blob:`, `frame-src blob:`, `worker-src 'self'`, `object-src 'none'`, `base-uri 'none'`, `form-action 'self'`, `frame-ancestors 'none'`; inline styles stay allowed. (c) After 10 wrong bearer tokens within 10 minutes a client gets 429 for 5 minutes without its token being checked; a right token clears its count. The client is the peer address, or the right-most `X-Forwarded-For` entry when the peer is loopback (a local reverse proxy); a remote peer's header is ignored. At most 1000 clients are tracked. (d) `aurora --web` starts on the page's remembered model (R387) if its provider has a key, else the `local` entry, else the first configured model whose provider has a usable key, and refuses to start only when there is none. Tests: `tests/test_web_r454.py`, `tests/test_web_r418.py`.
- **R455 — The TUI says only what it checked (part 4 of card `20261123-183314-aurora-bash-mode-weaknesses-deep-dive`, #23–#28, #30–#33, 2026-11-23).** (a) A bare address in `100.64.0.0/10` (the shared range VPNs use) is a local host (`_is_lan_host`): it is probed before "ready" is shown and gets the short connect budget; before, Python's `is_private` is False for it, so a dead backend there showed "✔ ready". (b) The `/model` picker marks the current model with `(current)`, never a green ✔ that could sit next to "⚠ unreachable". (c) A connection failure mid-turn gives Ollama advice (`ollama serve`) for an Ollama provider, llama.cpp advice otherwise. (d) A turn whose model never answered (no text, thinking, tool call or usage) leaves no "thought for Ns" row. (e) Retry notices have one bullet and no blank line between them. (f) The TUI's `?` screen is its own short help (`ui.TUI_HELP`: Basics, Keys, Commands, one line each, no classic-REPL notes); the classic REPL keeps `HELP`. (g) The status bar's draft estimate reads `draft N tok`, only for a prompt (never for a `/command`, so `/` no longer shows `↑0`). (h) The `/` completion menu sits directly above the input box (`_AboveInputFloat`), never over its border or the status bar. (i) The hint row runs most useful first (`? Help · Esc … · / commands · ! bash · Ctrl+J newline · \n/\br newline`), so a narrow screen drops the least useful. (j) Esc on the model picker prints "cancelled — model unchanged" under the echoed `/model`. (#29 was R452.) Tests: `tests/test_r455_tui.py`, `tests/test_tui_smoke.py`.
- **R456 — End-to-end smoke tests in a pseudo-terminal (part 5 C of card `20261123-183314-aurora-bash-mode-weaknesses-deep-dive`, 2026-11-23).** `tests/test_tui_smoke.py` starts the real `python -m aurora` in a pty (110×32) against a fake local model server (llama.cpp-style `/props` plus streamed OpenAI chat, tool calls included), emulates the screen with `pyte` and asserts on its text: start-up with the backend up ("✔ … ready", a reply) and down (no "ready", the picker, a turn that fails with no "thought for" row and no doubled bullet), one turn with a tool call, bash mode (real bash, `[exit 1]`), `/model` and Esc, Esc-Esc cancelling a streaming turn, the `?` help, and the `/` menu leaving the input box framed. `pyte` is in the `dev` extra; without it the file is skipped. Run before every release.
- **R458 — The web page says which device a turn belongs to, and where a typed command lives (#22 and #13 of card `20261123-183314-aurora-bash-mode-weaknesses-deep-dive`, 2026-11-23).** (a) The one live chat is shared by every device. The `user` event carries the `client` id of the message being answered, and `/api/state` carries `busy_client`; queued messages keep theirs. A page that did not send that message shows "busy on another device…" (with a tooltip saying its own messages queue behind it) instead of "working…". (b) A typed `/command` is still never run on the web, but the refusal says where its web equivalent is (Clear, Compact, Context, Diff and Undo in the ⋯ menu; the model menu; Plan mode in Settings; New Chat, the chat list, search and download in the sidebar), or that it exists only in the terminal, naming what the web does have. The same applies to an edited and re-sent message. Whether more commands should run on the web is left open. Tests: `tests/test_web_r458.py`, `tests/test_web.py`, `tests/test_r453_commands.py`.
- **R460 — A note opened by link is context in a chat (2026-11-24).** For someone who does not think to give Aurora background (names, email addresses, workplace, preferences) every time. (a) Notes › the link button copies `<page>/?note=<id>` (the id, so a rename does not break it). (b) Opening it loads that note into a chat at once, before any message (`POST /api/load-note {id, new_chat}`): into the open chat when it is empty, otherwise the page asks "New chat" or "Add to this chat" (Esc does neither). The page then drops `?note=` from the address, so a reload does not load it again. A bad or deleted note's link is said in the chat. (c) The note goes into the system prompt in one `<context-notes>` block (up to 20,000 characters a note) as it was when loaded; more links add more notes. It is kept in the session log (`context_note`), so a resumed chat has it again; a new chat does not. `/api/state` carries `chat_empty`. Nothing personal is in the code: the context lives in the user's notes. Tests: `tests/test_web_r460.py`.
- **R461 — A chat has a link (2026-11-24).** The web page keeps the open chat in the address bar as `/?chat=<session id>` (updated whenever the chat changes: a tap in the chat list, New chat, a resume from another device), so the address can be bookmarked or sent to someone who also has access to the page. Opening such a link resumes that chat (`/api/resume`) instead of "New chat on open" or continuing the last chat; a chat that cannot be opened (unknown, or with no finished turn) is said in the chat, and while a turn runs the page says to open the link again later. It is not a share link: it needs the page's token. The read-only snapshot to send to anyone is still Share (R418). Tests: `tests/test_web_r461.py`.
- **R462 — Memory and several notes in one link (2026-11-24).** (a) A bundled extension adds the `remember {fact}` tool: it appends `- <fact>` (one line, up to 300 characters, a repeat skipped) to the note titled "Memory", creating it (Personal) when there is none. No approval: it only adds a line to the user's own note, which the user reads, edits and deletes like any note. A link that loads the Memory note gives each new chat what earlier chats saved. (b) A page link can name several notes, `/?note=<id>,<id>,…` (at most 5): the page asks New chat / Add to this chat once, and loads them in order; only the first may start the new chat. A missing note is said and skipped. Tests: `tests/test_web_r462.py`.
- **R463 — `remember` hardening (2026-11-24, review).** A fact over 300 characters is refused with its length, not cut; a fact that `secrets.scan` flags is refused; the save passes the note's `updated` as `base` and rereads and retries once on a conflict, so a simultaneous edit in Notes is never overwritten; with several notes titled Memory the oldest is always the one used. Tests: `tests/test_web_r463.py`.
- **R464 — Bash mode's carried state cannot be planted by another local user (audit, 2026-11-24).** The file the EXIT trap writes the final directory and environment to (R452) is `state.json` inside a fresh `mkdtemp` directory (mode 0700, removed after the command). It used to be a `mkstemp` name in the shared temp dir, unlinked so the trap could recreate it. The name is in the `bash -c` script, which any local user can read in `ps`. Another user could create a file at that path first: `fs.protected_regular` then stops the trap from writing over it, and the next command ran with the directory and environment that file named (`LD_PRELOAD`, `PATH`). Tests: `tests/test_audit_r464.py`.
- **R467 — `remember` really writes to the oldest Memory note (audit, 2026-11-24).** R463 picks the note titled Memory with the smallest `created`, but `notes.listing()` did not return `created`, so every note scored 0 and the random note id chose: with two Memory notes the facts were split between them at random (and `test_r463_oldest_memory_note_wins` passed about half the time). The listing now carries each note's `created` (0 for a note from before that field, which is older than any that has it). Tests: `tests/test_audit_r464.py::test_r467_oldest_memory_note_wins_whatever_the_ids`, `tests/test_web_r463.py`.
- **R468 — The note editor's toolbar holds only buttons (owner, 2026-11-24: "Link copied: open it to start Aurora with this note as context" stood one word per line beside the buttons, "very bad place").** The editor pane is about 350 px wide and its buttons fill it, so any status text there was squeezed to one word a line and made the toolbar tall. The save status (Editing…, Saved, save errors) now follows the word count in the footer (`16 words · 97 characters · Saved`), where a long error wraps across the full width. A one-off confirmation ("Link copied", "Copied", "Moved to Work") is a small dark toast just under the toolbar, gone after 2.5 s. The link button's confirmation is just "Link copied" (its tooltip says what the link does); without clipboard access it copies the link the older way, and only if that fails too offers it in a box. Checked at 992 and 390 px with Playwright. Tests: `tests/test_web_r468.py`.

- **R470 — "Delete all files" answers an oversized request with 413 (audit, 2026-11-24).** `/api/files/delete_all` read its body without catching `TooLarge` (every other POST did, R392), so a body over 1 MB escaped as an uncaught error: the connection dropped with no answer instead of a 413. A `kind` that is not a string is a 400. Tests: `tests/test_audit_r470.py`.

- **R471 — The offline cache keeps the 20 most recently read transcripts, and never a deleted chat (audit, 2026-11-24).** The service worker (R396) stored every transcript the page ever opened (up to 300k characters each) and never removed any: a deleted chat stayed readable offline on the phone, and storage grew with every chat read. Now a transcript read again is stored as the newest and the oldest go past 20; a 404 from the server removes the copy; a chat deleted on the page (`/api/chat/delete`) has its copy and the cached chat list dropped once the server agrees, and deleting a folder drops every cached transcript. Tests: `tests/test_audit_r470.py` runs the real `sw.js` in Node (`tests/js/sw_harness.js`) with a fake Cache API.

- **R472 — A NUL in chat text cannot break the HTML export or a share link (audit, 2026-11-24).** The export's inline Markdown (R421) sets code spans and links aside behind `\x00<n>\x00` markers; text that itself held such a marker raised `IndexError` (the export, and the `/shared/` page of that chat, failed) or pulled in a different piece. A NUL in the text is now shown as U+FFFD, as an HTML parser would. Tests: `tests/test_audit_r470.py` (`test_r472_*`).

- **R482 — `aurora-rs -t`: the Rust core's headless mode, checked against Python (owner, 2026-11-24).** The first
  runnable Rust Aurora is a one-shot command with Python's `aurora -t` contract (R311, R337): it loads the same
  `config.yaml`, picks the model as Python does (`--model`, else the last one used, else the first with a key), runs
  one task to the end and writes a session Python can open. stdout carries only the reply, or with `--json` one
  object (`ok`, `exit_code`, `reply`, `reasons`, `model`, `session`, `tool_calls`, `context_tokens`, `cost_usd`);
  progress goes to stderr; exit 0 is a clean finish, 1 an incomplete one (a denied call, a provider error, the step
  cap), 2 a usage error. Gated calls are denied unless `--yes`; `--ask` asks on the terminal instead (once, no, stop,
  comment, yes to all). Ctrl+C cancels the turn. The binary is `aurora-rs`, so it never shadows `aurora`. Tests:
  `tests/test_rust_compat_r482.py` runs the same scripted model through `python -m aurora -t` and `aurora-rs -t` and
  compares the session records and the requests the model received; it is skipped when cargo is not installed.

- **R493 — The Rust core finds, lists, loads and runs skills as Python does (R11, R247, R475) (owner, 2026-11-26).**
  `aurora-tools::skills` ports `aurora/skills.py`. Executable files and `.py` files in `<config dir>/skills/` and
  `AURORA_HOME/skills/` are skills, and earlier directories shadow later ones. The blurb is the first line's trailing
  comment. `SKILL.md` doc skills are found recursively under `doc_skill_roots:`, following symlinked directories, with
  the lenient `key: value` fallback for frontmatter that is not valid YAML. `load_doc` gives the same `# Skill:` header
  and body. `run` splits arguments like `shlex`, is never boxed (the user typed it), and gives the same timeout, exit and
  error strings. One deliberate difference: a non-executable `.py` skill runs with `python3` instead of Python Aurora's
  own interpreter. The `/name` command comes with the front ends. Test: `tests/test_rust_skills_r493.py` (13
  operations, Python as the oracle).

## R582 - Version 2.0

Asked for 2026-11-27: the release that moves the agent loop to Rust is 2.0.0, not 1.3.119. `VERSION_BASELINE.json`
`major_minor` becomes `2.0` and `baseline_commit_count` becomes 733, this commit's count, so the live version is
`2.0.<commits since 733>` and this commit is 2.0.0. The CHANGELOG entry and its scheme line say 2.0. R441's `1.3.<n>`
scheme ends here.

Tests: `tests/test_version_scheme.py`.
