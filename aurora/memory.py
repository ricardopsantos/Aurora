"""/remember (R52, reworked R87): a save feature — Aurora writes its own
findings into the project's context protocol folder (conventionally named
`.agentic_context`, but the name itself is never checked — see
`find_context_root`).

The bootstrap READS the memory system; this closes the loop — the agent
checks the session against MEMORY/SKILL.md's write-criteria (non-obvious +
will recur + too narrow for KNOWLEDGE), drafts finding files in the house
format, and each one goes through the normal approval challenge: `y` saves
it, free text redrafts it with your note folded in. After any save the
context's rebuild-index.sh runs so the INDEX never drifts.

`/remember [all|last [k]]` controls how much of the session gets checked:
no argument or "last" is just the last question/reply pair, "last k" the
last k pairs, "all" the whole session (the original R52 scope). If no
context protocol folder is found, findings are saved flat into
`~/AURORA_PFCS/MEMORY/` instead — a fixed, machine-wide location outside
any project (not per-project: there's no project to anchor a fallback to)
— no INDEX.md/rebuild tooling, since that's specific to the protocol
folder.
"""

import datetime
import re
import subprocess
from pathlib import Path

from . import compact, context

_DRAFT_PROMPT = """\
Review this coding session transcript and extract findings worth keeping in \
a persistent memory system. STRICT write-criteria — a finding must be ALL of:
- non-obvious (not derivable from the code, docs, or an official manual)
- likely to recur (will matter again in a future session)
- narrow (a gotcha/quirk/fix, not general reference documentation)
Do NOT extract: one-off context, secrets/keys, things the repo already \
documents, or restatements of what was done. Most sessions yield 0-2 \
findings; an empty answer is a good answer.

For EACH finding output exactly this block, blocks separated by a line \
containing only "===":
GROUP: <kebab-case topic folder, reuse one of: {groups}>
TITLE: <short title>
SUMMARY: <one line: the finding + why it matters>
BODY:
<2-8 markdown lines: ## Finding, then details/caveats>

If nothing qualifies reply with exactly: NONE

Transcript:
{transcript}
"""


# the ONE detector, shared with the bootstrap (R90c) — re-exported here
# because `memory.find_context_root` is what every caller already imports
find_context_root = context.find_context_root


def _fallback_root() -> Path:
    """Where /remember saves when no context protocol folder is found — a
    fixed, machine-wide location outside any project (there's no project
    root to anchor a per-project fallback to), not per-cwd."""
    return Path.home() / "AURORA_PFCS" / "MEMORY"


def _last_k_messages(messages: list[dict], k: int) -> list[dict]:
    """The tail of `messages` starting at the k-th-last user turn — the last
    k question/reply pairs (a "reply" may itself span several assistant/tool
    messages from a multi-iteration tool loop, all kept)."""
    user_idxs = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not user_idxs:
        return messages
    start = user_idxs[-k] if k <= len(user_idxs) else user_idxs[0]
    return messages[start:]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "finding"


def existing_groups(root: Path) -> list[str]:
    return sorted(d.name for d in (root / "MEMORY").iterdir()
                  if d.is_dir() and not d.name.startswith("."))


def parse_findings(reply: str) -> list[dict]:
    """Parse the model's GROUP/TITLE/SUMMARY/BODY blocks; skip malformed
    ones silently — a bad block must never sink the good ones."""
    if reply.strip().upper() == "NONE":
        return []
    out = []
    for block in re.split(r"^\s*===\s*$", reply, flags=re.M):
        m = {}
        body = None
        for line in block.splitlines():
            if body is not None:
                body.append(line)
                continue
            k, _, v = line.partition(":")
            key = k.strip().upper()
            if key == "BODY":
                body = [v.strip()] if v.strip() else []
            elif key in ("GROUP", "TITLE", "SUMMARY"):
                m[key.lower()] = v.strip()
        if body is not None and m.get("title") and m.get("summary"):
            m["group"] = _slug(m.get("group") or "general")
            m["body"] = "\n".join(body).strip()
            out.append(m)
    return out


def render_finding(f: dict, when: datetime.datetime, flat: bool = False) -> tuple[str, str]:
    """(relative path, file text) in the MEMORY house format — line 2
    `> summary:` is mandatory, the index is derived from it. `flat=True`
    (the ~/AURORA_PFCS/MEMORY/ fallback, no group folders) drops the
    `MEMORY/<group>/` prefix."""
    name = f"{when:%Y%m%d_%H%M%S}_{_slug(f['title'])}.md"
    rel = name if flat else f"MEMORY/{f['group']}/{name}"
    text = (f"# {f['title']}\n"
            f"> summary: {f['summary']}\n\n"
            f"**Discovered:** {when:%Y-%m-%d}\n"
            f"**Context:** Aurora session (/remember)\n\n"
            f"{f['body']}\n")
    return rel, text


def _draft(engine, transcript: str, groups: list[str],
           guidance: str = "", previous: str = "") -> str:
    ask = _DRAFT_PROMPT.format(groups=", ".join(groups) or "any",
                               transcript=transcript)
    if guidance:
        ask += (f"\n\nYou proposed:\n{previous}\n\nThe user said: {guidance}\n"
                "Re-emit ONLY that finding, same block format, adjusted.")
    msg = [{"role": "user", "content": ask}]
    provider = engine._provider_for(engine.current, interactive=True)
    result = provider.turn(engine.current.get("model", ""), msg,
                           "", None, lambda _s: None, lambda: False)
    return (result.text or "").strip()


def _rebuild_index(root: Path) -> str:
    script = root / "scripts" / "rebuild-index.sh"
    if not script.is_file():
        return "no rebuild-index.sh — update the INDEX by hand"
    try:
        r = subprocess.run(["bash", str(script)], cwd=str(root), timeout=60,
                           capture_output=True, text=True)
        return "index rebuilt" if r.returncode == 0 \
            else f"rebuild-index.sh failed: {(r.stderr or r.stdout)[-200:]}"
    except Exception as e:
        return f"rebuild-index.sh failed: {e}"


def run_stats(root: Path) -> str:
    """Runs the context folder's own stats.sh (size/count stats for
    KNOWLEDGE/MEMORY/SKILLS — folder/file counts, avg/biggest/smallest) and
    returns its output. Used by `/agentic_report`'s "Stats" choice."""
    script = root / "scripts" / "stats.sh"
    if not script.is_file():
        return "no stats.sh — can't compute stats"
    try:
        r = subprocess.run(["bash", str(script)], cwd=str(root), timeout=60,
                           capture_output=True, text=True)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"stats.sh failed: {e}"


def _parse_scope(arg: str) -> tuple[str, int] | None:
    """("all"|"last", k) from the /remember argument, or None if malformed.
    k is meaningless for "all". No argument means "last 1"."""
    tokens = arg.split()
    mode = tokens[0].lower() if tokens else "last"
    if mode == "all":
        return "all", 0
    if mode != "last":
        return None
    if len(tokens) <= 1:
        return "last", 1
    if len(tokens) == 2 and tokens[1].isdigit() and int(tokens[1]) > 0:
        return "last", int(tokens[1])
    return None


def remember(engine, fe, arg: str = "") -> None:
    """The /remember flow: draft → per-finding approval challenge → write →
    rebuild the index (when writing into the context protocol folder's
    MEMORY/). `c`/free-text on a challenge redrafts that finding."""
    scope = _parse_scope(arg)
    if scope is None:
        fe.notify("usage: /remember [all|last [k]]")
        return
    mode, k = scope
    if not engine.messages:
        fe.notify("nothing learned yet — the session is empty")
        return
    if mode == "all":
        scope_msgs, scope_label = engine.messages, "the whole session"
    else:
        scope_msgs = _last_k_messages(engine.messages, k)
        scope_label = "the last exchange" if k == 1 else f"the last {k} exchanges"

    root = find_context_root(".")
    flat = root is None
    if flat:
        root = _fallback_root()
    groups = [] if flat else existing_groups(root)
    fe.notify(f"checking {scope_label} for anything worth saving to "
             f"{root if flat else root / 'MEMORY'} …")
    try:
        findings = parse_findings(_draft(
            engine, compact.flatten_history(scope_msgs), groups))
    except Exception as e:
        fe.notify(f"draft failed: {e}")
        return
    if not findings:
        fe.notify("nothing met the write-criteria (that's a good answer)")
        return

    written = []
    now = datetime.datetime.now()
    for f in findings:
        for _attempt in range(3):     # y/n/s or two redrafts max
            rel, text = render_finding(f, now, flat=flat)
            ans, note = fe.approve("write_file", {"path": str(root / rel)},
                                   "\n".join("+ " + l
                                             for l in text.splitlines()))
            if ans in ("y", "a"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                written.append(rel)
                fe.notify(f"remembered → {rel}")
                break
            if ans == "s":
                fe.notify("stopped")
                ans = "stop-all"
                break
            if ans == "c" and note:   # redraft this one with the guidance
                try:
                    redone = parse_findings(_draft(
                        engine, compact.flatten_history(scope_msgs),
                        groups, guidance=note, previous=text))
                except Exception as e:
                    fe.notify(f"redraft failed: {e}")
                    break
                if not redone:
                    fe.notify("model withdrew the finding")
                    break
                f = redone[0]
                continue
            break                     # 'n' (or c without note) → skip it
        if ans == "stop-all":
            break

    if written:
        if flat:
            fe.notify(f"wrote {len(written)} finding(s) to {root} "
                     "(no context protocol folder found — index not rebuilt)")
        else:
            fe.notify(_rebuild_index(root))
        engine.session.log("remember", files=written, scope=arg or "last")
    else:
        fe.notify("nothing written")
