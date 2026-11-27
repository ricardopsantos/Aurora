# summary: R462 web-only `remember` tool: appends a lasting fact to the note titled "Memory" (registered by WebSession).
"""R462: `remember` — Aurora keeps a lasting fact in the note titled "Memory" (web Notes), one line per fact.

The note is the memory: the user reads, edits and deletes it like any note, and a page link that loads it
(`/?note=<id>,…`, R460) gives every new chat what earlier chats learned. Ungated: it only appends one line
to the user's own note, never reads anything back to the model beyond a confirmation.
Web-only (the notes live in the web package), so WebSession installs it next to the extension tools.
"""

MEMORY_TITLE = "Memory"
MAX_FACT = 300

SPEC = [{
    "name": "remember",
    "description": ("Save one lasting fact for future chats (a person and their email, a preference, a recurring "
                    "detail) to the user's Memory note. Use it when the user says 'remember…' or tells you something "
                    "that will matter again. One short sentence per call; never secrets or passwords."),
    "parameters": {"type": "object", "properties": {
        "fact": {"type": "string", "description": "one short sentence"}}, "required": ["fact"]},
}]


def _memory_note():
    from . import notes
    same = [n for n in notes.listing() if n["title"].strip().lower() == MEMORY_TITLE.lower()]
    if same:   # R463: two notes titled Memory must not split the facts: always the oldest one
        # R467: `created` comes from the listing (it was missing there, so every note scored 0 and the random id
        # decided). A note from before `created` existed is older than any that has it, so it counts as 0.
        return notes.get(min(same, key=lambda n: (float(n.get("created") or 0), n["id"]))["id"])
    nid, why = notes.create(MEMORY_TITLE, "", kind="personal")
    if nid is None:
        raise RuntimeError(why)
    return notes.get(nid)


def remember(fact: str = "", **_) -> str:
    from . import notes
    from .. import service
    fact = " ".join(str(fact).split())
    if not fact:
        return "[remember: nothing to save]"
    if len(fact) > MAX_FACT:
        return f"[remember: not saved: too long ({len(fact)} characters, the limit is {MAX_FACT}); shorten it]"
    try:
        if service.has_secret(fact):
            return "[remember: not saved: it looks like a secret or password; those are never stored]"
        why = ""
        for _attempt in range(2):          # R463: a save races the note editor; reread and retry once on a conflict
            n = _memory_note()
            lines = n["text"].splitlines()
            if any(line.strip("- ").strip().lower() == fact.lower() for line in lines):
                return f"already in the {MEMORY_TITLE} note"
            text = (n["text"].rstrip("\n") + "\n" if n["text"].strip() else "") + f"- {fact}\n"
            _, why = notes.save(n["id"], n["title"], text, base=n["updated"])
            if why != notes.CONFLICT:
                break
        if why:
            return f"[remember: not saved: {why}]"
    except Exception as e:
        return f"[remember: not saved: {e}]"
    return f"saved to the {MEMORY_TITLE} note: {fact}"


RUNNERS = {"remember": remember}


def install() -> None:
    """Add `remember` to the extension tools the engine already installed (kept: set_extensions dedupes)."""
    from .. import service
    service.add_tools(SPEC, RUNNERS)
