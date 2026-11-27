# summary: web-only store of hidden chats and chat folders (JSON in AURORA_HOME); never touches the session logs the TUI shares.
"""R383: the sidebar's folders and hidden ("deleted") chats, kept beside the session logs, not in them."""
import json
import os
import threading
import time
import uuid

from ..service import aurora_home, write_text_atomic

MAX_NAME = 60
MAX_FOLDERS = 100
_lock = threading.Lock()


def _file():
    return aurora_home() / "web-chats.json"


problem = ""   # R392: the last damaged store found (shown by /health), "" when none


def _load() -> dict:
    """R392 (bug 12): a damaged web-chats.json is moved aside, never written over by the next change."""
    global problem
    try:
        raw = _file().read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = "{}"
    except OSError as e:
        raise RuntimeError(f"web-chats.json unreadable: {e}")
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not an object")
    except ValueError:
        aside = _file().with_name(f"web-chats.json.corrupt-{int(time.time())}")
        os.replace(_file(), aside)
        problem = f"web-chats.json was damaged: moved to {aside.name}; folders, hidden chats and names start empty"
        d = {}
    hidden = [h for h in d.get("hidden", []) if isinstance(h, str)]
    folders = [f for f in d.get("folders", []) if isinstance(f, dict) and isinstance(f.get("id"), str)]
    for f in folders:
        f["name"] = str(f.get("name", ""))[:MAX_NAME] or "Folder"
        f["open"] = bool(f.get("open", True))
        f["chats"] = [c for c in f.get("chats", []) if isinstance(c, str)]
    titles = {k: v[:MAX_NAME] for k, v in (d.get("titles") or {}).items()
              if isinstance(k, str) and isinstance(v, str) and v.strip()} if isinstance(d.get("titles"), dict) else {}
    return {"hidden": hidden, "folders": folders, "titles": titles}


def _save(d: dict) -> None:
    write_text_atomic(_file(), json.dumps(d, ensure_ascii=False))      # R513: 0600 from the start, UTF-8, fsynced


_cache: tuple = (None, None)      # R517: ((mtime_ns, size, inode), parsed) of web-chats.json


def view() -> dict:
    """R517: the parsed store, re-read only when the file changed (every sidebar refresh, search and share hit
    used to read and parse it again). A copy, so a caller cannot change the cache."""
    import copy
    global _cache
    with _lock:
        try:
            st = _file().stat()
            sig = (str(_file()), st.st_mtime_ns, st.st_size, st.st_ino)   # a save is a rename: a new inode
        except OSError:
            sig = None
        if sig is not None and _cache[0] == sig:
            return copy.deepcopy(_cache[1])
        d = _load()
        _cache = (sig, d) if sig is not None else (None, None)
        return copy.deepcopy(d)


def hide_chat(sid: str) -> None:
    with _lock:
        d = _load()
        for f in d["folders"]:
            f["chats"] = [c for c in f["chats"] if c != sid]
        if sid not in d["hidden"]:
            d["hidden"].append(sid)
        _save(d)


def set_title(sid: str, name: str) -> None:
    """R385: the sidebar's own name for a chat; an empty name takes it back to the first message."""
    name = " ".join(name.split())[:MAX_NAME]
    with _lock:
        d = _load()
        if name:
            d["titles"][sid] = name
        else:
            d["titles"].pop(sid, None)
        _save(d)


def create(name: str) -> tuple[str | None, str]:
    name = name.strip()[:MAX_NAME]
    if not name:
        return None, "a folder needs a name"
    with _lock:
        d = _load()
        if len(d["folders"]) >= MAX_FOLDERS:
            return None, "too many folders"
        fid = uuid.uuid4().hex[:8]
        d["folders"].insert(0, {"id": fid, "name": name, "open": True, "chats": []})
        _save(d)
    return fid, ""


def _folder(d: dict, fid: str):
    return next((f for f in d["folders"] if f["id"] == fid), None)


def rename(fid: str, name: str) -> tuple[bool, str]:
    name = name.strip()[:MAX_NAME]
    if not name:
        return False, "a folder needs a name"
    with _lock:
        d = _load()
        f = _folder(d, fid)
        if f is None:
            return False, "no such folder"
        f["name"] = name
        _save(d)
    return True, ""


def set_open(fid: str, is_open: bool) -> bool:
    with _lock:
        d = _load()
        f = _folder(d, fid)
        if f is None:
            return False
        f["open"] = is_open
        _save(d)
    return True


def delete(fid: str, keep: set[str]) -> tuple[list[str] | None, str]:
    """Remove the folder and hide its chats, except those in `keep` (the live one). Returns the chats hidden."""
    with _lock:
        d = _load()
        f = _folder(d, fid)
        if f is None:
            return None, "no such folder"
        gone = [c for c in f["chats"] if c not in keep]
        d["folders"] = [x for x in d["folders"] if x["id"] != fid]
        d["hidden"] += [c for c in gone if c not in d["hidden"]]
        _save(d)
    return gone, ""


def move(sid: str, fid: str | None) -> tuple[bool, str]:
    """Put a chat in exactly one folder, or (fid None) back in the plain list."""
    with _lock:
        d = _load()
        target = None
        if fid is not None:
            target = _folder(d, fid)
            if target is None:
                return False, "no such folder"
        for f in d["folders"]:
            f["chats"] = [c for c in f["chats"] if c != sid]
        if target is not None:
            target["chats"].append(sid)
        _save(d)
    return True, ""
