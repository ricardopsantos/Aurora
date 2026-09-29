"""Skills (R11, ported from the Terminal-Agent V2 prototype): `/name args`
runs an executable or python script from a skills dir; `/skills` lists them.

Search order (first hit wins): <repo>/skills/ next to the config file, then
AURORA_HOME/skills/. A skill is any executable file, or a *.py run with the
venv's python. The first line's trailing comment (after `#`) is its blurb.

R247: a SECOND kind of skill, alongside the executable one above — a plain
Markdown "doc skill" (frontmatter `name:`/`description:` + prose body, the
same SKILL.md shape Claude Code and Hermes both already use). It has no
interpreter to run, so `/name` for one of these does not subprocess — see
`ui._handle_command`'s fallback, which loads the body and feeds it to the
model as a real turn via `_run_turn`, the same mechanism `/bootstrap`
already uses to turn a saved file into a tool-enabled prompt. This module
only discovers and reads them; it has no opinion on how the caller uses
the text.

Aurora is not tied to any one machine's layout, so there is deliberately NO
default path baked in here — a SKILL.md library only exists at all if the
user's own config.yaml names it, via a `doc_skill_roots:` list of
directories to search recursively. No entry means no doc skills, not a
guess at where they might be."""

import os
import re
import shlex
import sys
from pathlib import Path

import yaml

from . import tools
from .paths import aurora_home

_SKILL_TIMEOUT = 300   # module-level so tests can shrink it


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
        # a dir that passed `_dirs()`'s `is_dir()` check can still fail to
        # list (permission changed, removed) by the time we get here — the
        # same TOCTOU race `dir_stamp()` above already guards against.
        # This runs behind the `/command` completer, once per keystroke
        # (R96a) — an unguarded OSError here didn't just skip one skill, it
        # crashed the whole listing on every keystroke until the directory
        # was fixed.
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_file() and (os.access(p, os.X_OK) or p.suffix == ".py"):
                found.setdefault(p.stem, p)
    return found


def _doc_roots(doc_roots: list[str] | None) -> list[Path]:
    """No `doc_skill_roots` configured means no doc skills — an unset config
    is not license to guess a path on the user's machine."""
    if not doc_roots:
        return []
    out = []
    for r in doc_roots:
        p = Path(r).expanduser()
        if p.is_dir():
            out.append(p)
    return out


_FRONTMATTER_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]?(.*)$")


def _lenient_frontmatter(raw: str) -> dict:
    """Fallback for frontmatter that fails strict YAML. Confirmed against
    the real skill corpus, not a hypothetical: a `description:` whose prose
    contains its own bare `word: word` (e.g. "...a topic: SOUL.md's machine
    brief...") is invalid YAML — an unquoted colon+space ends a plain
    scalar's mapping value — despite reading as obviously fine to whatever
    authored the file, and this shape is common, not a one-off typo. Pulls
    flat `key: value` lines, splitting each on only its FIRST colon, which
    is exactly enough for the two fields this module reads (name,
    description) without attempting to be a real YAML parser."""
    out = {}
    for line in raw.splitlines():
        m = _FRONTMATTER_LINE_RE.match(line)
        if m:
            out.setdefault(m.group(1), m.group(2).strip())
    return out


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md's leading `---`-fenced YAML block from its body.
    Returns ({}, text) unchanged if there is no frontmatter fence at all —
    same fail-open shape as the rest of this module: a malformed doc skill
    should read as a doc skill with no description, not crash discovery."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = _lenient_frontmatter(parts[1])
    return meta, parts[2].lstrip("\n")


def discover_docs(doc_roots: list[str] | None = None) -> dict[str, Path]:
    """name -> path to its SKILL.md, found recursively under each root
    (Claude Code's are nested by category, e.g. SKILLS/meta/plan/SKILL.md;
    Hermes' are flat). Earlier roots shadow later ones; same
    permission/TOCTOU tolerance as discover() — an unreadable root
    contributes nothing rather than raising.

    `os.walk(..., followlinks=True)`, NOT `Path.rglob` — a category directory
    under a real SKILL.md root is routinely a symlink (this machine's own
    `.agentic_context/SKILLS/{analysis,meta,writing}` all are, pointing into
    a separate skills repo so multiple consumers share one canonical copy).
    `rglob` does not follow symlinked directories by default; on <3.13 it
    cannot be told to (`recurse_symlinks` is a 3.13+ kwarg, this project
    supports 3.11+) and even on 3.13 the default silently skips them. Proven
    against the real skill library: `rglob` found 1 of 6 SKILL.md files
    under this repo's own SKILLS/ root, all silently skipped, no error."""
    found: dict[str, Path] = {}
    for root in _doc_roots(doc_roots):
        try:
            walker = os.walk(root, followlinks=True)
        except OSError:
            continue
        matches = []
        for dirpath, _dirnames, filenames in walker:
            if "SKILL.md" in filenames:
                matches.append(Path(dirpath) / "SKILL.md")
        for p in sorted(matches):
            found.setdefault(p.parent.name, p)
    return found


def _doc_blurb(path: Path) -> str:
    try:
        meta, _ = _parse_frontmatter(
            path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return str(meta.get("description", ""))[:100]


def load_doc(name: str, doc_roots: list[str] | None = None) -> str | None:
    """The skill's prose body (frontmatter stripped, description folded into
    a header) — text meant to be fed to the MODEL as instructions, not
    printed as a command's output. None if no doc skill has this name."""
    found = discover_docs(doc_roots)
    path = found.get(name)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    header = f"# Skill: {meta.get('name', name)}\n"
    if meta.get("description"):
        header += f"> {meta['description']}\n"
    return header + "\n" + body


def listing(config_base: str | None = None,
           doc_roots: list[str] | None = None) -> str:
    sk = discover(config_base)
    docs = discover_docs(doc_roots)
    if not sk and not docs:
        return ("[no skills installed — drop executables or .py files in "
                "skills/, or point doc_skill_roots at a SKILL.md library]")
    lines = [f"/{name}  {_blurb(path)}" for name, path in sk.items()]
    # An executable skill wins a name collision — same shadowing rule
    # discover() already applies between its own two search dirs, extended
    # to cover the doc-skill roots too, so `/name` never has to guess which
    # one you meant.
    lines += [f"/{name}  [doc] {_doc_blurb(path)}"
             for name, path in docs.items() if name not in sk]
    return "\n".join(lines)


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
    # R171/I4: was a bare `subprocess.run(..., timeout=300)` — the exact
    # shape R125c fixed for TUI bash mode (`_run_command_once`): on timeout,
    # `subprocess.run`'s own TimeoutExpired handling kills only the direct
    # child, so a skill that forks/backgrounds something (a dev server, a
    # build) leaves it running forever, reparented to init, with no
    # `[timeout]` + process-group-kill semantics the rest of the codebase
    # standardized on. Route through the same helper `run_command` uses.
    try:
        out, code = tools._run_command_once(shlex.join(cmd), None,
                                            timeout=_SKILL_TIMEOUT)
    except OSError as e:
        # an executable skill with a bad/missing interpreter (Exec format
        # error, ENOENT shebang) must come back as text, not kill the turn
        return f"[skill error: {e}]"
    if code is None:
        msg = f"[skill timeout after {_SKILL_TIMEOUT}s]"
        return f"{out}\n{msg}" if out else msg
    return (out.strip() or "[no output]") + (f"\n[exit {code}]" if code else "")
