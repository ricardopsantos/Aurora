"""ANSI colours for the terminal UI. Honours NO_COLOR and non-tty output
(pipes/redirects get plain text). UI-side module — the engine never colours."""

import os
import sys


def _enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


if _enabled():
    BOLD = "\033[1m"; DIM = "\033[2m"; RESET = "\033[0m"
    CYAN = "\033[36m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
    RED = "\033[31m"; MAGENTA = "\033[35m"
else:
    BOLD = DIM = RESET = CYAN = GREEN = YELLOW = RED = MAGENTA = ""


def dim(s: str) -> str:
    return f"{DIM}{s}{RESET}"


def bold(s: str) -> str:
    return f"{BOLD}{s}{RESET}"


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
