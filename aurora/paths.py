"""AURORA_HOME resolution — the per-machine data dir (allowlist, sessions,
key store). Chosen at install time; everything project-agnostic lives here so
the git repo stays machine-neutral."""

import os
from pathlib import Path

_MARKER = Path.home() / ".aurora-path"


def aurora_home() -> Path:
    """Resolution order: $AURORA_HOME → ~/.aurora-path marker file → ~/.aurora."""
    env = os.environ.get("AURORA_HOME")
    if env:
        home = Path(env).expanduser()
    elif _MARKER.exists():
        home = Path(_MARKER.read_text().strip()).expanduser()
    else:
        home = Path.home() / ".aurora"
    home.mkdir(parents=True, exist_ok=True)
    return home


def sessions_dir() -> Path:
    d = aurora_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d
