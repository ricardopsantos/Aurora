"""The tool set Aurora exposes to the model (R6). Each tool is a spec (name +
JSON-schema parameters, provider-agnostic) plus a `run(args) -> str`.
Writes/commands are gated by the caller (agent.py) via approve.py; the tools
themselves just do the work. Reads and web tools need no approval."""

import subprocess
from pathlib import Path

MAX_READ_BYTES = 200_000

# which tools mutate / execute — the caller gates these
NEEDS_APPROVAL = {"write_file", "edit_file", "run_command"}


def _resolve(path: str) -> Path:
    return Path(path).expanduser()


def read_file(path: str, **_) -> str:
    p = _resolve(path)
    if not p.is_file():
        return f"[error: no such file: {path}]"
    with p.open("rb") as f:  # never slurp a huge file just to keep the head
        data = f.read(MAX_READ_BYTES + 1)
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_READ_BYTES:
        text += f"\n[truncated at {MAX_READ_BYTES} bytes]"
    return text


def list_dir(path: str = ".", **_) -> str:
    p = _resolve(path)
    if not p.is_dir():
        return (f"[error: not a directory: {path} — cwd is {Path.cwd()}; "
                f"use an absolute path or ~/…]")
    out = []
    for c in sorted(p.iterdir()):
        out.append(f"{'d' if c.is_dir() else '-'} {c.name}")
    return "\n".join(out) or "[empty]"


GREP_PRUNE = [".git", "node_modules", ".venv", "venv", "__pycache__",
              ".build", "build", "dist", ".mypy_cache", ".pytest_cache"]


def grep(pattern: str, path: str = ".", **_) -> str:
    try:
        excludes = [f"--exclude-dir={d}" for d in GREP_PRUNE]
        r = subprocess.run(["grep", "-rnI", *excludes, "--", pattern,
                            str(_resolve(path))],
                           capture_output=True, text=True, timeout=30)
        out = r.stdout.strip()
        return out[:MAX_READ_BYTES] or "[no matches]"
    except Exception as e:
        return f"[grep error: {e}]"


def write_file(path: str, content: str, **_) -> str:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"[wrote {len(content)} bytes to {path}]"


def edit_file(path: str, old: str, new: str, **_) -> str:
    p = _resolve(path)
    if not p.is_file():
        return f"[error: no such file: {path}]"
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        return "[error: `old` text not found — it must match exactly]"
    if n > 1:
        return f"[error: `old` text appears {n} times — make it unique]"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"[edited {path}]"


def run_command(command: str, **_) -> str:
    try:
        r = subprocess.run(command, shell=True, capture_output=True,
                           text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "[timeout after 300s]"
    out = (r.stdout or "") + (r.stderr or "")
    return (out.strip() or "[no output]") + (f"\n[exit {r.returncode}]" if r.returncode else "")


RUNNERS = {
    "read_file": read_file, "list_dir": list_dir, "grep": grep,
    "write_file": write_file, "edit_file": edit_file, "run_command": run_command,
}

SPEC = [
    {"name": "read_file", "description": "Read a UTF-8 text file.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_dir", "description": "List a directory's entries.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "default '.'"}}, "required": []}},
    {"name": "grep", "description": "Recursively search for a regex/string; returns file:line matches.",
     "parameters": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string"}},
         "required": ["pattern"]}},
    {"name": "write_file", "description": "Create or overwrite a file with content (asks approval).",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace one unique occurrence of `old` with `new` in a file (asks approval).",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
         "required": ["path", "old", "new"]}},
    {"name": "run_command", "description": "Run a shell command (asks approval).",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string"}}, "required": ["command"]}},
]


def specs(include_web: bool) -> list[dict]:
    from . import context, websearch
    s = list(SPEC)
    if include_web:
        s += websearch.SPEC
    if context.active():
        s += context.SPEC
    return s


# Hard cap on what a single tool result feeds the model. 200KB of grep/file
# output is ~50k tokens — one call could evict the whole conversation on a
# 65k local context. ~15k tokens is plenty; the model can re-read narrower.
TOOL_OUTPUT_LIMIT = 60_000


def run_tool(name: str, args: dict) -> str:
    from . import context, websearch
    for table in (RUNNERS, websearch.RUNNERS, context.RUNNERS):
        if name in table:
            try:
                out = table[name](**args)
            except Exception as e:
                # a raising tool must NOT kill the turn: the assistant message
                # already carries the tool_use, and a missing tool result makes
                # every later request invalid (roles/tool_result pairing) —
                # feed the failure back as the result instead
                return (f"[tool error: {name}: "
                        f"{e.__class__.__name__}: {e}]")
            if len(out) > TOOL_OUTPUT_LIMIT:
                out = (out[:TOOL_OUTPUT_LIMIT]
                       + f"\n[output truncated at {TOOL_OUTPUT_LIMIT} chars "
                         f"({len(out)} total) — narrow the query/read a "
                         f"specific range]")
            return out
    return f"[error: unknown tool '{name}']"
