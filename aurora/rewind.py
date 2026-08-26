"""Checkpoints + /rewind (R47): damage-undo for the approval gate.

A shadow git repository (GIT_DIR under AURORA_HOME/checkpoints/<cwd-hash>,
work-tree = the cwd) snapshots the working tree just before every approved
mutation (write_file / edit_file / run_command). The project's own .git is
untouched — git refuses to track paths under a .git directory, and the
project's .gitignore files are honoured because they live in the work-tree.

/rewind lists the snapshots (newest first, labelled with the causing prompt)
and restores one: reset --hard + clean -fd against the shadow repo, after
first checkpointing the current state so a rewind is itself rewindable.

Checkpointing must never break a turn: every public function swallows its
own failures (no git binary, unreadable tree, …) and returns None/[]/error
text instead of raising.
"""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .paths import aurora_home

# belt-and-braces on top of the project's .gitignore — a project without one
# must not drag its venv or node_modules into every snapshot
EXCLUDES = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".build/",
            "build/", "dist/", ".mypy_cache/", ".pytest_cache/", "*.pyc",
            ".DS_Store"]

MAX_LABEL = 72

# R193: the per-file snapshot holds the target's WHOLE content in the marker
# JSON, and `snapshot_before_write` sits directly on the approval path — so an
# unbounded read here is a memory spike triggered by whatever file the model
# decided to edit. Past this, no snapshot is taken (and any older one is
# invalidated); `/undo` then falls back to the tree diff exactly as it already
# does for a file outside the checkpointed tree.
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024

# R151: how many snapshots a project's shadow repo keeps. One commit per
# APPROVED MUTATION, forever, was unbounded in two ways at once: the history
# chain itself, and — worse — every `/rewind` left a permanent `undo-<hash>`
# tag PINNING an orphaned commit, so even a manual `git gc` could not reclaim
# it. Growth tracks changed content (git dedups blobs), not tree size, but a
# daily driver accumulates thousands of commits per project with no ceiling
# and, before this, no way to prune from inside Aurora or out.
#
# 200 is comfortably more than `entries()` ever shows (20) while still being a
# real bound. Undo tags get their own, much smaller budget: an undo point more
# than a few rewinds ago is not something anyone reaches back for, and each
# one costs a pinned commit.
RETENTION = 200
UNDO_TAG_RETENTION = 5

# Pruning runs a `git gc`, so it must not happen on every approval. A
# counter fires it roughly once or twice a session, and always on a background
# thread — `checkpoint()` sits directly on the approval path, where R47's
# synchronous `git add -A` is already the acknowledged cost.
_PRUNE_EVERY = 50

# R191: both of these are keyed PER SHADOW REPO, and both used to be a single
# process-wide value.
#
# The counter being global meant the "every Nth checkpoint" cadence was shared
# across every project Aurora had ever touched in one process: 49 checkpoints
# in project A made project B's very first checkpoint trigger a gc of B.
#
# The lock is the load-bearing half. `prune()` runs `reflog expire` and
# `gc --prune=now` on a DAEMON THREAD while `checkpoint()` may be running
# `git add -A` + `git commit` on the same repo — and both swallow failures by
# design ("a failed prune must leave checkpointing working"), so the collision
# was silent. Measured: 1 in 6 runs of 30 back-to-back checkpoints lost one,
# and one run left HEAD unreadable, reporting 0 commits. A lost checkpoint is
# exactly the failure R47/R151 exist to prevent — the mutation is approved and
# applied, but `/rewind` has nothing to restore.
_locks_guard = threading.Lock()
_repo_locks: dict[str, threading.Lock] = {}
_since_prune: dict[str, int] = {}


def _repo_lock(wt: Path) -> threading.Lock:
    """The lock serialising checkpoint/prune for one shadow repo."""
    key = str(_gitdir(wt))
    with _locks_guard:
        return _repo_locks.setdefault(key, threading.Lock())


def _gitdir(cwd: Path) -> Path:
    return (aurora_home() / "checkpoints"
            / hashlib.sha1(str(cwd).encode()).hexdigest()[:16])


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--git-dir", str(_gitdir(cwd)), "--work-tree", str(cwd),
         "-c", "user.name=aurora", "-c", "user.email=aurora@localhost",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, timeout=60, check=check)


def _ensure(cwd: Path) -> None:
    gd = _gitdir(cwd)
    if not (gd / "HEAD").exists():
        gd.mkdir(parents=True, exist_ok=True)
        _git(cwd, "init", "--quiet")
        (gd / "info").mkdir(exist_ok=True)
        (gd / "info" / "exclude").write_text("\n".join(EXCLUDES) + "\n",
                                            encoding="utf-8")


def _last_mutation_path(wt: Path) -> Path:
    return _gitdir(wt) / "last-mutation.json"


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write `data` to `target` without ever leaving it truncated.

    R193: `Path.write_text` opens with "w" — it TRUNCATES first and encodes
    after, so a failure part-way through destroys the original and leaves
    nothing in its place. That was not theoretical: restoring a file with any
    non-ASCII byte under `LANG=C` raised `UnicodeEncodeError` from inside the
    write, `undo()` reported "undo failed", and the user's file was left
    EMPTY — the one outcome an undo must never produce. Write a sibling temp
    file and `os.replace` it in: the target is either the old content or the
    new one, never a partial write."""
    fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                               prefix=f".{target.name}.", suffix=".aurora-tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # `os.replace` takes the TEMP file's mode, and mkstemp creates 0600 —
        # without this a restored file comes back private to the user and
        # stripped of its executable bit.
        try:
            os.chmod(tmp, target.stat().st_mode & 0o7777)
        except OSError:
            pass   # target gone or unstatable — temp's own mode is the best left
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _snapshot_bytes(m: dict) -> bytes:
    """The snapshotted content as the exact bytes to write back.

    `content_b64` is the R193 format — byte-exact, so a file that is not
    valid UTF-8 (a latin-1 source file, a binary asset) round-trips
    unchanged. `content` is the legacy text field: markers written before
    R193 are still sitting in users' checkpoint dirs, and they only ever hold
    what a lossy `errors="replace"` decode produced, so re-encoding as UTF-8
    is the closest recovery available — not byte-exact, but no worse than
    what the old code would itself have written back."""
    b64 = m.get("content_b64")
    if b64 is not None:
        return base64.b64decode(b64)
    return (m.get("content") or "").encode("utf-8")


def snapshot_before_write(path: str, cwd: str = ".") -> None:
    """R181: capture `path`'s content right before a write_file/edit_file/
    apply_patch mutation touches it — regardless of whether `path` is
    INSIDE the checkpointed tree at all.

    The whole-tree `checkpoint()` above is blind to anything outside `cwd`
    (`covers()`'s documented gap: `git add -A` only ever sees paths inside
    the work-tree) — which is exactly the shape of two real incidents: a
    file written to the Desktop had no representation in the shadow repo
    at all, so `/undo` had nothing to act on for it. This is a plain
    per-file snapshot with no git involved, so it works no matter where
    the file lives. Overwrites any previous snapshot — like the tree
    checkpoint, this tracks only the single last mutation, not a history.

    Never raises — same fail-open contract as the rest of this module; a
    failed snapshot just means `/undo` has nothing to offer for that call,
    same as if this function didn't exist.

    R193: snapshots BYTES, not decoded text. The old `read_text(
    errors="replace")` silently replaced every byte it couldn't decode with
    U+FFFD, and `undo()` then wrote that back as the file's "original"
    content — so undoing an edit to any file that wasn't valid UTF-8 (or,
    under a non-UTF-8 locale, any file with non-ASCII at all) reported
    success while corrupting it. Bytes have no encoding to get wrong."""
    marker = None
    try:
        target = Path(path).expanduser().resolve()
        marker = _last_mutation_path(Path(cwd).resolve())
        marker.parent.mkdir(parents=True, exist_ok=True)
        existed = target.is_file()
        # R240: size-check BEFORE reading. This was `data = read_bytes()`
        # followed by `len(data) > MAX_SNAPSHOT_BYTES`, which caps what gets
        # STORED while doing nothing about the read itself — so the memory
        # spike `MAX_SNAPSHOT_BYTES` was introduced to prevent (see its
        # comment: "an unbounded read here is a memory spike triggered by
        # whatever file the model decided to edit") happened in full, and
        # only then was the result thrown away. Measured: a 63MB file peaked
        # at 63MB of allocation before being rejected by a 21MB cap. `stat`
        # answers the question without reading a byte.
        if existed and target.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("file too large to snapshot")
        data = target.read_bytes() if existed else b""
        if len(data) > MAX_SNAPSHOT_BYTES:
            # the file grew between the stat and the read
            raise ValueError("file too large to snapshot")
        # R240: atomic, for the same reason `_atomic_write_bytes` exists just
        # above — `write_text` truncates first. A crash mid-write leaves a
        # half-written marker, and while invalid JSON does fail safe (it
        # reads back as "no last mutation"), the mutation it was recording
        # goes ahead regardless, so `/undo` silently has nothing to offer for
        # a write that did happen.
        _atomic_write_bytes(marker, json.dumps(
            {"path": str(target), "existed": existed,
             "content_b64": base64.b64encode(data).decode("ascii") if existed
                            else None,
             "at": time.time()}).encode("utf-8"))
    except Exception:
        # R193: a stale marker from an EARLIER mutation must not survive a
        # failed/skipped snapshot — `undo_preview` would report it as "the
        # last mutation" and `/undo` would revert a different file than the
        # one just written. Same invalidation `clear_last_mutation` performs
        # for the tools that have no single target path.
        if marker is not None:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass


def clear_last_mutation(cwd: str = ".") -> None:
    """The R181 file-snapshot is only valid as "the last mutation" when
    NOTHING else has mutated since — a `run_command`/`wait_until` call
    (no single unambiguous target file) invalidates it, same as it would
    invalidate any other "last thing I did" tracking. Called instead of
    `snapshot_before_write` for those tools; never raises."""
    try:
        _last_mutation_path(Path(cwd).resolve()).unlink(missing_ok=True)
    except Exception:
        pass


def _read_last_mutation(wt: Path) -> dict | None:
    try:
        return json.loads(_last_mutation_path(wt).read_text(encoding="utf-8"))
    except Exception:
        return None


def checkpoint(label: str, cwd: str = ".") -> str | None:
    """Snapshot the working tree. Returns the short hash, or None when the
    tree is unchanged since the last snapshot or git is unavailable."""
    try:
        wt = Path(cwd).resolve()
        _ensure(wt)
        # R191: held across add/commit/rev-parse so a background prune's
        # `gc --prune=now` cannot land in the middle of them.
        with _repo_lock(wt):
            _git(wt, "add", "-A")
            msg = " ".join(label.split())[:MAX_LABEL] or "checkpoint"
            r = _git(wt, "commit", "--quiet", "-m", msg, check=False)
            if r.returncode:  # "nothing to commit" — tree unchanged
                return None
            short = _git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
        # Scheduled OUTSIDE the lock: the prune thread takes the same lock, so
        # dispatching while still holding it would just make it wait on us.
        _prune_soon(str(wt))       # R151, background + rate-limited
        return short
    except Exception:
        return None


def prune(cwd: str = ".", keep: int = RETENTION) -> int:
    """Bound one project's shadow repo to `keep` snapshots. Returns how many
    commits were cut off (0 if nothing to do). Never raises — a failed prune
    must leave checkpointing working.

    Three steps, in this order:

    1. Drop all but the newest `UNDO_TAG_RETENTION` `undo-*` tags. These are
       the reason growth was previously irreducible: each one makes an
       orphaned commit reachable, so no `gc` anywhere could collect it.
    2. Cut the ancestry chain by marking the `keep`-th commit as a shallow
       root (the same mechanism `git fetch --depth` uses). Deleting old
       commits is not possible while they are ancestors of HEAD, and
       rewriting history would rewrite every hash — including the ones
       `/rewind`'s own output has already shown the user.
    3. Expire the reflog and `gc --prune=now`, which is what actually
       reclaims the disk.

    A snapshot older than the cut is genuinely gone, which is the point: this
    trades unbounded recovery depth for a bounded footprint. `entries()` only
    ever showed the newest 20, so nothing visible changes."""
    try:
        wt = Path(cwd).resolve()
        gd = _gitdir(wt)
        if not (gd / "HEAD").exists():
            return 0

        # R191: the whole body runs under the repo lock. A concurrent
        # checkpoint would otherwise be committing into the repo this is
        # gc-ing, and both sides swallow their errors, so the loss was silent.
        with _repo_lock(wt):
            tags = [t for t in _git(wt, "tag", "--list", "undo-*",
                                    "--sort=-creatordate",
                                    check=False).stdout.split() if t]
            for stale in tags[UNDO_TAG_RETENTION:]:
                _git(wt, "tag", "-d", stale, check=False)

            revs = _git(wt, "rev-list", "HEAD", check=False).stdout.split()
            cut = 0
            if len(revs) > keep:
                # revs[keep - 1] keeps exactly `keep` commits reachable; marking
                # it shallow makes git treat it as a root and stop walking past it
                (gd / "shallow").write_text(revs[keep - 1] + "\n",
                                            encoding="utf-8")
                cut = len(revs) - keep

            _git(wt, "reflog", "expire", "--expire=now", "--all", check=False)
            _git(wt, "gc", "--prune=now", "--quiet", check=False)
        return cut
    except Exception:
        return 0


def _prune_soon(cwd: str) -> None:
    """Fire `prune` on a daemon thread if enough checkpoints have accumulated
    FOR THIS REPO. Off the approval path deliberately — see `_PRUNE_EVERY`."""
    try:
        key = str(_gitdir(Path(cwd).resolve()))
    except Exception:
        return
    with _locks_guard:
        n = _since_prune.get(key, 0) + 1
        if n < _PRUNE_EVERY:
            _since_prune[key] = n
            return
        _since_prune[key] = 0
    try:
        threading.Thread(target=prune, args=(cwd,), daemon=True).start()
    except Exception:
        pass


def _excluded(rel: str) -> bool:
    """Does `rel` (checkpoint-root-relative, forward-slash separated) match
    one of EXCLUDES — the same patterns `_ensure()` writes into the shadow
    repo's own `info/exclude`? Mirrors plain gitignore matching for the
    simple pattern shapes EXCLUDES actually uses: `name/` matches a
    directory component anywhere in the path, `*.ext` matches by suffix,
    anything else matches an exact path component anywhere (covers
    `.DS_Store` as a bare filename at any depth)."""
    parts = rel.split("/")
    for pat in EXCLUDES:
        if pat.endswith("/"):
            if pat[:-1] in parts:
                return True
        elif pat.startswith("*"):
            if rel.endswith(pat[1:]):
                return True
        elif pat in parts:
            return True
    return False


def covers(path: str, cwd: str = ".") -> bool:
    """R130: is `path` inside the tree a checkpoint actually snapshots?

    A checkpoint's work-tree is the CWD (see `checkpoint`), so a mutation
    aimed anywhere else — `~/Desktop/notes.md`, `/etc/hosts`, a sibling
    project — is approved, executed, and then NOT recoverable by /rewind,
    even though R47 promises a snapshot before "every approved mutation".
    That gap is easy to hit precisely because R30's system prompt tells the
    model to prefer absolute and `~` paths.

    Best-effort like everything else here: an unresolvable path counts as
    NOT covered, so the honest warning is the failure mode, never a silent
    false assurance.

    Review pass: in-tree is necessary but not sufficient. `checkpoint()`
    runs `git add -A`, which skips anything EXCLUDES marks — a write to
    `dist/config.json` or `.venv/pyvenv.cfg` is just as unrecoverable as one
    outside the tree entirely, the exact false assurance this function
    exists to prevent. This does NOT also check the project's own arbitrary
    `.gitignore` patterns — that would need a `git check-ignore` subprocess
    call on every approval-prompt render, a much larger and riskier change
    to a safety-messaging path. EXCLUDES covers the common, predictable
    cases (build artifacts, caches, venvs); a project-specific ignore
    pattern outside that list is a known residual gap, not silently claimed
    to be handled."""
    try:
        target = Path(path).expanduser().resolve()
        root = Path(cwd).resolve()
    except Exception:
        return False
    if not (target == root or root in target.parents):
        return False
    if target == root:
        return True
    return not _excluded(target.relative_to(root).as_posix())


_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's canonical empty-tree hash


def head(cwd: str = ".") -> str | None:
    """Current shadow-repo HEAD (short hash), or None if no checkpoint exists
    yet for this project. `/diff`'s "before this turn" marker: `engine.send`
    calls this before the turn runs, so a later `/diff` can show exactly what
    THIS turn's approved mutations changed, not the whole project history."""
    try:
        wt = Path(cwd).resolve()
        if not (_gitdir(wt) / "HEAD").exists():
            return None
        r = _git(wt, "rev-parse", "--short", "HEAD", check=False)
        return r.stdout.strip() or None
    except Exception:
        return None


def diff_since(ref: str | None, cwd: str = ".") -> str:
    """What changed in the working tree since `ref` (a shadow-repo commit, as
    returned by `head()`/`checkpoint()`). `ref=None` means no checkpoint
    existed yet when the turn started — diff against git's empty tree, so a
    project's very first approved mutation still shows a real diff instead of
    silently returning nothing.

    Diffs against the WORKING TREE (`git diff <ref>`, not `<ref> HEAD`): in
    the common case they're identical, since `checkpoint()` commits before
    every mutation and nothing changes after the last one — but this also
    surfaces any stray uncommitted difference (a mutation outside the
    checkpointed tree per R130, or the excluded-paths gap `covers()` already
    documents) rather than silently omitting it.

    P-4 fix: `checkpoint()` runs BEFORE each mutation, not after, so a turn
    whose last (or only) mutation is a brand-new file leaves that file
    UNTRACKED relative to `ref` — plain `git diff <ref> --` never shows
    untracked files at all, so the turn that most wants a visible "new file"
    diff showed nothing. `git add -A` (same call `checkpoint()` itself makes,
    respecting the same `info/exclude`) stages it first, then diff against
    the INDEX so new files show as real additions; the next `checkpoint()`
    re-runs `git add -A` regardless, so leaving this staged has no lasting
    effect.

    Never raises — same contract as the rest of this module; a diff failure
    must not interrupt the command that asked for it."""
    try:
        wt = Path(cwd).resolve()
        if not (_gitdir(wt) / "HEAD").exists():
            return ""   # no checkpoints for this project at all — nothing to show
        base = ref if ref else _EMPTY_TREE
        if ref and _git(wt, "rev-parse", "--verify", f"{ref}^{{commit}}",
                        check=False).returncode:
            return f"[diff error: no such checkpoint: {ref}]"
        _git(wt, "add", "-A", check=False)
        return _git(wt, "diff", "--cached", base, "--", check=False).stdout
    except Exception as e:
        return f"[diff error: {e.__class__.__name__}: {e}]"


def entries(cwd: str = ".", limit: int = 20) -> list[dict]:
    """Newest-first snapshots: [{'id', 'age', 'label'}]."""
    try:
        wt = Path(cwd).resolve()
        if not (_gitdir(wt) / "HEAD").exists():
            return []
        # --all: a rewind moves HEAD back, but the pre-rewind snapshot (kept
        # via its undo-* tag) must stay listed
        r = _git(wt, "log", "--all", f"-{limit}",
                 "--format=%h%x00%ct%x00%s", check=False)
        rows = []
        for line in r.stdout.splitlines():
            h, ct, s = line.split("\x00", 2)
            rows.append({"id": h, "age": _age(int(ct)), "label": s})
        return rows
    except Exception:
        return []


def restore(ref: str, cwd: str = ".") -> str:
    """Restore the working tree to a snapshot (tracked files reset, files
    created since removed — gitignored/excluded files are left alone).
    Checkpoints the current state first so the rewind can be undone.
    Returns a human-readable result line."""
    try:
        wt = Path(cwd).resolve()
        if not (_gitdir(wt) / "HEAD").exists():
            return "no checkpoints for this directory"
        if _git(wt, "rev-parse", "--verify", f"{ref}^{{commit}}",
                check=False).returncode:
            return f"no such checkpoint: {ref}"
        # the undo point: a fresh snapshot, or — when the tree is unchanged
        # since the last one — that last snapshot itself (otherwise the
        # reset below would orphan every commit newer than `ref`)
        undo = (checkpoint(f"before /rewind to {ref}", cwd=str(wt))
                or _git(wt, "rev-parse", "--short", "HEAD").stdout.strip())
        _git(wt, "reset", "--hard", "--quiet", ref)
        _git(wt, "clean", "-fdq", check=False)
        # keep the pre-rewind state reachable even though HEAD moved back
        if undo:
            _git(wt, "tag", "-f", f"undo-{undo}", undo, check=False)
        return (f"restored {ref}"
                + (f" (undo with /rewind {undo})" if undo else ""))
    except Exception as e:
        return f"rewind failed: {e.__class__.__name__}: {e}"


def undo_preview(cwd: str = ".") -> tuple[str, list[str]]:
    """What `undo()` WOULD revert, without touching anything — split out so
    a caller can show the affected paths BEFORE asking "really undo this?".

    Only ever reports the UNCOMMITTED diff against HEAD. There is no
    "already sealed into HEAD, fall back to reverting HEAD~1..HEAD" case —
    an earlier version of this function had one, and it caused two real
    incidents in one session: `checkpoint()` runs BEFORE a mutation, never
    after, so the mutation that JUST ran — if it touched this checkpointed
    tree at all — is ALWAYS still sitting as an uncommitted diff against
    HEAD right now; nothing has run since to seal it, because sealing only
    happens via the NEXT approved mutation's own pre-checkpoint call. If the
    tree is clean against HEAD, that means the last mutation did NOT touch
    this tree — its target was outside it (`covers()`'s documented gap —
    e.g. an absolute path elsewhere on disk), or it was a genuine no-op —
    and there is nothing here representing it, in either case. `HEAD`'s own
    diff against `HEAD~1` is always some OLDER, unrelated, already-reviewed
    mutation in that situation, never "the last one" — offering to revert
    it as if it were is precisely the bug: a click meant to undo one file
    silently reverted a different, unrelated batch of real work instead,
    twice, because the tree happened to be clean against HEAD both times.

    Checks the R181 per-file snapshot FIRST — `snapshot_before_write()`
    covers write_file/edit_file/apply_patch regardless of location, so a
    file living outside this tree still has something to undo, unlike the
    tree-diff check below. `clear_last_mutation()` invalidates it the
    moment a differently-shaped mutation (run_command/wait_until — no
    single unambiguous target) runs afterward, so a stale snapshot is
    never mistaken for "the last mutation" once something else has run.

    Returns (kind, paths): kind is "file" (the R181 snapshot — one path),
    "uncommitted" (the tree-diff fallback, for run_command's own in-tree
    effects), "none" (nothing — whatever the last action was didn't leave
    a trace here), or "error" (paths[0] is the message)."""
    try:
        wt = Path(cwd).resolve()
        m = _read_last_mutation(wt)
        if m is not None:
            return ("file", [m["path"]])
        if not (_gitdir(wt) / "HEAD").exists():
            return ("none", [])
        _git(wt, "add", "-A", check=False)
        dirty = _git(wt, "diff", "--cached", "--name-status", "--no-renames",
                     "HEAD", check=False).stdout.strip()
        if not dirty:
            return ("none", [])
        paths = [line.split("\t", 1)[1] for line in dirty.splitlines() if line]
        return ("uncommitted", paths)
    except Exception as e:
        return ("error", [f"{e.__class__.__name__}: {e}"])


def undo_diff(cwd: str = ".") -> str:
    """A unified diff of exactly what `undo()` would revert, for showing
    alongside `undo_preview()`'s file list in a confirmation prompt —
    naming files got the incident fixed; SHOWING the actual change is what
    lets a user judge it at a glance instead of trusting the filename
    alone. Same before→now direction as `/diff`, so it reads the same way.
    Empty string when there's nothing to show (kind "none"/"error") or on
    any failure — a diff failure must not block the confirm it's decorating."""
    try:
        wt = Path(cwd).resolve()
        m = _read_last_mutation(wt)
        if m is not None:
            import difflib
            target = Path(m["path"])
            try:
                current = target.read_text(errors="replace") if target.is_file() else ""
            except Exception:
                current = ""
            # R193: decode for DISPLAY only — lossily on purpose. The stored
            # bytes are authoritative for the restore; this side just has to
            # render something a human can read in the confirm prompt.
            old = (_snapshot_bytes(m).decode("utf-8", errors="replace")
                   if m.get("existed") else "")
            return "".join(difflib.unified_diff(
                old.splitlines(keepends=True), current.splitlines(keepends=True),
                fromfile=f"{m['path']} (before)", tofile=f"{m['path']} (now)"))
        if not (_gitdir(wt) / "HEAD").exists():
            return ""
        _git(wt, "add", "-A", check=False)
        return _git(wt, "diff", "--cached", "HEAD", check=False).stdout
    except Exception:
        return ""


def undo(cwd: str = ".") -> str:
    """Revert just the LAST mutation, not the whole tree (`/rewind` is the
    coarse, whole-tree version of this) — the uncommitted diff against
    HEAD, and ONLY that. See `undo_preview()` for why there is deliberately
    no "reach back into already-sealed history" fallback: that fallback
    caused two real incidents (it can never actually represent "the last
    mutation" — see its docstring for the invariant that proves it).

    `git add -A` (to see untracked additions at all — plain `git diff`
    can't) then `git reset --hard HEAD` throws away exactly what's dirty:
    added paths vanish, modified/deleted ones snap back. Never raises —
    same contract as the rest of this module.

    Wanting to revert something OLDER/already-sealed is `/rewind` — list
    checkpoints and restore one (whole-tree, not scoped to a single
    mutation's files, but explicit about which snapshot you're picking)."""
    try:
        wt = Path(cwd).resolve()
        kind, paths = undo_preview(cwd=str(wt))
        if kind == "error":
            return f"undo failed: {paths[0]}"
        if kind == "none":
            if not (_gitdir(wt) / "HEAD").exists():
                return "no checkpoints for this directory"
            return ("nothing to undo — the last action left no change in this "
                    "tracked directory (its target may be outside the project, "
                    "or it was a no-op); /rewind lists older checkpoints if "
                    "you're after something further back")
        if kind == "file":
            m = _read_last_mutation(wt)
            _last_mutation_path(wt).unlink(missing_ok=True)   # consume it
            if m is None:
                return "nothing to undo"   # race: consumed between preview and here
            target = Path(m["path"])
            try:
                if m["existed"]:
                    # R193: byte-exact and atomic — see `_atomic_write_bytes`.
                    _atomic_write_bytes(target, _snapshot_bytes(m))
                else:
                    target.unlink(missing_ok=True)
            except Exception as e:
                return f"undo failed: {e.__class__.__name__}: {e}"
            return f"undone: reverted {m['path']}"
        _git(wt, "reset", "--hard", "--quiet", "HEAD")
        return (f"undone: reverted {len(paths)} uncommitted file(s) — "
                + ", ".join(paths[:5])
                + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""))
    except Exception as e:
        return f"undo failed: {e.__class__.__name__}: {e}"


def _age(ts: int) -> str:
    d = max(0, int(time.time()) - ts)
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"
