# summary: web-only plain-text notes (user-ordered, R404) for aurora-web (one .txt per note plus a title index in AURORA_HOME/web-notes); never touches session logs.
"""R389: the Notes section. Plain text for now; one file per note so a note can be read or backed up with any tool."""
import json
import os
import re
import threading
import time
import uuid

from ..service import aurora_home

MAX_TITLE = 80
MAX_TEXT = 100_000
MAX_NOTES = 500
_ID = re.compile(r"^[0-9a-f]{12}$")
TYPES = ("personal", "work", "skills")   # R422: the three kinds of note; a note from before R422 is personal
_lock = threading.Lock()


def _dir():
    d = aurora_home() / "web-notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


problem = ""   # R392: the last damaged index found (shown by /health), "" when none


def _index() -> dict:
    """R392 (bug 12): a damaged index.json is moved aside (never written over) and the index is rebuilt from the
    .txt files, so a bad file costs the titles at most, never the notes themselves."""
    global problem
    path = _dir() / "index.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise RuntimeError(f"notes index unreadable: {e}")
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not an object")
    except ValueError:
        aside = path.with_name(f"index.json.corrupt-{int(time.time())}")
        os.replace(path, aside)
        problem = f"web-notes/index.json was damaged: moved to {aside.name}, titles rebuilt from the note files"
        d = {}
        for f in _dir().glob("*.txt"):
            if _ID.match(f.stem):
                text = f.read_text(encoding="utf-8", errors="replace")
                d[f.stem] = {"title": "", "preview": _preview(text), "updated": f.stat().st_mtime}
        _write(path, json.dumps(d, ensure_ascii=False))
    return {k: v for k, v in d.items() if isinstance(k, str) and _ID.match(k) and isinstance(v, dict)}


def _write(path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _title(t: str) -> str:
    return " ".join(str(t).split())[:MAX_TITLE]          # may be empty: the page shows "Untitled"


def _preview(text: str) -> str:
    return " ".join(text.split())[:100]


def _read(nid: str) -> str:
    try:
        return (_dir() / f"{nid}.txt").read_text(encoding="utf-8")
    except OSError:
        return ""


def _ordered(idx: dict) -> bool:
    """R404: every note has a `pos` (its place in the list, smallest first). Notes from before R404 get one in their
    old newest-first order, after any that already have one. Returns True when it changed the index."""
    missing = sorted((k for k, v in idx.items() if not isinstance(v.get("pos"), (int, float))),
                     key=lambda k: idx[k].get("updated", 0), reverse=True)
    if not missing:
        return False
    top = max((v["pos"] for v in idx.values() if isinstance(v.get("pos"), (int, float))), default=-1)
    for n, k in enumerate(missing, 1):
        idx[k]["pos"] = top + n
    return True


def _pos(v) -> float:
    p = v.get("pos")
    return p if isinstance(p, (int, float)) else float("inf")


def _type(v: dict) -> str:
    t = v.get("type")
    return t if t in TYPES else "personal"


def listing(q: str = "", kind: str = "") -> list[dict]:
    """In the user's order (R404; new notes on top), each with a one-line preview; `q` keeps notes whose title or
    text contains it (case-insensitive); `kind` (R422) keeps one type, "" keeps all."""
    q = q.strip().lower()[:100]
    kind = kind if kind in TYPES else ""
    with _lock:
        idx = _index()
        if _ordered(idx):
            _write(_dir() / "index.json", json.dumps(idx, ensure_ascii=False))
        out = []
        for k, v in idx.items():
            if kind and _type(v) != kind:
                continue
            prev = v.get("preview")
            text = None
            if prev is None:                                   # a note saved before previews existed
                text = _read(k)
                prev = _preview(text)
            if q and q not in v.get("title", "").lower():
                if text is None:
                    text = _read(k)
                if q not in text.lower():
                    continue
            out.append({"id": k, "title": v.get("title", ""), "preview": prev, "updated": v.get("updated", 0),
                        "type": _type(v)})
    order = {k: _pos(v) for k, v in idx.items()}
    return sorted(out, key=lambda n: (order[n["id"]], -n["updated"]))


def valid_id(nid) -> bool:
    return isinstance(nid, str) and bool(_ID.match(nid))


def get(nid: str) -> dict | None:
    with _lock:
        meta = _index().get(nid)
        if meta is None:
            return None
        text = _read(nid)
    return {"id": nid, "title": meta.get("title", ""), "text": text, "updated": meta.get("updated", 0),
            "source_session": meta.get("source_session"), "type": _type(meta)}


def create(title: str = "", text: str = "", source_session: str | None = None,
           kind: str = "personal") -> tuple[str | None, str]:
    """R418 (I3): `source_session` is the chat a reply was saved from, so the note can open it again.
    R422: `kind` is one of TYPES."""
    if kind not in TYPES:
        return None, "type: personal, work or skills"
    if len(text) > MAX_TEXT:
        return None, f"a note is limited to {MAX_TEXT} characters"
    with _lock:
        idx = _index()
        if len(idx) >= MAX_NOTES:
            return None, "too many notes"
        nid = uuid.uuid4().hex[:12]
        _write(_dir() / f"{nid}.txt", text)
        _ordered(idx)
        top = min((v["pos"] for v in idx.values()), default=1)
        idx[nid] = {"title": _title(title), "preview": _preview(text), "updated": _now(), "pos": top - 1, "type": kind,
                    **({"source_session": source_session} if source_session else {})}
        _write(_dir() / "index.json", json.dumps(idx, ensure_ascii=False))
    return nid, ""


CONFLICT = "changed on another device"


def _now() -> float:
    return round(time.time(), 3)


def save(nid: str, title: str, text: str, base=None) -> tuple[float | None, str]:
    """Returns (new `updated`, "") or (None, why). R392 (bug 13): with `base` (the `updated` the page loaded),
    a note changed since by another device is refused with CONFLICT instead of being overwritten."""
    if len(text) > MAX_TEXT:
        return None, f"a note is limited to {MAX_TEXT} characters"
    with _lock:
        idx = _index()
        if nid not in idx:
            return None, "no such note"
        if base is not None and abs(float(idx[nid].get("updated", 0)) - float(base)) > 0.0005:
            return None, CONFLICT
        _write(_dir() / f"{nid}.txt", text)
        now = max(_now(), float(idx[nid].get("updated", 0)) + 0.001)
        idx[nid] = {**idx[nid], "title": _title(title), "preview": _preview(text), "updated": now}   # keeps `pos`
        _write(_dir() / "index.json", json.dumps(idx, ensure_ascii=False))
    return now, ""


def set_type(nid: str, kind: str) -> tuple[bool, str]:
    """R422: move a note to another type. Like `pos`, this never changes `updated`, so an open editor
    elsewhere does not see a save conflict."""
    if kind not in TYPES:
        return False, "type: personal, work or skills"
    with _lock:
        idx = _index()
        if nid not in idx:
            return False, "no such note"
        idx[nid]["type"] = kind
        _write(_dir() / "index.json", json.dumps(idx, ensure_ascii=False))
    return True, ""


def reorder(ids) -> tuple[bool, str]:
    """R404: `ids` is the new order of some notes (all of them, or the ones a search shows). They swap places among
    the positions they already hold, so notes not listed keep theirs. Never changes `updated` (no save conflict)."""
    if not isinstance(ids, list) or not ids or len(ids) > MAX_NOTES or not all(valid_id(i) for i in ids):
        return False, "ids: a list of note ids"
    if len(set(ids)) != len(ids):
        return False, "ids: each note once"
    with _lock:
        idx = _index()
        if any(i not in idx for i in ids):
            return False, "no such note"
        _ordered(idx)
        slots = sorted(idx[i]["pos"] for i in ids)
        for i, p in zip(ids, slots):
            idx[i]["pos"] = p
        _write(_dir() / "index.json", json.dumps(idx, ensure_ascii=False))
    return True, ""


def delete(nid: str) -> tuple[bool, str]:
    with _lock:
        idx = _index()
        if nid not in idx:
            return False, "no such note"
        del idx[nid]
        _write(_dir() / "index.json", json.dumps(idx, ensure_ascii=False))
        try:
            (_dir() / f"{nid}.txt").unlink()
        except OSError:
            pass
    return True, ""
