"""Skills (R11, ported from the Terminal-Agent V2 prototype): `/name args`
runs an executable or python script from a skills dir; `/skills` lists them.

Search order (first hit wins): <repo>/skills/ next to the config file, then
AURORA_HOME/skills/. A skill is any executable file, or a *.py run with the
venv's python. The first line's trailing comment (after `#`) is its blurb."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

from .paths import aurora_home


def _dirs(config_base: str | None) -> list[Path]:
    out = []
    if config_base:
        out.append(Path(config_base) / "skills")
    out.append(aurora_home() / "skills")
    return [d for d in out if d.is_dir()]


def _blurb(path: Path) -> str:
    """The first line's trailing comment. Reads only the HEAD of the file
    (R96a): a skill is an arbitrary script — this used to `read_text()` the
    whole thing and split every line just to look at the first three, and it
    runs behind the `/command` completer, once per skill per keystroke."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            head = [f.readline(512) for _ in range(3)]
        for line in head:
            if "#" in line and not line.startswith("#!"):
                return line.split("#", 1)[1].strip()[:70]
    except Exception:
        pass
    return ""


def dir_stamp(config_base: str | None = None) -> tuple:
    """A cheap fingerprint of the skills dirs — (path, mtime_ns) per dir.

    A directory's mtime changes when a file is added or removed, which is
    exactly what `discover()`'s answer depends on. Lets a caller cache the
    listing across keystrokes and still notice a newly-dropped skill, for
    two `stat()` calls instead of a directory walk plus a read per skill
    (R96a). An in-place EDIT of an existing skill's blurb line doesn't move
    the dir mtime, so a cached blurb can lag until the next restart — the
    listing itself (which names exist) is always current.
    """
    out = []
    for d in _dirs(config_base):
        try:
            out.append((str(d), d.stat().st_mtime_ns))
        except OSError:
            continue
    return tuple(out)


def discover(config_base: str | None = None) -> dict[str, Path]:
    """name -> path; earlier dirs shadow later ones."""
    found: dict[str, Path] = {}
    for d in _dirs(config_base):
        for p in sorted(d.iterdir()):
            if p.is_file() and (os.access(p, os.X_OK) or p.suffix == ".py"):
                found.setdefault(p.stem, p)
    return found


def listing(config_base: str | None = None) -> str:
    sk = discover(config_base)
    if not sk:
        return "[no skills installed — drop executables or .py files in skills/]"
    return "\n".join(f"/{name}  {_blurb(path)}" for name, path in sk.items())


def run(name: str, args: str, config_base: str | None = None) -> str:
    sk = discover(config_base)
    if name not in sk:
        return f"[unknown skill: /{name} — try /skills]"
    path = sk[name]
    cmd = ([sys.executable, str(path)] if path.suffix == ".py"
           and not os.access(path, os.X_OK) else [str(path)])
    try:
        cmd += shlex.split(args) if args else []
    except ValueError as e:   # unbalanced quotes etc.
        return f"[skill args error: {e}]"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "[skill timeout after 300s]"
    except OSError as e:
        # an executable skill with a bad/missing interpreter (Exec format
        # error, ENOENT shebang) must come back as text, not kill the turn
        return f"[skill error: {e}]"
    out = (r.stdout or "") + (r.stderr or "")
    return (out.strip() or "[no output]") + (
        f"\n[exit {r.returncode}]" if r.returncode else "")
