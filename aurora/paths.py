"""AURORA_HOME resolution — the per-machine data dir (allowlist, sessions,
key store). Chosen at install time; everything project-agnostic lives here so
the git repo stays machine-neutral."""

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
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent),
                               prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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


def _umask() -> int:
    """Read the process umask without leaving it changed — there is no
    read-only accessor before Python 3.13's `os.umask` semantics change."""
    current = os.umask(0o022)
    os.umask(current)
    return current


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
    return _ensure_dir(home, "AURORA_HOME")


def sessions_dir() -> Path:
    return _ensure_dir(aurora_home() / "sessions", "the sessions directory")
