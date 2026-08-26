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

# Terminals cap an OSC52 payload at roughly this many base64 bytes. Divisible
# by 4 on purpose — base64 decodes in 4-char groups, so a cut anywhere else
# would hand the terminal a partial group.
OSC52_MAX_B64 = 100_000


def _osc52_would_truncate(text: str) -> bool:
    """R235: whether `_osc52` will silently drop the tail of `text`."""
    return len(base64.b64encode(text.encode())) > OSC52_MAX_B64


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
    """Copy `text`; returns a human description of the method used.

    R171/S4: this is reachable from the model's own output (the copy
    picker's "last reply", `/copy-all`, and any tool result the user
    selects) with no size/rate gate beyond `_osc52`'s 100KB truncation — the
    symmetric direction of the policy `_strip_dangerous_escapes` (tui.py)
    enforces for OSC sequences arriving FROM a subprocess: a single
    confirm/click is the only gate on writing arbitrary model-controlled
    content to the real OS clipboard. Accepted risk, same trust boundary as
    the rest of the model's output; documented rather than rate-limited,
    since a legitimate large copy (a full session export) is a normal use."""
    over_ssh = bool(os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"))
    if not over_ssh:
        tool = _local_tool(text)
        if tool:
            return tool
    if _osc52(text):
        # R235: OSC52 is the ONE path that silently drops data — it caps the
        # payload, and being fire-and-forget it can't report that back. The
        # docstring above names "a full session export" as a normal large
        # copy, and such an export sails past the cap, so reporting a bare
        # success handed the user a truncated paste with nothing to notice.
        if _osc52_would_truncate(text):
            return ("OSC52 (terminal) — TRUNCATED, only the first ~75KB was "
                    "sent (terminals cap OSC52); use a local clipboard tool "
                    "or write to a file for the whole thing")
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
    payload = base64.b64encode(text.encode())[:OSC52_MAX_B64]
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
