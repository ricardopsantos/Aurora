"""User-defined bootstrap prompt (/bootstrap). A saved free-text prompt sent
as the FIRST user turn of a session — the model executes it with tools (e.g.
the .agentic_context bootstrap ritual). Whenever a non-empty prompt exists,
startup asks to run it (Enter = yes). Global default in
AURORA_HOME/bootstrap.md; a project's .aurora/bootstrap.md overrides it.

`/bootstrap set <url>` downloads and caches the content instead of reading a
local file — the URL is remembered in a sidecar `.source` file so startup
can offer "run the cached copy" vs "re-download" instead of silently
re-fetching (or silently going stale) every session.

Engine-side module: no terminal I/O beyond the URL fetch itself; no UI
imports.
"""

from pathlib import Path

import httpx

from .paths import aurora_home

_PROJECT_REL = Path(".aurora") / "bootstrap.md"


def _global_path() -> Path:
    return aurora_home() / "bootstrap.md"


def _project_path(cwd: str | Path = ".") -> Path:
    return Path(cwd).resolve() / _PROJECT_REL


def _source_url_path(p: Path) -> Path:
    return p.with_name(p.name + ".source")


def _active_source(cwd: str | Path = ".") -> tuple[Path, str] | None:
    """(path, label) for whichever bootstrap file `load()` would use —
    project override wins over global, and an existing-but-empty file is
    skipped in favor of the next one, same as `load()` itself."""
    for p, label in ((_project_path(cwd), "project"), (_global_path(), "global")):
        try:
            if p.is_file() and p.read_text(encoding="utf-8").strip():
                return p, label
        except Exception:
            pass
    return None


def load(cwd: str | Path = ".") -> tuple[str, str] | tuple[None, None]:
    """(prompt, source-label) — project override wins over global."""
    active = _active_source(cwd)
    if active is None:
        return None, None
    p, label = active
    return p.read_text(encoding="utf-8").strip(), f"{label} ({p})"


def source_url(cwd: str | Path = ".") -> str | None:
    """The URL the active bootstrap prompt was downloaded from, if it was
    set via `/bootstrap set <url>` rather than a local file/paste."""
    active = _active_source(cwd)
    if active is None:
        return None
    sp = _source_url_path(active[0])
    try:
        if sp.is_file():
            return sp.read_text(encoding="utf-8").strip() or None
    except Exception:
        pass
    return None


def is_url(text: str) -> bool:
    candidate = text.strip()
    return ("\n" not in candidate
            and candidate.lower().startswith(("http://", "https://")))


def fetch_url(url: str) -> str:
    """Plain-text GET — bootstrap prompts are markdown/plain text (e.g. a
    GitHub raw link), not HTML pages, so no tag-stripping like web_fetch."""
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Aurora/0.1"}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


def refresh_from_source(cwd: str | Path = ".") -> tuple[str, Path] | None:
    """Re-download the active bootstrap prompt from its saved URL and persist
    the fresh content at the same path (project vs global) it was loaded
    from. Returns (new_text, path), or None if no URL-sourced prompt is
    active — callers fall back to the cached `load()` in that case."""
    active = _active_source(cwd)
    if active is None:
        return None
    p, _ = active
    url = source_url(cwd)
    if not url:
        return None
    text = fetch_url(url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.rstrip() + "\n", encoding="utf-8")
    _source_url_path(p).write_text(url.strip() + "\n", encoding="utf-8")
    return text, p


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


def save(text: str, project: bool = False, cwd: str | Path = ".",
         source_url: str | None = None) -> Path:
    p = _project_path(cwd) if project else _global_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.rstrip() + "\n", encoding="utf-8")
    sp = _source_url_path(p)
    if source_url:
        sp.write_text(source_url.strip() + "\n", encoding="utf-8")
    elif sp.is_file():
        # overwriting a URL-sourced prompt with a paste/local file must not
        # leave a stale URL behind that a later startup would offer to re-run
        sp.unlink()
    return p


def clear(project: bool = False, cwd: str | Path = ".") -> Path | None:
    p = _project_path(cwd) if project else _global_path()
    sp = _source_url_path(p)
    if sp.is_file():
        sp.unlink()
    if p.is_file():
        p.unlink()
        return p
    return None
