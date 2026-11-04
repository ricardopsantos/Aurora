# summary: web-only store of read-only share links (token -> session id) in AURORA_HOME/web-shares.json.
"""R418 (F1): a share link is a random token that maps to one session, readable without the bearer token and
revocable. The token is only ever a dict key, never a path, so a crafted link cannot reach the filesystem."""
import json
import os
import re
import secrets
import threading
import time

from ..service import aurora_home

MAX_SHARES = 500
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")   # secrets.token_urlsafe(16)
_lock = threading.Lock()


def _file():
    return aurora_home() / "web-shares.json"


def _load() -> dict:
    try:
        d = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if isinstance(k, str) and TOKEN_RE.match(k)
            and isinstance(v, dict) and isinstance(v.get("session"), str)}


def _save(d: dict) -> None:
    p = _file()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def create(sid: str) -> tuple[str | None, str]:
    """The session's existing link, or a new one."""
    with _lock:
        d = _load()
        for tok, v in d.items():
            if v["session"] == sid:
                return tok, ""
        if len(d) >= MAX_SHARES:
            return None, f"too many share links ({MAX_SHARES}): stop sharing some first"
        tok = secrets.token_urlsafe(16)
        d[tok] = {"session": sid, "created": round(time.time())}
        _save(d)
    return tok, ""


def lookup(tok: str) -> str | None:
    if not isinstance(tok, str) or not TOKEN_RE.match(tok):
        return None
    with _lock:
        v = _load().get(tok)
    return v["session"] if v else None


def of_session(sid: str) -> str | None:
    with _lock:
        return next((t for t, v in _load().items() if v["session"] == sid), None)


def delete(tok: str) -> bool:
    with _lock:
        d = _load()
        if tok not in d:
            return False
        del d[tok]
        _save(d)
    return True
