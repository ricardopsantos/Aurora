"""AURORA_HOME resolution — the per-machine data dir (allowlist, sessions,
key store). Chosen at install time; everything project-agnostic lives here so
the git repo stays machine-neutral."""

import errno
import os
import tempfile
from pathlib import Path


class AuroraHomeError(Exception):
    """R217: AURORA_HOME (or a directory under it) cannot be created or used.

    Everything persistent goes through `aurora_home()` — sessions, the
    allowlist, the key store, checkpoints — so an unusable one is fatal, and
    it used to be fatal as a raw `PermissionError`/`FileExistsError`
    traceback from whichever caller happened to touch it first. The cause is
    always an environment mistake the user can fix (AURORA_HOME set to a
    file, or pointing somewhere unwritable), so it deserves a sentence
    naming the variable, not a stack trace through `pathlib`."""


def _ensure_dir(path: "Path", what: str) -> "Path":
    """R217: `mkdir(parents=True, exist_ok=True)` with a usable failure."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError as e:
        raise AuroraHomeError(
            f"{what} exists but is not a directory: {path}\n"
            "AURORA_HOME must name a directory (or something Aurora may "
            "create); it currently points at a file.") from e
    except OSError as e:
        raise AuroraHomeError(
            f"cannot create {what}: {path}\n{e.strerror}. Check AURORA_HOME "
            "and that its parent is writable.") from e
    return path


class file_lock:
    """R530: an exclusive lock on `<path>.lock` for a read-change-write of `path` shared by several processes
    (the terminal and the web page) and threads. Without fcntl (Windows) it is a lock within this process only."""
    import threading as _threading
    _local = _threading.RLock()          # reentrant: a lock taken inside another never deadlocks this thread

    def __init__(self, path: "Path | str"):
        self.path = Path(str(path) + ".lock")
        self.fh = None

    def __enter__(self):
        file_lock._local.acquire()
        try:
            import fcntl
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, "a")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            if self.fh is not None:
                self.fh.close()
            self.fh = None
        return self

    def __exit__(self, *exc):
        try:
            if self.fh is not None:
                self.fh.close()            # closing releases the flock
        finally:
            file_lock._local.release()
        return False


def _fsync_dir(directory: "Path") -> None:
    """R171/I6: fsyncing a temp file makes ITS contents durable, but the
    rename that makes it visible under the final name is a separate metadata
    write to the DIRECTORY entry — on a power loss before that directory write
    reaches disk, the rename can be lost even though the temp file's data was
    safely flushed, leaving the OLD contents as the surviving file (the new
    inode unlinked). Windows has no directory fd to fsync; best effort there,
    same fallback shape as the POSIX-only `flock` in session.py.

    R234: shared by both atomic writers. It used to live inline in
    `write_text_atomic` only, which left `write_bytes_atomic` — the writer the
    encrypted key store uses, i.e. the one file whose loss is unrecoverable —
    without the very durability step R171 was added to guarantee."""
    try:
        dirfd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except (OSError, AttributeError):
        pass


def write_bytes_atomic(path: "Path | str", data: bytes,
                       mode: int | None = None) -> None:
    """Byte counterpart to `write_text_atomic` below, with an optional
    permission mode applied BEFORE the file becomes visible.

    R201: the encrypted key store was written with a plain `write_bytes`
    followed by `chmod(0o600)` — two windows in one line. A crash between
    truncate and write left an undecryptable blob (every stored key gone,
    since nothing else holds them), and between write and chmod the file
    briefly carried the umask's permissions. Setting the mode on the temp
    file closes the second window the same way `os.replace` closes the
    first: the name only ever points at a complete, correctly-permissioned
    file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_text_atomic(path: "Path | str", text: str) -> None:
    """Replace a file's contents in one step, or not at all (R146a).

    Every YAML Aurora persists — config.yaml (the committed file holding
    every provider and model), state.yaml, allowlist.yaml, denylist.yaml —
    was written with a plain `write_text()` over the live file: truncate,
    then write. A crash, ^C, ENOSPC or a kill in that window leaves a
    truncated or empty file, and `load_config` then fails at the next start.
    Reachable in normal use: `persist_runtime_value` fires mid-turn from
    `/redact allowlist`.

    Writing a sibling temp file and `os.replace`-ing it is atomic on POSIX,
    and the `fsync` before the rename is what makes it survive a power loss
    rather than merely a crashed process. The temp file is a sibling, not in
    /tmp, because `os.replace` is only atomic within one filesystem."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def write_text_preserving(path: "Path | str", text: str) -> None:
    """R241: atomic write for a file that BELONGS TO THE USER — the model's
    `write_file`/`edit_file`/`apply_patch` targets, not Aurora's own state.

    Same crash-safety as `write_text_atomic`, plus the two things that
    matter when the file is someone's source tree rather than a config
    Aurora owns:

    * **Mode is preserved.** `mkstemp` creates 0600 and `os.replace` carries
      the temp file's mode across, so a plain atomic write turns an
      executable script into a private, non-executable file. The existing
      mode is copied onto the temp file before the rename.
    * **Symlinks are followed.** `os.replace` would swap the LINK for a
      regular file, silently orphaning the file the user actually meant to
      edit. Resolving first writes through the link, which is what
      `write_text` did.

    The forward path needed this because it did not have it: `write_file`,
    `edit_file` and `apply_patch` used `Path.write_text`, which truncates
    and then writes. A crash, ENOSPC or a kill in that window leaves the
    user's source file truncated or empty — the very outcome `rewind`'s
    `_atomic_write_bytes` exists to prevent when UNDOING a change, while
    making one was unprotected."""
    p = Path(path)
    if p.is_symlink():
        p = p.resolve()
    try:
        mode = p.stat().st_mode & 0o7777
    except OSError:
        mode = None          # new file — let the umask decide, as before
    if mode is not None and p.is_file():
        _rewrite_in_place(p, text)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent),
                               prefix=f".{p.name}.", suffix=".tmp")
    try:
        # R269: newline="" — write exactly the text given. Text mode's
        # default newline translation is what callers preserving a file's
        # own line endings (tools._read_for_edit) must not be undone by.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is None:
            os.chmod(tmp, 0o666 & ~_umask())
        else:
            os.chmod(tmp, mode)
        os.replace(tmp, p)
        _fsync_dir(p.parent)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _rewrite_in_place(p: Path, text: str) -> None:
    """R322: overwrite an EXISTING file through its own inode.

    `os.replace` gives the path a new inode, so a single file bind-mounted into
    a container (`./Caddyfile:/etc/caddy/Caddyfile`) keeps showing the old
    content, a hard link is split off, and owner/group/xattrs are lost. Writing
    through the open file keeps all of them. To stay safe against a failed
    write: the space is reserved first (ENOSPC raises before anything is
    touched) and the old bytes are put back if the write itself fails."""
    data = text.encode("utf-8")
    with open(p, "r+b") as f:
        old = f.read()
        try:
            try:
                # macOS has no posix_fallocate; skip the up-front reservation there.
                if hasattr(os, "posix_fallocate"):
                    os.posix_fallocate(f.fileno(), 0, len(data))
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    raise
            f.seek(0)
            f.truncate(0)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            f.seek(0)
            f.truncate(0)
            f.write(old)
            f.flush()
            raise


def _umask() -> int:
    """The process umask, read from /proc/self/status (R323) so no thread ever
    sees it briefly changed; computed once and cached. Falls back to the
    set-and-restore trick (no read-only accessor exists) only where /proc is
    missing, e.g. macOS — and then only on the first call, before threads."""
    global _UMASK
    if _UMASK is None:
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("Umask:"):
                    _UMASK = int(line.split()[1], 8)
                    break
        except (OSError, ValueError, IndexError):
            pass
        if _UMASK is None:
            current = os.umask(0o022)
            os.umask(current)
            _UMASK = current
    return _UMASK


_UMASK: "int | None" = None


_MARKER = Path.home() / ".aurora-path"


def aurora_home() -> Path:
    """Resolution order: $AURORA_HOME → ~/.aurora-path marker file → ~/.aurora."""
    env = os.environ.get("AURORA_HOME")
    if env:
        home = Path(env).expanduser()
    elif _MARKER.exists():
        home = Path(_MARKER.read_text(encoding="utf-8").strip()).expanduser()
    else:
        home = Path.home() / ".aurora"
    _ensure_dir(home, "AURORA_HOME")
    # R450 (S10): the home folder's own mode is what keeps other local users out of sessions, notes and keys
    try:
        mode = home.stat().st_mode & 0o7777
        if mode & 0o077:
            home.chmod(mode & ~0o077)       # only ever removes group/other access, never grants the owner any
    except OSError:
        pass
    return home


def sessions_dir() -> Path:
    return _ensure_dir(aurora_home() / "sessions", "the sessions directory")


def starts_with_path(line: str, cwd: str | None = None) -> bool:
    """R453: is a line that starts with `/` a message about a path rather
    than a command? True when its first word contains another `/`
    (`/etc/hosts has a bad entry`) or names something that exists
    (`/tmp is full`). Command and skill names never contain `/`."""
    if not line.startswith("/"):
        return False
    first = line.split(None, 1)[0] if line.strip() else ""
    if len(first) < 2:
        return False
    if "/" in first[1:]:
        return True
    try:
        return Path(first).exists()
    except OSError:
        return False
