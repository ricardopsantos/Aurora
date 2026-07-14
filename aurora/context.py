"""agentic_context injection (R12) — OPT-IN, never automatic. The agent
knows nothing about `.agentic_context/` unless the user's /bootstrap prompt
(or an explicit bootstrap_context() caller) introduces it. When invoked, it
self-heals the indexes, loads AGENTS.md + the three INDEX.md files + every
[CORE] KNOWLEDGE doc, and embeds the per-task protocol in the system prompt.
`open_context_doc` lazy-loads any other doc on summary match (the tool is
only offered while a context is active).

Engine-side module: no terminal I/O, no UI imports.
"""

import re
import subprocess
from pathlib import Path

CONTEXT_DIR = ".agentic_context"
_INDEXES = ("KNOWLEDGE/INDEX.md", "MEMORY/INDEX.md", "SKILLS/INDEX.md")
_CORE_RE = re.compile(r"^- `([^`]+)` — \[CORE\]", re.M)

_PROTOCOL = """\
## agentic_context protocol (run it yourself, every task)
- BEFORE a task: check MEMORY/INDEX.md above for matching entries; call
  open_context_doc on a finding only if KNOWLEDGE didn't answer.
- Non-[CORE] KNOWLEDGE docs are listed above by summary only — call
  open_context_doc(path) when a summary matches the task.
- AFTER a task: if you learned something non-obvious that will recur and is
  too narrow for KNOWLEDGE, write it as a MEMORY finding
  (MEMORY/<slug>/YYYYMMDD_HHMMSS_<topic>.md, line 2 `> summary: ...`) via
  write_file (it passes the normal approval gate), then run
  `.agentic_context/scripts/rebuild-index.sh` via run_command.
- When a MEMORY finding answers a task, append the task's domain to its
  `**Used in:**` line and rebuild.
- Surface [PROMOTE?] entries to the user; promotion into KNOWLEDGE and any
  [CORE] tagging are always the user's call."""

# module state so the tool runner knows the active root
_root: Path | None = None


def detect(cwd: str | Path = ".") -> Path | None:
    p = Path(cwd).resolve() / CONTEXT_DIR
    return p if p.is_dir() else None


def _read(root: Path, rel: str) -> str:
    p = root / rel
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except Exception:
        return ""


def _self_heal(root: Path) -> None:
    script = root / "scripts" / "rebuild-index.sh"
    if script.is_file():
        try:
            subprocess.run([str(script)], capture_output=True, timeout=60)
        except Exception:
            pass  # best-effort; stale indexes still beat no indexes


def bootstrap(cwd: str | Path = ".") -> str:
    """Build the context system prompt. Returns '' when no .agentic_context.
    Side effect: registers the root for open_context_doc."""
    global _root
    root = detect(cwd)
    if not root:
        _root = None
        return ""
    _self_heal(root)
    _root = root

    parts = []
    agents = _read(root, "AGENTS.md")
    if agents:
        parts.append("# Rules (AGENTS.md — follow exactly)\n" + agents)
    for rel in _INDEXES:
        body = _read(root, rel)
        if body:
            parts.append(f"# {rel}\n{body}")
    # [CORE] docs load in full at start; the rest stay lazy
    kn_index = _read(root, "KNOWLEDGE/INDEX.md")
    for rel in _CORE_RE.findall(kn_index):
        body = _read(root, f"KNOWLEDGE/{rel}")
        if body:
            parts.append(f"# KNOWLEDGE/{rel} [CORE]\n{body}")
    parts.append(_PROTOCOL)
    return "\n\n---\n\n".join(parts)


def active() -> bool:
    return _root is not None


def open_context_doc(path: str, **_) -> str:
    if _root is None:
        return "[error: no active .agentic_context]"
    p = (_root / path).resolve()
    try:
        p.relative_to(_root)
    except ValueError:
        return "[error: path escapes the context root]"
    if not p.is_file():
        return f"[error: no such context doc: {path}]"
    return p.read_text(encoding="utf-8")


SPEC = [
    {"name": "open_context_doc",
     "description": "Load a doc from the project's .agentic_context by its "
                    "index path (e.g. 'KNOWLEDGE/project/Rules.md' or "
                    "'MEMORY/bugs/2026...md'). Use when an INDEX summary "
                    "matches the task.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
]

RUNNERS = {"open_context_doc": open_context_doc}
