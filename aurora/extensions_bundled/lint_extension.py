"""Bundled default extension (R121): a `lint_check` tool the model can call
after writing or editing a file, to catch bugs/style issues before they
ever reach the user — inspired by comparing against a competing agent's
`pi-lens` extension (full LSP diagnostics + tree-sitter structural
analysis across many languages). Deliberately scoped WAY down from that:
no LSP client, no bundled parsers, Python only for v1 — just "shell out to
whatever linter is already configured/installed, show what it says."

Static SPEC/RUNNERS (see aurora/extensions.py's docstring for that style)
— the simplest possible extension shape, same as the `current_datetime`
teaching example, just with a real subprocess behind it instead of one
line of Python.

This is a MODEL-CALLABLE tool, not an automatic hook: it only runs when
the model chooses to call it after an edit (nudged by this tool's own
description). Aurora's extension mechanism has no lifecycle-hook system
yet (see EXTENSIONS.md's "what's scoped out" section) — a guaranteed
"runs after every write/edit, no matter what" version would need that
built first.
"""

import shutil
import subprocess
from pathlib import Path

_TIMEOUT = 15.0

SPEC = [{
    "name": "lint_check",
    "description": (
        "Run a linter against a Python file and report issues. Call this "
        "after writing or editing a .py file to catch bugs, unused "
        "imports, and style problems before the user sees them. Uses "
        "`ruff` if installed; otherwise falls back to a syntax-only check "
        "(no style/type feedback) and says so."),
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string",
                 "description": "path to the .py file to check"}},
        "required": ["path"]},
}]


def lint_check(path: str = "", **_) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        return f"[lint error: no such file: {p}]"
    if p.suffix.lower() != ".py":
        return (f"[lint: unsupported file type {p.suffix or '(none)'!r} — "
                "only .py is supported for now]")
    if shutil.which("ruff"):
        try:
            r = subprocess.run(["ruff", "check", str(p)],
                              capture_output=True, text=True, timeout=_TIMEOUT)
            out = (r.stdout + r.stderr).strip()
            return out if out else "ruff: no issues found"
        except Exception as e:
            return f"[lint error running ruff: {e.__class__.__name__}: {e}]"
    # ruff not installed — a syntax-only fallback, so the tool still does
    # SOMETHING useful with zero setup, but is honest that it's a much
    # weaker check than real linting.
    #
    # R243: the built-in `compile()`, NOT `python -m py_compile`. py_compile
    # WRITES `__pycache__/<name>.cpython-XY.pyc` beside the source as a side
    # effect — and `lint_check` is an ungated tool (it is not in
    # `tools.NEEDS_APPROVAL` and is not an `mcp_*` name, so
    # `tools.needs_approval` returns False and it runs with no approval
    # prompt). A tool the user never approves must not create files in their
    # tree: it dirties a clean repo, and it fails outright on a read-only
    # directory. `compile()` does the same syntax check in-process, with no
    # subprocess and no filesystem write.
    try:
        source = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"[lint error: {e.__class__.__name__}: {e}]"
    try:
        compile(source, str(p), "exec")
    except SyntaxError as e:
        where = f"line {e.lineno}" + (f", col {e.offset}" if e.offset else "")
        return f"syntax error: {p} {where}: {e.msg}"
    except ValueError as e:
        # e.g. source containing a null byte — a real problem with the file,
        # not a crash of the linter
        return f"syntax error: {p}: {e}"
    return ("no syntax errors (ruff not installed — this was a "
            "syntax-only check, no style/type feedback; install ruff "
            "for real lint results)")


RUNNERS = {"lint_check": lint_check}
