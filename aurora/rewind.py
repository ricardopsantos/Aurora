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

import hashlib
import subprocess
import time
from pathlib import Path

from .paths import aurora_home

# belt-and-braces on top of the project's .gitignore — a project without one
# must not drag its venv or node_modules into every snapshot
EXCLUDES = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".build/",
            "build/", "dist/", ".mypy_cache/", ".pytest_cache/", "*.pyc",
            ".DS_Store"]

MAX_LABEL = 72


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
        (gd / "info" / "exclude").write_text("\n".join(EXCLUDES) + "\n")


def checkpoint(label: str, cwd: str = ".") -> str | None:
    """Snapshot the working tree. Returns the short hash, or None when the
    tree is unchanged since the last snapshot or git is unavailable."""
    try:
        wt = Path(cwd).resolve()
        _ensure(wt)
        _git(wt, "add", "-A")
        msg = " ".join(label.split())[:MAX_LABEL] or "checkpoint"
        r = _git(wt, "commit", "--quiet", "-m", msg, check=False)
        if r.returncode:  # "nothing to commit" — tree unchanged
            return None
        return _git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
    except Exception:
        return None


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


def _age(ts: int) -> str:
    d = max(0, int(time.time()) - ts)
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"
