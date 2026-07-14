"""Lightweight streaming markdown → ANSI for the terminal (display only —
history, /copy and /export always keep the raw markdown). Line-based so it
works mid-stream: the UI buffers until each newline and renders whole lines.

Deliberately small: bold, inline code, headers, bullets, dim code fences.
When colours are off (NO_COLOR / non-tty) the raw text passes through
untouched, so pipes see exactly what the model wrote."""

import re

from .colors import BOLD, CYAN, DIM, RESET

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_BULLET = re.compile(r"^(\s*)[*-]\s+")
_HEADER = re.compile(r"^(#{1,6})\s+(.*)$")


class LineRenderer:
    """Stateful per-turn renderer (tracks ``` fences)."""

    def __init__(self):
        self.in_fence = False

    def render(self, line: str) -> str:
        if not RESET:  # colours disabled — stay byte-faithful
            return line
        if line.strip().startswith("```"):
            self.in_fence = not self.in_fence
            return f"{DIM}{line}{RESET}"
        if self.in_fence:
            return f"{DIM}{line}{RESET}"
        m = _HEADER.match(line)
        if m:
            return f"{BOLD}{CYAN}{m.group(2)}{RESET}"
        line = _BOLD.sub(f"{BOLD}\\1{RESET}", line)
        line = _CODE.sub(f"{CYAN}\\1{RESET}", line)
        line = _BULLET.sub(r"\1• ", line)
        return line
