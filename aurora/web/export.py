"""R421: one chat as a single self-contained HTML file, at two levels of detail.

brief: the prompts and the final answers. full: also each round's thinking and every tool call with its output
(as much as the session log keeps). R435: answers are rendered from Markdown, each prompt starts a numbered turn,
and the full level folds a turn's thinking and tool calls into one "Worked: N steps" block. Everything is escaped
text first: the file runs no script and loads nothing."""
import html
import re

from .. import service

LEVELS = ("brief", "full")

_CSS = (
    ":root{color-scheme:light dark;--bg:#fff;--fg:#1f2328;--dim:#59636e;--line:#d0d7de;--box:#f6f8fa;--acc:#0969da;--you:#ddf4ff;--ok:#1a7f37;--bad:#cf222e}"
    "@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--dim:#9198a1;--line:#30363d;--box:#161b22;--acc:#4493f8;--you:#0c2d4a;--ok:#3fb950;--bad:#f85149}}"
    "*{box-sizing:border-box}"
    "body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,-apple-system,'Segoe UI',Roboto,'Noto Sans','DejaVu Sans',sans-serif}"
    "main{max-width:820px;margin:0 auto;padding:32px 16px 64px}"
    "header{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:32px}"
    "h1{font-size:24px;line-height:1.3;margin:0 0 8px;overflow-wrap:anywhere}"
    ".meta{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}"
    ".meta li{font-size:13px;color:var(--dim);background:var(--box);border:1px solid var(--line);border-radius:999px;padding:2px 10px}"
    ".turn{margin:0 0 32px;padding-top:16px;border-top:1px solid var(--line)}.turn:first-of-type{border-top:0;padding-top:0}"
    ".lbl{display:flex;gap:8px;align-items:baseline;font-size:13px;color:var(--dim);margin:0 0 8px}.lbl b{color:var(--fg);font-weight:600}"
    ".you{display:flex;flex-direction:column;align-items:flex-end;margin:0 0 16px}.you .lbl{justify-content:flex-end}"
    ".bubble{background:var(--you);border:0;border-radius:16px 16px 4px 16px;padding:8px 16px;max-width:85%;overflow-wrap:anywhere;margin:0}"
    ".bubble.long>summary{padding:0;color:var(--fg);list-style:none;display:flex;flex-direction:column;gap:4px}.bubble.long>summary::-webkit-details-marker{display:none}"
    ".bubble.long .more{font-size:13px;color:var(--acc)}.bubble.long[open]>summary{border:0;padding-bottom:8px}.bubble.long[open] .pv{display:none}.bubble.long[open] .more::after{content:' (hide)'}"
    ".ans .md{overflow-wrap:anywhere}.md>:first-child{margin-top:0}.md>:last-child{margin-bottom:0}"
    ".md p,.md ul,.md ol,.md pre,.md table,.md blockquote{margin:0 0 16px}.md ul,.md ol{padding-left:24px}.md li{margin:4px 0}"
    ".md h1,.md h2,.md h3,.md h4{font-size:17px;margin:24px 0 8px;line-height:1.3}.md h1{font-size:20px}.md h2{font-size:18px}"
    ".md code{font:14px ui-monospace,'SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace;background:var(--box);border:1px solid var(--line);border-radius:4px;padding:0 4px}"
    ".md pre{background:var(--box);border:1px solid var(--line);border-radius:8px;padding:12px 16px;overflow-x:auto}"
    ".md pre code{border:0;padding:0;background:none;white-space:pre}"
    ".md blockquote{border-left:3px solid var(--line);padding-left:16px;color:var(--dim)}"
    ".md table{border-collapse:collapse;display:block;overflow-x:auto}.md th,.md td{border:1px solid var(--line);padding:4px 10px;text-align:left}.md th{background:var(--box)}"
    ".md hr{border:0;border-top:1px solid var(--line);margin:24px 0}.md a{color:var(--acc)}"
    "details{border:1px solid var(--line);border-radius:8px;background:var(--box);margin:0 0 16px}"
    "details>summary{cursor:pointer;padding:8px 16px;font-size:14px;color:var(--dim);overflow-wrap:anywhere}"
    "details[open]>summary{border-bottom:1px solid var(--line)}"
    ".steps>ol{margin:0;padding:8px 16px 8px 40px}.steps li{margin:8px 0}"
    ".steps details{background:var(--bg);margin:0}.steps .k{color:var(--fg);font:13px ui-monospace,'SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace}"
    "pre.out{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:8px 16px;max-height:480px;overflow:auto;font:13px/1.45 ui-monospace,'SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace}"
    "footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);font-size:13px;color:var(--dim)}"
    "@media (max-width:600px){main{padding:16px 12px 48px}.bubble{max-width:100%}}"
    "@media print{:root{--bg:#fff;--fg:#000;--box:#f6f8fa;--you:#eef6ff}main{max-width:none;padding:0}.turn{break-inside:avoid-page}pre.out{max-height:none}}"
)


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _when(ts: str) -> str:
    return (ts or "")[:16].replace("T", " ")


# ── a small, safe Markdown → HTML for the answers (escaped first; links only http(s)) ──
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_URL = re.compile(r"(?<![\"'=>])\b(https?://[^\s<]+[^\s<.,;:!?)\]'\"])")


def _inline(t: str) -> str:
    keep = []

    def stash(h):
        keep.append(h)
        return f"\x00{len(keep) - 1}\x00"
    t = _CODE.sub(lambda m: stash(f"<code>{_e(m.group(1))}</code>"), t)
    t = _LINK.sub(lambda m: stash(f'<a href="{_e(m.group(2))}">{_e(m.group(1))}</a>'), t)
    t = _URL.sub(lambda m: stash(f'<a href="{_e(m.group(1))}">{_e(m.group(1))}</a>'), t)
    t = _e(t)
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w])", lambda m: f"<em>{m.group(1) or m.group(2)}</em>", t)
    t = re.sub(r"~~(.+?)~~", r"<del>\1</del>", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], t)


_LI = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown(src: str) -> str:
    lines, out, i = str(src or "").replace("\r\n", "\n").split("\n"), [], 0
    para = []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()
    while i < len(lines):
        ln = lines[i]
        fence = re.match(r"^\s*(```+|~~~+)\s*([\w+#.-]*)", ln)
        if fence:
            flush()
            mark, body, i = fence.group(1), [], i + 1
            while i < len(lines) and not lines[i].strip().startswith(mark):
                body.append(lines[i]); i += 1
            lang = f' class="language-{_e(fence.group(2))}"' if fence.group(2) else ""
            out.append(f"<pre><code{lang}>{_e(chr(10).join(body))}</code></pre>"); i += 1
            continue
        if not ln.strip():
            flush(); i += 1; continue
        h = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", ln)
        if h:
            flush(); n = len(h.group(1)); out.append(f"<h{n}>{_inline(h.group(2))}</h{n}>"); i += 1; continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", ln):
            flush(); out.append("<hr>"); i += 1; continue
        if ln.lstrip().startswith(">"):
            flush(); body = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                body.append(re.sub(r"^\s*>\s?", "", lines[i])); i += 1
            out.append(f"<blockquote>{markdown(chr(10).join(body))}</blockquote>"); continue
        if "|" in ln and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", lines[i + 1]):
            flush(); head = _cells(ln); i += 2; rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i])); i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table>")
            continue
        if _LI.match(ln):
            flush(); h, i = _list(lines, i); out.append(h); continue
        para.append(ln.strip()); i += 1
    flush()
    return "".join(out)


def _list(lines, i):
    """A list, nested by indentation: (html, first line after it). R437: no shared state, exports run in parallel threads."""
    m = _LI.match(lines[i]); ind = len(m.group(1)); tag = "ol" if m.group(2)[0].isdigit() else "ul"
    items = []
    while i < len(lines):
        m = _LI.match(lines[i])
        if m and len(m.group(1)) == ind:
            text = m.group(3)
            box = re.match(r"^\[( |x|X)\]\s+(.*)$", text)
            items.append([("☑ " if box.group(1).lower() == "x" else "☐ ") + "\x01" + box.group(2) if box else text, ""]); i += 1
        elif m and len(m.group(1)) > ind and items:
            sub, i = _list(lines, i); items[-1][1] += sub
        elif lines[i].strip() and not m and items and lines[i].startswith(" " * (ind + 2)):
            items[-1][0] += " " + lines[i].strip(); i += 1
        else:
            break
    li = "".join("<li>" + (_e(t[:2]) + _inline(t[3:]) if t.startswith(("☑ \x01", "☐ \x01")) else _inline(t)) + sub + "</li>" for t, sub in items)
    return f"<{tag}>{li}</{tag}>", i


LONG_LINES, LONG_CHARS, PREVIEW_LINES = 15, 1200, 6


def _plain_title(text: str) -> str:
    """The first line with words, without Markdown marks, for the title."""
    for ln in text.split("\n"):
        t = re.sub(r"[#*_`\[\]|~]+", "", re.sub(r"^\s*>+", "", ln)).strip(" -:")
        t = " ".join(t.split())
        if len(t) >= 3:
            return t[:80]
    return ""


def _prompt(text: str) -> str:
    """A prompt bubble, rendered like an answer; a long one (a pasted document) folds to its first lines."""
    lines = text.strip("\n").split("\n")
    if len(lines) <= LONG_LINES and len(text) <= LONG_CHARS:
        return f'<div class="bubble md">{markdown(text)}</div>'
    head = " ".join(" ".join(lines[:PREVIEW_LINES]).split())[:300]
    return (f'<details class="bubble long"><summary><span class="pv">{_e(head)} …</span>'
            f'<span class="more">Show all {len(lines)} lines</span></summary><div class="md">{markdown(text)}</div></details>')


def _call(name, args) -> tuple:
    """A tool call as (one-line title, full arguments): the title is the tool and its command, path or first value."""
    import json
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            return f"{name}: {' '.join(args.split())[:120]}", args
    if not isinstance(args, dict) or not args:
        return str(name or "tool"), ""
    key = next((k for k in ("command", "path", "url", "pattern", "query", "file") if isinstance(args.get(k), str)), None)
    hint = args[key] if key else next((v for v in args.values() if isinstance(v, str)), "")
    hint = " ".join(str(hint).split())
    full = args["command"] if key == "command" and len(args) == 1 else json.dumps(args, ensure_ascii=False, indent=2)
    return f"{name}" + (f": {hint[:120]}{'…' if len(hint) > 120 else ''}" if hint else ""), full


def _steps(steps: list) -> str:
    if not steps:
        return ""
    tools = sum(1 for s in steps if s[0] == "tool")
    what = ", ".join(x for x in (f"{tools} tool call{'s' if tools != 1 else ''}" if tools else "",
                                  "thinking" if any(s[0] == "thinking" for s in steps) else "") if x)
    items = "".join(f'<li><details><summary><span class="k">{_e(head)}</span></summary><pre class="out">{_e(body)}</pre></details></li>'
                    for _, head, body in steps)
    return (f'<details class="steps"><summary>Worked: {len(steps)} step{"s" if len(steps) != 1 else ""} ({what})</summary>'
            f"<ol>{items}</ol></details>")


def render(sid: str, level: str = "brief", title: str = "") -> str:
    """The HTML document for session `sid` (the caller has checked that it exists)."""
    full = level == "full"
    turns, first, start, end, models = [], "", "", "", []
    cur = None

    def new_turn(prompt="", ts=""):
        t = {"prompt": prompt, "ts": ts, "body": [], "steps": []}
        turns.append(t)
        return t
    for r in service.session_records(sid):
        ev = r.get("event")
        ts = _when(r.get("ts"))
        start = start or ts
        end = ts or end
        if ev == "user":
            first = first or _plain_title(str(r.get("text") or ""))
            cur = new_turn(str(r.get("text") or ""), ts)
        elif ev == "assistant":
            cur = cur or new_turn()
            m = r.get("model") or "?"
            if m not in models:
                models.append(m)
            if cur["steps"]:
                cur["body"].append(_steps(cur["steps"])); cur["steps"] = []
            cur["body"].append(f'<div class="ans"><p class="lbl"><b>Aurora</b><span>{_e(m)}</span>'
                               f'{"<span>" + _e(ts) + "</span>" if ts else ""}</p><div class="md">{markdown(r.get("text"))}</div></div>')
        elif full and ev == "thinking":
            cur = cur or new_turn()
            more = int(r.get("chars") or 0) - len(r.get("text") or "")
            cut = f"\n[… {more} more characters not kept in the session log]" if more > 0 else ""
            cur["steps"].append(("thinking", "Thinking", str(r.get("text") or "") + cut))
        elif full and ev == "tool":
            cur = cur or new_turn()
            out = str(r.get("output", ""))
            lost = int(r.get("chars") or len(out)) - len(out)
            if lost > 0:
                out += f"\n[… {lost} more characters not kept in the session log]"
            head, args = _call(r.get("name"), r.get("args"))
            cur["steps"].append(("tool", head, (args + "\n\n── output ──\n" if args else "") + (out or "(no output)")))
    parts = []
    n = 0
    for t in turns:
        if t["steps"]:
            t["body"].append(_steps(t["steps"]))
        you = ""
        if t["prompt"]:
            n += 1
            you = (f'<div class="you"><p class="lbl"><b>You</b><span>#{n}</span>{"<span>" + _e(t["ts"]) + "</span>" if t["ts"] else ""}</p>'
                   f'{_prompt(t["prompt"])}</div>')
        parts.append(f'<section class="turn">{you}{"".join(t["body"])}</section>')
    name = title or first or "Aurora chat"
    what = "prompts, thinking, tool calls and answers" if full else "prompts and final answers"
    when = start if not end or end[:10] != start[:10] else start + (" – " + end[11:] if end != start else "")
    if end and end[:10] != start[:10]:
        when = start + " – " + end
    meta = [when, f"{n} prompt{'s' if n != 1 else ''}", ", ".join(models) if models else "", what]
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta http-equiv=Content-Security-Policy content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            f"<title>{_e(name)} · Aurora</title><style>{_CSS}</style></head><body><main>"
            f"<header><h1>{_e(name)}</h1><ul class=meta>" + "".join(f"<li>{_e(x)}</li>" for x in meta if x) + "</ul></header>"
            + "".join(parts)
            + f"<footer>Aurora chat {_e(sid)} · exported as one file; it runs no script and loads nothing.</footer>"
            "</main></body></html>")
