"""ANSI colours for the terminal UI. Honours NO_COLOR and non-tty output
(pipes/redirects get plain text). UI-side module — the engine never colours."""

import os
import re
import sys

# bare (unbracketed) URL — stop before trailing punctuation/closing brackets
# that are almost always part of the surrounding sentence, not the link.
URL_RE = re.compile(r'https?://[^\s<>"\')\]]+[^\s<>"\')\].,!?:;]')

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
