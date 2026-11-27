"""R353: the web Frontend and the one shared session behind the HTTP server.

Single user, single engine: `engine.messages` keeps its one writer (the turn
thread). A /api/send while a turn runs is queued (R373) and runs after it, one turn at a time.
"""

import os
import re
import threading
import time

from .. import service
from ..service import ProviderError
from . import folders

MAX_EVENTS = 20000          # replay buffer; a reconnect asks for what it missed
MAX_TRANSCRIPT = 300_000     # one past session sent to the viewer, chars
MAX_OUTPUT = 20000          # tool output / diff sent to the browser, chars
APPROVE_TIMEOUT_S = 600.0   # no answer = deny (R418 F3: AURORA_WEB_APPROVE_TIMEOUT overrides it, seconds)
ANSWERS = {"y", "n", "s"}   # once / deny / stop the turn: never 'always'
AUTO_CLEAR_RATIO = 0.9      # a turn that ends with the context this full starts the next one fresh
MAX_SENT_IDS = 500                       # R396: remembered outbox ids (a resend window, not a log)
PRICE_REFRESH_GAP_S = 20                 # R397: a second manual refresh within this is refused (the catalog is ~1 MB)
MAX_QUEUE = 20              # messages waiting behind a running turn; more are refused
MAX_QUEUE_IMAGE_BYTES = 48_000_000   # R510: pictures (data URLs) held by waiting messages together; more are refused
MAX_HISTORY = 200           # messages of a resumed session drawn on the page
MAX_DIFF = 200_000          # diff text sent to the page, chars
COMMANDS = ("clear", "compact", "context")   # the only commands the page can run; anything else is refused
MAX_EVENT_BYTES = 32_000_000   # R392 (P7): the replay buffer is capped by size too, not only by count
COALESCE_S = 0.05              # R392 (P1): text/think chunks are sent at most every 50 ms ...
COALESCE_BYTES = 1024          # ... or once 1 KB has gathered
MAX_STREAMS = 20               # R392 (P12): open /api/stream connections; more get 429
# R458 (#13): what a typed /command gets instead of a bare refusal: where its web
# equivalent is, or that it exists only in the terminal
WEB_EQUIVALENT = {"clear": "the Clear item in the ⋯ menu", "compact": "the Compact item in the ⋯ menu",
                  "context": "the Context item in the ⋯ menu", "diff": "the Diff item in the ⋯ menu",
                  "undo": "the Undo item in the ⋯ menu", "model": "the model menu above the message box",
                  "plan": "Plan mode in Settings", "new": "New Chat in the sidebar",
                  "resume": "the chat list in the sidebar", "search": "the search box in the sidebar",
                  "export": "the download button on the chat in the sidebar"}


def typed_command_refusal(text: str) -> str:
    name = text[1:].split(None, 1)[0].lower() if text[1:].strip() else ""
    where = WEB_EQUIVALENT.get(name)
    if where:
        return f"typed commands are not run on the web: for /{name} use {where}"
    return (f"/{name or '…'} works only in the terminal (aurora). On the web: Clear, Compact, Context, "
            "Diff and Undo are in the ⋯ menu, the model menu sits above the message box, "
            "Plan mode is in Settings")


def _vision_memory(new: dict | None = None) -> dict:
    """R412: per model, the last "can it see pictures" answer given while it was awake (AURORA_HOME/web-vision.json)."""
    import json as _json
    from ..service import aurora_home
    f = aurora_home() / "web-vision.json"
    if new is not None:
        try:
            f.write_text(_json.dumps(new), encoding="utf-8")
            os.chmod(f, 0o600)            # R450 (S10)
        except OSError:
            pass
        return new
    try:
        d = _json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


VISION_CACHE_S = 600.0         # R392 (bug 17): a probed "can this model see pictures" answer is kept 10 minutes
# R392: the web's own denylist. It wins even with auto-approve on; the terminal's approvals are untouched.
WEB_DENY_DEFAULT = (r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", r"\bsudo\b", r"\bgit\s+push\b", r"\bmkfs",
                    r"\bdd\s+if=", r"\b(shutdown|reboot|poweroff)\b", r"\b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b",
                    r"\bchmod\s+-R\s+777\b", r":\(\)\s*\{",
                    # R447: what matters most on a host with Docker and ssh keys: the socket, other machines, the secrets
                    r"\bdocker(-compose)?\b", r"docker\.sock", r"\b(ssh|scp|sftp|rsync|nsenter)\b", r"\bsu\b",
                    r"\b(env|printenv|export\s+-p|set)\s*($|[|;&>])", r"/etc/environment", r"/proc/[^\s]*/environ", r"\.ssh\b",
                    r"\bos\.system\b|\bsubprocess\b|\bpty\.spawn\b", r"\b(base64|xxd)\b[^|]*-d[^|]*\|\s*(ba|z)?sh\b",
                    r"\beval\b",
                    # R450 (S4): more ways to run a program, and the policy/code files (S1/S8) a command could rewrite
                    r"\bos\.(popen|exec[a-z]*|spawn[a-z]*)\b|\bpexpect\b", r"\b(perl|ruby|php|lua)\b\s+-[a-z]*e\b",
                    r"\bnode\b\s+-e\b|\bawk\b[^|;]*\bsystem\s*\(", r"[$`(][^)`]*\b(base64|xxd)\b[^)`]*-d",
                    r"web-denylist|\ballowlist\.yaml|\bdenylist\.yaml|\bstate\.yaml|\.aurora/(extensions|bootstrap)")


def web_denylist() -> list[str]:
    """Regexes from AURORA_HOME/web-denylist.txt (one per line, # comments), else WEB_DENY_DEFAULT."""
    try:
        lines = (service.aurora_home() / "web-denylist.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return list(WEB_DENY_DEFAULT)
    pats = [x.strip() for x in lines if x.strip() and not x.lstrip().startswith("#")]
    return pats or list(WEB_DENY_DEFAULT)      # R450 (S1): an empty file never means "deny nothing"


_COMMAND_TOOLS = ("run_command", "wait_until")
_PATH_KEYS = ("path", "file", "filename")


def _strings(v, depth: int = 0):
    """R450 (S4): every string inside nested lists/dicts (MCP tool arguments were not scanned at all)."""
    if isinstance(v, (str, int, float)):
        yield str(v)
    elif depth < 6 and isinstance(v, dict):
        for x in v.values():
            yield from _strings(x, depth + 1)
    elif depth < 6 and isinstance(v, (list, tuple)):
        for x in v:
            yield from _strings(x, depth + 1)


GLOB_BUDGET_S = 0.5           # R469: time the denylist may spend expanding one command's path globs
GLOB_MAX_FRONTIER = 5000      # ... and how many paths one glob level may hold


def _bounded_glob(pattern: str, deadline: float, limit: int = 20) -> tuple[list[str], bool]:
    """R469: glob.iglob's matches (hidden names only for a part that starts with '.'), at most `limit`, giving up at
    `deadline` or when one level holds more than GLOB_MAX_FRONTIER paths. Returns (matches, complete). iglob itself
    walked `/*/*/*/*/*/*/x` for 15 s on this machine, longer for each level, before the approval was even shown."""
    import fnmatch
    import glob
    pattern = os.path.expanduser(pattern)
    bases = ["/"] if pattern.startswith("/") else [""]
    for part in [p for p in pattern.split("/") if p]:
        nxt = []
        for b in bases:
            if time.monotonic() > deadline:
                return [], False
            if not glob.has_magic(part):
                p = os.path.join(b, part)
                if os.path.lexists(p):
                    nxt.append(p)
                continue
            try:
                with os.scandir(b or ".") as it:
                    names = [e.name for e in it]
            except OSError:
                continue
            nxt.extend(os.path.join(b, n) for n in names
                       if fnmatch.fnmatch(n, part) and (part.startswith(".") or not n.startswith(".")))
            if len(nxt) > GLOB_MAX_FRONTIER:
                return [], False
        bases = nxt
        if not bases:
            break
    return bases[:limit], True


def _glob_expanded(text: str, budget_s: float | None = None) -> tuple[str, bool]:
    """R450 (S4): `cat ~/.s?h/id_ed25519` or `/usr/bin/doc?er` — what a path glob in the command really names.
    R469: bounded (GLOB_BUDGET_S for the whole command); returns (paths, complete)."""
    import itertools
    import shlex
    try:
        toks = shlex.split(text)
    except ValueError:
        toks = text.split()
    deadline = time.monotonic() + (GLOB_BUDGET_S if budget_s is None else budget_s)
    out, complete = [], True
    for t in itertools.islice((t for t in toks if "/" in t and any(c in t for c in "*?[")), 200):
        got, done = _bounded_glob(t, deadline)
        out.extend(got)
        if not done:
            complete = False
            break
    return "\n".join(out), complete


_SEGMENT = re.compile(r"&&|\|\||[;&|\n()]|\$\(|`")
_REDIRECT = re.compile(r"^\d*[<>]+&?")


def _resolved_paths(command: str, start: str) -> str:
    """R473: every word of `command` as a path from the directory it would really be read in: `cd /etc && cat
    environment` names /etc/environment, and so does `cat environment` run with cwd /etc, or `cat /etc/./environment`.
    The denylist matched only the words as typed. `cd`/`pushd` move the directory for the rest of the command;
    `cd` with no argument (or `~`) goes home; a word that is a $VARIABLE leaves the directory unknown."""
    import shlex
    home = os.path.expanduser("~")
    cur: str | None = os.path.normpath(start) if start and os.path.isabs(start) else None
    out: list[str] = []
    for seg in _SEGMENT.split(command)[:400]:
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        words = [_REDIRECT.sub("", w) for w in words]
        words = [w for w in words if w]
        if not words:
            continue
        if words[0] in ("cd", "pushd"):
            arg = next((w for w in words[1:] if not w.startswith("-")), "~")
            if "$" in arg or "`" in arg:
                cur = None
            elif arg.startswith("~"):
                cur = os.path.normpath(os.path.expanduser(arg))
            elif os.path.isabs(arg):
                cur = os.path.normpath(arg)
            elif cur is not None:
                cur = os.path.normpath(os.path.join(cur, arg))
            continue
        for w in words[1:]:
            w = w.split("=", 1)[1] if w.startswith("-") and "=" in w else w
            if not w or w.startswith("-") or "$" in w:
                continue
            if w.startswith("~"):
                w = os.path.expanduser(w)
            if os.path.isabs(w):
                out.append(os.path.normpath(w).replace("//", "/"))
            elif cur is not None:
                out.append(os.path.normpath(os.path.join(cur, w)))
            elif w.startswith(".."):
                out.append(os.path.normpath(os.path.join(home, w)))   # unknown start: still catch ../../etc/x
    return "\n".join(out[:2000])


def web_denied(tool: str, args: dict, patterns: list[str] | None = None) -> str:
    """The first web-denylist pattern the call matches, or "" (see web_check)."""
    return web_check(tool, args, patterns)[0]


def web_check(tool: str, args: dict, patterns: list[str] | None = None) -> tuple[str, bool]:
    """(the first web-denylist pattern the call matches or "", checked). R469: `checked` is False when a path glob
    in the command was too broad to expand in time: the call is then put to the page, never auto-approved.

    R450 (B1): the command patterns are matched against what is RUN: every string inside the arguments of a command
    tool (`run_command`, `wait_until`) or an MCP tool, nested ones included. A file tool is matched only on its path,
    so writing a README that says `docker`, or code with `import subprocess`, is no longer refused."""
    args = args or {}
    if tool in _COMMAND_TOOLS or tool.startswith("mcp_"):
        text = "\n".join(_strings(args))
    else:
        text = "\n".join(str(args[k]) for k in _PATH_KEYS if isinstance(args.get(k), str))
    if not text:
        return "", True
    # R447: also match with quotes and backslashes removed, so su""do / s\\udo / 'rm' -rf cannot step round a pattern
    plain = re.sub(r"[\"'\\]", "", text)
    variants, checked = [text, plain], True
    if tool in _COMMAND_TOOLS:
        ex, checked = _glob_expanded(plain)
        if ex:
            variants.append(ex)
        # R473: the words as paths from where the command really runs (its `cwd` argument, then each `cd`)
        cwd = args.get("cwd") if isinstance(args.get("cwd"), str) else ""
        start = os.path.join(os.getcwd(), os.path.expanduser(cwd)) if cwd else os.getcwd()
        cmd = args.get("command") if isinstance(args.get("command"), str) else ""
        rp = _resolved_paths(re.sub(r"[\"'\\]", "", cmd), start) if cmd else ""
        if rp:
            variants.append(rp)
    for pat in (web_denylist() if patterns is None else patterns):
        try:
            if any(re.search(pat, v, re.I) for v in variants):
                return pat, checked
        except re.error:
            continue
    return "", checked


def _ev_size(ev: dict) -> int:
    return 64 + sum(len(v) for v in ev.values() if isinstance(v, str)) + sum(
        len(str(v)) for v in ev.values() if isinstance(v, (dict, list)))


_NOTE_BLOCK = re.compile(r"\[(Note|File): ([^\]\n]*)\]\n.*?\n\[End of (?:note|file)\]\n\n", re.S)


def _block(n: dict) -> str:
    """R401 note / R405 file, as the model sees it in front of the message (a file's text goes in raw)."""
    kind = "File" if n.get("kind") == "file" else "Note"
    return f"[{kind}: {n['title'] or 'Untitled'}]\n{n['text']}\n[End of {kind.lower()}]\n\n"


def _chips_line(found: list[tuple[str, str]]) -> str:
    """What the chat shows instead of the blocks, from (kind, title) pairs: `📝 note titles · 📎 file names`."""
    notes = [t or "Untitled" for k, t in found if k == "Note"]
    files = [t for k, t in found if k == "File"]
    return " · ".join(p for p in (notes and "📝 " + ", ".join(notes), files and "📎 " + ", ".join(files)) if p)


def _split_notes(text: str) -> tuple[str, str, str]:
    """R401/R405: a message sent with notes or files starts with `[Note: t] … [End of note]` / `[File: n] … [End of file]`
    blocks. Returns (the blocks, the chips line, the message after them); ("", "", text) when it has none."""
    pos, found = 0, []
    while (m := _NOTE_BLOCK.match(text, pos)):
        found.append((m.group(1), m.group(2))); pos = m.end()
    return (text[:pos], _chips_line(found), text[pos:]) if found else ("", "", text)


class WebSession:
    def __init__(self, engine, approve_timeout: float | None = None, auto_approve: bool | None = None):
        self.engine = engine
        service.set_web_guard(True)                       # R450: file tools refuse secrets and Aurora's own policy files
        if approve_timeout is None:
            try:
                approve_timeout = float(os.environ.get("AURORA_WEB_APPROVE_TIMEOUT", "") or APPROVE_TIMEOUT_S)
            except ValueError:
                approve_timeout = APPROVE_TIMEOUT_S
        self.approve_timeout = approve_timeout
        self.cond = threading.Condition()
        self.events: list[dict] = []
        self._sizes: list[int] = []                # R416: byte estimate of each kept event, same order as events
        self._cleared = False                      # R415 (B9): the last turn's auto-clear emptied the history
        self._bytes = 0
        self.boot = str(int(time.time() * 1000))   # R392 (bug 11): ids restart with the process; the page compares this
        self.streams = 0                           # open /api/stream connections
        self.last_activity = 0.0                   # time of the last user/text/done event (bug 9)
        self._vision_cache: dict = {}
        self._next_id = 1
        self._last_reset = 0                   # id of the newest reset: a fresh page replays from there, not from event 1
        self.busy = False
        self.busy_client = None                # R458: client_id of the message the running turn answers
        self._link_notes: list[dict] = []      # R460: notes a page link loaded into this chat ({id, title, text})
        from . import memory
        memory.install()                       # R462: the `remember` tool (writes the Memory note)
        self._price_refreshing = False         # R397: one manual refresh at a time
        self._price_refreshed = 0.0
        self._sent_ids: dict[str, None] = {}        # R396: outbox ids already accepted, oldest first
        self._queue: list[tuple] = []          # R373: (text, images, shown, temperature, client_id) sent while a turn runs, oldest first
        self._stop = False
        self._approval: dict | None = None     # {"id", "answer"}
        self._approval_seq = 0
        if auto_approve is None:                # R392 (bug 15): the page's switch survives a restart (state.yaml)
            # R474: until the switch is turned on, every write and command asks the page first (it used to start on:
            # anyone holding the token, or a prompt injection in a fetched page, ran commands with no one asked)
            try:
                auto_approve = service.load_state().get("web_auto_approve", False) is True
            except Exception:
                auto_approve = False
        self.auto = auto_approve                # R357/R392: the web's own auto-approve; the web denylist wins over it
        engine.auto_approve = False             # the engine always asks the page, which applies the web policy
        engine.headless = True                  # keep max_iterations binding, no steering
        self.emit("session", session=engine.session.id)

    # ── event log ───────────────────────────────────────────────────────
    def emit(self, type_: str, **data) -> int:
        size = _ev_size(data) + len(type_)     # R416 (P-C): measured once, outside the lock the streams wait on
        with self.cond:
            ev = {"id": self._next_id, "type": type_, "t": round(time.time(), 2), **data}
            self._next_id += 1
            self.events.append(ev)
            self._sizes.append(size)
            self._bytes += size
            if type_ == "reset":
                self._last_reset = ev["id"]
            if type_ in ("user", "text", "done", "tool_start"):
                self.last_activity = ev["t"]
            # R573: trimmed in batches (to 7/8 of a cap) once a cap is passed; at the cap it used to shift the whole
            # 20,000-event list (and sum a slice of sizes) for every streamed chunk
            drop = 0
            if len(self.events) > MAX_EVENTS or self._bytes > MAX_EVENT_BYTES:
                drop = max(0, len(self.events) - MAX_EVENTS * 7 // 8)
                gone = sum(self._sizes[:drop])
                while drop < len(self.events) - 1 and self._bytes - gone > MAX_EVENT_BYTES * 7 // 8:
                    gone += self._sizes[drop]
                    drop += 1
            if drop:
                self._bytes -= gone
                del self.events[:drop]
                del self._sizes[:drop]
            self.cond.notify_all()
            return ev["id"]

    def _fresh_start(self) -> int:
        """Where a fresh page starts: the newest reset, or (bug 7) when that was trimmed away, the first
        user message still kept, so a page never opens partway through a reply."""
        first = self.events[0]["id"] if self.events else 1
        if self._last_reset >= first:
            return self._last_reset - 1
        if first == 1:                         # nothing trimmed yet: the whole log
            return 0
        k = next((e["id"] for e in self.events if e["type"] in ("user", "history", "reset")), None)
        return (k - 1) if k is not None else self._next_id - 1

    def wait_events(self, after: int, timeout: float, boot: str | None = None) -> list[dict]:
        """Events after `after`. Ids are contiguous, so the start is found by arithmetic, not a scan.
        after=0 (a fresh page) starts at the newest reset. R392 (bug 11/P10): a page from another server
        run (its `boot` differs, or its `after` is beyond the newest id) gets a `restart` marker and the
        log from the newest reset, never a splice of the new log onto the old one."""
        with self.cond:
            last = self._next_id - 1
            if after > last or (after and boot is not None and boot != self.boot):
                start = self._fresh_start()
                first = self.events[0]["id"] if self.events else 1
                return [{"id": 0, "type": "restart", "boot": self.boot, "t": round(time.time(), 2)}] + self.events[max(0, start + 1 - first):]
            if after == 0:
                after = self._fresh_start()
            if after >= last:
                self.cond.wait(timeout)
            first = self.events[0]["id"] if self.events else 1
            return self.events[max(0, after + 1 - first):]

    # ── state ───────────────────────────────────────────────────────────
    def state(self) -> dict:
        model = self.engine.current.get("model", "")
        try:
            st = self.engine.context_stats()
            ctx = {"used": st.used, "limit": st.limit, "live_model": st.local_model, "compactions": getattr(st, "compactions", 0)}
        except Exception:
            ctx = {}
        with self.cond:
            pend = self._approval["id"] if self._approval else None
        return {"busy": self.busy, "busy_client": self.busy_client or "", "model": model, "provider": self.engine.current.get("provider", ""), "context": ctx, "loaded": bool(ctx.get("live_model")), "auto_approve": bool(self.auto), "sandbox": service.sandbox_status(), "context_file": _context_file(),
                "pending_approval": pend, "last_event": self._next_id - 1, "boot": self.boot,
                "plan_mode": bool(getattr(self.engine, "plan_mode", False)), "streams": self.streams,
                "stt": bool(os.environ.get("AURORA_WEB_STT_URL")),
                "idle_s": round(time.time() - self.last_activity, 1) if self.last_activity else None,
                "session_id": self.engine.session.id, "chat_empty": not self.engine.messages, "version": _code_version(), "queued": len(self._queue), "vision": self.vision()}

    def search(self, query: str, limit: int = 30) -> list[dict]:
        """R377: sessions whose log contains `query` (case-insensitive), newest first, one snippet each."""
        query = query.strip()
        if len(query) < 2:
            return []
        cur = self.engine.session.id
        v = folders.view()
        hidden, titles = set(v["hidden"]), v["titles"]
        return [{"id": sid, "short": sid[:6], "when": when, "ts": service.session_mtime(sid), "first": titles.get(sid, snip), "current": sid == cur,
                 "prompts": service.count_prompts(sid), "renamed": sid in titles, "snippet": snip, "hits": hits, "capped": capped}
                for sid, when, _ev, snip, hits, capped in service.search_sessions(query[:200], limit + len(hidden), counts=True)
                if sid not in hidden][:limit]

    def sessions(self, limit: int = 15) -> list[dict]:
        """Recent sessions, newest first, read-only: the short id, when, and the first message.
        R383: hidden chats are left out; each carries its folder id (or None)."""
        cur = self.engine.session.id
        v = folders.view()
        hidden = set(v["hidden"])
        where = {c: f["id"] for f in v["folders"] for c in f["chats"]}
        rows = [(sid, when, first) for sid, when, first in service.list_sessions(limit + len(hidden) + len(where))
                if sid not in hidden]
        loose = [r for r in rows if r[0] not in where][:limit]
        kept = [r for r in rows if r[0] in where]
        titles = v["titles"]
        return [{"id": sid, "short": sid[:6], "when": when, "ts": service.session_mtime(sid), "first": titles.get(sid, first), "current": sid == cur,
                 "folder": where.get(sid), "prompts": service.count_prompts(sid), "renamed": sid in titles}
                for sid, when, first in sorted(loose + kept, key=lambda r: r[1], reverse=True)]

    def folders(self) -> list[dict]:
        return [{"id": f["id"], "name": f["name"], "open": f["open"]} for f in folders.view()["folders"]]

    def delete_chat(self, sid: str) -> tuple[bool, str]:
        """R383: hide a chat from the web list; the log stays on disk for the TUI. The live chat is refused."""
        if not service.session_exists(sid):
            return False, "no such session"
        if sid == self.engine.session.id:
            return False, "that chat is open: start a new one first"
        folders.hide_chat(sid)
        from . import shares
        shares.delete_session([sid])           # R466: a deleted chat's share link would stay live, with no way to revoke it
        return True, ""

    def rename_chat(self, sid: str, name: str) -> tuple[bool, str]:
        if not service.session_exists(sid):
            return False, "no such session"
        folders.set_title(sid, name)
        return True, ""

    def move_chat(self, sid: str, fid: str | None) -> tuple[bool, str]:
        """R395: only a real session goes into a folder (delete and rename already checked; move took any text)."""
        if not service.session_exists(sid):
            return False, "no such session"
        return folders.move(sid, fid)

    def delete_folder(self, fid: str) -> tuple[bool, str]:
        gone, why = folders.delete(fid, {self.engine.session.id})
        if gone:
            from . import shares
            shares.delete_session(gone)         # R466: the folder's chats are hidden: their links go too
        return (gone is not None), why

    def session_markdown(self, sid: str) -> str | None:
        """Read-only transcript of one past session, or None for an id that is not an exact session id
        (so a crafted id can never reach the filesystem). Capped; the cut is said, not silent."""
        if not service.session_exists(sid):
            return None
        md = service.export_markdown(sid, MAX_TRANSCRIPT)     # R465: stops reading past the cap
        if sid == self.engine.session.id and self.busy:   # R415 (B7): the live chat, read while a turn appends to it
            md += "\n\n[… this chat is in progress: the reply being written is not shown yet]"
        return md if len(md) <= MAX_TRANSCRIPT else md[:MAX_TRANSCRIPT] + f"\n\n[… transcript cut at {MAX_TRANSCRIPT} characters]"

    # ── actions ─────────────────────────────────────────────────────────
    def vision(self, probe: bool = False) -> bool:
        """R381/R382: the model entry says `vision: true`, or (probe=True, never from the status poll: it can wake a
        sleeping model) the provider's server reports the `multimodal` capability, as llama-server does with --mmproj."""
        if self.engine.current.get("vision"):
            return True
        key = (self.engine.current.get("provider"), self.engine.current.get("model"))
        hit = self._vision_cache.get(key)
        if hit and time.monotonic() - hit[0] < VISION_CACHE_S:
            return hit[1]
        if not probe:
            return False
        try:
            prov = self.engine._provider_for(self.engine.current)
            ok = getattr(prov, "serves_vision", lambda: False)()
        except Exception:
            return False
        known = _vision_memory()
        mkey = f"{key[0]}/{key[1]}"
        if ok is None:      # R412: the model is asleep; use what it said when awake, and let the picture through when never asked
            return known.get(mkey, True)
        ok = bool(ok)
        self._vision_cache[key] = (time.monotonic(), ok)
        if known.get(mkey) is not ok:
            known[mkey] = ok
            _vision_memory(known)
        return ok

    def _outputs_rule(self) -> None:
        """R413: the system prompt tells the agent where files made for the user go, so the page can show them;
        re-added before each message because a reset restores the bare system prompt."""
        from . import attach
        try:
            d = attach.outputs_dir()
        except OSError:
            return
        rule = (f"\n\nWeb chat: save every file you create for the user (pictures, documents, exports) in {d}/ "
                "and write its full path in your reply; the chat shows pictures from there inline with a Download button.")
        if rule not in (self.engine.system or ""):
            self.engine.system = (self.engine.system or "") + rule

    def _context_rule(self) -> None:
        """R460: the notes a page link (`/?note=<id>`, see `load_note`) loaded into this chat go into the system
        prompt, in one <context-notes> block. Re-applied before every turn because a reset (auto-clear) or a resume
        starts from the bare system prompt. Runs on the turn thread or holding the claim: the one writer."""
        system = self.engine.system or ""
        if CTX_START in system and CTX_END in system:
            a = system.index(CTX_START)
            system = system[:a] + system[system.index(CTX_END, a) + len(CTX_END):]
        if self._link_notes:
            system += (f"{CTX_START}The user loaded these notes as background for this chat. Use them when they help; "
                       "do not repeat them back unless asked.\n\n" + "\n\n".join(_ctx_part(n) for n in self._link_notes)
                       + CTX_END)
        self.engine.system = system

    def _restore_context_notes(self) -> None:
        """R460: a resumed chat gets back the notes a link loaded into it (from its log)."""
        got: dict[str, dict] = {}
        for r in self.engine.session.iter_records(events={"context_note"}):
            if isinstance(r.get("id"), str) and isinstance(r.get("text"), str):
                got.pop(r["id"], None)
                got[r["id"]] = {"id": r["id"], "title": str(r.get("title", "")), "text": r["text"]}
        self._link_notes = list(got.values())
        self._context_rule()

    def load_note(self, nid: str, new_chat: bool) -> tuple[bool, str]:
        """R460: a page opened as `/?note=<id>` loads that note into a chat at once, before any message:
        into a new chat, or added to the current one. Kept in the session log, so a resumed chat has it again."""
        from . import notes
        n = notes.get(nid) if notes.valid_id(nid) else None
        if n is None:
            return False, "no such note"
        if new_chat:
            ok, why = self.new_chat()
            if not ok:
                return False, why
        if not self._claim():
            return False, "a turn is running \u2014 stop it first"
        try:
            snap = {"id": n["id"], "title": n["title"], "text": n["text"][:NOTE_CTX_MAX]}
            self._link_notes = [x for x in self._link_notes if x["id"] != snap["id"]] + [snap]
            self._context_rule()
            try:
                self.engine.session.log("context_note", **snap)
            except Exception:
                pass
        finally:
            self._release()
        self.emit("notify", text=f"note \u201c{n['title'] or 'Untitled'}\u201d loaded as context: Aurora has it from your next message")
        return True, ""

    def send(self, text: str, images: list | None = None, client_id: str | None = None,
             notes: list | None = None, temperature: float | None = None) -> tuple[bool, str]:
        """R396: `client_id` (the page's outbox id) makes a resend after a dropped connection a no-op:
        the same id is accepted once; a repeat answers ok without running the message again.
        R401: `notes` ({title, text} dicts) go to the model in front of the message, for this message only;
        the chat shows just their titles, so a long note does not flood the log."""
        if client_id:
            with self.cond:
                if client_id in self._sent_ids:
                    return True, "duplicate"
        images = images or []
        text = text.strip()
        if images and not self.vision(probe=True):
            if "[Attached picture saved at: " not in text:
                return False, f"the current model ({self.engine.current.get('model', '')}) cannot see pictures"
            images = []     # R412: it cannot see them, but its tools can open the saved files
            text += "\n(the current model cannot see pictures: use the saved files above)"
        if images and not text:
            text = "(see the attached picture" + ("s)" if len(images) > 1 else ")")
        notes = notes or []      # R401 notes and R405 files ({title, text, kind: "file"}), in that order
        if notes and not text:
            files = all(n.get("kind") == "file" for n in notes)
            text = "(see the attached " + ("file" if files else "note") + ("s)" if len(notes) > 1 else ")")
        if not text:
            return False, "empty message"
        if text.startswith("/") and not service.starts_with_path(text):     # R453: `/etc/hosts …` is a message
            return False, typed_command_refusal(text)
        self._outputs_rule()
        shown = text
        if notes:
            why = self._notes_fit(notes)
            if why:
                return False, why
            shown = _chips_line([("File" if n.get("kind") == "file" else "Note", n["title"]) for n in notes]) + "\n" + text
            text = "".join(_block(n) for n in notes) + text
        with self.cond:
            if client_id and client_id in self._sent_ids:   # R415 (bug 1): checked again with the accept, so two
                return True, "duplicate"                    # resends racing in can never both run
            if self.busy:
                if len(self._queue) >= MAX_QUEUE:
                    return False, f"the queue is full ({MAX_QUEUE} messages): wait or stop"
                held = sum(len(u) for q in self._queue for u in q[1])
                if images and held + sum(len(u) for u in images) > MAX_QUEUE_IMAGE_BYTES:
                    return False, "the queue is full (pictures waiting take too much room): wait for the reply, then send"
                self._queue.append((text, images, shown, temperature, client_id))
                pos = len(self._queue)
            else:
                pos = 0
                self.busy = True
                self._stop = False
                self._cleared = False
            if client_id:
                self._sent_ids[client_id] = None
                while len(self._sent_ids) > MAX_SENT_IDS:
                    self._sent_ids.pop(next(iter(self._sent_ids)))
        if pos:
            self.emit("queued", text=shown, position=pos, images=len(images))
            return True, ""
        self.busy_client = client_id     # R458 (#22): which page started the running turn
        self.emit("user", text=shown, images=len(images), client=client_id or "")
        threading.Thread(target=self._turn, args=(text, images, temperature), daemon=True).start()
        return True, ""

    def _start_queued(self, nxt: tuple) -> None:
        self.busy_client = nxt[4] if len(nxt) > 4 else None
        self.emit("user", text=nxt[2], images=len(nxt[1]), client=self.busy_client or "")
        threading.Thread(target=self._turn, args=(nxt[0], nxt[1], nxt[3]), daemon=True).start()

    def cancel_queued(self, position, text) -> tuple[bool, str]:
        """R418 (I2): drop one waiting message. `text` must match what the page shows at that place, so a queue
        that moved on meanwhile never loses the wrong message."""
        if isinstance(position, bool) or not isinstance(position, int) or not isinstance(text, str):
            return False, "position (int) and text required"
        with self.cond:
            if not 1 <= position <= len(self._queue) or self._queue[position - 1][2] != text:
                return False, "that message is no longer waiting"
            del self._queue[position - 1]
        self.emit("queue_cancel", position=position, text=text)
        return True, ""

    def _notes_fit(self, notes: list) -> str:
        """R401: refuse notes that would not fit in what is left of the context, rather than send them cut."""
        try:
            st = self.engine.context_stats()
            used, limit = int(st.used or 0), int(st.limit or 0)
        except Exception:
            return ""
        if not limit:
            return ""
        need = sum(service.estimate_tokens(n["text"]) for n in notes)
        room = int(limit * 0.9) - used
        if need > room:
            return f"the attached notes/files need about {need} tokens but only {max(room, 0)} are left in the context: compact, start a new chat or attach less"
        return ""

    def _finish(self, turn: dict | None = None) -> None:
        """A turn (or a compact) ended: announce it, then start the oldest queued message or go idle."""
        with self.cond:
            nxt = self._queue.pop(0) if self._queue else None   # Stop already emptied the queue; later sends still run
            if nxt is None:
                self.busy = False
            else:
                self._stop = False             # R392 (bug 1): a Stop belonged to the turn that just ended
        # R392 (bug 16): `done` goes out first; the model probe (up to seconds when the server sleeps) runs after it
        self.emit("done", **{k: v for k, v in self.state().items() if k in ("model", "context")}, **({"turn": turn} if turn else {}))
        if nxt is not None:
            self._start_queued(nxt)
        threading.Thread(target=self._refresh_after, daemon=True).start()

    def _refresh_after(self) -> None:
        try:
            self.engine.refresh_live_state()   # R384: the turn may have just woken the model
        except Exception:
            return
        self.emit("state", **{k: v for k, v in self.state().items() if k in ("model", "context", "loaded")})

    def _turn(self, text: str, images: list | None = None, temperature: float | None = None) -> None:
        fe = WebFrontend(self)
        service.set_secret_prompter(fe.ask_secret)
        t0 = time.monotonic()
        # R418 (F5): the page's temperature, for this turn only; never written to the model entry or state.yaml
        self.engine.turn_overrides = {"temperature": temperature} if temperature is not None else {}
        try:
            self._context_rule()
            self.engine.send(text, fe, images=images)
        except ProviderError as e:
            fe.notify(f"provider error: {e}")
        except Exception as e:
            fe.notify(f"error: {type(e).__name__}: {e}")
        finally:
            self.engine.turn_overrides = {}
            fe.flush()
            fe.check_outputs()
            turn = self._turn_summary(fe, time.monotonic() - t0)
            self._auto_clear()
            self._finish(turn)

    def _turn_summary(self, fe, secs: float) -> dict:
        """R418 (F2): what this one turn cost: tokens in/out over all its model calls, dollars, seconds."""
        out = {"input": fe.tin, "output": fe.tout, "secs": round(secs, 1), "cost": None}
        if fe.tin or fe.tout:
            try:
                prov = self.engine._provider_for(self.engine.current)
                out["cost"] = round(float(prov.cost(self.engine.current.get("model", ""), fe.tin, fe.tout, 0)), 6)
            except Exception:
                pass
        return out

    def _auto_clear(self) -> None:
        """A full context makes every later turn slower and worse, so a turn that ends at or above
        AUTO_CLEAR_RATIO wipes the engine's history. The page keeps showing the chat; a note says why."""
        try:
            st = self.engine.context_stats()
            if not (st.limit and st.used / st.limit >= AUTO_CLEAR_RATIO):
                return
            self.engine.reset()
        except Exception:
            return
        self._cleared = True
        self.emit("notify", text=f"context was {round(100 * st.used / st.limit)}% full ({st.used} / {st.limit}): "
                                 "history cleared, the next message starts fresh")

    def stop(self) -> None:
        with self.cond:
            self._stop = True
            dropped, self._queue = len(self._queue), []
        if dropped:
            self.emit("queue_cleared", count=dropped)
        self.answer(None, "s")

    def answer(self, approval_id, answer: str) -> bool:
        """Settle the pending approval. `approval_id` None = whatever is pending (stop)."""
        if answer not in ANSWERS:
            return False
        with self.cond:
            a = self._approval
            if a is None or (approval_id is not None and a["id"] != approval_id):
                return False
            a["answer"] = answer
            self.cond.notify_all()
            return True

    def set_auto(self, on: bool) -> None:
        self.auto = on
        try:                                   # R392 (bug 15): remembered for the web page only
            service.save_state(web_auto_approve=on)
        except Exception:
            pass
        self.emit("notify", text="auto-approve " + ("ON: tool calls run without asking (the web denylist still blocks)" if on else "OFF: you will be asked before any write or command"))

    def set_plan(self, on: bool) -> None:
        """R392: the engine's plan mode (R339) from the page; nothing that changes state runs while it is on."""
        self.engine.plan_mode = on
        self.emit("plan", on=on)
        self.emit("notify", text="plan mode " + ("ON: the model plans, nothing that changes files or runs commands is executed" if on else "OFF"))

    def usage(self) -> dict:
        """R392: per-model tokens and cost for the live session, plus the budget caps (the terminal's /cost, /budget)."""
        rows = service.usage_by_model(self.engine.session.id)
        out = []
        for m, r in rows.items():
            cost = None
            try:
                entry = next((x for x in self.engine.models if x.get("model") == m), None)
                if entry is not None:
                    prov = self.engine._provider_for(entry)
                    cost = round(float(prov.cost(m, r.get("billed", r.get("input", 0)), r.get("output", 0), r.get("cached", 0))), 4)
            except Exception:
                cost = None
            out.append({"model": m, "turns": r.get("turns", 0), "input": r.get("billed", r.get("input", 0)),
                        "output": r.get("output", 0), "cached": r.get("cached", 0), "cost": cost})
        try:
            today = round(float(self.engine.spent_today()), 4)
        except Exception:
            today = None
        return {"models": out, "session_cost": round(float(getattr(self.engine, "_cost", 0.0)), 4), "today": today,
                "session_budget": float(getattr(self.engine, "session_budget", 0) or 0),
                "daily_budget": float(getattr(self.engine, "daily_budget", 0) or 0)}

    def set_budget(self, which: str, usd) -> tuple[bool, str]:
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not 0 <= usd <= 10_000:   # R518: NaN fails too
            return False, "budget must be a number of dollars, 0 = no limit"
        if which == "session":
            self.engine.set_session_budget(float(usd))
        elif which == "daily":
            self.engine.set_daily_budget(float(usd))
        else:
            return False, "which must be session or daily"
        self.emit("notify", text=f"{which} budget: " + (f"${usd:.2f}" if usd else "no limit"))
        return True, ""

    def _claim(self) -> bool:
        """Take the turn slot for a state change that must not overlap a turn (bug 8)."""
        with self.cond:
            if self.busy:
                return False
            self.busy = True
            return True

    def _release(self) -> None:
        """R415 (bug 5): a message sent while a model switch, resume, undo or new chat held the slot was queued;
        it starts now instead of waiting behind the next turn."""
        with self.cond:
            nxt = self._queue.pop(0) if self._queue else None
            if nxt is None:
                self.busy = False
            else:
                self._stop = False
        if nxt is not None:
            self._start_queued(nxt)

    def retry(self, text: str | None = None, temperature: float | None = None) -> tuple[bool, str]:
        """R392: regenerate the last reply (text None) or replace the last message with `text` and resend it.
        The engine's history is cut back to before that message; files a tool changed are not reverted (Undo does that)."""
        if text is not None:
            text = text.strip()
            if not text:
                return False, "empty message"
            if text.startswith("/") and not service.starts_with_path(text):
                return False, typed_command_refusal(text)
        if not self._claim():
            return False, "a turn is running — stop it first"
        msgs = self.engine.messages
        k = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"
                  and isinstance(msgs[i].get("content"), (str, list))), None)
        if k is None:
            self._release()
            if self._cleared:
                return False, "the history was cleared when the context filled up: send the message again"
            return False, "nothing to retry yet"
        images = list(msgs[k].get("images") or [])       # R415 (bug 3): a picture message is retried with its pictures
        old = msgs[k].get("content")
        old_text = old if isinstance(old, str) else " ".join(p.get("text", "") for p in old if isinstance(p, dict))
        prefix, names, body = _split_notes(old_text)       # R401/R405: an edit keeps the notes and files the message carried
        if text is None:
            text = old_text
        elif prefix:
            line = ""
            if text.startswith(("📝 ", "📎 ")):              # the page edits what it showed: the titles line comes first
                line, _, text = text.partition("\n")
                text = text.strip()
            # R415 (B6): keep only the notes/files still named in that line; deleting the line detaches them all
            prefix = "".join(m.group(0) for m in _NOTE_BLOCK.finditer(prefix) if line and (m.group(2) or "Untitled") in line)
            text = prefix + (text or body)
        _, names, rest = _split_notes(text)
        shown = f"{names}\n{rest}" if names else text
        del msgs[k:]
        try:
            self.engine.session.log("web_retry", edited=text != old)
        except Exception:
            pass
        with self.cond:
            self._stop = False
        kept = len(list(_NOTE_BLOCK.finditer(_split_notes(text)[0])))
        self.emit("retry", edited=text != old, images=len(images), notes=kept)   # R418 (I4): says what went again
        self.emit("user", text=shown, images=len(images))
        threading.Thread(target=self._turn, args=(text, images, temperature), daemon=True).start()
        return True, ""

    # ── R427: config.yaml / config.local.yaml as raw text (secrets hidden), validated before it is saved ──
    def _config_paths(self) -> dict:
        return {"config.yaml": self._cfg_path(), "config.local.yaml": service.aurora_home() / "config.local.yaml"}

    def config_files(self) -> dict:
        out = []
        for name, p in self._config_paths().items():
            try:
                text = p.read_text(encoding="utf-8") if p.is_file() else None
            except OSError as e:
                return {"error": f"{name}: {e}"}
            if text is None and name != "config.yaml":
                continue
            out.append({"name": name, "path": str(p), "text": service.config_redact(text or ""), "base": service.config_digest(text or "")})
        return {"files": out}

    def _merged_with(self, name: str, text: str) -> dict:
        paths = self._config_paths()
        main = text if name == "config.yaml" else paths["config.yaml"].read_text(encoding="utf-8")
        over = text if name == "config.local.yaml" else (
            paths["config.local.yaml"].read_text(encoding="utf-8") if paths["config.local.yaml"].is_file() else "")
        base = service.config_parse(main, "config.yaml")
        o = service.config_parse(over, "config.local.yaml") if over.strip() else {}
        return service.config_merge(base, o) if o else base

    def config_check(self, name: str, text: str, base: str | None, save: bool) -> tuple[int, dict]:
        import yaml
        paths = self._config_paths()
        if name not in paths:
            return 404, {"error": "config.yaml or config.local.yaml"}
        if not isinstance(text, str) or len(text) > 500_000:
            return 400, {"error": "text: the file, at most 500 kB"}
        path = paths[name]
        with self.cond:
            try:
                cur_text = path.read_text(encoding="utf-8") if path.is_file() else ""
                if base is not None and base != service.config_digest(cur_text):
                    return 409, {"error": f"{name} changed on disk since you opened it; Revert to load it again"}
                full = service.config_restore(text, cur_text)
                merged = self._merged_with(name, full)
            except service.ConfigError as e:
                return 400, {"error": str(e), "errors": [str(e)], "line": e.line, "column": e.column}
            except (OSError, yaml.YAMLError) as e:
                return 500, {"error": str(e)}
            cur = self.engine.current
            errs = service.config_check(merged, (cur.get("provider"), cur.get("model")) if cur else None)
            if errs:
                return 400, {"error": errs[0], "errors": errs}
            if not save:
                return 200, {"ok": True, "valid": True}
            if self.busy:
                return 409, {"error": "wait for the reply to finish"}
            try:
                bak = service.config_backup_write(path, full) if full != cur_text else None
            except OSError as e:
                return 500, {"error": f"{name}: {e}"}
            restart = merged.get("runtime") != self.engine.cfg.get("runtime")
            fresh = service.load_config(paths["config.yaml"])
            self.engine.cfg["providers"].clear(); self.engine.cfg["providers"].update(fresh["providers"])
            self.engine.models[:] = fresh["models"]
            key = (cur.get("provider"), cur.get("model")) if cur else None
            self.engine.current = next((m for m in self.engine.models if (m.get("provider"), m.get("model")) == key),
                                       self.engine.current)
            self.engine._provider_key = None              # rebuilt from the new providers: on the next use
            self.engine._side_provider_key = None
        self.emit("models")
        return 200, {"ok": True, "saved": True, "backup": bak, "base": service.config_digest(full),
                     "restart": restart}

    # ── R426: wake a sleeping local model on request (re-picking it, or tapping the status) ──
    def wake(self) -> tuple[int, dict]:
        if self.busy:
            return 409, {"error": "a reply is running: the model is awake"}
        if self.engine.current.get("model") != "local" or self.state().get("loaded"):
            return 200, {"ok": True, "awake": True}
        with self.cond:
            if getattr(self, "_waking", False):
                return 200, {"ok": True, "waking": True}
            self._waking = True

        def run():
            try:
                ok, why = self.engine.wake_model()
                self.emit("notify", text=why)
            finally:
                self._waking = False
                self.emit("state", **{k: v for k, v in self.state().items() if k in ("model", "context", "loaded")})
        self.emit("notify", text="waking the model… the first load can take a minute")
        threading.Thread(target=run, daemon=True, name="aurora-wake").start()
        return 200, {"ok": True, "waking": True}

    # ── R423: the models list in config.yaml, edited from the page (comments kept: modelcfg edits text) ──
    MODEL_FIELDS = ("provider", "model", "tools", "vision")

    def _cfg_path(self):
        from pathlib import Path
        return Path(self.engine.cfg["_path"])

    def _overlay_models(self) -> bool:
        """config.local.yaml (R316) replaces the whole models: list when it has one; the page then cannot edit it."""
        import yaml
        f = service.aurora_home() / "config.local.yaml"
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) if f.is_file() else None
        except (OSError, yaml.YAMLError):
            return True
        return isinstance(d, dict) and "models" in d

    def model_config(self) -> dict:
        import yaml
        path = self._cfg_path()
        try:
            raw = service.model_entries(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError, service.ModelEditError) as e:
            return {"models": [], "providers": [], "editable": False, "why": f"config.yaml: {e}", "path": str(path)}
        cur = self.engine.current
        cur_i = next((i for i, m in enumerate(self.engine.models) if m is cur), None)
        rows = []
        for i, m in enumerate(raw):
            m = m if isinstance(m, dict) else {}
            extra = {k: v for k, v in m.items() if k not in self.MODEL_FIELDS}
            rows.append({"i": i, "provider": str(m.get("provider", "")), "model": str(m.get("model", "")),
                         "tools": bool(m.get("tools", True)), "vision": bool(m.get("vision", False)),
                         "extra": yaml.safe_dump(extra, sort_keys=False, allow_unicode=True) if extra else "",
                         "current": i == cur_i})
        overlay = self._overlay_models()
        return {"models": rows, "providers": sorted(self.engine.cfg.get("providers") or {}),
                "editable": not overlay, "path": str(path),
                "why": "config.local.yaml has its own models: list; edit that file by hand" if overlay else ""}

    def _entry_from(self, body: dict, old: dict | None) -> dict:
        import yaml
        prov, model = str(body.get("provider", "")).strip(), str(body.get("model", "")).strip()
        if prov not in (self.engine.cfg.get("providers") or {}):
            raise service.ModelEditError("provider: one of the providers in config.yaml")
        if not model or "\n" in model or len(model) > 200:
            raise service.ModelEditError("model: a name, one line")
        e = {"provider": prov, "model": model, "tools": bool(body.get("tools", True))}
        if body.get("vision") or (old and "vision" in old):
            e["vision"] = bool(body.get("vision"))
        text = str(body.get("extra") or "").strip()
        if text:
            try:
                extra = yaml.safe_load(text)
            except yaml.YAMLError as err:
                raise service.ModelEditError(f"other settings: not valid YAML ({str(err).splitlines()[0]})")
            if not isinstance(extra, dict):
                raise service.ModelEditError("other settings: YAML keys and values, e.g. extra_body: …")
            bad = [k for k in extra if k in self.MODEL_FIELDS or not isinstance(k, str)]
            if bad:
                raise service.ModelEditError(f"other settings: {bad[0]} has its own field")
            e.update(extra)
        if old:     # keep the old key order: known keys first in their old places
            e = {**{k: e[k] for k in old if k in e}, **{k: v for k, v in e.items() if k not in old}}
        return e

    def model_edit(self, op: str, body: dict) -> tuple[int, dict]:
        """op: add | save | delete. `i` and `expect` ({provider, model}) name the entry, so a file changed
        meanwhile (another device, a hand edit) is refused instead of editing the wrong line."""
        import yaml
        if self.busy:
            return 409, {"error": "wait for the reply to finish"}
        if self._overlay_models():
            return 409, {"error": "config.local.yaml has its own models: list; edit that file by hand"}
        path = self._cfg_path()
        with self.cond:
            try:
                text = path.read_text(encoding="utf-8")
                raw = service.model_entries(text)
                i = body.get("i")
                old = None
                if op != "add":
                    if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(raw):
                        return 404, {"error": "no such model entry"}
                    old = raw[i] if isinstance(raw[i], dict) else {}
                    exp = body.get("expect") or {}
                    if (str(old.get("provider", "")), str(old.get("model", ""))) != (exp.get("provider"), exp.get("model")):
                        return 409, {"error": "config.yaml changed meanwhile; reopen Models"}
                cur = self.engine.current
                cur_i = next((k for k, m in enumerate(self.engine.models) if m is cur), None)
                if op == "delete":
                    if i == cur_i:
                        return 409, {"error": "this is the model in use; switch to another one first"}
                    new = service.model_delete(text, i)
                elif op == "save":
                    new = service.model_update(text, i, self._entry_from(body, old))
                elif op == "add":
                    new = service.model_add(text, self._entry_from(body, None))
                else:
                    return 404, {"error": "not found"}
            except service.ModelEditError as e:
                return 400, {"error": str(e)}
            except (OSError, yaml.YAMLError) as e:
                return 500, {"error": f"config.yaml: {e}"}
            if new != text:
                service.write_text_atomic(path, new)
            fresh = service.load_models(path)
            self.engine.models[:] = fresh            # Engine.models aliases cfg["models"]: same list object
            if cur_i is not None:
                k = cur_i - (1 if op == "delete" and i < cur_i else 0)
                if 0 <= k < len(fresh):
                    self.engine.current = fresh[k]
                    if op == "save" and i == cur_i:      # renamed the model in use: the page's remembered pick follows
                        service.save_state(web_last_model=fresh[k].get("model"), web_last_provider=fresh[k].get("provider"))
        self.emit("models")
        return 200, {"ok": True}

    def models(self) -> list[dict]:
        """R387: the configured models, as the TUI's /model lists them; `ready` is false when the provider needs a key nobody set."""
        cur = self.engine.current
        out = []
        for i, m in enumerate(self.engine.list_models()):
            row = {"i": i, "provider": m.get("provider", ""), "model": m.get("model", ""), "current": m is cur,
                   "ready": self.engine._has_key(m.get("provider"))}
            if row["provider"] == "openrouter":          # R397: the cached catalog price, and how old it is
                row["price"] = service.model_price(row["model"])
            out.append(row)
        ids = self._openrouter_ids()
        if ids:                                         # R397: the terminal /model's 24 h refresh; the page redraws on "prices"
            service.refresh_prices_in_background(ids, on_done=lambda: self.emit("prices"))
        return out

    def _openrouter_ids(self) -> list[str]:
        return list(dict.fromkeys(m.get("model") for m in self.engine.list_models()
                                  if m.get("provider") == "openrouter" and m.get("model")))

    def refresh_prices(self) -> tuple[int, dict]:
        """R397: refresh every configured OpenRouter model's price from the catalog now (the page's button).
        One at a time, and not again within PRICE_REFRESH_GAP_S of the last one; says what changed."""
        ids = self._openrouter_ids()
        if not ids:
            return 400, {"error": "no OpenRouter models in config.yaml"}
        with self.cond:
            now = time.time()
            if self._price_refreshing:
                return 429, {"error": "a price refresh is already running"}
            if now - self._price_refreshed < PRICE_REFRESH_GAP_S:
                return 429, {"error": f"prices were refreshed {int(now - self._price_refreshed)} s ago; try again shortly"}
            self._price_refreshing = True
        try:
            before = {mid: service.model_price(mid) for mid in ids}
            try:
                info, ok = service.refresh_prices(ids)
            except Exception as e:
                info, ok = {}, False
                why = f"{type(e).__name__}: {e}"
            else:
                why = "could not reach the OpenRouter catalog (openrouter.ai): try again later"
            if not ok:
                return 502, {"error": why}
            with self.cond:
                self._price_refreshed = time.time()
            changed = []
            for mid in ids:
                b, a = before.get(mid) or {}, service.model_price(mid) or {}
                if mid in info and (b.get("in"), b.get("out")) != (a.get("in"), a.get("out")):
                    changed.append({"model": mid, "before": {"in": b.get("in"), "out": b.get("out")},
                                    "after": {"in": a.get("in"), "out": a.get("out")}})
            missing = [mid for mid in ids if mid not in info]
            self.emit("prices")
            self.emit("notify", text=f"prices refreshed: {len(info)} model{'' if len(info) == 1 else 's'}, "
                      + (f"{len(changed)} changed" if changed else "no changes")
                      + (f", not on OpenRouter: {', '.join(missing)}" if missing else ""))
            return 200, {"ok": True, "refreshed": len(info), "changed": changed, "missing": missing}
        finally:
            with self.cond:
                self._price_refreshing = False

    def switch_model(self, i) -> tuple[bool, str]:
        """R387: switch the active model and remember it for the web page only (state.yaml `web_last_model`), never the TUI's."""
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(self.engine.list_models()):
            return False, "no such model"
        entry = self.engine.list_models()[i]
        if not self.engine._has_key(entry.get("provider")):
            return False, f"{entry.get('provider')} has no API key on this machine"
        if not self._claim():
            return False, "a turn is running — stop it first"
        try:
            self.engine.current = entry
            self.engine._limit_cache = {}
            self.engine.session.log("model_switch", model=entry.get("model"))
        finally:
            self._release()
        try:
            service.save_state(web_last_model=entry.get("model"), web_last_provider=entry.get("provider"))
        except Exception:
            pass
        self.emit("notify", text="model: " + str(entry.get("model")))
        return True, ""

    def new_chat(self) -> tuple[bool, str]:
        if not self._claim():
            return False, "a turn is running — stop it first"
        try:
            had_turns = next(iter(self.engine.session.iter_records(events={"user"})), None) is not None
            self.engine.reset()
            self._link_notes = []              # R460: a link's notes belong to the chat they were loaded into
            if had_turns:                      # R377: a chat with turns stays in the list; the next one gets its own session
                self.engine.session = service.new_session()
        finally:
            self._release()
        self.emit("reset")
        self.emit("session", session=self.engine.session.id)
        return True, ""

    def command(self, name: str) -> tuple[bool, str]:
        """The page's command buttons: a fixed allowlist, never free text (send() still refuses '/...')."""
        if name not in COMMANDS:
            return False, "unknown command"
        if name == "clear":
            return self.new_chat()
        if name == "context":
            c = self.state()["context"]
            used, limit = c.get("used", 0), c.get("limit", 0)
            self.emit("notify", text=f"context: {used} / {limit} tokens ({round(100 * used / limit)}%)"
                      if limit else f"context: {used} tokens (limit unknown)")
            return True, ""
        with self.cond:                       # compact: summarises with the model, so it holds the turn slot
            if self.busy:
                return False, "a turn is already running"
            self.busy = True
        threading.Thread(target=self._compact, daemon=True).start()
        return True, ""

    def _compact(self) -> None:
        fe = WebFrontend(self)
        try:
            n = self.engine.compact_history(notify=fe.notify)
            fe.notify(f"compacted: {n} messages folded into a summary" if n else "nothing to compact")
        except ProviderError as e:
            fe.notify(f"provider error: {e}")
        except Exception as e:
            fe.notify(f"error: {type(e).__name__}: {e}")
        finally:
            self._finish()

    # ── resume, diff, undo (R376) ───────────────────────────────────────
    def resume(self, sid: str) -> tuple[bool, str]:
        """Continue a past session: its plain-text history becomes the live one and new turns append to its log.
        Exact ids only (like session_markdown); refused while a turn runs."""
        if not service.session_exists(sid):
            return False, "no such session"
        with self.cond:
            if self.busy:
                return False, "a turn is running — stop it first"
            self.busy = True
        try:
            n = self.engine.resume_from(sid)
            self._restore_context_notes()          # R460
        except Exception as e:
            n, err = -1, f"{type(e).__name__}: {e}"
        finally:
            self._release()
        if n < 0:
            return False, "resume failed: " + err
        if n == 0 and self.engine.session.id != sid:
            return False, "that session has no finished turns to resume"
        self.emit("reset")
        self.emit("session", session=self.engine.session.id)
        hist = [{"role": m["role"], "text": str(m.get("content", ""))[:MAX_OUTPUT]}
                for m in self.engine.messages[-MAX_HISTORY:] if m.get("role") in ("user", "assistant")]
        self.emit("history", messages=hist, turns=n)
        self.emit("notify", text=f"resumed session {sid[:6]}: {n} turn{'' if n == 1 else 's'} restored"
                  + (f" (showing the last {MAX_HISTORY} messages)" if len(self.engine.messages) > MAX_HISTORY else ""))
        self.emit("done", **{k: v for k, v in self.state().items() if k in ("model", "context")})
        return True, ""

    def diff(self) -> dict:
        """What the last turn changed (the checkpoint taken before it vs the working tree), read-only."""
        if self.busy:          # R415 (B10): the turn may be writing those files and checkpointing the same repo
            return {"diff": "", "note": "a turn is running: the diff is shown once it ends"}
        base = getattr(self.engine, "last_turn_diff_base", None)
        if base is None and not service.checkpoints():
            return {"diff": "", "note": "no checkpoints yet: one is taken before every approved write, edit or command"}
        d = service.diff_since(base)
        if d.startswith("[diff error:"):
            return {"diff": "", "note": d.strip("[]")}
        return {"diff": d[:MAX_DIFF], "note": "" if d.strip() else "no changes since the last turn started",
                "cut": len(d) > MAX_DIFF}

    def undo_preview(self) -> dict:
        kind, paths = service.undo_preview()
        return {"kind": kind, "paths": paths, "diff": service.undo_diff()[:MAX_DIFF] if kind != "none" else ""}

    def undo(self) -> tuple[bool, str]:
        """Revert just the last mutation (the terminal's /undo); refused while a turn runs, since that turn may be writing."""
        if not self._claim():
            return False, "a turn is running — stop it first"
        try:
            msg = service.undo()
        finally:
            self._release()
        self.emit("notify", text=msg)
        return True, msg

    # ── approvals (called from the turn thread) ─────────────────────────
    def ask(self, tool: str, args: dict, diff: str):
        with self.cond:
            self._approval_seq += 1
            aid = self._approval_seq
            self._approval = {"id": aid, "answer": None}
        self.emit("approve", approval=aid, tool=tool, args=_clip_args(args), diff=(diff or "")[:MAX_OUTPUT],
                  timeout=round(self.approve_timeout, 1))   # R418 (F3): the page counts down from the event's time
        deadline = time.monotonic() + self.approve_timeout
        reason = ""
        with self.cond:
            while self._approval["answer"] is None:
                if self._stop:
                    self._approval["answer"] = "s"
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    self._approval["answer"] = "n"
                    reason = "no answer in time — denied"
                    break
                self.cond.wait(min(left, 1.0))
            ans = self._approval["answer"]
            self._approval = None
        self.emit("approve_done", approval=aid, answer=ans)
        if reason:
            self.emit("notify", text=f"denied {tool}: no answer within {round(self.approve_timeout)} s")
        return ("n", reason) if reason else ans


def _code_version() -> str:
    """The code version shared with the terminal version (aurora.__version__)."""
    return service.version()


CTX_START = "\n\n<context-notes>\n"   # R460: the block _context_rule adds to the system prompt
CTX_END = "\n</context-notes>"
NOTE_CTX_MAX = 20_000                  # characters sent of each context note; the rest is cut, it is billed every turn


def _ctx_part(n: dict) -> str:
    text = n["text"][:NOTE_CTX_MAX].replace("</context-notes>", "").replace("</note>", "")
    title = re.sub(r'["<>\r\n]', "", n.get("title") or "")[:200] or "Untitled"    # R519: a title cannot close the tag
    return f'<note title="{title}">\n{text}\n</note>'


def _context_file() -> str:
    """R358: the workspace's bootstrap file (the server runs with the workspace as cwd), or "" when it has none."""
    path = os.path.join(os.getcwd(), "context.md")
    return path if os.path.isfile(path) else ""


def _outputs_path():
    from . import attach
    return attach.folder("outputs")


def _outputs_snapshot() -> dict | None:
    """name -> (mtime_ns, size) of the files directly in workspace/outputs; None when it cannot be read."""
    try:
        d = _outputs_path()
        if not d.is_dir():
            return {}
        out = {}
        with os.scandir(d) as it:
            for e in it:
                if e.is_file(follow_symlinks=False) and not e.name.startswith("."):
                    st = e.stat(follow_symlinks=False)
                    out[e.name] = (st.st_mtime_ns, st.st_size)
        return out
    except OSError:
        return None


def _clip(v, budget: list):
    """R519: strings anywhere in the value cut to what is left of the budget; lists and dicts walked, so an
    `edits: [...]` list or a nested patch no longer goes to the page (and the replay buffer) at full size."""
    if isinstance(v, str):
        # a short string (a path, a name) is never cut; a long one gets at most MAX_OUTPUT of what is left
        keep = len(v) if len(v) <= 500 else max(0, min(len(v), MAX_OUTPUT, budget[0]))
        budget[0] -= keep
        return v[:keep] + ("…" if keep < len(v) else "")
    if isinstance(v, list):
        return [_clip(x, budget) for x in v[:200]]
    if isinstance(v, dict):
        return {str(k)[:200]: _clip(x, budget) for k, x in list(v.items())[:200]}
    return v


def _clip_args(args: dict) -> dict:
    budget = [4 * MAX_OUTPUT]
    return {k: _clip(v, budget) for k, v in (args or {}).items()}


class WebFrontend:
    """R392 (P1): text and think chunks are gathered and sent every COALESCE_S or COALESCE_BYTES, not one event
    per token; any other event sends what was gathered first, so the order on the page never changes."""

    def __init__(self, session: WebSession):
        self.s = session
        self._lock = threading.Lock()
        self._kind = ""
        self._buf: list[str] = []
        self._size = 0
        self._timer: threading.Timer | None = None
        self.tin = self.tout = 0                # R418 (F2): this turn's tokens
        self._outs = _outputs_snapshot()        # R418 (F4): workspace/outputs when the turn started

    def flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not self._buf:
                return
            kind, text = self._kind, "".join(self._buf)
            self._buf, self._size = [], 0
            self.s.emit(kind, text=text)      # under the lock: two flushes can never swap their order

    def _chunk(self, kind: str, chunk: str) -> None:
        if not chunk:
            return
        if self._kind != kind and self._buf:
            self.flush()
        with self._lock:
            self._kind = kind
            self._buf.append(chunk)
            self._size += len(chunk)
            big = self._size >= COALESCE_BYTES
            if not big and self._timer is None:
                self._timer = threading.Timer(COALESCE_S, self.flush)
                self._timer.daemon = True
                self._timer.start()
        if big:
            self.flush()

    def on_text(self, chunk: str) -> None:
        self._chunk("text", chunk)

    def on_think(self, chunk: str) -> None:
        self._chunk("think", chunk)

    def on_tool_start(self, name: str, args: dict) -> None:
        self.flush()
        self.s.emit("tool_start", name=name, args=_clip_args(args))

    def on_tool_result(self, name: str, output: str) -> None:
        self.flush()
        self.s.emit("tool_result", name=name, output=(output or "")[:MAX_OUTPUT])
        self.check_outputs()

    def check_outputs(self) -> None:
        """R418 (F4): a file that appeared or changed in workspace/outputs is announced, whether or not the reply names it."""
        if self._outs is None:
            return
        now = _outputs_snapshot()
        if now is None:
            return
        for name, st in now.items():
            if self._outs.get(name) != st:
                self.s.emit("output", name=name, path=str(_outputs_path() / name), size=st[1])
        self._outs = now

    def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.flush()
        self.tin += int(input_tokens or 0)
        self.tout += int(output_tokens or 0)
        self.s.emit("usage", input=input_tokens, output=output_tokens)

    def notify(self, message: str) -> None:
        self.flush()
        self.s.emit("notify", text=message)

    def invalidate_status(self) -> None:
        pass

    def approve(self, tool: str, args: dict, diff: str):
        """R392 web approval policy: the web denylist refuses without asking (even with auto-approve on);
        auto-approve answers yes; otherwise the page is asked. Reads are never gated, so 'off' = reads only."""
        self.flush()
        pat, checked = web_check(tool, args)
        if pat:
            self.s.emit("notify", text=f"denied by the web denylist ({pat}): {tool}")
            return ("n", "blocked by the web denylist: this command is never run from the web page")
        sb = service.sandbox_status()
        unboxed = tool in _COMMAND_TOOLS and sb["wanted"] and not sb["active"]
        if not checked:          # R469: a glob too broad to expand in time is not auto-approved
            self.s.emit("notify", text=f"{tool}: a path pattern in it was too broad to check against the web denylist: asking")
        elif unboxed and self.s.auto:   # R475: no sandbox on this machine: a command is never run without asking
            self.s.emit("notify", text=f"{tool}: no command sandbox here ({sb['problem']}): asking")
        elif self.s.auto and not (tool == "web_fetch" and service.url_needs_approval(str(args.get("url", "")))):
            return "y"          # R450 (S3): a fetch that could carry data out (R283) is always put to the page
        return self.s.ask(tool, args, diff)

    def ask_continue(self, iterations: int):
        self.notify(f"stopped after {iterations} steps — send a message to carry on")
        return False

    def ask_secret(self, label: str) -> str:
        return ""

    def secret_challenge(self, context: str, matches: list, source_text: str = "") -> str:
        self.notify(f"a likely secret was found in {context} and redacted before sending")
        return "redact"

    def cancelled(self) -> bool:
        return self.s._stop
