"""Aurora — micro terminal coding agent."""

import json as _json
import subprocess as _subprocess
from pathlib import Path as _Path

# Frozen version for any checkout that ISN'T GitTea's own dev repo (a public
# GitHub clone, or any downstream/production install of it) — such a
# checkout has its own unrelated git history, so counting ITS commits would
# produce a meaningless number, not GitTea's real one. scripts/
# github-deploy.sh overwrites this exact line on every deploy to GitHub, so
# it always reflects GitTea's version at deploy time. Left empty here in
# GitTea itself — empty means "compute live from git" below.
_PINNED_VERSION = "1.2.42"

# 1.2+: the patch number is commits SINCE THE LAST DEPLOY, not the total
# commit count (that was the 1.1.<total> scheme). VERSION_BASELINE.json
# records the major.minor and the commit count at the last deploy;
# scripts/github-deploy.sh bumps baseline_commit_count on every real deploy.
_REPO_ROOT = _Path(__file__).resolve().parent.parent
_BASELINE_FILE = _REPO_ROOT / "VERSION_BASELINE.json"


def _commit_count() -> int:
    try:
        out = _subprocess.run(
            ["git", "-C", str(_Path(__file__).resolve().parent), "rev-list",
             "--count", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return int(out.stdout.strip())
    except (OSError, ValueError):
        pass
    return 0


def _live_version() -> str:
    try:
        baseline = _json.loads(_BASELINE_FILE.read_text())
        major_minor = baseline["major_minor"]
        baseline_count = int(baseline["baseline_commit_count"])
    except (OSError, ValueError, KeyError):
        return "1.2.0"
    return f"{major_minor}.{max(0, _commit_count() - baseline_count)}"


__version__ = _PINNED_VERSION or _live_version()
