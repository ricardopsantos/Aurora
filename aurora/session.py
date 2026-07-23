"""Session identity + JSONL event log (R20). Every turn, tool call/result,
approval, switch, and error is appended; nothing is ever auto-deleted.
Supports resume (rebuild message history from a past log) and markdown export."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .paths import sessions_dir


class Session:
    def __init__(self, session_id: str | None = None):
        self.id = session_id or uuid.uuid4().hex[:12]
        self.log_path = sessions_dir() / f"{self.id}.jsonl"

    def log(self, event: str, **data) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(),
               "event": event, **data}
        with open(self.log_path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def iter_records(self, events: "set[str] | None" = None):
        """Stream the log line by line — a long-lived session's JSONL is
        unbounded (nothing is ever auto-deleted, R20), so resume/export must
        not hold the whole file in memory on top of the parsed records
        (R90g). A corrupt/truncated line (killed mid-write, disk full) is
        skipped, never fatal to the rest of the session.

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
        if not self.log_path.exists():
            return
        needles = ([f'"event": "{e}"' for e in events]
                   if events is not None else None)
        with open(self.log_path) as f:
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
    logs = sorted(sessions_dir().glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return logs[-1].stem if logs else None


def list_sessions(limit: int = 20) -> list[tuple[str, str, str]]:
    """(id, mtime-iso, first-user-message) newest first."""
    out = []
    for p in sorted(sessions_dir().glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        first = ""
        with open(p) as f:  # stream — don't load a whole (possibly huge) log
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # skip the bootstrap boilerplate turn — preview the real first task
                if r.get("event") == "user" and not r.get("bootstrap"):
                    first = (r.get("text", "") or "")[:60]
                    break
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        out.append((p.stem, mtime, first))
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


def usage_all_sessions() -> dict[str, dict]:
    """usage_by_model summed across every session log on this machine."""
    total: dict[str, dict] = {}
    for p in sessions_dir().glob("*.jsonl"):
        for model, row in usage_by_model(p.stem).items():
            acc = total.setdefault(model, {"turns": 0, "input": 0, "billed": 0,
                                           "output": 0, "cached": 0})
            for k, v in row.items():
                acc[k] += v
    return total


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
            lines.append(f"> 🔧 `{r.get('name')}` → {str(r.get('output', ''))[:200]}\n")
    return "\n".join(lines)
