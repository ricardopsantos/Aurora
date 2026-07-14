"""/remember (R52): Aurora writes its own findings into `.agentic_context`.

The bootstrap READS the memory system; this closes the loop — the agent
reviews the session against MEMORY/SKILL.md's write-criteria (non-obvious +
will recur + too narrow for KNOWLEDGE), drafts finding files in the house
format, and each one goes through the normal approval challenge: `y` writes
it, free text redrafts it with your note folded in. After any write the
context's rebuild-index.sh runs so the INDEX never drifts.
"""

import datetime
import re
import subprocess
from pathlib import Path

from . import compact

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


def find_context_root(start: str = ".") -> Path | None:
    """Nearest `.agentic_context` walking up from `start` (the protocol dir
    this repo's bootstrap points at)."""
    p = Path(start).resolve()
    for d in (p, *p.parents):
        cand = d / ".agentic_context"
        if (cand / "MEMORY").is_dir():
            return cand
    return None


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


def render_finding(f: dict, when: datetime.datetime) -> tuple[str, str]:
    """(relative path, file text) in the MEMORY house format —
    line 2 `> summary:` is mandatory, the index is derived from it."""
    name = f"{when:%Y%m%d_%H%M%S}_{_slug(f['title'])}.md"
    rel = f"MEMORY/{f['group']}/{name}"
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
    kind = engine.provider_kind()
    msg = ([{"role": "user", "content": [{"type": "text", "text": ask}]}]
           if kind == "anthropic" else [{"role": "user", "content": ask}])
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


def remember(engine, fe) -> None:
    """The /remember flow: draft → per-finding approval challenge → write →
    rebuild the index. `c`/free-text on a challenge redrafts that finding."""
    root = find_context_root(".")
    if root is None:
        fe.notify(".agentic_context with a MEMORY/ not found from here up — nothing to write into")
        return
    if not engine.messages:
        fe.notify("nothing learned yet — the session is empty")
        return
    groups = existing_groups(root)
    fe.notify(f"reviewing the session against {root}/MEMORY …")
    try:
        findings = parse_findings(_draft(
            engine, compact.flatten_history(engine.messages), groups))
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
            rel, text = render_finding(f, now)
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
                        engine, compact.flatten_history(engine.messages),
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
        fe.notify(_rebuild_index(root))
        engine.session.log("remember", files=written)
    else:
        fe.notify("nothing written")
