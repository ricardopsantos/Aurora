# Aurora — Requirements: Performance & robustness

Bounded work, background jobs, frame cost, dead endpoints, review passes and audit fix batches.
Part of the index in [`../REQUIREMENTS.md`](../REQUIREMENTS.md); numbers are shared across all files.

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
   endpoint dead (off-LAN, or a VPN down; the shipped `local` provider
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


99. **R346 — the product contains nothing personal.** Aurora is a standalone
    app for anyone. No tracked file may name its author's machines, networks,
    hostnames, addresses, services or accounts; examples use `example.com`-style
    hosts and documentation addresses. The author's own `config.yaml`,
    `.context/` (dev notes) and publishing script are not in the repo (ignored /
    kept outside it); `install.sh` creates `config.yaml` from
    `config.yaml.example`. `.ts.net` stays only as an entry in the private-host
    suffix list (a hostname ending in it is never fetched without a prompt). The
    forbidden words live in the test, not here.

    **Test:** `tests/test_no_personal_references.py` greps every tracked file.


100. **R393 — Aurora is generic and machine-independent (owner, 2026-11-02).** Beyond R346 (nothing personal), nothing
    in the product assumes the machine it runs on: no default path, host, port, user or service outside the user's own
    config (`aurora --web --workspace` defaults to the current directory); deploy files are templates with `YOUR_*`
    placeholders (`deploy/aurora-web.service.example`), never one site's units; a non-loopback `--bind` prints a
    warning; `SECURITY.md` and `CONTRIBUTING.md` state the rules. The scrub test covers every tracked text file,
    including `.service`, `.js`, `.css`, `.cfg`, `.ini`.

    **Test:** `tests/test_no_personal_references.py`; `tests/test_web_r392.py::test_r393_*`. The project is MIT-licensed (`LICENSE`, `pyproject.toml`; owner, 2026-11-02). `aurora --version` prints the version (asked for by SECURITY.md); `install.sh` was run in a clean container (2026-11-02) and installs and serves.


101. **R441 — Editing an existing file works where the OS has no `posix_fallocate` (macOS).** `_rewrite_in_place` reserves
    space up front only when `os.posix_fallocate` exists; without it the write still goes through the same inode, with the
    old bytes restored on failure. Before this, every edit of an existing file raised `AttributeError` on macOS.

    **Test:** `tests/test_paths.py::test_rewrite_in_place_works_without_posix_fallocate`.
