"""Session identity + JSONL event log (R20). Every turn, tool call/result,
approval, switch, and error is appended; nothing is ever auto-deleted.
(Approvals were named here from the start but only actually written as of
R133c — this docstring described an intent, not the code.)
Supports resume (rebuild message history from a past log) and markdown export.

Size-based rotation (feature request, 2026-07-27): a daily driver's session
log is unbounded in TIME (R20's whole point), which on a long-running
session made the single JSONL unbounded in SIZE too — multi-MB logs that
every `/cost`/`/search` walked in full, and that made `iter_records`'s own
prefilter (R96e) less effective the bigger a single file got. Past
SESSION_LOG_MAX_BYTES, a session's log continues in `<id>.2.jsonl`,
`<id>.3.jsonl`, etc — the "nothing is ever deleted" guarantee is unchanged,
only which FILE a given record lives in. A session id therefore maps to a
*sequence* of part files, oldest (the original `<id>.jsonl`) first; every
reader in this module (`iter_records`, `list_sessions`,
`usage_all_sessions`, `search_sessions`, `latest_session_id`) walks that
sequence instead of assuming one id == one file. The cap is approximate,
not a hard limit — the rotation check runs once per `log()` call against
whatever the current part's size was at that moment, so a single very large
record can push a part somewhat past the cap before the NEXT record rolls
to a new one."""

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl  # POSIX only — R170f, see log()'s docstring
except ImportError:
    fcntl = None

from .paths import sessions_dir

# 5MB: comfortably larger than any single day's normal use, small enough that
# a rotated part still parses (and re-parses, on every /cost) in well under a
# second even on a slow disk.
SESSION_LOG_MAX_BYTES = 5 * 1024 * 1024

# A rotated part's name: "<id>.<n>.jsonl", n >= 2 (the base "<id>.jsonl" IS
# part 1, unnumbered — renaming it on first rotation would break every path
# already holding it open, resume, and every existing on-disk reference).
_ROTATED_RE = re.compile(r"^(.+)\.([2-9][0-9]*)\.jsonl$")


def _parts_for(session_id: str) -> list[Path]:
    """Every log part for one session id, oldest (the base file) first.
    Only ever grows forward (part 2 can't exist without part 1 existing
    first — `log()`'s rotation always advances by exactly one), so probing
    n=2, 3, ... until one is missing is exact, not a heuristic."""
    d = sessions_dir()
    base = d / f"{session_id}.jsonl"
    parts = [base] if base.exists() else []
    n = 2
    while True:
        p = d / f"{session_id}.{n}.jsonl"
        if not p.exists():
            break
        parts.append(p)
        n += 1
    return parts


def _base_session_ids() -> list[str]:
    """Every session id that has ever logged anything — one entry per
    SESSION, never per rotated part (a rotated `<id>.2.jsonl` is the same
    session as `<id>.jsonl`, not a second one)."""
    return [p.stem for p in sessions_dir().glob("*.jsonl")
            if not _ROTATED_RE.match(p.name)]


def _latest_mtime(session_id: str) -> float:
    """The freshest activity for a session, across ALL its parts — a
    rotated session's base file stops changing the moment rotation happens,
    so sorting/display must follow whichever part is CURRENTLY being
    written to, not the original file."""
    parts = _parts_for(session_id)
    return max((p.stat().st_mtime for p in parts), default=0.0)


class Session:
    def __init__(self, session_id: str | None = None):
        self.id = session_id or uuid.uuid4().hex[:12]
        self.log_path = sessions_dir() / f"{self.id}.jsonl"   # part 1

    def _write_target(self) -> Path:
        """Which part file the NEXT record appends to. Recomputed on every
        call (not cached at construction, not tracked as instance state) so
        a process resumed onto an existing session — or two writers on the
        same id, R170f's whole reason for existing — always re-derives the
        current part from what's actually on disk, rather than trusting a
        possibly-stale in-memory guess."""
        parts = _parts_for(self.id)
        if not parts:
            return self.log_path   # first-ever write for this session
        current = parts[-1]
        try:
            size = current.stat().st_size
        except OSError:
            size = 0
        if size < SESSION_LOG_MAX_BYTES:
            return current
        return sessions_dir() / f"{self.id}.{len(parts) + 1}.jsonl"

    def log(self, event: str, **data) -> None:
        rec = {"ts": datetime.now(UTC).isoformat(),
               "event": event, **data}
        # R146b: UTF-8 explicitly, not the locale encoding. json.dumps runs
        # with ensure_ascii=False, so a non-ASCII prompt or tool output raised
        # UnicodeEncodeError from inside Engine.send under LANG=C/POSIX (cron,
        # CI, minimal containers) — and a log written on a UTF-8 machine was
        # unreadable on one that was not.
        #
        # R170f: O_APPEND makes a single write() atomic, but only WITHIN one
        # process's view of the fd — two Aurora processes resumed onto the
        # SAME session id (`aurora --resume abc123` twice) had no
        # coordination at all, so their writes could interleave at the OS
        # scheduler's whim. An advisory `flock` around the write serializes
        # the two without needing any new state file — every writer takes
        # the same lock on the log itself. POSIX only (no `fcntl` on
        # Windows); best effort there, same as before this fix.
        # R171/P5: `separators=(", ", ": ")` pinned explicitly — matches
        # json.dumps's own default, but `iter_records`' prefilter below
        # depends on `'"event": "<name>"'` being an exact substring of every
        # line. That was previously an IMPLICIT contract riding on the
        # default never changing; pinning it here means a future
        # change to this call site breaks loudly (wrong substring, records
        # silently skipped) only if someone edits both sides inconsistently,
        # not from a stdlib default drifting out from under an unstated
        # assumption.
        # R232: `f.write()` fills Python's buffer; the actual write() syscall
        # happens at flush. Unlocking before flushing therefore released the
        # lock with the record still in userspace, and R170f's serialization
        # covered nothing that touched the file. For a record larger than the
        # 8KB buffer — a tool result, i.e. most of a session's bytes — the
        # writer flushes full chunks under the lock and leaves the REMAINDER
        # to flush after it, so a second process resumed onto the same id
        # could take the lock and append between our chunks: a physically
        # interleaved, unparseable line, the exact corruption R170f exists to
        # prevent. The flush must be inside the locked region.
        line = json.dumps(rec, ensure_ascii=False,
                          separators=(", ", ": ")) + "\n"
        target = self._write_target()
        with open(target, "a", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            else:
                f.write(line)

    def iter_records(self, events: "set[str] | None" = None):
        """Stream the log line by line, across every rotated PART in order —
        a long-lived session's JSONL is unbounded (nothing is ever
        auto-deleted, R20), so resume/export must not hold the whole thing
        in memory on top of the parsed records (R90g). A corrupt/truncated
        line (killed mid-write, disk full) is skipped, never fatal to the
        rest of the session.

        `events`, if given, is a set of event names to keep — everything
        else is skipped WITHOUT calling `json.loads` (R96e). `log()` always
        writes with `json.dumps`'s defaults, so `'"event": "<name>"'` is an
        exact, stable substring of any record with that event — cheap
        (C-level `str.__contains__`) and a false hit only costs one wasted
        parse, never a false miss. Tool records dominate a session's log
        (one per tool result, often several KB of output each) and calls
        like `/cost`'s `usage_by_model` only ever want `assistant` records —
        parsing every line to filter them was most of the cost.
        """
        needles = ([f'"event": "{e}"' for e in events]
                   if events is not None else None)
        for path in _parts_for(self.id):
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    if needles is not None and not any(n in line for n in needles):
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if events is not None and rec.get("event") not in events:
                        continue   # the substring guard can false-positive; never a false negative
                    yield rec

    def records(self) -> list[dict]:
        return list(self.iter_records())


def latest_session_id() -> str | None:
    ids = _base_session_ids()
    return max(ids, key=_latest_mtime) if ids else None


def list_sessions(limit: int = 20) -> list[tuple[str, str, str]]:
    """(id, mtime-iso, first-user-message) newest first."""
    out = []
    ids = sorted(_base_session_ids(), key=_latest_mtime, reverse=True)[:limit]
    for sid in ids:
        first = ""
        # skip the bootstrap boilerplate turn — preview the real first task
        for r in Session(sid).iter_records(events={"user"}):
            if not r.get("bootstrap"):
                first = (r.get("text", "") or "")[:60]
                break
        mtime = datetime.fromtimestamp(_latest_mtime(sid)).strftime("%Y-%m-%d %H:%M")
        out.append((sid, mtime, first))
    return out


def usage_by_model(session_id: str) -> dict[str, dict]:
    """Per-model token totals for one session, read straight out of its JSONL
    (R92) — every `assistant` event already carries model/input/output, so
    this is a pure read over data Aurora has always logged, with no new
    bookkeeping and no state to keep in sync.

    `billed` is the cost basis: the SUM of every iteration's prompt in a
    multi-tool turn (R37), falling back to `input_tokens` for turns logged
    before that field existed. `cached` is the part the provider served from
    its prompt cache (R91), 0 when unreported."""
    out: dict[str, dict] = {}
    for r in Session(session_id).iter_records(events={"assistant"}):
        m = r.get("model") or "?"
        row = out.setdefault(m, {"turns": 0, "input": 0, "billed": 0,
                                 "output": 0, "cached": 0})
        row["turns"] += 1
        row["input"] += int(r.get("input_tokens") or 0)
        row["billed"] += int(r.get("billed_input")
                             or r.get("input_tokens") or 0)
        row["output"] += int(r.get("output_tokens") or 0)
        row["cached"] += int(r.get("cached_input") or 0)
    return out


def last_latency_by_model(session_scan_limit: int = 20) -> dict[str, float]:
    """Most recent successful request latency per model (seconds), read
    straight from `assistant` records' `latency_s` field (feature request,
    2026-07-27) — for `/model`'s picker, so "which model is fastest right
    now" is an instant, no-network-call read instead of a live per-entry
    probe. Cached by nature: a model with no `assistant` record in the
    scanned window (unused recently, or logged before `latency_s` existed)
    just has no entry — the picker shows nothing for it rather than a stale
    guess.

    Scans the newest `session_scan_limit` sessions, newest first. Within
    each session `iter_records` yields oldest-to-newest, so the LAST
    occurrence of a model as that session is walked is genuinely its most
    recent request in that session; `setdefault` at the session level then
    means the newest session to have used a model wins outright — an older
    session is never allowed to overwrite a fresher reading."""
    out: dict[str, float] = {}
    ids = sorted(_base_session_ids(), key=_latest_mtime, reverse=True)
    ids = ids[:session_scan_limit]
    for sid in ids:
        session_latest: dict[str, float] = {}
        for r in Session(sid).iter_records(events={"assistant"}):
            m = r.get("model")
            lat = r.get("latency_s")
            if m and lat is not None:
                session_latest[m] = lat
        for m, lat in session_latest.items():
            out.setdefault(m, lat)
    return out


def usage_all_sessions() -> dict[str, dict]:
    """usage_by_model summed across every session on this machine — one
    call per SESSION ID via `_base_session_ids()`, not per part file:
    `usage_by_model` already walks every rotated part for the id it's
    given, so iterating `*.jsonl` directly here would double (triple, …)
    count any session that ever rotated."""
    total: dict[str, dict] = {}
    for sid in _base_session_ids():
        for model, row in usage_by_model(sid).items():
            acc = total.setdefault(model, {"turns": 0, "input": 0, "billed": 0,
                                           "output": 0, "cached": 0})
            for k, v in row.items():
                acc[k] += v
    return total


def search_sessions(query: str, limit: int = 20) -> list[tuple[str, str, str, str]]:
    """(session_id, mtime-iso, event, snippet) across every SESSION on this
    machine, newest session first (R161) — one entry per session id, not
    per rotated part file (walks every part for the id via `_parts_for`, in
    order, but still stops at the first hit). Plain case-insensitive
    substring match over each record's `text`/`output` field — the same
    unbounded-log-friendly streaming `list_sessions`/`usage_all_sessions`
    already use, no index to build or keep in sync. `snippet` is the match
    trimmed to ~120 chars around the hit so a hit inside a large tool output
    doesn't dump the whole thing."""
    needle = query.lower()
    out: list[tuple[str, str, str, str]] = []
    ids = sorted(_base_session_ids(), key=_latest_mtime, reverse=True)
    for sid in ids:
        if len(out) >= limit:
            break
        mtime = datetime.fromtimestamp(_latest_mtime(sid)).strftime("%Y-%m-%d %H:%M")
        found = False
        for p in _parts_for(sid):
            if found:
                break
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    field = r.get("text") if "text" in r else r.get("output")
                    if not isinstance(field, str) or needle not in field.lower():
                        continue
                    idx = field.lower().index(needle)
                    start = max(0, idx - 40)
                    snippet = field[start:start + 120].replace("\n", " ").strip()
                    out.append((sid, mtime, r.get("event", "?"), snippet))
                    found = True
                    break   # one hit per session is enough for a search list
    return out


def export_markdown(session_id: str) -> str:
    s = Session(session_id)
    lines = [f"# Aurora session {session_id}\n"]
    for r in s.iter_records():
        ev = r.get("event")
        if ev == "user":
            lines.append(f"## User\n\n{r.get('text', '')}\n")
        elif ev == "assistant":
            lines.append(f"## Assistant ({r.get('model', '?')})\n\n{r.get('text', '')}\n")
        elif ev == "tool":
            lines.append(f"> tool: `{r.get('name')}` → {str(r.get('output', ''))[:200]}\n")
    return "\n".join(lines)
