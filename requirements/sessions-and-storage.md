# Aurora — Requirements: Sessions, checkpoints & storage

Session records, checkpoints and the shadow repo, atomic writes, compaction, history.
Part of the index in [`../REQUIREMENTS.md`](../REQUIREMENTS.md); numbers are shared across all files.

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

44. **R274 — nested git repos are never claimed as checkpointed, and never
   disable checkpoints.** `git add -A` records a repository nested below the
   checkpoint root (a sub-repo when Aurora runs from `~/projects` or `~`,
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

