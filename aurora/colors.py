"""ANSI colours for the terminal UI. Honours NO_COLOR and non-tty output
(pipes/redirects get plain text). UI-side module — the engine never colours."""

import os
import re
import sys

# bare (unbracketed) URL — stop before trailing punctuation/closing brackets
# that are almost always part of the surrounding sentence, not the link.
# P-6: control chars (\x00-\x1f, \x7f) are excluded too, not just \s — \s
# doesn't cover ESC (0x1b), so a URL copied verbatim from fetched content
# (e.g. by the model, into its own reply) could carry a raw ESC byte through
# unmatched by `\s`. linkify() below wraps the match in an OSC-8 hyperlink
# escape with the URL text spliced in verbatim; an embedded ESC there lets a
# "URL" smuggle an arbitrary terminal escape sequence (e.g. an OSC-52
# clipboard write) into what the terminal actually interprets.
URL_RE = re.compile(r'https?://[^\s<>"\')\]\x00-\x1f\x7f]+[^\s<>"\')\].,!?:;\x00-\x1f\x7f]')

# Set True by tui.py: the full-screen TUI redirects stdout into its own
# buffer and re-parses it with prompt_toolkit's ANSI parser, which only
# understands CSI (\x1b[) sequences — an OSC-8 hyperlink (\x1b]8;;...)
# would come out as garbage text. The TUI instead makes URLs clickable
# itself at the fragment level (see tui.py's _linkify_fragments), so
# linkify() here becomes a no-op while it's active.
IN_TUI = False


def _enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


if _enabled():
    BOLD = "\033[1m"; DIM = "\033[2m"; RESET = "\033[0m"
    CYAN = "\033[36m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
    RED = "\033[31m"; MAGENTA = "\033[35m"
    UNDERLINE = "\033[4m"
else:
    BOLD = DIM = RESET = CYAN = GREEN = YELLOW = RED = MAGENTA = ""
    UNDERLINE = ""


def dim(s: str) -> str:
    return f"{DIM}{s}{RESET}"


def bold(s: str) -> str:
    return f"{BOLD}{s}{RESET}"


def linkify(s: str) -> str:
    """Wrap bare URLs in cyan+underline plus an OSC-8 hyperlink escape, so
    terminals that support it (iTerm2, Terminal.app, kitty, WezTerm, ...)
    make them Cmd/Ctrl-clickable. No-op with colours disabled — a piped/
    NO_COLOR consumer should see the plain URL, not escape codes."""
    if not RESET or IN_TUI:
        return s

    def _wrap(m: "re.Match") -> str:
        url = m.group(0)
        return f"\033]8;;{url}\033\\{CYAN}{UNDERLINE}{url}{RESET}\033]8;;\033\\"

    return URL_RE.sub(_wrap, s)


def colour_diff(diff: str) -> str:
    """Green additions, red removals, cyan hunk headers."""
    out = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(f"{GREEN}{line}{RESET}")
        elif line.startswith("-") and not line.startswith("---"):
            out.append(f"{RED}{line}{RESET}")
        elif line.startswith("@@"):
            out.append(f"{CYAN}{line}{RESET}")
        else:
            out.append(f"{DIM}{line}{RESET}")
    return "\n".join(out)


# R170l: OSC/DCS/APC/PM/SOS strings a subprocess can emit — clipboard
# hijack (OSC 52), a window-title/resize request, a "define this string as
# a macro" DCS payload, and similar. `ANSI(text).__pt_formatted_text__()`
# (used to render bash output, see `_entry_fragments`) only recognizes CSI
# (`\x1b[`) sequences for styling; anything else after an ESC falls through
# its "not '[' → continue" branch, which drops the ESC and the ONE
# character read after it but then resumes parsing the REST of the
# sequence's body as ordinary text — so the payload shows up as garbled
# literal characters in the transcript rather than actually being
# forwarded to the real terminal (prompt_toolkit fully owns rendering and
# never blindly passes raw bytes through) — but a crafted payload
# containing its OWN embedded CSI sequence could still inject real style
# codes into that fallthrough text, and the garbled byte-soup itself is
# confusing/unwanted regardless. Stripped entirely before storage, so
# neither the display NOR anything copied out of it (/copy-all, session
# export) carries the raw sequence. Plain CSI sequences (`\x1b[...m` colors,
# the common and legitimate case) are deliberately left alone.
DANGEROUS_ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC ... (BEL or ST terminated)
    r"|\x1b[PX^_][^\x1b]*\x1b\\"            # DCS / SOS / PM / APC ... ST
    # R171: a crashed binary, a tool cut off by the output cap, or a
    # deliberately malformed payload can emit an OSC/DCS/APC/PM/SOS
    # introducer with NO terminator at all — the two alternatives above both
    # require one, so `"\x1b]52;c;PAYLOAD"` (no trailing BEL/ST) passed
    # through unchanged. Since this only ever runs on a fully-captured
    # command output (not a mid-stream chunk), "no terminator anywhere in
    # the rest of the text" is unambiguous — strip from the introducer to
    # the end of the string.
    # R198: the unterminated alternatives must consume ANYTHING to the end of
    # the text, ESC included. They were `[^\x1b]*\Z`, which cannot reach `\Z`
    # once another ESC intervenes — and since the two TERMINATED alternatives
    # above also exclude ESC from their bodies, an OSC whose payload contained
    # an ESC matched no alternative at all. The scan then advanced PAST the
    # introducer, stripped the inner sequence, and left the outer one live:
    #
    #   "\x1b]52;c;PAY" + "\x1b]0;t\x07" + "LOAD"  ->  "\x1b]52;c;PAYLOAD"
    #
    # i.e. a still-open OSC 52 (clipboard write) survived the sanitizer that
    # exists to remove it. Order matters and is load-bearing: Python's `re`
    # tries alternatives left to right and takes the first that matches, so a
    # properly terminated sequence still matches above and only IT is removed,
    # leaving following text intact. Only a genuinely unterminated introducer
    # reaches these two and takes the rest of the text with it — which is
    # R171's own already-accepted trade, since a real terminal would swallow
    # that text as OSC payload anyway.
    r"|\x1b\][\s\S]*\Z"                     # unterminated OSC → end of text
    r"|\x1b[PX^_][\s\S]*\Z"                 # unterminated DCS/SOS/PM/APC
)


def strip_dangerous_escapes(text: str) -> str:
    return DANGEROUS_ESCAPES.sub("", text)
