"""Forward-pass HTML to text helpers (R282, R450/S6/B2). Every `find` starts where the last one ended, so a
page full of unclosed `<script`, `<!--` or `<a ` cannot make them quadratic. Shared by the web_fetch tool and the
web page's `.html` attachments; no third-party imports."""
import re


def _strip_blocks(html: str, tags: tuple[str, ...]) -> str:
    """R282: drop `<tag>…</tag>` blocks in ONE forward pass.

    Was `re.sub(r"<(script|style)[\\s\\S]*?</\\1>", ...)`: on an opener
    with no closer, the lazy scan runs to the end of the text, and it is
    retried from every later opener — quadratic. Measured: 224KB of
    unclosed `<script` took 33.7s, against a 2MB fetch cap, on an ungated
    tool Esc could not interrupt. Here every search starts where the last
    one ended. An unclosed block swallows the rest of the page, which is
    what a browser does with it too."""
    low = html.lower()
    out, pos = [], 0
    def _open(t, at):
        # R539: `<scripts>` or `<stylesheet-x>` is not `<script>`/`<style>`: the name must end there
        while True:
            i = low.find(f"<{t}", at)
            if i == -1 or i + len(t) + 1 >= len(low) or not (low[i + len(t) + 1].isalnum() or low[i + len(t) + 1] in "-_"):
                return i
            at = i + 1
    nxt = {t: _open(t, 0) for t in tags}   # S6: each tag searched forward only
    while True:
        best, tag = -1, ""
        for t in tags:
            if nxt[t] != -1 and nxt[t] < pos:
                nxt[t] = _open(t, pos)
            i = nxt[t]
            if i != -1 and (best == -1 or i < best):
                best, tag = i, t
        if best == -1:
            out.append(html[pos:])
            break
        out.append(html[pos:best])
        close = low.find(f"</{tag}", best)
        if close == -1:
            break                         # unclosed: the rest is inside it
        end = low.find(">", close)
        pos = len(html) if end == -1 else end + 1
    return "".join(out)


_BLOCK_NAMES = frozenset(("p div section article main header ul ol table tr blockquote "
                          "pre figure figcaption dl dt dd h1 h2 h3 h4 h5 h6 br").split())
_TAGNAME = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)")


def _strip_comments(html: str) -> str:
    """S6: `<!-- ... -->` dropped in one forward pass (unclosed: rest dropped)."""
    out, pos = [], 0
    while True:
        i = html.find("<!--", pos)
        if i == -1:
            out.append(html[pos:])
            break
        out.append(html[pos:i])
        e = html.find("-->", i + 4)
        if e == -1:
            break
        pos = e + 3
    return "".join(out)


def _flatten(html: str) -> str:
    """S6: comments and tags removed, block tags to line breaks, `<li>` to
    `- `, in ONE forward pass. Was four regexes (`<!--[\\s\\S]*?-->`, `<[^>]+>`
    and two tag patterns) that go quadratic on unclosed openers: 128 kB of
    `<!-- ` took 33 s on an ungated tool. Every `find` here starts where the
    last one ended; an unclosed comment swallows the rest, as in a browser;
    a `<` with no `>` anywhere after it is plain text."""
    out, pos = [], 0
    while True:
        i = html.find("<", pos)
        if i == -1:
            out.append(html[pos:])
            break
        out.append(html[pos:i])
        if html.startswith("<!--", i):
            e = html.find("-->", i + 4)
            if e == -1:
                break
            pos = e + 3
            continue
        e = html.find(">", i + 1)
        if e == -1:
            out.append(html[i:])          # no ">" later on: all text
            break
        if e == i + 1:                    # "<>" is not a tag
            out.append("<")
            pos = i + 1
            continue
        nxt = html[i + 1]
        if not (nxt.isalpha() or nxt in "/!?"):  # R538: "a < b > c" is text, as in a browser
            out.append("<")
            pos = i + 1
            continue
        m = _TAGNAME.match(html, i)
        if m:
            name = m.group(1).lower()
            if name == "li" and html[i + 1] != "/":
                out.append("\n- ")
            elif name in _BLOCK_NAMES:
                out.append("\n")
        pos = e + 1
    return "".join(out)
