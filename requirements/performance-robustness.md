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


101. **R446 — Editing an existing file works where the OS has no `posix_fallocate` (macOS).** `_rewrite_in_place` reserves
    space up front only when `os.posix_fallocate` exists; without it the write still goes through the same inode, with the
    old bytes restored on failure. Before this, every edit of an existing file raised `AttributeError` on macOS.

    **Test:** `tests/test_paths.py::test_rewrite_in_place_works_without_posix_fallocate`.

102. **R465 — The web chat viewer stops reading a session at its cap (audit, 2026-11-24).** `GET /api/sessions/<id>`
    shows at most 300,000 characters, but it built the Markdown of the whole log first (`export_markdown`) and cut it
    afterwards: a 36 MB session peaked at 129 MB of memory per request. `export_markdown(sid, max_chars)` stops once the
    text passes `max_chars` (one record over at most); the web passes `MAX_TRANSCRIPT`, so the same request peaks at
    about 2 MB. The text shown and the "transcript cut" line are unchanged. The terminal's `/export` still writes it all.

    **Test:** `tests/test_audit_r464.py` (`test_r465_*`).

- **R478 — Aurora 2.0 starts: a Rust workspace beside the Python app, with written contracts and a migration
  baseline (owner, 2026-11-24, card "Create Aurora 2.0").** The repo root has a Cargo workspace (`Cargo.toml`,
  `crates/*`) that the Python app neither needs nor touches: `pip install` and every Python test behave as before.
  `aurora-core` holds the contracts the rest is built on, with no I/O of its own: `Provider` (one streamed model turn,
  cancellable), `Frontend` (an event stream of serialisable `AgentEvent`s plus `approve` / `ask_continue`, so a
  terminal, a web page or a remote client are the same to the engine), `Tool` / `ToolBox`, `ApprovalPolicy` (the R133c
  decision words), `SessionLog`, the `aurora-ext/1` wire types (R477) and the agent loop. The loop keeps Python's
  rules: every tool call gets exactly one result (denied, skipped and stopped calls included), reads run in parallel,
  a malformed call is retried once and then the tools are dropped, a provider error is a notice and never a crash, and
  the iteration cap asks before going on. `documents/migration/` holds the baseline (what Python is, measured), the
  plan, the target architecture, the data formats both must share, the test strategy, and `03-FEATURE-PARITY.md`, a
  table of every requirement R1–Rmax with its Rust status, built by `build_parity.py` from `parity-status.yaml`.
  Tests: `cargo test -p aurora-core`; `tests/test_migration_parity_r478.py` (the table is current and lists every
  number from R1 to the highest).

- **R509 — The web server says when it runs older code than the disk has (review F1) (owner, 2026-11-27).**
  A pull or an edit without a restart left the live page on day-old code, and because some modules are imported on
  first use, a process could mix old and new files. `/health` now has `code`: `stale` (up to 20 source files changed
  since the process started) with `stale_count`, `started`, and `core` / `core_build` (`release`, `debug`,
  `installed` or `none`: the `aurora-rs` a turn would start). `deploy/restart-web.sh` prints the core build and warns
  when it is not a release build. Tests: `tests/test_review_r509_r512.py`.
- **R510 — Bounded connections and memory on the web server (review F2-F4) (owner, 2026-11-27).**
  At most `MAX_CONNECTIONS` (64) connections are served at once; one more gets an immediate `503` with `Retry-After`
  and no thread. The socket timeout is 20 s (above the 15 s stream heartbeat), so an idle keep-alive connection frees
  its slot soon. At most `BIG_BODIES` (2) bodies over 1 MB are read at once. Audio and video uploads are copied to the
  input folder in 1 MB chunks and never held whole (a short upload leaves no file). Voice input refuses a recording
  whose 16 kHz WAV would pass `WAV_MAX` (40 MB, about 20 minutes), and ffmpeg itself stops there. `/api/files/file`
  streams the file in chunks and honours one `Range` (206, 416). Messages waiting in the queue may hold at most
  48 MB of pictures together. Tests: `tests/test_review_r509_r512.py`.
- **R513–R535 — 25 bugs and performance issues found after the review (owner, 2026-11-27).**
  Tests: `tests/test_review_r513_r535.py` (one test per item).
  - R513: the notes, folders and shares stores are written through `write_text_atomic` (0600 from the first byte, fsync).
  - R514: a damaged share store is moved aside (`.corrupt-<time>`) and named on `/health`, not overwritten.
  - R515: a shared page is rendered once per snapshot (16 kept).
  - R516: static files, the script hash and the CSP are cached until the file changes.
  - R517: the folders view is parsed again only when its file changes; callers get a copy.
  - R518: JSON bodies refuse NaN and Infinity; a NaN budget no longer switches the cap off.
  - R519: nested approval arguments are clipped (short strings kept whole); a note title cannot close its tag.
  - R520: a picture over `MAX_PIXELS` (50 M) is refused before it is decoded.
  - R521: thumbnails are cached per file version (256) and JPEGs are drafted down.
  - R522: a text preview reads only what it shows; a document over the limit is refused before it is read.
  - R523: spreadsheet and slide extraction stop once the text limit is passed.
  - R524: a note preview, once made, is kept in the index.
  - R525: the Rust `read_file` checks the final newline with a seek, not a second whole read.
  - R526: a rebuilt `aurora-rs` is picked up at the next turn.
  - R527: wrong tokens from a loopback proxy also count per peer (10× the limit), so rotating `X-Forwarded-For` no longer gives unlimited guesses.
  - R528: an export is capped at 20 MB and one step at 200 kB, with a note saying what was left out.
  - R529: `/health` checks the notes index without building the listing.
  - R530: `spend.json` and `state.yaml` are changed under a file lock; a malformed ledger reads as empty.
  - R531: "always allow/deny" rules are added under a file lock shared by the Python and Rust loops.
  - R532: the Rust client never sends a local or LAN model through an environment proxy.
  - R533: voice input reaches the speech server directly, never through an environment proxy.
  - R534: a streamed event is serialised once for every open page.
  - R535: `/health` has `HEALTH_SPARE` (4) connection slots of its own, so a monitor sees the truth when the server is full.
- **R536–R556 — 21 more bugs, with tests for modules that had few (owner, 2026-11-27).**
  Tests: `tests/test_review_r536_r555.py` (also covers htmltext, diagnose, tokens, mdrender, modelcfg, trybox,
  gitcommit, headless, bashmode, exthost and the key store beyond the fixes).
  - R536: two `/try` sandboxes made in the same second get two folders.
  - R537: an untracked symlink (to a folder too) is copied into the sandbox as the link.
  - R538: a `<` that does not open a tag (`a < b`) stays text.
  - R539: `<scripts>` or `<stylesheet>` is not taken for `<script>`/`<style>`.
  - R540: 401/403/404/429 are matched as whole numbers (a port 4013 is not an auth failure).
  - R541: the `/commit` read helpers return '' when git is missing or times out.
  - R542: `**` inside a code span stays literal in the terminal.
  - R543: the single-byte C1 OSC/DCS/SOS/PM/APC introducers are stripped like their ESC forms.
  - R544: token counts of a million and more read `1.2M`, never `1000k`.
  - R545: a headless run started off the main thread works (no SIGINT handler of its own).
  - R546: headless progress lines show escapes as text.
  - R547: the extension-host client writes the host's stderr to a file; a full pipe hung it.
  - R548: "copy" in bash mode also drops OSC hyperlinks.
  - R549: `cd` into a folder without search permission is refused.
  - R550: a quoted or escaped `cd` target is left to bash.
  - R551: a missing `keys.salt` next to an existing key store is an error, never a new salt.
  - R552: the key-store key is derived once per passphrase and salt.
  - R553: `/context` turn labels show escapes as text.
  - R554: a share expiry of infinity or 1e300 days is capped (it raised OverflowError).
  - R555: a hand-edited `upto`/`expires` in a share makes a dead link, not a 500.
  - R556: invalid YAML in `config.yaml` is a model-edit error, not a crash.
- **R557–R581 — 25 bugs and slow paths in the Rust core and the web page's server (owner, 2026-11-27).**
  Tests: `tests/test_review_r557_r576.py` and the crates' own tests (`cargo test --workspace`).
  - R557: the SSE line splitter scans only the new bytes and drains once per chunk (it rescanned and shifted per line).
  - R558: an IPv4-mapped IPv6 address (`::ffff:192.168.1.5`) and a trailing-dot name (`localhost.`) count as LAN.
  - R559: the Rust client never re-sends a request after a read timeout (Python never did).
  - R560: tool-call deltas without an `index` are told apart by their `id` (two calls were glued into one).
  - R561: `text::tail(s, 0)` is "" (it panicked).
  - R562: a fold's summary request honours the turn's cancel; a cancelled fold folds nothing, an interrupted one is not kept as the summary.
  - R563: the tokens/s clock starts at the first streamed thought, not the first text.
  - R564: the rate-limit hint needs 429 as a whole number in the Rust loop too (R540).
  - R565: `web_fetch` treats `localhost.` / `nas.local.` like the names without the dot (no approval skipped).
  - R566: a squeezed command output keeps exactly `limit` characters (an odd limit lost one; limit 1 kept all of it).
  - R567: a background job is done when its shell exits; a child still holding the pipes is kept draining and can still be cancelled.
  - R568: bridged provider chunks that arrive with the answer keep their kind (a late thought showed as reply text).
  - R569: a cancelled bridged request or question leaves no waiting slot.
  - R570: a session append looks for newer parts from the last one seen, not from the first part every time.
  - R571: counting a session's folds prefilters lines on `"event": "compact"` instead of parsing the whole log.
  - R572: app.js's `?v=` hash is tied to the bytes it was taken of (an `id()` key could serve a stale hash).
  - R573: the web replay buffer trims in batches to 7/8 of its caps (it shifted 20,000 events per chunk at the cap).
  - R574: a hand-edited notes index (non-number `updated`, non-text `title`, bool `pos`) never breaks the list or a save.
  - R575: Python's background-job drain follows R567 (the same never-done bug).
  - R576: a POST body that is not a JSON object is a 400; it used to run the action with every default.
  - R578: the terminal starts with no model when none was picked on this machine ("no model selected — /model to pick one"), never on the config's first entry; `-t` and `--web` keep their defaults.
  - R579: a dead backend's advice fits the machine: no "start Ollama" where Ollama is not installed, and the configured models that answer now come first.
  - R580: at start, the configured models that cannot work on this machine are named in one line (with why), and the picker refuses them instead of switching to a dead model.
  - R581: the personal-string audit (R346) scans the Rust sources too; a Rust test named a real private host.
  - R577: note `created` times are strictly increasing (a same-millisecond tie let a random id pick the Memory note).
