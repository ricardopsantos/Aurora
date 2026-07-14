"""Clipboard (R19). Local sessions: the OS tool first (pbcopy / wl-copy /
xclip) — it's verifiable and always lands. Over SSH: OSC52 first — it rides
the terminal back to the local machine, the only thing that can. OSC52 is
fire-and-forget: some terminals silently ignore it (Terminal.app always;
iTerm2 unless its clipboard-access pref is on), so it must never be the
first choice when a real tool is available. UI-side module."""

import base64
import os
import shutil
import subprocess
import sys


def _local_tool(text: str) -> str | None:
    for tool, cmd in (("pbcopy", ["pbcopy"]),
                      ("wl-copy", ["wl-copy"]),
                      ("xclip", ["xclip", "-selection", "clipboard"])):
        if shutil.which(tool):
            try:
                subprocess.run(cmd, input=text.encode(), timeout=5, check=True)
                return tool
            except Exception:
                continue
    return None


def copy(text: str) -> str:
    """Copy `text`; returns a human description of the method used."""
    over_ssh = bool(os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"))
    if not over_ssh:
        tool = _local_tool(text)
        if tool:
            return tool
    if _osc52(text):
        return "OSC52 (terminal)"
    if over_ssh:
        tool = _local_tool(text)   # remote-side tool: last resort
        if tool:
            return f"{tool} (remote side!)"
    return "failed — no clipboard method available"


def _osc52(text: str) -> bool:
    # NEVER sys.stdout: the TUI redirects it into the chat pane, which would
    # render the escape sequence as visible garbage. Write straight to the
    # controlling terminal; fall back to the REAL process stdout when there
    # is no /dev/tty (and only if it's an actual terminal).
    payload = base64.b64encode(text.encode())[:100_000]  # terminals cap ~100KB
    seq = f"\x1b]52;c;{payload.decode()}\x07"
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(seq)
            tty.flush()
        return True
    except OSError:
        pass
    try:
        real = sys.__stdout__
        if real is None or not real.isatty():
            return False
        real.write(seq)
        real.flush()
        return True
    except Exception:
        return False
