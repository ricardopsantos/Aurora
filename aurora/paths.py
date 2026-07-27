"""AURORA_HOME resolution — the per-machine data dir (allowlist, sessions,
key store). Chosen at install time; everything project-agnostic lives here so
the git repo stays machine-neutral."""

import os
import tempfile
from pathlib import Path


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
        # R171/I6: fsyncing the temp file makes ITS contents durable, but
        # the rename that makes it visible AS `path` is a separate metadata
        # write to the DIRECTORY entry — on a power loss before that
        # directory write itself reaches disk, the rename can be lost even
        # though the temp file's data was safely flushed, leaving the OLD
        # contents as the surviving file (the new inode unlinked). Windows
        # has no directory fd to fsync; best effort there, same fallback
        # shape as the POSIX-only `flock` in session.py.
        try:
            dirfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except (OSError, AttributeError):
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

_MARKER = Path.home() / ".aurora-path"


def aurora_home() -> Path:
    """Resolution order: $AURORA_HOME → ~/.aurora-path marker file → ~/.aurora."""
    env = os.environ.get("AURORA_HOME")
    if env:
        home = Path(env).expanduser()
    elif _MARKER.exists():
        home = Path(_MARKER.read_text().strip()).expanduser()
    else:
        home = Path.home() / ".aurora"
    home.mkdir(parents=True, exist_ok=True)
    return home


def sessions_dir() -> Path:
    d = aurora_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d
