# summary: web-only store of read-only share links (token -> session id) in AURORA_HOME/web-shares.json.
"""R418 (F1): a share link is a random token that maps to one session, readable without the bearer token and
revocable. The token is only ever a dict key, never a path, so a crafted link cannot reach the filesystem.

R454 (#17): a link is a SNAPSHOT: it records how many session records existed when it was made (`upto`) and shows
only those, so what is said in the chat afterwards is not published too. Sharing the same chat again moves the
snapshot to now. A link expires (`expires`, default SHARE_DAYS days; 0 = never) and then reads as not found."""
import json
import os
import re
import secrets
import threading
import time

from ..service import aurora_home, write_text_atomic

MAX_SHARES = 500
SHARE_DAYS = 7
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")   # secrets.token_urlsafe(16)
_lock = threading.Lock()


def _file():
    return aurora_home() / "web-shares.json"


problem = ""   # R514: the last damaged store found (shown by /health), "" when none


def _load() -> dict:
    """R514: a damaged web-shares.json is moved aside and said on /health, like the notes and folders stores. It
    used to read as empty, so the next share wrote over it and every other link was lost without a word."""
    global problem
    try:
        raw = _file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise RuntimeError(f"web-shares.json unreadable: {e}")
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not an object")
    except ValueError:
        aside = _file().with_name(f"web-shares.json.corrupt-{int(time.time())}")
        os.replace(_file(), aside)
        problem = f"web-shares.json was damaged: moved to {aside.name}; share links start empty"
        return {}
    return {k: v for k, v in d.items() if isinstance(k, str) and TOKEN_RE.match(k)
            and isinstance(v, dict) and isinstance(v.get("session"), str)}


def _save(d: dict) -> None:
    write_text_atomic(_file(), json.dumps(d))      # R513: the tokens are never readable by others, not even briefly


def _record_count(sid: str) -> int:
    from .. import service
    return sum(1 for _ in service.session_records(sid))


def _expiry(days: float | None) -> int:
    import math
    days = SHARE_DAYS if days is None else days
    if not isinstance(days, (int, float)) or math.isnan(days) or days <= 0:
        return 0
    return round(time.time() + min(days, 3650) * 86400)    # R554: inf or 1e300 days raised OverflowError


def create(sid: str, days: float | None = None) -> tuple[str | None, str]:
    """The session's link (its existing token, with the snapshot and expiry moved to now), or a new one."""
    upto = _record_count(sid)
    with _lock:
        d = _load()
        now = time.time()                  # R466: an expired link is dropped here, so it no longer counts to MAX_SHARES
        d = {t: v for t, v in d.items() if not (isinstance(v.get("expires"), (int, float)) and v["expires"]
                                                and now >= v["expires"])}
        tok = next((t for t, v in d.items() if v["session"] == sid), None)
        if tok is None:
            if len(d) >= MAX_SHARES:
                return None, f"too many share links ({MAX_SHARES}): stop sharing some first"
            tok = secrets.token_urlsafe(16)
            d[tok] = {"session": sid, "created": round(time.time())}
        d[tok].update(upto=upto, expires=_expiry(days), shared=round(time.time()))
        _save(d)
    return tok, ""


def info(tok: str) -> dict | None:
    """{session, upto, expires} for a live link; None for an unknown, revoked or expired one. A link made before
    R454 has no snapshot: it is frozen at its first read after the upgrade and given the default expiry."""
    if not isinstance(tok, str) or not TOKEN_RE.match(tok):
        return None
    with _lock:
        d = _load()
        v = d.get(tok)
        if v is None:
            return None
        if "upto" not in v:
            v["upto"], v["expires"] = _record_count(v["session"]), _expiry(None)
            _save(d)
    try:                                  # R555: a hand-edited "upto"/"expires" is a dead link, never a 500
        upto, expires = int(v["upto"]), int(v.get("expires") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if expires and time.time() >= expires:
        return None
    return {"session": v["session"], "upto": upto, "expires": expires}


def lookup(tok: str) -> str | None:
    v = info(tok)
    return v["session"] if v else None


def of_session(sid: str) -> str | None:
    with _lock:
        return next((t for t, v in _load().items() if v["session"] == sid), None)


def delete_session(sids) -> int:
    """R466: revoke every link to these sessions (a chat deleted on the page: its row, which holds the revoke
    button, is gone with it). Returns how many links went."""
    sids = set(sids)
    with _lock:
        d = _load()
        keep = {t: v for t, v in d.items() if v["session"] not in sids}
        if len(keep) != len(d):
            _save(keep)
    return len(d) - len(keep)


def delete(tok: str) -> bool:
    with _lock:
        d = _load()
        if tok not in d:
            return False
        del d[tok]
        _save(d)
    return True
