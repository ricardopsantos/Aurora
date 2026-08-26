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

from .paths import aurora_home, write_text_atomic

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


# R237: same cap, same reasoning as `web_extension._FETCH_CAP`. A bootstrap
# prompt is a page of markdown; anything approaching this is not one. Smaller
# than web_fetch's 2MB because this content is not merely read — it is sent
# verbatim as the first TOOL-ENABLED turn, so an oversized body costs context
# and tokens on top of memory.
_FETCH_CAP = 512_000


def fetch_url(url: str) -> str:
    """Plain-text GET — bootstrap prompts are markdown/plain text (e.g. a
    GitHub raw link), not HTML pages, so no tag-stripping like web_fetch.

    R237: streamed with a byte cap. This was a plain `c.get(url)` reading
    `r.text`, which pulls an arbitrarily large body fully into memory before
    any caller can look at it — the exact shape `web_extension.web_fetch`
    already streams around, in a module whose fetched content is used far
    less dangerously than this one's. The URL here is remembered and
    re-fetched at startup, so a body that grew (or a hijacked redirect) is
    re-downloaded every session."""
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Aurora/0.1"}) as c:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) >= _FETCH_CAP:
                    break
        return bytes(buf[:_FETCH_CAP]).decode(r.encoding or "utf-8",
                                              errors="replace")


def refresh_from_source(cwd: str | Path = ".", confirm=None
                        ) -> tuple[str, Path] | None:
    """Re-download the active bootstrap prompt from its saved URL and persist
    the fresh content at the same path (project vs global) it was loaded
    from. Returns (new_text, path), or None if no URL-sourced prompt is
    active, the fetch was rejected by `confirm`, or the content is unchanged
    (nothing to persist) — callers fall back to the cached `load()` in that
    case.

    `confirm(old_text, new_text) -> bool`, if given, is asked BEFORE the
    fetched content is written and BEFORE it is later sent as a tool-enabled
    turn (R171/S3): a compromised URL, a hijacked redirect, or a
    man-in-the-middle on the first-ever download all reach the same "run
    with tools" turn with no integrity check between fetch and execution.
    `confirm` gives the caller a chance to show the diff and require an
    explicit yes rather than treating a fetch as automatically trustworthy.
    `confirm=None` (the default) keeps the old unconditional-overwrite
    behavior for callers that don't have a UI to ask through."""
    active = _active_source(cwd)
    if active is None:
        return None
    p, _ = active
    url = source_url(cwd)
    if not url:
        return None
    old_text = p.read_text(encoding="utf-8") if p.is_file() else ""
    text = fetch_url(url)
    if text.rstrip() == old_text.rstrip():
        return None   # nothing changed — no re-confirmation, no rewrite
    if confirm is not None and not confirm(old_text, text):
        return None
    # R237: atomic, like every other persisted file (R146a). A crash mid-write
    # leaves a TRUNCATED bootstrap prompt, which is then sent as the first
    # tool-enabled turn of the next session.
    write_text_atomic(p, text.rstrip() + "\n")
    write_text_atomic(_source_url_path(p), url.strip() + "\n")
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
    write_text_atomic(p, text.rstrip() + "\n")   # R237
    sp = _source_url_path(p)
    if source_url:
        write_text_atomic(sp, source_url.strip() + "\n")
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
