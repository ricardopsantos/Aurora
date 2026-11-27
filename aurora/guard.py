"""R450: path guard for the web page (card 20261114-203412, S1/S2/S8).

The web page defaults to auto-approve, and `read_file`/`grep`/`list_dir`/
`find_files` are never gated, so a prompt injection could read
`/proc/self/environ` (the web token) or `~/.ssh`, and could write
`web-denylist.txt` to switch the denylist off. In web mode every file tool
passes through `check()` first. The terminal is unchanged: a person approves
each write there.

  * reads and writes: secret locations (ssh/gpg/aws dirs, `/etc/environment`,
    `/etc/shadow`, `/proc/<pid>/environ`, `*.token`, `*.pem`, `keys.enc` ...)
  * writes only: everything inside AURORA_HOME except `workspace/` (policy
    files, state, extensions, bootstrap, sessions).

`WEB` is switched on by `WebSession`; paths are resolved (symlinks, `..`)
before they are compared."""
import os
import re
from pathlib import Path

WEB = False

SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws"})
SECRET_NAMES = frozenset({".netrc", ".git-credentials", "keys.enc", ".pgpass"})
SECRET_ETC = frozenset({"/etc/environment", "/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/master.passwd"})
SECRET_SUFFIXES = (".token", ".pem", ".key")
_PROC_ENV = re.compile(r"^/proc/[^/]+(/task/[^/]+)?/environ$")

_READ_TOOLS = {"read_file": "path", "list_dir": "path", "grep": "path", "find_files": "path"}
_WRITE_TOOLS = {"write_file": "path", "edit_file": "path", "apply_patch": "path"}


def _real(path: str) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path) or ".")))


def _home() -> Path:
    from .paths import aurora_home
    return Path(os.path.realpath(aurora_home()))


def secret_reason(p: Path) -> str:
    s = str(p)
    if s.startswith("/private/etc/"):          # macOS: /etc is a link to /private/etc, and paths arrive resolved
        s = s[len("/private"):]
    if _PROC_ENV.match(s):
        return "a process environment holds secrets"
    parts = p.parts
    if SECRET_DIRS.intersection(parts):
        return "key/credential folder"
    if s in SECRET_ETC or p.name in SECRET_NAMES or s.startswith("/etc/sudoers.d/"):
        return "credential file"
    if p.name.lower().endswith(SECRET_SUFFIXES):
        return "token/key file"
    return ""


def policy_reason(p: Path) -> str:
    """Writes inside AURORA_HOME, outside `workspace/`."""
    try:
        rel = p.relative_to(_home())
    except (ValueError, OSError):
        return ""
    if rel.parts and rel.parts[0] == "workspace":
        return ""
    return "Aurora's own settings, policy and code files are not editable by the agent"


def check(name: str, args: dict | None) -> str:
    """"" when the call may run, else the refusal text the model gets back."""
    if not WEB:
        return ""
    args = args or {}
    key = _READ_TOOLS.get(name) or _WRITE_TOOLS.get(name)
    if not key:
        return ""
    raw = args.get(key, ".")
    if not isinstance(raw, str):
        return ""
    p = _real(raw)
    why = secret_reason(p)
    if not why and name in _WRITE_TOOLS:
        why = policy_reason(p)
    return f"[blocked on the web page: {raw} ({why})]" if why else ""


def grep_excludes() -> list[str]:
    """Extra `grep -r` excludes in web mode, so a search of `~` or `/` cannot walk into them."""
    if not WEB:
        return []
    return [f"--exclude-dir={d}" for d in sorted(SECRET_DIRS | {"proc"})] + \
           [f"--exclude={n}" for n in sorted(SECRET_NAMES | {"environment", "shadow"})] + [f"--exclude=*{s}" for s in SECRET_SUFFIXES]
