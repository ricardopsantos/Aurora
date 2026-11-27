"""R346 — Aurora is a standalone product: no tracked file may name the
author's machines, networks, hosts, services or accounts."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# word-ish patterns; `.ts.net` is allowed ONLY as the private-host suffix list
# entry in the two source files that implement it
_FORBIDDEN = re.compile(
    r"\bm7\b|tailscale|tailnet|tail[0-9a-f]{4,}\.|ricardo|ricardopsantos|"
    r"\bhermes\b|llamadesk|forgejo|gitea|gittea|syncthing|homelab|tutamail|"
    r"192\.168\.1\.158|100\.114\.199\.78", re.I)
_ALLOWED = {
    # the changelog of the original author's project history is scrubbed too;
    # only these spots may keep a literal product/identity string
    "README.md": re.compile(r"github\.com/ricardopsantos/Aurora|raw\.githubusercontent\.com/ricardopsantos/Aurora"),
    "bootstrap-install.sh": re.compile(r"github\.com/ricardopsantos/Aurora"),
    "tests/test_no_personal_references.py": re.compile(r".*"),
    "LICENSE": re.compile(r"^Copyright \(c\) \d{4} Ricardo Santos$"),   # the copyright holder, nothing else
    # asserts the diagnosis text does NOT contain these words
    "tests/test_diagnose.py": re.compile(r'for personal in \('),
}
_TEXT = re.compile(r"\.(py|rs|md|sh|yaml|yml|toml|txt|json|html|js|css|example|service|cfg|ini)$")   # R393: deploy units too; R581: the Rust core too


def _tracked():
    out = subprocess.check_output(["git", "-C", str(ROOT), "ls-files"]).decode()
    return [f for f in out.splitlines() if f and _TEXT.search(f)]


def test_no_tracked_file_names_the_authors_infrastructure():
    bad = []
    for f in _tracked():
        try:
            text = (ROOT / f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        allow = _ALLOWED.get(f)
        for n, line in enumerate(text.splitlines(), 1):
            if _FORBIDDEN.search(line) and not (allow and allow.search(line)):
                bad.append(f"{f}:{n}: {line.strip()[:100]}")
    assert not bad, "personal reference(s) in the product:\n" + "\n".join(bad[:30])


def test_personal_files_are_not_tracked():
    tracked = set(subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files"]).decode().splitlines())
    for private in ("config.yaml", "config.yaml.bak-preollama"):
        assert private not in tracked
    assert not any(f.startswith((".context/", ".agentic_context/", "scripts/"))
                   for f in tracked)
