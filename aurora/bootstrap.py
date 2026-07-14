"""User-defined bootstrap prompt (/bootstrap). A saved free-text prompt sent
as the FIRST user turn of a session — the model executes it with tools (e.g.
the .agentic_context bootstrap ritual). Whenever a non-empty prompt exists,
startup asks to run it (Enter = yes). Global default in
AURORA_HOME/bootstrap.md; a project's .aurora/bootstrap.md overrides it.

Engine-side module: no terminal I/O, no UI imports.
"""

from pathlib import Path

from .paths import aurora_home

_PROJECT_REL = Path(".aurora") / "bootstrap.md"


def _global_path() -> Path:
    return aurora_home() / "bootstrap.md"


def _project_path(cwd: str | Path = ".") -> Path:
    return Path(cwd).resolve() / _PROJECT_REL


def load(cwd: str | Path = ".") -> tuple[str, str] | tuple[None, None]:
    """(prompt, source-label) — project override wins over global."""
    for p, label in ((_project_path(cwd), "project"), (_global_path(), "global")):
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8").strip()
                if text:
                    return text, f"{label} ({p})"
        except Exception:
            pass
    return None, None


def from_input(text: str) -> tuple[str, Path | None]:
    """Path detection: one line ending in .md/.txt that exists as a file →
    return its contents (snapshot at set-time) + the source path; otherwise
    the text unchanged."""
    candidate = text.strip()
    if (candidate and "\n" not in candidate
            and candidate.lower().endswith((".md", ".txt"))):
        p = Path(candidate).expanduser()
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8"), p
        except Exception:
            pass
    return text, None


def save(text: str, project: bool = False, cwd: str | Path = ".") -> Path:
    p = _project_path(cwd) if project else _global_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.rstrip() + "\n", encoding="utf-8")
    return p


def clear(project: bool = False, cwd: str | Path = ".") -> Path | None:
    p = _project_path(cwd) if project else _global_path()
    if p.is_file():
        p.unlink()
        return p
    return None
