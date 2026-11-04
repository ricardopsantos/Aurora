"""R342: `/try <task>` — run a turn inside a throwaway `git worktree`, then
apply or discard its diff. Complements `/rewind`, which only covers the cwd
tree: here the real tree is never touched until the user says so.

The sandbox starts as a copy of the working tree INCLUDING uncommitted and
untracked changes, committed as a private baseline, so "what the turn changed"
is exactly `baseline..now` and applying it never re-applies the user's own
pre-existing edits."""

import os
import shutil
import subprocess
import time
from pathlib import Path

from .paths import aurora_home

_ID = ["-c", "user.name=aurora-try", "-c", "user.email=try@aurora.invalid"]


class TryError(Exception):
    pass


def _git(cwd, *args, check=True, input=None):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       input=input, timeout=120)
    if check and r.returncode:
        raise TryError((r.stderr or r.stdout).decode("utf-8", "replace").strip()
                       or f"git {args[0]} failed")
    return r


def toplevel(cwd) -> Path:
    r = _git(cwd, "rev-parse", "--show-toplevel", check=False)
    if r.returncode:
        raise TryError("not inside a git repository — /try needs one")
    return Path(r.stdout.decode().strip())


class Sandbox:
    def __init__(self, root: Path, path: Path, rel: Path):
        self.root, self.path, self.rel = root, path, rel

    @property
    def cwd(self) -> Path:
        return self.path / self.rel


def create(cwd=".") -> Sandbox:
    cwd = Path(cwd).resolve()
    root = toplevel(cwd)
    if _git(root, "rev-parse", "--verify", "HEAD", check=False).returncode:
        raise TryError("the repository has no commits yet")
    path = aurora_home() / "try" / time.strftime("%Y%m%d-%H%M%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "--detach", str(path), "HEAD")
    try:
        dirty = _git(root, "diff", "HEAD", "--binary").stdout
        if dirty:
            _git(path, "apply", "--binary", input=dirty)
        others = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
        for rel in filter(None, others.stdout.decode().split("\0")):
            dst = path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if (root / rel).is_file():
                shutil.copy2(root / rel, dst, follow_symlinks=False)
        _git(path, "add", "-A")
        _git(path, *_ID, "commit", "-q", "--allow-empty", "-m", "try baseline")
    except BaseException:
        discard(Sandbox(root, path, Path(".")))
        raise
    return Sandbox(root, path, cwd.relative_to(root))


def diff(sb: Sandbox, stat_only: bool = False) -> str:
    """What the turn changed, as a unified diff ('' when nothing)."""
    _git(sb.path, "add", "-A")
    args = ["diff", "--cached", "HEAD"] + (["--stat"] if stat_only else [])
    return _git(sb.path, *args).stdout.decode("utf-8", "replace")


def apply(sb: Sandbox) -> str:
    """Apply the turn's changes to the real tree, then remove the sandbox."""
    _git(sb.path, "add", "-A")
    patch = _git(sb.path, "diff", "--cached", "HEAD", "--binary").stdout
    if patch:
        r = _git(sb.root, "apply", "--binary", "--whitespace=nowarn", check=False,
                 input=patch)
        if r.returncode:
            raise TryError("could not apply the changes to the working tree "
                           "(it changed meanwhile?): "
                           + r.stderr.decode("utf-8", "replace").strip()
                           + f"\nthe sandbox is kept at {sb.path}")
    discard(sb)
    return "applied" if patch else "nothing to apply"


def discard(sb: Sandbox) -> None:
    _git(sb.root, "worktree", "remove", "--force", str(sb.path), check=False)
    shutil.rmtree(sb.path, ignore_errors=True)
    _git(sb.root, "worktree", "prune", check=False)


def run_in(sb: Sandbox, fn):
    """Run `fn()` with the process cwd inside the sandbox, restoring it."""
    old = os.getcwd()
    os.chdir(sb.cwd)
    try:
        return fn()
    finally:
        os.chdir(old)
