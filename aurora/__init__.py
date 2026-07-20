"""Aurora — micro terminal coding agent."""

import subprocess as _subprocess
from pathlib import Path as _Path

# Frozen version for any checkout that ISN'T GitTea's own dev repo (a public
# GitHub clone, or any downstream/production install of it) — such a
# checkout has its own unrelated git history, so counting ITS commits would
# produce a meaningless number, not GitTea's real one. scripts/
# github-deploy.sh overwrites this exact line on every deploy to GitHub, so
# it always reflects GitTea's version at deploy time. Left empty here in
# GitTea itself — empty means "compute live from git" below.
_PINNED_VERSION = "1.0.146"


def _commit_count() -> str:
    try:
        out = _subprocess.run(
            ["git", "-C", str(_Path(__file__).resolve().parent), "rev-list",
             "--count", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except OSError:
        pass
    return "0"


__version__ = _PINNED_VERSION or f"1.0.{_commit_count()}"
