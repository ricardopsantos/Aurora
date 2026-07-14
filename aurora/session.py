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

    def records(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(l) for l in self.log_path.read_text().splitlines() if l.strip()]


def latest_session_id() -> str | None:
    logs = sorted(sessions_dir().glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return logs[-1].stem if logs else None


def list_sessions(limit: int = 20) -> list[tuple[str, str, str]]:
    """(id, mtime-iso, first-user-message) newest first."""
    out = []
    for p in sorted(sessions_dir().glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        first = ""
        for line in p.read_text().splitlines():
            r = json.loads(line)
            # skip the bootstrap boilerplate turn — preview the real first task
            if r.get("event") == "user" and not r.get("bootstrap"):
                first = (r.get("text", "") or "")[:60]
                break
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        out.append((p.stem, mtime, first))
    return out


def export_markdown(session_id: str) -> str:
    s = Session(session_id)
    lines = [f"# Aurora session {session_id}\n"]
    for r in s.records():
        ev = r.get("event")
        if ev == "user":
            lines.append(f"## User\n\n{r.get('text', '')}\n")
        elif ev == "assistant":
            lines.append(f"## Assistant ({r.get('model', '?')})\n\n{r.get('text', '')}\n")
        elif ev == "tool":
            lines.append(f"> 🔧 `{r.get('name')}` → {str(r.get('output', ''))[:200]}\n")
    return "\n".join(lines)
