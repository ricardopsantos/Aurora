"""Scratch task list for multi-step work (R93).

Why it exists: the loop nudge (R27) and the iteration cap (R9) both exist
because a model drifts on multi-step work — it re-runs a call it already
ran, or wanders off the original request three tools deep. Both are
*brakes*. A visible task list is the cheap structural fix on the other side:
the model writes down the plan, then re-reads its own list every time it
calls the tool again, so "what was I doing" is answerable from the
conversation instead of re-derived from the transcript.

Deliberately dumb: a list of (task, status) in memory for the life of the
session, rewritten wholesale by each call — no ids, no partial updates, no
persistence. /clear resets it with the rest of the conversation. Engine-side
module: no terminal I/O, no UI imports; the rendered text goes back to the
model as the tool result and is shown by `/todo`.
"""

STATUSES = ("pending", "in_progress", "done")
_MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}

_items: list[dict] = []


def items() -> list[dict]:
    return list(_items)


def clear() -> None:
    _items.clear()


def render() -> str:
    """The list as a checklist, or '' when empty. Same text the model gets
    back and `/todo` prints — one representation, no drift between them."""
    if not _items:
        return ""
    lines = [f"{_MARK[i['status']]} {i['task']}" for i in _items]
    done = sum(1 for i in _items if i["status"] == "done")
    return "\n".join(lines) + f"\n({done}/{len(_items)} done)"


def todo_write(todos=None, **_) -> str:
    """Replace the task list. `todos` is a list of {task, status} (status
    defaults to 'pending'); a plain string is accepted as a one-task list
    because small models pass one often enough to be worth tolerating."""
    global _items
    if isinstance(todos, str):
        todos = [{"task": todos}]
    if todos is None or not isinstance(todos, list):
        return ("[error: todo_write needs a `todos` list of "
                "{task, status} objects]")
    parsed = []
    for raw in todos:
        if isinstance(raw, str):
            raw = {"task": raw}
        if not isinstance(raw, dict):
            return f"[error: not a task object: {raw!r}]"
        task = str(raw.get("task") or raw.get("content") or "").strip()
        if not task:
            continue
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in STATUSES:
            status = "pending"
        parsed.append({"task": task, "status": status})
    if not parsed:
        _items = []
        return "[task list cleared]"
    _items = parsed
    return render()


SPEC = [
    {"name": "todo_write",
     "description": "Write or update your task list for multi-step work. "
                    "Call it once with the full plan before starting, then "
                    "again after each step to mark progress — pass the WHOLE "
                    "list every time, it replaces the previous one. Returns "
                    "the current list. Skip it for single-step requests.",
     "parameters": {"type": "object", "properties": {
         "todos": {"type": "array", "description": "the complete task list",
                   "items": {"type": "object", "properties": {
                       "task": {"type": "string"},
                       "status": {"type": "string",
                                  "enum": list(STATUSES)}},
                       "required": ["task"]}}},
         "required": ["todos"]}},
]

RUNNERS = {"todo_write": todo_write}
