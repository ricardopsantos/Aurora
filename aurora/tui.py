"""Full-screen terminal UI — chat pane that scrolls, prompt pinned at the
bottom (requirement: scrolling the conversation must never move the input).

Layout (prompt_toolkit Application, alternate screen):

    ┌───────────────────────────────────────┐
    │ chat pane (scrollable)                  │
    ├───────────────────────────────────────┤  ← dim rule
    │ > input (Ctrl+J newline)                │
    │ model │ ctx │ cost │ session          │
    │ / commands · ! bash · ? Help · Esc…   │
    └───────────────────────────────────────┘

Reuse strategy: all existing flows (slash commands, /model picker, approvals,
bootstrap prompts) are plain print()/input() code in ui.py. A single worker
"session thread" runs them with sys.stdout redirected into the chat pane and
builtins.input() routed to the pinned input field ("question mode"), so the
whole REPL feature set works unchanged inside the full-screen app. The UI
event loop itself never prints.
"""

import builtins
import io
import os
import queue
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.completion import Completer, PathCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    ScrollablePane,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu, CompletionsMenuControl
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from . import bootstrap, colors, rewind, tools, ui
from .colors import BOLD, CYAN, GREEN, RED, RESET, URL_RE, YELLOW, dim
from .colors import strip_dangerous_escapes as _strip_dangerous_escapes
from .engine import Engine
from .paths import aurora_home

_DOUBLE_CLICK_S = 0.3     # same window prompt_toolkit's BufferControl uses
_SCROLL_STEP = 3          # wheel ticks are per-notch; keep it gentle
_PAGE_STEP = 10

# R102: tools whose status-bar phase word gets replaced by a short "what's
# actually running" label — the ones that can run long enough, and opaquely
# enough, that "thinking… Ns" is actively misleading while they execute.
# Deliberately NOT every tool: a read/grep/edit is near-instant, and naming
# every tool call here would be status-bar churn, not a useful signal.
_RUNNING_COMMAND_TOOLS = {"run_command": "running", "wait_until": "waiting on"}
_RUNNING_NOTE_MAX = 60

# R110: /nano — a small built-in text editor, not a shell-out to real nano
# (bash mode has no PTY — see R107's docstring — so an actual interactive
# terminal program can't run through it). Extension allowlist + size cap
# keep it to "plain text files", the only thing it's meant to handle.
_NANO_EXTS = (".txt", ".md", ".json", ".yml", ".yaml", ".xml", ".sh")
_NANO_MAX_BYTES = 1_000_000
_NANO_WHEEL_LINES = 3   # document lines per mouse-wheel notch
# filenames inside bash-mode command output (e.g. `ls`) get linkified the
# same way bare URLs already are (see _linkify_fragments) — backtracking
# naturally excludes trailing punctuation (a comma, closing paren, ...)
# without needing an explicit boundary: greedy \S+ only holds onto exactly
# what's needed for the extension alternation to match. The trailing
# `(?!\S)` is NOT optional, though: without it "notes.txtbak" or
# "archive.txt.bak" would match just the "notes.txt"/"archive.txt" PREFIX
# (nothing after the alternation forces the rest to be consumed) — a
# wrong/nonexistent filename that either fails to open or, worse, silently
# opens some unrelated file that happens to share that prefix.
_NANO_FILENAME_RE = re.compile(
    r"\S+\.(?:" + "|".join(e[1:] for e in _NANO_EXTS) + r")(?!\S)",
    re.IGNORECASE)


class _SafeCompletionsMenuControl(CompletionsMenuControl):
    """prompt_toolkit's completion-menu mouse handler asserts an active
    `complete_state` on MOUSE_UP, but a stray click on the (now stale) menu
    region after completions cleared — e.g. clicking after returning to the
    terminal window — arrives with `complete_state is None` and the assert
    crashes the whole app. Ignore mouse events when no completion is active."""
    def mouse_handler(self, mouse_event):
        if get_app().current_buffer.complete_state is None:
            return None
        return super().mouse_handler(mouse_event)


def _completions_menu() -> CompletionsMenu:
    """A CompletionsMenu whose control is crash-guarded (see above)."""
    menu = CompletionsMenu(max_height=10, scroll_offset=1)
    menu.content.content = _SafeCompletionsMenuControl()
    return menu


class _ChatControl(FormattedTextControl):
    """Chat text control that consumes wheel events itself — the default
    Window scroll would fight the follow-the-tail cursor anchor. Everything
    else (e.g. clicks on a collapsed-thinking header fragment) goes to the
    normal per-fragment dispatch."""

    def __init__(self, tui, **kw):
        self._tui = tui
        super().__init__(**kw)

    def mouse_handler(self, mouse_event):
        ev = mouse_event.event_type
        if ev == MouseEventType.SCROLL_UP:
            self._tui.scroll_by(-_SCROLL_STEP)
            return None
        if ev == MouseEventType.SCROLL_DOWN:
            self._tui.scroll_by(_SCROLL_STEP)
            return None
        # drag-select → auto-copy (R48). position is in content coords
        # (unwrapped line, column) — the Window undoes wrapping for us.
        pos = (mouse_event.position.y, mouse_event.position.x)
        if (ev == MouseEventType.MOUSE_DOWN
                and mouse_event.button == MouseButton.LEFT):
            self._tui.sel_begin(pos)
            return None
        if (ev == MouseEventType.MOUSE_MOVE
                and mouse_event.button == MouseButton.LEFT):
            return self._tui.sel_drag(pos)
        if ev == MouseEventType.MOUSE_UP:
            if self._tui.sel_finish():
                return None          # a drag ended in a copy — swallow it
            # plain click → per-fragment handlers (thinking toggle)
            return super().mouse_handler(mouse_event)
        return super().mouse_handler(mouse_event)


def _span(text, y, x):
    """(line, col) reached after `text`, from (y, x)."""
    nl = text.count("\n")
    if nl:
        return y + nl, len(text) - text.rfind("\n") - 1
    return y, x + len(text)


def _overlay(frags, start, end):
    """Re-style the (start..end) content range (tuple-compared (line, col)
    positions) in reverse video. Splits only the fragments the selection
    crosses; everything fully outside passes through untouched.

    R96c: "passes through untouched" is now a list SLICE rather than an
    append per fragment. This runs on every frame while a selection is live
    or frozen — and a drag invalidates on every mouse-move — so the old
    per-fragment Python loop over the whole transcript cost 21ms/frame at
    102k fragments and got worse as the session grew. A fragment's position
    is only knowable from everything before it, so the walk to find the
    first crossing fragment is still linear; but walking is just a
    `count("\\n")` each, and it no longer builds a list as it goes.
    """
    n = len(frags)
    y, x = 0, 0
    i = 0
    while i < n:                       # walk to the first crossed fragment
        ey, ex = _span(frags[i][1], y, x)
        if (ey, ex) > start:
            break
        y, x = ey, ex
        i += 1
    if i == n:
        return list(frags)             # selection is past the content

    mid = []
    j = i
    while j < n and (y, x) < end:      # rebuild only what the range crosses
        f = frags[j]
        style, text = f[0], f[1]
        segs, cur, cin = [], [], False
        for ch in text:
            ins = start <= (y, x) < end
            if cur and ins != cin:
                segs.append((cin, "".join(cur)))
                cur = []
            cin = ins
            cur.append(ch)
            y, x = (y + 1, 0) if ch == "\n" else (y, x + 1)
        if cur:
            segs.append((cin, "".join(cur)))
        mid += [((style + " reverse") if inside else style, s, *f[2:])
                for inside, s in segs]
        j += 1
    return frags[:i] + mid + frags[j:]


def _open_url(url: str) -> None:
    import shutil
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener):
        subprocess.Popen([opener, url],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import webbrowser
        webbrowser.open(url)


def _url_click_handler(url: str):
    def handler(mouse_event):
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            _open_url(url)
            return None
        return NotImplemented
    return handler


def _merge_char_runs(frags):
    """`ANSI(text).__pt_formatted_text__()` emits ONE fragment PER
    CHARACTER (confirmed against the installed prompt_toolkit — it does
    not coalesce even same-style, escape-free runs). A substring regex
    (URL_RE, _NANO_FILENAME_RE) can never match a multi-character span
    against single-character fragments, so every downstream linkify pass
    needs same-style neighbors merged back into runs first — this was a
    silent no-op for URL-click before R110 surfaced it while wiring up
    filename-click (bash-mode output only).

    R148: accumulate each run in a list and `"".join` it once, instead of
    rebuilding `out[-1]`'s string per character. Since ANSI emits one
    fragment per character, the old `out[-1] = (f[0], out[-1][1] + f[1])`
    was a fresh string AND a fresh tuple per character — quadratic in run
    length, and CPython's in-place `+=` fast path doesn't apply because the
    target is a tuple slot, not a local. Measured on the real 4096-char
    `_MERGE_LIMIT` tail: 1.09ms → 0.49ms (2.2x); at 16k chars 6.18ms →
    1.90ms (3.3x), the gap widening exactly as a quadratic would. This runs
    on the tail entry on every frame while streaming."""
    out: list = []
    run: list[str] = []          # pending text for out[-1], not yet joined

    def _flush():
        if run:
            out[-1] = (out[-1][0], out[-1][1] + "".join(run))
            run.clear()

    for f in frags:
        if (out and not run and out[-1][0] == f[0]
                and len(out[-1]) == 2 and len(f) == 2):
            run.append(f[1])                     # start a run
        elif run and out[-1][0] == f[0] and len(f) == 2:
            run.append(f[1])                     # extend it
        else:
            _flush()
            out.append(f)
    _flush()
    return out


def _linkify_fragments(frags):
    """Split any bare URL out of each fragment's text and re-style it
    cyan+underline with a click handler that opens it (R3-style clickable
    links, chat pane only — see colors.linkify for the classic REPL's OSC-8
    equivalent, which this deliberately does NOT use; see colors.IN_TUI).
    Callers must pass already-merged fragments (see _merge_char_runs)."""
    out = []
    for f in frags:
        style, text = f[0], f[1]
        pos = 0
        matched = False
        for m in URL_RE.finditer(text):
            matched = True
            if m.start() > pos:
                out.append((style, text[pos:m.start()], *f[2:]))
            out.append(("class:link", m.group(0), _url_click_handler(m.group(0))))
            pos = m.end()
        if not matched:
            out.append(f)
        elif pos < len(text):
            out.append((style, text[pos:], *f[2:]))
    return out





class _ChatWriter(io.TextIOBase):
    """A file-like stdout: every write lands in the chat pane. The worker
    thread is the only printer; prompt_toolkit renders through its own
    Output object, so the redirect never touches the UI's escape codes.

    R170k: `write()` buffers until a newline instead of calling
    `self._tui.append()` (lock + `app.invalidate()`) on every single call.
    `print()` itself is the common case this actually helps: it calls
    `.write()` TWICE per statement — once for the joined args, once more
    for `end` (`"\n"` by default) — so `print("hello")` used to be two full
    append() round trips for one visible line. A blob with embedded
    newlines (a multi-line string, a subprocess's captured output) still
    flushes as soon as it's written, same as before — only the split
    between a print()'s content and its trailing newline is now one round
    trip instead of two. `flush()` was a no-op before this and nothing in
    Aurora's own code relied on it doing anything; it now actually pushes
    the buffered remainder, so `print(x, end="")` output — none exists in
    Aurora's own code today, but a user's own extension could reasonably
    write that way for a progress indicator — still reaches the screen the
    moment the writer explicitly asks for it, same contract a real
    terminal gives a partially-buffered stream.

    R171: subclasses `io.TextIOBase` so `isinstance(sys.stdout, io.IOBase)`
    holds for any extension that checks it, and so `write()`'s return value
    (chars ACCEPTED, not chars already flushed to screen) matches the
    standard buffered-stream contract instead of being an undocumented
    surprise — a real `io.BufferedWriter.write()` returns the same way while
    buffering internally."""

    def __init__(self, tui):
        self._tui = tui
        self._buf: list[str] = []

    def write(self, s):
        if not s:
            return 0
        self._buf.append(s)
        if "\n" in s:
            self.flush()
        return len(s)

    def writable(self):
        return True

    def flush(self):
        if self._buf:
            text = "".join(self._buf)
            self._buf = []
            self._tui.append(text)

    def isatty(self):
        return True   # colors.py and friends keep emitting ANSI


class _ModeCompleter(Completer):
    """Dispatches Tab-completion by input mode: `/`-command completion in
    prompt mode (SlashCompleter), filesystem-path completion in bash mode
    (PathCompleter) — so e.g. `cd Xxx<Tab>` completes a matching directory
    the same way a real shell would. `/nano <file>` gets the same
    path-completion treatment in prompt mode (bug fix, R117): SlashCompleter
    only ever completes the command name itself — it returns nothing once
    the text has a space in it (`get_completions`'s `" " in text` guard) —
    so `/nano some-file<Tab>` never offered a single path, unlike `cd` in
    bash mode right next to it."""

    def __init__(self, tui, slash_completer):
        self._tui = tui
        self._slash = slash_completer
        self._path = PathCompleter(get_paths=lambda: [self._tui._bash_cwd])
        self._cwd_path = PathCompleter()   # /nano resolves against the real cwd, not _bash_cwd

    def get_completions(self, document, complete_event):
        if self._tui._bash_mode:
            # PathCompleter treats the WHOLE text before the cursor as the
            # path to complete, so `cd Xxx` must be narrowed to just the
            # trailing word ("Xxx") first — same as a real shell only
            # completing the token under the cursor, not the full command
            # line.
            text = document.text_before_cursor
            word = text.rsplit(None, 1)[-1] if text.strip() else ""
            yield from self._path.get_completions(
                document.__class__(word, len(word)), complete_event)
            return
        text = document.text_before_cursor
        if text.startswith("/nano "):
            word = text[len("/nano "):]
            yield from self._cwd_path.get_completions(
                document.__class__(word, len(word)), complete_event)
            return
        yield from self._slash.get_completions(document, complete_event)


class TuiFrontend(ui.TerminalFrontend):
    """TerminalFrontend already writes everything through sys.stdout /
    input(), which the TUI redirects — overridden here: ask_secret (hidden
    input in the pinned field) and thinking (collapsed clickable block
    instead of an inline marker, à la Copilot)."""

    def __init__(self, tui, **kw):
        super().__init__(**kw)
        self._tui = tui
        self._reset_token_counters()

    def _reset_token_counters(self) -> None:
        self._live_in = 0
        self._live_out = 0
        self._stream_len = 0
        self._think_len = 0

    def ask_secret(self, label: str) -> str:
        return self._tui.ask(label, secret=True)

    def begin_turn(self) -> None:
        super().begin_turn()
        self._reset_token_counters()
        self._tui.set_phase("thinking")
        self._tui.set_running_note("")

    def on_request(self) -> None:
        # every LLM request (each tool round too) gets its own timed row in
        # the chat, mirroring the toolbar's phase+elapsed — even for models
        # that never stream thinking tokens
        self._tui.set_phase("thinking")
        self._tui.begin_think(live=self.show_thinking)

    def end_turn(self) -> None:
        super().end_turn()
        self._tui.finish_think()
        tag = self._final_token_tag()
        if tag:
            self._tui.append(dim(f" {tag}\n"))
        self._reset_token_counters()
        # R102 safety net: a secret-challenge "stop" mid-tool returns from
        # run_turn without ever calling on_tool_result for that call, which
        # would otherwise leave a stale "running: …" note stuck in the
        # status bar for the rest of the session. end_turn() always fires
        # exactly once per turn regardless of how it ended, so it's the
        # right place to guarantee this gets cleared.
        self._tui.set_running_note("")

    def on_think(self, chunk: str) -> None:
        self.think_buffer += chunk            # /copy-last still works
        self._think_len += len(chunk)
        self._tui.think_chunk(chunk, live=self.show_thinking)

    def on_text(self, chunk: str) -> None:
        self._tui.finish_think()              # answer starts → close the block
        self._tui.set_phase("generating")
        self._think_marker_shown = False      # never let the marker logic fire
        self._stream_len += len(chunk)
        super().on_text(chunk)

    def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        self._live_in += input_tokens
        self._live_out += output_tokens
        self._tui.invalidate_status()

    def invalidate_status(self) -> None:
        """R154: the engine's context gauge moved (a tool result was just
        counted) — repaint the status bar. `Tui.invalidate_status` is already
        exception-proof and safe off the event-loop thread."""
        self._tui.invalidate_status()

    def on_tool_start(self, name: str, args: dict) -> None:
        # R102: label the status bar with what's actually running instead
        # of leaving it on "thinking…" for the whole duration — see
        # Tui.set_running_note. The chat-pane echo (super().on_tool_start)
        # is unchanged; this only adds the status-bar side of it.
        label = _RUNNING_COMMAND_TOOLS.get(name)
        if label:
            cmd = " ".join(str(args.get("command", "")).split())
            if len(cmd) > _RUNNING_NOTE_MAX:
                cmd = cmd[:_RUNNING_NOTE_MAX - 1] + "…"
            self._tui.set_running_note(f"{label}: {cmd}" if cmd else f"{label} a command…")
        super().on_tool_start(name, args)

    def on_tool_result(self, name: str, output: str) -> None:
        if name in _RUNNING_COMMAND_TOOLS:
            self._tui.set_running_note("")
        if not self._tui._has_checkpoints and name in tools.NEEDS_APPROVAL:
            # cheap once this flips True (never re-checked); see __init__.
            # R181: NOT just `rewind.head()` — a mutation whose target is
            # outside the checkpointed tree (e.g. a Desktop file) leaves
            # that None forever, hiding the button even though the R181
            # per-file snapshot means there IS something to undo.
            # `undo_preview` checks both.
            self._tui._has_checkpoints = rewind.undo_preview(cwd=".")[0] != "none"
        super().on_tool_result(name, output)

    def _estimated_out(self) -> int:
        # _stream_len and _think_len are already character counts, not text.
        return (self._stream_len + self._think_len) // 4

    @staticmethod
    def _fmt(n: int) -> str:
        return ui.fmt_token_count(n)

    def live_token_tag(self) -> str:
        """Best-known token counts for the live status bar."""
        parts = []
        if self._live_in:
            parts.append(f"↑{self._fmt(self._live_in)}")
        out = max(self._live_out, self._estimated_out())
        if out:
            parts.append(f"↓{self._fmt(out)}")
        return " ".join(parts)

    def _final_token_tag(self) -> str:
        """Final tag appended to the response; uses real usage only."""
        parts = []
        if self._live_in:
            parts.append(f"↑{self._fmt(self._live_in)}")
        if self._live_out:
            parts.append(f"↓{self._fmt(self._live_out)}")
        return " ".join(parts)


class Tui:
    def __init__(self, engine: Engine, debug: bool = False):
        self.engine = engine
        self._debug = debug          # --debug: tint chat green, status bar pink
        self._chat: list = []        # str | think dict
        self._cache: list = []       # per-entry (fragments, nlines) | None
        # R205: per-entry line count, parallel to _chat/_cache, invalidated by
        # the same `_dirty(i)` that invalidates the render cache — so it stays
        # derived from the entries themselves (the drift-free property
        # `_evict_locked` insists on) without rescanning their bytes.
        self._line_counts: list = []
        self._text_cache = None      # flattened fragments for the whole chat
        self._offsets: list = []     # R96b: _offsets[i] = (fragment index,
        # line count) where entry i starts inside _text_cache — lets a rebuild
        # truncate to any dirty entry in O(1) instead of re-flattening
        self._dirty_from: int | None = None   # lowest entry needing a re-parse
        self._clock_key = None       # R95j: displayed second of live think rows
        self._nlines = 0
        self._follow = True          # stick to the tail until the user scrolls
        self._scroll_y = 0
        self._lock = threading.Lock()

        self._inbox: queue.Queue[str] = queue.Queue()   # submitted lines
        self._answers: queue.Queue[str] = queue.Queue() # question-mode replies
        self._question: str | None = None
        self._secret = False
        self._menu_prompt: str | None = None   # select()-mode: arrow-key menu
        self._menu_options: list[tuple[str, str]] | None = None
        self._menu_index = 0
        # set only for a menu opened via _open_ui_menu (Esc-Esc quit/leave-
        # bash) — resolved by calling this back directly instead of the
        # answers queue, since nothing is blocked waiting on select_menu()
        self._menu_on_select: object = None
        # R155: selection text captured when the copy button is clicked,
        # because opening the menu resets the input buffer and would destroy
        # it — see _copy_menu_click.
        self._copy_menu_sel = ""
        # /undo status-bar button (feature request, 2026-07-27): shown once
        # the FIRST checkpoint exists for this project, never hidden again
        # this session. Checked lazily (on a tool result, not on every
        # render — status() runs on a 0.5s ticker) and only while still
        # False, so this costs at most one `rewind.head()` subprocess call
        # per session, not one per render.
        self._has_checkpoints = False
        self._bash_mode = False      # `!` on an empty prompt → persistent bash
        self._bash_cwd = os.getcwd()  # tracked separately: each `!` command
        # runs in its own subprocess, so a plain `cd` inside it never
        # affects the parent process's cwd — we intercept `cd` ourselves
        # and pass this along as `cwd=` to every other command
        self._last_bash_output = ""   # captured stdout+stderr of the last
        self._last_bash_at = 0.0      # `!` command, for "copy last" (R109)
        self._last_llm_at = 0.0       # timestamp of the last LLM turn, so
        # "copy last" knows which of the two happened more recently
        self._editor: dict | None = None   # {"path": Path, "original": str}
        # while set — /nano (R110): the chat area shows an editable buffer
        # instead of the transcript, the input line is hidden, and the
        # status bar swaps to save/close/save-and-close buttons. Built once
        # in _build_app (self._editor_area); this dict is just "is it open"
        # + what to diff/write against.
        self._nano_dirty_cache: bool | None = None   # None = needs recompute.
        # _nano_dirty() is read on every status-bar render (every keystroke,
        # every 0.5s ticker tick, every scroll) — a full buffer-vs-original
        # string compare on each of those for a near-1MB file is real,
        # repeated work for a value that only actually changes once per
        # edit. The buffer's own on_text_changed invalidates this; recompute
        # happens at most once per change, not once per render (same idea as
        # _live_clock_key's "only reparse when the displayed value could
        # actually differ").
        # R125c: "close" while dirty must not discard unsaved edits on the
        # first click — every other risky action in the TUI (quit, leave-
        # bash) confirms first; nano's close button was the one silent
        # exception, and it's the ONLY way out of the editor (no key
        # binding). Armed by the first click, consumed (closes for real) by
        # the second; any further edit, a save, or actually closing all
        # disarm it — see _nano_close_click/_nano_save/_nano_close.
        self._nano_close_confirm = False
        self._exit_confirm = False   # Esc while idle → "exit? [y/N]"
        # generic double-Esc-within-2s confirm gesture, shared by cancel/
        # bash-exit/quit: kind of the pending action ("cancel"|"bash"|"exit")
        # + when it was armed, so a stale/expired first press never counts
        self._esc_armed: str | None = None
        self._esc_armed_at = 0.0
        self._busy = False           # a turn/command is running in the worker
        self._phase = ""             # thinking / generating / working
        # R102: short label shown INSTEAD of _phase while a shell command
        # (run_command/wait_until) is actually executing — see set_running_note
        self._running_note = ""
        self._busy_since = 0.0
        self._spin = 0
        self._ui_thread: threading.Thread | None = None
        self._sel_anchor: tuple | None = None   # (line, col) of MOUSE_DOWN
        self._sel: tuple | None = None           # normalized ((y,x),(y,x)) — live drag
        self._sel_frozen: tuple | None = None    # finished selection, stays
        # highlighted and offers "copy selected" on the status bar until
        # copied or a new drag starts
        self._sel_notice: tuple = ("", 0.0)      # ("copied …", monotonic ts)
        # R212: last distinct status-render failure, so the bar reports it
        # once instead of every frame. See `status()`'s handler.
        self._status_error: str | None = None
        self._input_click_at = 0.0   # R135a: last left MOUSE_UP in the prompt,
        # for the double-click gesture (monotonic)
        self._open_think = False     # a live (undone) think row exists
        # R171/P3: object refs (not indices — `_evict_locked` drops from the
        # FRONT of `_chat`, which would shift any stored index) to every
        # currently-open think row, maintained at create/close time so
        # `_live_clock_key` never has to walk the whole scrollback to find
        # the almost-always-0-or-1 open rows.
        self._open_think_items: list = []
        self._saved_draft = ""       # input text preserved across challenges
        self._help_visible = False
        self._help_text = ANSI(ui.help_text()).__pt_formatted_text__()

        self.fe = TuiFrontend(
            self,
            show_thinking=bool(engine.runtime.get("show_thinking", False)),
            render_md=bool(engine.runtime.get("render_markdown", True)))

        self._build_app()

    # ── chat buffer (plain strings + collapsible thinking entries) ────────
    # Rendering is cached PER ENTRY (parsed fragments + line count): a long
    # session appends thousands of chunks, and re-parsing the whole ANSI
    # transcript on every append is O(n²) over the session. Small consecutive
    # strings merge into one entry so the entry list stays short. `_cache[i]`
    # is (frags, nlines) or None when entry i needs a re-parse.
    #
    # R96b: the per-entry cache alone was not enough. The FLATTENED list was
    # thrown away on every append too, so each frame re-concatenated every
    # fragment in the session — 34ms/frame at 4MB of scrollback, growing for
    # as long as the session runs. Appends always land on the LAST entry, so
    # `_dirty_from` records the lowest changed index and the rebuild resumes
    # from there; the flatten is now proportional to what actually changed.
    _MERGE_LIMIT = 4096

    def _dirty(self, i: int) -> None:
        """Mark entry i for a re-parse and remember the lowest dirty index —
        the next render re-flattens only from there (R96b)."""
        self._cache[i] = None
        self._line_counts[i] = None      # R205
        self._dirty_from = i if self._dirty_from is None \
            else min(self._dirty_from, i)

    # R152: scrollback bound. `_fragments()` itself is flat in session length
    # (R96b did that), but the list it returns is handed WHOLE to
    # prompt_toolkit's FormattedTextControl, whose `create_content` splits and
    # copies every line and hashes every fragment BEFORE its own content cache
    # is consulted — so that half is unavoidably linear and nothing Aurora
    # caches can help. Measured after R148 (fragments ≈ lines):
    #
    #     4.4k lines  9ms/frame     17.7k lines  24ms
    #     8.8k lines 13ms/frame     35.4k lines  44ms   70.8k lines  86ms
    #
    # Every frame, and `append()` invalidates per streamed chunk. At 10k the
    # app still has real headroom (~70fps); past ~35k it visibly degrades.
    # A cap is the only lever, and it is what every terminal emulator does.
    _SCROLLBACK_MAX_LINES = 10_000
    _SCROLLBACK_KEEP_LINES = 8_000

    @staticmethod
    def _entry_lines(item) -> int:
        """Approximate rendered line count for one `_chat` entry. Approximate
        is fine — this decides when to trim a 10k-line backlog, not layout."""
        if isinstance(item, str):
            return item.count("\n") + 1
        # a think/bash_output block also renders a header row (and think a
        # closing clock line), so bias slightly high rather than low
        return (item.get("text") or "").count("\n") + 2

    def _entry_line_count(self, i: int) -> int:
        """R205: `_entry_lines` for entry i, memoised in `_line_counts`.

        The cache is filled lazily and cleared by `_dirty(i)` — the same call
        that invalidates the render cache, and already made at every point an
        entry's text changes — so a stale count is not reachable without also
        leaving a stale rendering, which the render path would surface loudly."""
        n = self._line_counts[i]
        if n is None:
            n = self._line_counts[i] = self._entry_lines(self._chat[i])
        return n

    def _total_lines(self) -> int:
        # fill only the holes, then let `sum` run at C level over the list —
        # a generator calling a method per entry costs more than the adds do
        counts = self._line_counts
        for i, n in enumerate(counts):
            if n is None:
                counts[i] = self._entry_lines(self._chat[i])
        return sum(counts)

    def _evict_locked(self) -> int:
        """Drop oldest entries once the transcript passes
        `_SCROLLBACK_MAX_LINES`, down to `_SCROLLBACK_KEEP_LINES`. Caller holds
        `self._lock`. Returns entries dropped.

        Called only when a NEW entry is created, never on a merge into the
        last one — so the sum here runs about once per `_MERGE_LIMIT` of
        output, not per streamed chunk. Recomputing the total from the entries
        themselves each time (rather than maintaining a running counter across
        four append sites) keeps it drift-free.

        R205: still recomputed, but from MEMOISED per-entry counts. The sum
        used to call `_entry_lines` on every entry, and that does
        `text.count("\\n")` over the entry's whole text — so each new entry
        rescanned every byte of the transcript, and a session that creates
        entries rather than merging into one (tool results, notices, think
        rows) was quadratic in total bytes. Measured on entries just over
        `_MERGE_LIMIT`: 2.1ms per new entry at 2k entries, 8.1ms at 12k,
        against 1.2us when text merges — 96 SECONDS to append 12k entries.
        With the memo it is an int sum over ~5k cached values.

        The drift-free property is unchanged: counts are still derived from
        the entries, just not re-derived when nothing changed — `_dirty(i)`
        clears the memo at every point an entry's text is mutated.

        Trimming shifts every absolute line coordinate, so the flattened
        caches are dropped wholesale and any selection is INVALIDATED rather
        than remapped: a live drag or a frozen "copy selected" range is
        cheap to redo and easy to get subtly wrong. The full re-flatten this
        forces costs one frame's worth of work, amortized over the ~2k lines
        between the high and low marks."""
        total = self._total_lines()
        if total <= self._SCROLLBACK_MAX_LINES:
            return 0
        dropped = 0
        # never drop the newest entry — think_chunk/append hold a reference to
        # it and keep writing into it
        while len(self._chat) > 1 and total > self._SCROLLBACK_KEEP_LINES:
            total -= self._entry_line_count(0)
            del self._chat[0]
            if self._cache:
                del self._cache[0]
            if self._line_counts:
                del self._line_counts[0]   # R205: stays parallel to _chat
            dropped += 1
        if dropped:
            self._text_cache = None      # force a full re-flatten
            self._offsets = []
            self._nlines = 0
            self._dirty_from = None
            self._sel = self._sel_frozen = self._sel_anchor = None
        return dropped

    def append(self, s: str) -> None:
        # R171: R170l stripped OSC/DCS/APC/PM/SOS only from bash_output, on
        # the reasoning that model output is "trusted-ish" — but a
        # compromised or prompt-injected model can put an OSC 52 clipboard
        # write (or a window-title/DCS payload) in its own reply just as
        # easily as a subprocess can, and this is the path that reply
        # renders through. Same strip, same CSI-colors-untouched behavior.
        s = _strip_dangerous_escapes(s)
        with self._lock:
            # plain output (tool start/result, notices) means the request
            # moved past its thinking phase — close the live row, or a
            # tool-only round (no on_text) leaves it "thinking…" forever
            self._close_think_locked()
            if (self._chat and isinstance(self._chat[-1], str)
                    and len(self._chat[-1]) < self._MERGE_LIMIT):
                self._chat[-1] += s
                self._dirty(len(self._chat) - 1)
            else:
                self._chat.append(s)
                self._cache.append(None)
                self._line_counts.append(None)
                self._dirty(len(self._chat) - 1)
                self._evict_locked()          # R152
        try:
            self.app.invalidate()
        except Exception:
            pass

    def clear_screen(self) -> None:
        """Bash-mode `clear`/`cls` (R116): a real terminal's `clear` resets
        the visible screen, but here stdout is captured by `subprocess.run`
        (not connected to a tty), so running it for real just prints a
        useless escape-sequence blob into the transcript. Emulate the intent
        instead — wipe the chat scrollback itself.

        Bug fix: clearing `_chat`/`_cache` alone left `_text_cache` (the
        flattened fragments `_fragments()` actually renders) untouched —
        `_rebuild_locked` only re-flattens when `_dirty_from is not None or
        _text_cache is None` (R96b's fast path), so with `_dirty_from` reset
        to `None` the stale pre-clear screen kept rendering forever.
        `_text_cache = None` forces the full rebuild, which is cheap here
        since `_chat` is empty."""
        with self._lock:
            self._chat.clear()
            self._cache.clear()
            self._line_counts.clear()
            self._dirty_from = None
            self._text_cache = None
            self._offsets = []
            self._nlines = 0
        try:
            self.app.invalidate()
        except Exception:
            pass

    def append_bash_output(self, text: str) -> None:
        """Bash-mode command output (R10) — same rendering path as
        append(), but tagged as its own entry kind (not merged into a plain
        string entry) so _entry_fragments can linkify filenames within it
        (R110) without doing that anywhere else in chat (LLM prose, etc.).
        R170l: raw subprocess output, so it's the one entry kind stripped
        of OSC/DCS/APC/PM/SOS control sequences before storage — see
        `_strip_dangerous_escapes`."""
        text = _strip_dangerous_escapes(text)
        with self._lock:
            self._close_think_locked()
            self._chat.append({"kind": "bash_output", "text": text})
            self._cache.append(None)
            self._line_counts.append(None)
            self._dirty(len(self._chat) - 1)
            self._evict_locked()              # R152
        try:
            self.app.invalidate()
        except Exception:
            pass

    def begin_think(self, live: bool = False) -> None:
        """Open this request's timed row ('✻ thinking… Ns') the moment the
        request starts — before (or without) any thinking tokens. `live`
        (/thinking toggle) starts it expanded so text streams visibly."""
        import time
        with self._lock:
            last = self._chat[-1] if self._chat else None
            if (isinstance(last, dict) and last.get("kind") == "think"
                    and not last["done"]):
                return                      # this request's row already exists
            row = {"kind": "think", "text": "", "open": live,
                  "done": False, "t0": time.monotonic(), "dt": 0}
            self._chat.append(row)
            self._open_think_items.append(row)
            self._cache.append(None)
            self._line_counts.append(None)
            self._dirty(len(self._chat) - 1)
            self._evict_locked()              # R152
            self._open_think = True
        self.app.invalidate()

    def think_chunk(self, chunk: str, live: bool = False) -> None:
        """Grow the current request's thinking block (create it if on_request
        didn't, e.g. under a plain frontend test); collapsed by default — a
        click on its header expands it."""
        with self._lock:
            last = self._chat[-1] if self._chat else None
            if not (isinstance(last, dict) and last.get("kind") == "think"
                    and not last["done"]):
                import time
                last = {"kind": "think", "text": "", "open": live,
                        "done": False, "t0": time.monotonic(), "dt": 0}
                self._chat.append(last)
                self._open_think_items.append(last)
                self._cache.append(None)
                self._line_counts.append(None)
                self._evict_locked()          # R152
                self._open_think = True
            # R204: the reasoning stream is model output, exactly like the
            # reply `append()` sanitizes — R171's argument ("a compromised or
            # prompt-injected model can put an OSC 52 in its own reply just as
            # easily as a subprocess can") is about the MODEL, not about which
            # of its two output channels carried it. This path was missed, so
            # a payload in `reasoning_content` reached `_chat` raw and was
            # copied out by /copy-all with it.
            last["text"] += _strip_dangerous_escapes(chunk)
            self._dirty(len(self._chat) - 1)
        self.app.invalidate()

    def _close_think_locked(self) -> None:
        """Close EVERY open think row (normally at most one; sweeping all of
        them guarantees no stuck live clock can survive — a single leaked row
        would disable the render cache for the whole session)."""
        import time
        if not self._open_think:
            return
        now = time.monotonic()
        for i, item in enumerate(self._chat):
            if (isinstance(item, dict) and item.get("kind") == "think"
                    and not item["done"]):
                item["done"] = True
                item["dt"] = now - item.get("t0", now)
                # R204: one full-text pass now that the stream is complete —
                # defence in depth, not the mechanism. A sequence SPLIT across
                # two chunks is already handled upstream, because R198's
                # unterminated alternative is greedy to end-of-text: a chunk
                # ending mid-sequence has its partial introducer removed as it
                # arrives, so the continuation lands as ordinary text. This
                # pass exists so that stops being load-bearing on a detail of
                # another rule's greediness. Once per request, never per
                # chunk — re-scanning the accumulated text on every chunk
                # would be O(n²) over a 20k-token reasoning stream.
                item["text"] = _strip_dangerous_escapes(item["text"])
                self._dirty(i)
        self._open_think_items = []
        self._open_think = False

    def finish_think(self) -> None:
        with self._lock:
            self._close_think_locked()

    def _think_toggler(self, entry, index):
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                with self._lock:
                    entry["open"] = not entry["open"]
                    self._dirty(index)
                self.app.invalidate()
                return None
            return NotImplemented
        return handler

    def _linkify_filenames(self, frags):
        """Second pass over already-URL-linkified fragments (bash-mode
        output only, see append_bash_output): splits out filenames matching
        _NANO_FILENAME_RE and makes each one clickable-to-edit via
        open_nano, resolved against the tracked bash cwd (R107) — same
        idea as _linkify_fragments for URLs, one level up."""
        out = []
        for f in frags:
            style, text = f[0], f[1]
            if style == "class:link" or len(f) > 2:
                out.append(f)          # already a URL/handled fragment
                continue
            pos = 0
            matched = False
            for m in _NANO_FILENAME_RE.finditer(text):
                matched = True
                if m.start() > pos:
                    out.append((style, text[pos:m.start()]))
                name = m.group(0)
                out.append(("class:link", name, self._nano_filename_click(name)))
                pos = m.end()
            if not matched:
                out.append(f)
            elif pos < len(text):
                out.append((style, text[pos:]))
        return out

    def _nano_filename_click(self, name: str):
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                self.open_nano(Path(self._bash_cwd) / name)
                self.app.invalidate()
        return handler

    def _entry_fragments(self, item, index):
        if isinstance(item, str):
            return _linkify_fragments(
                _merge_char_runs(ANSI(item).__pt_formatted_text__()))
        if isinstance(item, dict) and item.get("kind") == "bash_output":
            frags = _linkify_fragments(
                _merge_char_runs(ANSI(item["text"]).__pt_formatted_text__()))
            return self._linkify_filenames(frags)
        import time
        secs = int(item["dt"] if item["done"]
                   else time.monotonic() - item.get("t0", time.monotonic()))
        state = (f"thinking… {secs}s" if not item["done"]
                 else f"thought for {secs}s")
        if item["text"]:      # expandable — same header info as the toolbar
            arrow = "▾" if item["open"] else "▸"
            hint = "click to hide" if item["open"] else "click to read"
            frags = [("class:think.header underline",
                      f"{arrow} {state} — {hint}",
                      self._think_toggler(item, index)),
                     ("", "\n")]
            if item["open"]:
                frags.append(("class:think.body",
                              item["text"].rstrip("\n") + "\n"))
        else:                 # no thinking tokens (yet) — timed row only
            frags = [("class:think.header", f"✻ {state}"), ("", "\n")]
        return frags

    def _live_clock_key(self):
        """The whole-second value each live think row currently displays.

        A live row's header carries a running clock, so its fragments really
        do go stale — but only once per SECOND, while `_fragments` runs on
        every render: the 0.5s ticker, every keystroke, every mouse move,
        every status invalidate. Rebuilding the entire transcript on all of
        them (R95j) is work proportional to the whole scrollback for a clock
        that mostly hasn't changed. Keying on the displayed second rebuilds
        exactly when the display would differ.
        """
        import time
        now = time.monotonic()
        # R171/P3: was `for item in self._chat` — O(scrollback) per render
        # (every keystroke, every 0.5s ticker tick, every mouse move) to find
        # rows that are almost always 0 or 1 and always near the tail.
        # `_open_think_items` is maintained at create/close time instead.
        return tuple(int(now - item.get("t0", now))
                     for item in self._open_think_items if not item["done"])

    def _rebuild_locked(self) -> None:
        """Re-flatten `_text_cache` from the lowest dirty entry onward (R96b).

        `_offsets[i]` is (fragment index, line count) at the point entry i
        begins, so truncating to a dirty index is a `del` on the tail rather
        than a full re-concatenation. Callers hold `self._lock`; the list is
        mutated in place, which is safe because every reader of
        `_text_cache` (`_render_fragments`, `_sel_text`) runs on the UI
        thread — the worker only ever marks entries dirty.
        """
        start = 0 if self._text_cache is None else max(self._dirty_from or 0, 0)
        self._dirty_from = None
        if start == 0:
            self._text_cache, self._offsets, total = [], [], 0
        elif start >= len(self._offsets):
            # nothing already-flattened changed (the common case: a brand new
            # entry was appended) — resume at the end of what we have
            start, total = len(self._offsets), self._nlines
        else:
            fo, total = self._offsets[start]
            del self._text_cache[fo:]
            del self._offsets[start:]
        out = self._text_cache
        for i in range(start, len(self._chat)):
            self._offsets.append((len(out), total))
            cached = self._cache[i]
            if cached is None:
                frags = self._entry_fragments(self._chat[i], i)
                nl = sum(f[1].count("\n") for f in frags)
                cached = self._cache[i] = (frags, nl)
            out += cached[0]
            total += cached[1]
        self._nlines = total

    def _fragments(self):
        with self._lock:
            if self._open_think:
                key = self._live_clock_key()
                if key != self._clock_key:
                    # the displayed second moved — re-parse only the live
                    # rows (they are normally just the tail one), not the
                    # whole transcript
                    self._clock_key = key
                    for i, item in enumerate(self._chat):
                        if (isinstance(item, dict) and item.get("kind") == "think"
                                and not item["done"]):
                            self._dirty(i)
            if self._dirty_from is not None or self._text_cache is None:
                self._rebuild_locked()
            return self._text_cache

    # ── drag-select → "copy selected" button (R48) ─────────────────────────
    def _unpad(self, pos: tuple) -> tuple:
        """Mouse positions are content coords, and the rendered content is
        top-padded on a short transcript (`_render_fragments` prepends blank
        rows) — selections are stored in UNPADDED text coords so `_sel_text`
        indexes the real transcript, and `_render_fragments` shifts them back
        by the pad when overlaying."""
        return (max(0, pos[0] - self._pad()), pos[1])

    def sel_begin(self, pos: tuple) -> None:
        self._sel_frozen = None   # a fresh drag drops any pending selection
        # …and so does the prompt's, so only one selection is ever live:
        # "copy selected" is a single button and must never be ambiguous
        # about which of the two panes it copies (R135b).
        self.input.buffer.exit_selection()
        self._sel_anchor, self._sel = self._unpad(pos), None
        self.app.invalidate()

    def sel_drag(self, pos: tuple):
        if self._sel_anchor is None:
            return NotImplemented
        pos = self._unpad(pos)
        self._sel = (min(self._sel_anchor, pos), max(self._sel_anchor, pos))
        self.app.invalidate()
        return None

    def sel_finish(self) -> bool:
        """MOUSE_UP: freeze the dragged range — stays highlighted and offers
        "copy selected" on the status bar until tapped (or a new drag starts).
        True = a drag happened (the caller swallows the click); False = it
        was a plain click."""
        sel, self._sel_anchor, self._sel = self._sel, None, None
        if sel is None or sel[0] == sel[1]:
            self.app.invalidate()
            return False
        if self._sel_text(sel).strip():
            self._sel_frozen = sel
        self.app.invalidate()
        return True

    def _input_sel_text(self) -> str:
        """Text selected in the prompt (drag or double-click), "" if none.

        R135b: `cut_selection()` on the *document* returns the (new document,
        clipboard data) pair without touching the buffer — `Buffer.copy_selection`
        would drop the selection as a side effect, which would make the very
        act of rendering the "copy selected" button erase what it copies."""
        buf = self.input.buffer
        if buf.selection_state is None:
            return ""
        return buf.document.cut_selection()[1].text

    def _sel_text(self, sel: tuple) -> str:
        (y0, x0), (y1, x1) = sel
        lines = "".join(f[1] for f in self._fragments()).split("\n")
        y0, y1 = min(y0, len(lines) - 1), min(y1, len(lines) - 1)
        if y0 == y1:
            return lines[y0][x0:x1]
        return "\n".join([lines[y0][x0:]] + lines[y0 + 1:y1] + [lines[y1][:x1]])

    def _pad(self) -> int:
        """Blank rows prepended so a short transcript hugs the input line
        (bottom-anchored, terminal-style) instead of floating at the top of
        the pane — keeps a challenge adjacent to the text that raised it."""
        info = getattr(self._chat_win, "render_info", None)
        if info is None:
            return 0
        return max(0, info.window_height - self._nlines - 1)

    def _render_fragments(self):
        """What the chat control actually renders: the cached fragments,
        top-padded to bottom-anchor short content, with the live selection
        overlaid in reverse video (cache untouched)."""
        frags = self._fragments()
        pad = self._pad()
        if pad:
            frags = [("", "\n" * pad)] + frags
        sel = self._sel or self._sel_frozen
        if sel is None:
            return frags
        # selections are stored unpadded (see _unpad) — shift back for render
        (y0, x0), (y1, x1) = sel
        return _overlay(frags, (y0 + pad, x0), (y1 + pad, x1))

    # ── scrolling ─────────────────────────────────────────────────────────
    def scroll_by(self, n: int) -> None:
        if self._follow:
            self._scroll_y = self._nlines
        self._scroll_y = max(0, min(self._scroll_y + n, self._nlines))
        self._follow = self._scroll_y >= self._nlines
        self.app.invalidate()

    def scroll_end(self) -> None:
        self._follow = True
        self.app.invalidate()

    def _cursor(self):
        # the Window keeps this point visible — anchoring it to the last line
        # gives follow-the-tail; anchoring to _scroll_y holds a scroll spot.
        # Offsets by the bottom-anchor padding (0 once the pane is full).
        return Point(0, self._pad()
                     + (self._nlines if self._follow else self._scroll_y))

    def set_phase(self, phase: str) -> None:
        if phase != self._phase:
            self._phase = phase
            self.app.invalidate()

    def set_running_note(self, note: str) -> None:
        """R102: what the busy status line shows in place of the phase word
        while a shell command is actually executing. Without this, the
        status bar kept saying "thinking… Ns" for the whole duration of a
        run_command/wait_until call — on_request() sets phase "thinking"
        once per LLM request, and nothing re-labels it for the tool-
        execution window that follows a response with tool_calls, which is
        exactly when the model ISN'T thinking, it's waiting on a subprocess.
        `note` empty clears it, reverting to the normal phase word."""
        if note != self._running_note:
            self._running_note = note
            self.app.invalidate()

    def invalidate_status(self) -> None:
        try:
            self.app.invalidate()
        except Exception:
            pass

    # ── question mode (blocking asks from the worker thread) ─────────────
    def ask(self, prompt: str = "", secret: bool = False) -> str:
        # The UI (event-loop) thread is the one that DELIVERS answers, so an
        # ask() from it (e.g. via the builtins.input monkeypatch) can never
        # be answered — it deadlocks silently. Fail loudly instead. Any other
        # thread is fine: the worker calls engine.send() directly (R96f — no
        # nested per-turn thread in the TUI), so a mid-turn key prompt
        # arrives from the worker thread itself.
        if (self._ui_thread is not None
                and threading.current_thread() is self._ui_thread):
            raise RuntimeError(
                "ask()/input() called from the TUI event-loop thread — this "
                "would deadlock; route it through the session worker")
        # the question is NOT printed into the chat — it becomes the input
        # line's prompt, so the cursor sits right after it; the answered pair
        # is echoed into the transcript by the enter handler
        q = prompt or "?"
        if not q.endswith((" ", "\n")):
            q += " "
        # preserve any draft the user was typing before the challenge took
        # over the input line; restore it after the challenge is answered
        self._saved_draft = self.input.document.text
        self._question, self._secret = q, secret
        self.input.buffer.reset()
        self.app.invalidate()
        try:
            return self._answers.get()
        finally:
            self._question, self._secret = None, False
            # restore the draft the user was typing before the challenge
            # took over the input line; assign .text directly to avoid the
            # async completer that insert_text() would trigger. R170d: the
            # .text setter only clamps cursor_position if it now exceeds the
            # new text's length — it never MOVES it, so it stayed wherever
            # buffer.reset() left it (0) instead of where the user was
            # actually typing. Put it back at the end, same place Enter/
            # normal typing would leave it.
            self.input.buffer.text = self._saved_draft
            self.input.buffer.cursor_position = len(self._saved_draft)
            self._saved_draft = ""
            self.app.invalidate()

    def select_menu(self, prompt: str, options: list[tuple[str, str]],
                    default_index: int | None = None) -> str | None:
        """Arrow-key menu, rendered in place of the input prompt (see `ask`
        for the same blocking-from-worker-thread contract). `default_index`
        only sets which row starts highlighted (e.g. the current model in
        `/model`) — Enter always requires the user to actually press it on
        that row, so this carries none of the classic REPL's blank-Enter
        safety concern (see `ui.select`). Returns `None` if the menu was
        explicitly dismissed instead of picked (e.g. a second click on the
        status bar's model name while the `/model` menu is open) — callers
        that care must treat `None` as "no change", same spirit as the
        classic REPL's blank-Enter-keeps-current behavior."""
        if (self._ui_thread is not None
                and threading.current_thread() is self._ui_thread):
            raise RuntimeError(
                "select_menu() called from the TUI event-loop thread — this "
                "would deadlock; route it through the session worker")
        # R142a: an Esc-Esc confirm (`_open_ui_menu`) may already own the one
        # menu slot, and the two resolve by DIFFERENT routes — `_resolve_menu`
        # checks `_menu_on_select` first. Overwriting the prompt/options
        # without clearing that callback delivers THIS menu's answer to the
        # Esc confirm's resolver, so nothing ever reaches `_answers` and the
        # worker blocks forever mid-turn. Dropping the unanswered confirm
        # loses a question the user can simply ask again; keeping it loses
        # the session.
        if self._menu_on_select is not None:
            self._menu_on_select = None
            self.append(dim("· pending confirm dismissed — answer this first\n"))
        self._menu_prompt, self._menu_options = prompt, options
        self._menu_index = default_index or 0
        if hasattr(self, "input"):        # a stale draft must not bleed under the menu
            self._saved_draft = self.input.document.text
            self.input.buffer.reset()
        self.app.invalidate()
        try:
            return self._answers.get()
        finally:
            self._menu_prompt = self._menu_options = None
            if hasattr(self, "input"):    # restore the draft the user was typing
                self.input.buffer.text = self._saved_draft
                # R170d: same fix as ask() — the .text setter doesn't move
                # cursor_position to match, so it stayed at 0 from reset().
                self.input.buffer.cursor_position = len(self._saved_draft)
                self._saved_draft = ""
            self.app.invalidate()

    def update_menu_labels(self, prompt: str,
                           options: list[tuple[str, str]]) -> bool:
        """Swap the labels of the currently-open menu, keeping the highlighted
        row where it is, and repaint. Used by `/model`'s background price
        refresh (`_pick_model`): the fetch lands a second or two after the
        menu is already on screen, and the whole point is that the user sees
        the fresh price without reopening the picker.

        Guarded on prompt AND option count/keys matching, because the callback
        arrives from a network thread that has no idea what happened
        meanwhile: the user may have already picked a model and be sitting in
        an approval menu instead, and silently overwriting THAT menu's rows
        with model names would be a genuinely dangerous mislabel. Returns
        whether the repaint happened. Only the rows' display text changes;
        `_resolve_menu` keeps mapping the index onto the same key it did
        before, so an Enter in flight can't land on a different answer."""
        if not self._menu_options or self._menu_prompt != prompt:
            return False
        if [k for k, _ in options] != [k for k, _ in self._menu_options]:
            return False
        self._menu_options = options
        self.app.invalidate()
        return True

    def _menu_fragments(self):
        """The select() menu as formatted-text fragments for its own Window —
        each option on its own line, the pointer on the current index. Rendered
        as a real multi-line control (NOT the input prompt, whose BeforeInput
        processor turns embedded newlines into literal ^J).

        A label may itself carry raw ANSI colour codes (e.g. /model's [$]/
        [free] tags, built with the same GREEN/YELLOW constants the classic
        REPL prints directly) — parsed via ANSI(), not passed through as
        literal text, or those escapes would show as garbage characters
        instead of colour. Each parsed sub-fragment's own colour (if any) is
        layered ONTO the row's base style (selected/option) rather than
        replacing it, so a plain label (no escapes — the common case: approve/
        confirm menus) keeps exactly its old look."""
        try:
            if not self._menu_options:
                return []
            frags = [("class:menu.prompt",
                      (self._menu_prompt or "select") + "\n")]
            for i, (_, label) in enumerate(self._menu_options):
                selected = i == self._menu_index
                row_style = ("class:menu.selected" if selected
                             else "class:menu.option")
                click = self._menu_row_click(i)
                frags.append((row_style,
                              f" ❯ {i + 1}. " if selected else f"   {i + 1}. ",
                              click))
                for style, text, *_rest in ANSI(label).__pt_formatted_text__():
                    frags.append((f"{row_style} {style}".strip(), text, click))
                frags.append((row_style, "\n", click))
            frags.append(("class:menu.hint",
                          " ↑/↓ move · Enter/click select · number to jump"))
            return frags
        except Exception:
            return [("class:menu.prompt", "\n")]

    def _menu_height(self) -> int:
        # prompt + one row per option + hint row
        try:
            return (len(self._menu_options) + 2) if self._menu_options else 0
        except Exception:
            return 0

    def _menu_row_click(self, index: int):
        """Mouse handler for one `_menu_fragments()` row — click picks it
        outright rather than only moving the pointer, since a menu click
        already commits to a row the way Enter does (there's no hover-only
        state in a terminal). Works for BOTH menu paths (`select_menu()`'s
        blocking answers-queue and `_open_ui_menu()`'s callback) because
        both resolve through `_resolve_menu`, same as every other status-bar
        click handler already routes through it via Enter's key binding."""
        def handler(mouse_event):
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return
            self._menu_index = index
            self._resolve_menu(index)
        return handler

    # R155: the four copy buttons ("session id", "copy last", "copy all",
    # "copy selected") became ONE "copy" button plus a picker. The status bar
    # is fixed-width and every feature that ships a copyable thing wanted
    # another button on it; a menu is the surface that doesn't run out of
    # room, and it gives each option a full phrase instead of an
    # eight-character label ("copy all" never said WHAT — it reads as the
    # visible chat, but the text comes from the session log, which since
    # R152's scrollback cap can hold more than the pane still shows).
    _COPY_LABELS = {
        "last": "copy last",
        "session": "copy whole session transcript",
        "id": "copy session id",
        "selected": "copy selected",
    }

    def _copy_menu_click(self):
        """Click handler for the status bar's "copy" button — opens the
        picker.

        `_open_ui_menu`, not `select_menu()`: a mouse handler runs on the UI
        event-loop thread, and `select_menu()` raises there by design (it
        blocks waiting for an answer only the UI thread can deliver — see
        `ask`). Same non-blocking, callback-delivered path the Esc-Esc
        confirms use.

        **The selection is captured HERE, before the menu opens.**
        `_open_ui_menu` resets the input buffer, which DESTROYS a prompt
        selection — so reading it in the resolver instead would make "copy
        selected" reliably copy nothing, the one option that can't survive
        being asked about. Precedence matches the old `_copy_selected`
        button: a prompt selection wins over a frozen chat one (only one can
        be live at a time, and the prompt's is the one with a visible
        cursor)."""
        def handler(mouse_event):
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return
            sel = self._input_sel_text()
            if not sel and self._sel_frozen is not None:
                sel = self._sel_text(self._sel_frozen)
            # `_open_ui_menu` resets the input buffer, which clears the TEXT
            # as well as the selection — so opening this menu would eat a
            # half-typed prompt. Only the SELECTION needs capturing here
            # (the resolver can no longer read it after reset); the draft
            # itself is now saved/restored generically by `_open_ui_menu`/
            # `_resolve_menu` (R171c) — this used to duplicate that with its
            # own `_copy_menu_draft`, which only ever became a no-op restore.
            self._copy_menu_sel = sel if sel.strip() else ""
            options = [("last", self._COPY_LABELS["last"]),
                       ("session", self._COPY_LABELS["session"]),
                       ("id", self._COPY_LABELS["id"])]
            if self._copy_menu_sel:
                options.append(("selected", self._COPY_LABELS["selected"]))
            # Esc is a no-op while a menu is open (R62 — the pick must be
            # explicit), so a mis-click needs a way out that isn't quitting
            # the menu system: an explicit row.
            options.append(("cancel", "cancel"))
            # a selection the user just made is almost certainly why they
            # clicked copy — start on it. Order stays fixed either way, so
            # the digit shortcuts never change meaning between clicks.
            default = len(options) - 2 if self._copy_menu_sel else 0
            self._open_ui_menu("copy:", options, self._resolve_copy_menu,
                               default_index=default)
        return handler

    def _resolve_copy_menu(self, key: str) -> None:
        """Perform the picked copy. Each branch keeps the threading behavior
        its own button already had (R155 is a UI consolidation, not a
        rethread): the bounded ones copy inline on the UI thread, and the
        whole-session one still goes through the worker inbox because its
        work is unbounded — see `_copy_all_chat`'s R129 note."""
        import time

        from . import clipboard
        sel, self._copy_menu_sel = self._copy_menu_sel, ""
        if key == "cancel":
            self.app.invalidate()
            return
        if key == "session":
            self.append(f"\n{CYAN}{BOLD}> {RESET}/copy-all\n")
            self.scroll_end()
            self._inbox.put("/copy-all")
            return
        if key == "selected":
            if not sel.strip():
                # the selection went away between click and pick (a redraw
                # dropped the frozen one) — say so rather than silently
                # copying nothing
                self._sel_notice = ("selection is gone", time.monotonic())
            else:
                how = clipboard.copy(sel)
                self._sel_notice = (f"copied {len(sel)} chars — {how}",
                                    time.monotonic())
            self._sel_frozen = None
            self.app.invalidate()
            return
        if key == "id":
            # same source the status bar itself used before R155
            # (ContextStats.session_id) rather than reaching into
            # engine.session — no new engine coupling for a moved button
            sid = str(self.engine.context_stats().session_id)
            how = clipboard.copy(sid)
            self._sel_notice = (f"session id copied — {how}", time.monotonic())
            self.app.invalidate()
            return
        # "last"
        text, label = ui._last_copyable_text(self.engine, self.fe)
        if not text:
            self._sel_notice = ("nothing to copy yet", time.monotonic())
        else:
            how = clipboard.copy(text)
            self._sel_notice = (f"{label} copied — {how}", time.monotonic())
        self.app.invalidate()

    def _short_model(self, model: str, local_model: str | None = None) -> str:
        """Status-bar name for a model: the vendor prefix dropped
        (`moonshotai/kimi-k2.7-code` → `kimi-k2.7-code`).

        R134g: line 1 holds identity (R56) and had grown to ~117 chars with a
        real model id — past 80 columns, where prompt_toolkit clips it and
        the rightmost links stop being clickable. The vendor prefix is the
        cheapest 11 characters in it: it never disambiguates anything on
        screen, and the full id is one click away in the picker.

        UNLESS it does disambiguate — two configured models sharing a short
        name keep their full ids, since a status bar that can't tell you
        which of them you're talking to is worse than a long one.

        The `local` sentinel is the one name that identifies nothing on its
        own, so it renders as `local: <loaded model>` when the live name is
        known (same vendor-prefix trim)."""
        if model == "local" and local_model:
            return f"local: {local_model.rsplit('/', 1)[-1]}"
        short = model.rsplit("/", 1)[-1]
        if short == model:
            return model
        others = [m.get("model", "") for m in getattr(self.engine, "models", [])
                  if m.get("model") != model]
        if any(o.rsplit("/", 1)[-1] == short for o in others):
            return model
        return short

    def _compactions_click(self):
        """Click handler for the status bar's `⤵N` compaction counter (R159)
        — asks whether to fold again, and runs `/compact` on yes.

        All three R155 traps apply, because this is a status-bar button that
        opens a menu:
        - `_open_ui_menu`, never `select_menu()`: a mouse handler runs on the
          UI event-loop thread, where `select_menu()` raises by design.
        - the draft is saved/restored generically by `_open_ui_menu`/
          `_resolve_menu` (R171c) — asking "compact again?" must never cost
          the user a half-written prompt, same as every other Esc-Esc/menu
          confirm now gets for free.
        - nothing is read in the resolver that the reset could have destroyed.

        The fold itself goes through the inbox, not this handler: compaction
        is a model request (`compact_history` calls `provider.turn`), so it
        belongs on the worker thread exactly like `/context` and `/copy-all`.
        """
        def handler(mouse_event):
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return
            if not self._click_guard():
                return
            n = self.engine.compactions
            self._open_ui_menu(
                f"context folded {n}× this session — compact again?",
                [("y", "Yes — summarize older history and continue"),
                 ("n", "No")],
                self._resolve_compact_menu, default_index=1)
        return handler

    def _resolve_compact_menu(self, key: str) -> None:
        """The draft is already restored by `_resolve_menu` (R171c) before
        this callback runs — nothing left to do here but act on the pick."""
        if key == "y":
            self._inbox.put("/compact")

    def _undo_click(self):
        """Click handler for the status bar's `undo` button (feature request,
        2026-07-27) — reverts just the last mutation via `/undo`, not the
        whole tree.

        Routed through the inbox, NOT a status-bar confirm menu: a real
        incident showed a generic "undo the last mutation?" confirm (no file
        list — a mouse handler can't safely do the git plumbing `undo_preview`
        needs) let a click meant to undo one file (whose write landed OUTSIDE
        the checkpointed tree, `rewind.covers()`'s documented gap) silently
        revert a different, unrelated, already-sealed batch of real work
        instead — the confirm never named a single path, so there was nothing
        to catch it on. `ui._undo_cmd` (run on the worker thread, like
        `_cost_tree_click` routes `/context`) previews the affected paths and
        confirms against THOSE before touching anything."""
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._click_guard():
                self.append(f"\n{CYAN}{BOLD}> {RESET}/undo\n")
                self.scroll_end()
                self._inbox.put("/undo")
        return handler

    def _cost_tree_click(self):
        """Click handler for the status bar's ctx gauge — the cost tree is
        that number broken down, so the gauge is the link (R135c; R134 spelled
        it out as its own " cost tree" label). Routed through the inbox, NOT run
        here: a mouse handler runs on the UI event-loop thread and this walks
        the whole session JSONL, which only grows (R20) — the exact shape
        R129 fixed for "copy all"."""
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._click_guard():
                self.append(f"\n{CYAN}{BOLD}> {RESET}/context\n")
                self.scroll_end()
                self._inbox.put("/context")
        return handler

    def _cost_report_click(self):
        """Click handler for the status bar's `$` price (R168) — same
        machine-wide breakdown `/cost` prints, one tap away, same as tapping
        the ctx gauge is `/context`. Routed through the inbox for the same
        reason `_cost_tree_click` is: it reads every session log on the
        machine (R20's unbounded logs), not work for the UI thread."""
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._click_guard():
                self.append(f"\n{CYAN}{BOLD}> {RESET}/cost\n")
                self.scroll_end()
                self._inbox.put("/cost")
        return handler

    def _click_guard(self) -> bool:
        """Shared eligibility check for the line-2 hint buttons (/ commands,
        ! bash / > prompt, ? Help) — the input line must be free (empty, no
        challenge, no open menu, no exit-confirm, no open /nano editor) for
        a click to act. Valid in both prompt and bash mode — only the
        entering-bash-mode click additionally requires NOT already being in
        bash mode (see _enter_bash_mode_click). Also used as the `?` key
        binding's `filter=` (R110): with `self._editor is not None` making
        this False, the binding itself becomes ineligible and prompt_toolkit
        falls through to the focused editor's own default handling of `?`
        (a literal self-insert), instead of the handler running and
        stealing the keystroke via an in-body check."""
        return (self.input.document.text == "" and self._question is None
                and not self._secret and self._menu_options is None
                and not self._exit_confirm and self._editor is None)

    def _input_mouse_handler(self, inner):
        """Wraps the prompt's BufferControl handler so a double-click selects
        the WHOLE draft instead of prompt_toolkit's default word-under-cursor
        (R135a). A prompt draft is one thought you retype or copy wholesale,
        not prose you edit word by word — and word-select is still reachable
        by dragging.

        Wrapping rather than subclassing BufferControl: TextArea builds its
        own control internally, so there is no constructor to hook.

        `inner` gets every other event untouched, including the MOUSE_DOWNs of
        the double-click itself — those only move the cursor and drop the
        selection, which we redo here.

        R135f (review pass): two bugs found after R135b shipped.
        - A plain click-drag *inside* the prompt never went through the
          double-click branch below, so it never cleared a frozen CHAT
          selection — "only one selection is ever live" held for the
          double-click gesture but not a drag, leaving `_sel_frozen` and a
          fresh prompt selection lit up at once. Every MOUSE_DOWN here now
          drops it, mirroring `sel_begin()`'s clear for the chat side.
        - `_input_click_at` used to record `now` on every qualifying
          MOUSE_UP, including the one that just fired a double-click. A
          third click shortly after (meant to place the cursor) then saw
          itself as paired with THAT click and re-selected the whole draft
          instead. The fired click's timestamp is now consumed (reset to
          0.0) so it can't pair again."""
        def handler(mouse_event):
            if (mouse_event.event_type == MouseEventType.MOUSE_DOWN
                    and mouse_event.button == MouseButton.LEFT):
                self._sel_frozen = None
            if (mouse_event.event_type == MouseEventType.MOUSE_UP
                    and mouse_event.button == MouseButton.LEFT):
                now = time.monotonic()
                last, self._input_click_at = self._input_click_at, now
                buf = self.input.buffer
                # focus check: the click that focuses the prompt must not also
                # count as half a double-click on whatever was focused before
                if (now - last < _DOUBLE_CLICK_S and buf.text
                        and self.app.layout.current_control is self.input.control):
                    self._sel_frozen = None   # see _copy_selected
                    self._input_click_at = 0.0   # consumed — see docstring
                    buf.exit_selection()
                    buf.cursor_position = 0
                    buf.start_selection()
                    buf.cursor_position = len(buf.text)
                    self.app.invalidate()
                    return None
            return inner(mouse_event)
        return handler

    def _open_model_picker(self):
        """Click handler for the model name on the status bar — same as
        typing `/model` and hitting Enter: opens the model-selection menu.
        A second tap while that same menu is already open closes it instead
        (same "no change" outcome as a blank Enter in the classic REPL) —
        specifically the model picker, not just any open menu, so this click
        target only ever affects its own menu."""
        def handler(mouse_event):
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return
            if self._menu_options is not None:
                if self._menu_prompt == "Select model":
                    self._answers.put(None)   # unblocks select_menu(); its
                    # own finally clears menu state + invalidates
                return
            if self._click_guard():
                self.append(f"\n{CYAN}{BOLD}> {RESET}/model\n")
                self.scroll_end()
                self._inbox.put("/model")
        return handler

    def _open_commands(self):
        """Click handler for the "/ commands" hint — toggle: with the
        autocomplete popup closed, opens it listing every command (no `/`
        inserted into the prompt — SlashCompleter lists all commands for an
        empty buffer, each completion carrying its own leading `/`); with
        the popup already open, a second tap closes it instead."""
        def handler(mouse_event):
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return
            buf = self.input.buffer
            if buf.complete_state is not None:
                buf.cancel_completion()
                self.app.invalidate()
            elif self._click_guard():
                self.app.layout.focus(self.input)
                buf.start_completion()
                self.app.invalidate()
        return handler

    def _insert_newline_click(self):
        """Click handler for the "\\n/\\br newline" and "Ctrl+J newline"
        hints — inserts a real newline into the prompt, same as Ctrl+J."""
        def handler(mouse_event):
            if (mouse_event.event_type == MouseEventType.MOUSE_UP
                    and self._question is None and not self._secret
                    and self._menu_options is None
                    and not self._exit_confirm):
                self.app.layout.focus(self.input)
                self.input.buffer.insert_text("\n")
                self.app.invalidate()
        return handler

    @staticmethod
    def _bash_cd_target(cmd: str) -> str | None:
        """If `cmd` is a plain `cd` / `cd <path>` (no `&&`, `;`, `|`, etc.
        chaining it with anything else), return the target path (`~` when
        bare). Returns None for anything else, so those fall through to a
        normal subprocess run.

        Bug fix: this used to `shlex.split(cmd)` and refuse anything past
        2 tokens — meaning `cd Delete Latter` (a real, unquoted folder name
        with a space, e.g. inserted verbatim by Tab-completion's
        PathCompleter, which does not escape spaces) was rejected as
        "ambiguous" and fell through to `subprocess.run`, which mangled it
        into `cd: Delete: No such file or directory` (sh treats the second
        word as a second, ignored argument). `cd` only ever takes ONE path
        argument in any shell, so there is no real ambiguity to preserve:
        everything after `cd ` is now taken as the literal target,
        stripping a single layer of straight quotes if the whole remainder
        is quoted (so `cd "some dir"`/`cd 'some dir'`, typed the
        traditionally-quoted way, still resolves to `some dir` and not the
        literal quote characters)."""
        if any(op in cmd for op in ("&&", "||", ";", "|")):
            return None
        stripped = cmd.strip()
        if stripped != "cd" and not stripped.startswith("cd "):
            return None
        rest = stripped[2:].strip()
        if not rest:
            return "~"
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
            return rest[1:-1]
        return rest

    def _enter_bash_mode_click(self):
        """Click handler for the "! bash" hint — same as typing `!` on an
        empty prompt: enters persistent bash mode."""
        def handler(mouse_event):
            if (mouse_event.event_type == MouseEventType.MOUSE_UP
                    and not self._bash_mode and self._click_guard()):
                self._bash_mode = True
                self.app.layout.focus(self.input)
                self.app.invalidate()
        return handler

    def _leave_bash_mode_click(self):
        """Click handler for the "> prompt" hint shown while in bash mode —
        leaves bash mode and returns to the normal prompt (clears any typed
        shell command, same as the Esc-Esc "Leave bash mode?" -> Yes path,
        but as a single explicit tap)."""
        def handler(mouse_event):
            if (mouse_event.event_type == MouseEventType.MOUSE_UP and self._bash_mode
                    and self._question is None and not self._secret
                    and self._menu_options is None and not self._exit_confirm):
                self._bash_mode = False
                self.input.buffer.reset()
                self.app.layout.focus(self.input)
                self.app.invalidate()
        return handler

    # ── /nano — built-in text editor (R110) ────────────────────────────────
    def open_nano(self, path: Path, check_busy: bool = True) -> None:
        """Open `path` in the editor: chat area (section 1) shows the file,
        the input line (section 2) hides, and the status bar swaps to
        save/close buttons (section 3, "type 2"). Refuses (with a short
        message, same wording either way) on a bad extension, a missing
        file (never auto-created), a file over 1MB, an already-open editor
        (defensive — both entry points, `/nano` and a bash-output filename
        click, live in areas that are themselves hidden while `self._editor`
        is set, so this shouldn't be reachable in practice; it's cheap
        insurance against a future second call path silently discarding
        unsaved edits), or a read failure (permissions, or a file that
        matches the extension allowlist but isn't valid UTF-8) — no
        exception ever reaches the caller, since this can run on the UI
        thread directly (a filename click) with nothing above it to catch
        one. `path` must already be resolved by the caller: `/nano <file>`
        leaves relative paths to Python's normal cwd-relative resolution; a
        filename clicked in bash-mode output is joined against the tracked
        `_bash_cwd` (R107) before calling this.

        May run on the worker thread (`/nano` reaches here via
        `_handle_command`) or the UI thread (a filename click's mouse
        handler) — `layout.focus()` is dispatched through
        `call_soon_threadsafe` when called off the UI thread, matching the
        one other cross-thread UI call in this file (`/quit`'s
        `app.exit()`); unlike `select_menu()`'s plain flag mutation, a focus
        change touches the Layout's internal control stack and has no
        established thread-safety contract of its own.

        Refuses outright while a menu/question/secret challenge is active,
        OR while the worker is busy with something ELSE (bug found on
        review): a bash-output filename click is reachable from the mouse
        at ANY time — the chat pane's own visibility isn't gated on
        `self._menu_options`/`self._busy`, only on `self._editor`/help —
        so clicking a filename while e.g. `/model`'s picker is open used
        to open the editor right on top of it. `self._editor is not None`
        then made every key the menu needs — Enter, arrows, digits — is
        ineligible via `filter=_no_editor`, so the menu became permanently
        unresolvable and the worker thread, still blocked on
        `select_menu()`'s `self._answers.get()`, deadlocked for the rest
        of the session. The `self._busy` check closes a narrower version
        of the same race: a background LLM turn/bash command with no
        menu/question active YET can still reach one moments later (e.g.
        a tool-call approval gate) — `self._busy` stays True for a line's
        entire processing, menu/question included, so it's a strictly
        broader and simpler signal than enumerating every individual
        blocking state.

        `check_busy=False` for `/nano` itself (bug fix): the worker sets
        `self._busy = True` for a line BEFORE dispatching it, so `/nano`
        landing here via `_handle_command` always found itself "busy" —
        self-blocking on every call, `/nano <file>` could never open
        anything. The `_busy` guard only protects the OTHER entry point
        (a mouse click on the UI thread racing a DIFFERENT command already
        running on the worker thread) — `/nano`'s own synchronous call
        can't race itself, so it skips the check."""
        if self._editor is not None:
            print(f"nano: already editing {self._editor['path']} "
                  "— close it first")
            return
        if ((check_busy and self._busy) or self._menu_options is not None
                or self._question is not None or self._secret):
            print("nano: busy — try again once the current command/turn finishes")
            return
        # R150b: help and the editor both live in the chat area but their
        # visibility filters are independent, so `?` then `/nano <file>` drew
        # BOTH, splitting the screen — and help was then undismissable,
        # because `?` and the help click target both require
        # `self._editor is None` (via `_click_guard`) while Escape is
        # `filter=_no_editor`. Help is a transient overlay, not a challenge
        # that must be answered, so opening a file just closes it.
        self._help_visible = False
        if path.suffix.lower() not in _NANO_EXTS:
            print(f"nano: unsupported file type: {path.suffix or '(none)'}")
            return
        if not path.is_file():
            print(f"nano: no such file: {path}")
            return
        try:
            if path.stat().st_size > _NANO_MAX_BYTES:
                print(f"nano: file too large (> {_NANO_MAX_BYTES // 1_000_000}MB): {path}")
                return
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"nano: can't open {path}: {e}")
            return
        self._editor = {"path": path, "original": text}
        self._editor_area.buffer.document = Document(text, 0)
        self._nano_dirty_cache = False        # just loaded — known clean
        if self._ui_thread is not None and threading.current_thread() is not self._ui_thread:
            self.app.loop.call_soon_threadsafe(
                lambda: self.app.layout.focus(self._editor_area))
        else:
            self.app.layout.focus(self._editor_area)
        self.app.invalidate()

    def _nano_dirty(self) -> bool:
        if self._editor is None:
            return False
        if self._nano_dirty_cache is None:
            self._nano_dirty_cache = (
                self._editor_area.buffer.text != self._editor["original"])
        return self._nano_dirty_cache

    def _nano_save(self) -> bool:
        """Best-effort — a write failure (disk full, permissions, the
        parent directory disappeared mid-edit) is reported the same way an
        open failure is, and leaves `dirty` alone (still True) so the
        toolbar keeps offering save rather than silently pretending it
        succeeded. Returns whether it actually wrote, so "save and close"
        can refuse to discard the buffer on a failed save."""
        text = self._editor_area.buffer.text
        try:
            self._editor["path"].write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"nano: can't save {self._editor['path']}: {e}")
            return False
        self._editor["original"] = text
        self._nano_dirty_cache = False         # just saved — known clean
        self._nano_close_confirm = False
        return True

    def _nano_close(self) -> None:
        self._editor = None
        self._nano_dirty_cache = None
        self._nano_close_confirm = False
        self._editor_area.buffer.reset()
        self.app.layout.focus(self.input)

    def _nano_save_click(self):
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._editor is not None:
                self._nano_save()
                self.app.invalidate()
        return handler

    def _nano_close_click(self):
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._editor is not None:
                # R125c: dirty + first click arms the confirm instead of
                # discarding immediately — a second click (label now reads
                # "discard changes?") is what actually closes.
                if self._nano_dirty() and not self._nano_close_confirm:
                    self._nano_close_confirm = True
                else:
                    self._nano_close()
                self.app.invalidate()
        return handler

    def _nano_save_close_click(self):
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._editor is not None:
                if self._nano_save():        # don't discard on a failed write
                    self._nano_close()
                self.app.invalidate()
        return handler

    def _nano_scroll(self, page_up: bool) -> None:
        """Page the editor content, click-only (no key binding — R110's
        editor has none by design). Reuses prompt_toolkit's own
        scroll_page_up/scroll_page_down rather than reimplementing page
        math: both only read `event.app`, so a minimal stand-in with just
        that attribute is enough. Requires the editor's Window to have
        rendered at least once (`render_info` — unset before the first
        real draw); with no real terminal/render loop, this is a no-op,
        same as prompt_toolkit's own default PageUp/PageDown binding would
        be in that situation.

        Forces an immediate synchronous redraw (`app._redraw()`) after
        scrolling, not just `app.invalidate()` — confirmed bug otherwise:
        `scroll_page_up`/`down` compute against `Window.render_info`, which
        only updates on an actual render pass. `invalidate()` merely
        SCHEDULES one for whenever the event loop next gets to it, so
        clicking faster than a redraw can keep up (very much how someone
        actually clicks "page up" repeatedly to reach the top of a long
        file quickly) made every click after the first compute against the
        SAME stale render_info — each one only nudged the cursor up by a
        single line instead of a full page, so reaching the top took
        dozens of clicks instead of a handful. `_redraw()` is documented
        "not thread safe — from other threads use invalidate()"; safe here
        since mouse-click handlers already run on the UI/event-loop
        thread."""
        from prompt_toolkit.key_binding.bindings import scroll as pt_scroll
        event = type("_Event", (), {"app": self.app})()
        (pt_scroll.scroll_page_up if page_up else pt_scroll.scroll_page_down)(event)
        self.app._redraw()

    def _nano_wheel_scroll(self, up: bool) -> None:
        """Mouse-wheel scroll over the editor — installed over Window's own
        default `_scroll_up`/`_scroll_down` (see `_build_app`), which turned
        out to be broken for exactly the kind of file this feature is for:
        prose with long lines that wrap (confirmed against this repo's own
        ARCHITECTURE.md, 260-char lines at 80 columns). Window's default
        wheel-scroll decides whether to move the cursor along with the view
        via a screen-position heuristic (`cursor_position.y >= window_height
        - 1 - offset`) that prompt_toolkit's own source admits is incomplete
        for wrapped lines ("TODO: not entirely correct yet in case of line
        wrapping and long lines" — `layout/containers.py`'s `_scroll_up`).
        When that heuristic fails to track the cursor upward, the cursor
        falls behind the view, and the very next render's "keep the cursor
        visible" pass (which runs on every render, unconditionally) drags
        the view back down toward it — fighting every subsequent scroll-up
        tick in real time. Confirmed: 400 ticks of the DEFAULT scroll_up
        (even with a forced redraw after each one, ruling out the separate
        stale-render_info issue R110 already fixed for the page buttons)
        only netted 57 lines of net upward movement on ARCHITECTURE.md —
        scrolling down worked fully, scrolling up got stuck partway.
        (`scroll_page_up`/`scroll_page_down`, reused for the page up/down
        buttons, don't have this problem — they derive the target line from
        `render_info.first_visible_line()`/`last_visible_line()`, which
        already correctly translates through wrapping.)

        Fixed the same way: instead of nudging `vertical_scroll` directly
        and hoping the cursor follows, move the CURSOR to just past the
        current edge (via the same wrap-aware `first_visible_line()`/
        `last_visible_line()` translation) and let the proven-correct
        "keep cursor visible" auto-scroll bring the view along — the same
        mechanism `scroll_page_up`/`down` already rely on successfully.
        Confirmed fixed: the same 400+400 ticks now correctly reach
        `vertical_scroll == 0`, not partway."""
        w = self._editor_area.window
        buf = self._editor_area.buffer
        info = w.render_info
        if info is None:
            return
        if up:
            target = max(0, info.first_visible_line() - _NANO_WHEEL_LINES)
        else:
            target = min(buf.document.line_count - 1,
                         info.last_visible_line() + _NANO_WHEEL_LINES)
        buf.cursor_position = buf.document.translate_row_col_to_index(target, 0)
        self.app._redraw()

    def _nano_page_up_click(self):
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._editor is not None:
                self._nano_scroll(page_up=True)   # already redraws synchronously
        return handler

    def _nano_page_down_click(self):
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._editor is not None:
                self._nano_scroll(page_up=False)  # already redraws synchronously
        return handler

    def _nano_status_fragments(self):
        """The "type 2" toolbar (R110): actions on line 1 — "close" (clean)
        or "save"/"save and close" (dirty) first, then "page up"/"page
        down" always last (scrolling isn't gated on dirty) — each styled
        `class:status.id` (underlined, same as every other clickable
        status-bar link) so they read as tappable; file identity (bare
        filename, no path — line 2) on line 2, deliberately NOT underlined
        since it isn't a click target."""
        line1 = [("class:status", " ")]
        if self._nano_dirty():
            line1 += [("class:status.id", "save", self._nano_save_click()),
                      ("class:status", " · "),
                      ("class:status.id", "save and close",
                       self._nano_save_close_click()),
                      ("class:status", " · ")]
        close_label = ("discard changes?" if self._nano_close_confirm
                      else "close")
        line1 += [("class:status.id", close_label, self._nano_close_click()),
                  ("class:status", " · "),
                  ("class:status.id", "page up", self._nano_page_up_click()),
                  ("class:status", " · "),
                  ("class:status.id", "page down", self._nano_page_down_click())]

        path = self._editor["path"]
        doc = self._editor_area.buffer.document
        pos = f" — line {doc.cursor_position_row + 1}/{doc.line_count}"
        dirty_mark = " [modified]" if self._nano_dirty() else ""
        frags = list(line1)
        # R137: same blank-row gap as the normal status bar, now that the
        # window is 3 rows — leaving this at a bare "\n" would show as a
        # dangling empty row instead of it reading as the normal bar's
        # rhythm continuing while /nano is open.
        frags.append(("", "\n\n"))
        frags.append(("class:status", f" {path.name}{pos}{dirty_mark}"))
        return frags

    def _dismiss_notice_click(self):
        """Click handler for the "✂ {msg}" copy-notice on line 2 — dismisses
        it immediately instead of waiting out its 4s timer, so line 2 drops
        straight back to the key-hint row."""
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                self._sel_notice = ("", 0.0)
                self.app.invalidate()
        return handler

    def _toggle_help_click(self):
        """Click handler for the "? Help" hint — same as pressing `?` on an
        empty prompt: opens/closes the help overlay."""
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._click_guard():
                self._help_visible = not self._help_visible
                if not self._help_visible:
                    self.app.layout.focus(self.input)
                self.app.invalidate()
        return handler

    def _resolve_menu(self, index: int) -> None:
        key, label = self._menu_options[index]
        self.append(f"{self._menu_prompt}\n  → {label}\n")
        if self._menu_on_select is not None:
            # opened via _open_ui_menu (a UI-thread confirm, e.g. Esc-Esc
            # quit/leave-bash) — resolve by calling back directly, NOT the
            # answers queue: nothing is blocked on select_menu() waiting for
            # it (the opener IS the UI thread, so it couldn't have blocked).
            cb = self._menu_on_select
            self._menu_prompt = self._menu_options = self._menu_on_select = None
            draft, self._menu_draft = getattr(self, "_menu_draft", ""), ""
            if draft and hasattr(self, "input") and not self.input.buffer.text:
                self.input.buffer.text = draft
                self.input.buffer.cursor_position = len(draft)
            self.app.invalidate()
            cb(key)
        else:
            self._answers.put(key)

    def _open_ui_menu(self, prompt: str, options: list[tuple[str, str]],
                      on_select, default_index: int = 0) -> None:
        """Non-blocking arrow-key menu for a confirmation triggered directly
        by a key binding (Esc-Esc quit / Esc-Esc leave bash mode) — the UI
        thread itself is the opener, so it can't use the worker-thread-
        blocking `select_menu()` (that would deadlock waiting on its own
        answer, same reason `ask()`/`select_menu()` raise if called from the
        UI thread). Reuses the identical rendering/nav (`_menu_fragments`,
        arrow keys, digit jump — Esc is a no-op while open, the pick must be
        explicit) — only how the choice
        is DELIVERED differs (`on_select(key)` callback vs. the answers
        queue)."""
        self._menu_prompt, self._menu_options = prompt, options
        self._menu_index = default_index
        self._menu_on_select = on_select
        if hasattr(self, "input"):
            # R171: save whatever the user had typed BEFORE reset() clears
            # it. The Esc-Esc confirms (cancel/leave-bash/quit) all open
            # through here with no draft save of their own — the 2s arm
            # window lets the user type after arming, and the second Esc
            # used to eat that draft outright (same bug class as R150c/R155,
            # which only covered the copy menu and the compactions click).
            # Restored in `_resolve_menu` on every outcome, cancel included.
            self._menu_draft = self.input.buffer.text
            self.input.buffer.reset()
        self.app.invalidate()

    def _resolve_bash_leave_menu(self, key: str) -> None:
        if key == "leave":
            self._bash_mode = False
            self.input.buffer.reset()
        self.app.invalidate()

    def _resolve_quit_menu(self, key: str, app_exit) -> None:
        if key == "yes":
            app_exit()

    def _resolve_cancel_menu(self, key: str) -> None:
        if key == "cancel":
            self.fe.cancel_event.set()

    def _typed_over_exit(self) -> None:
        """The user started typing while a quit question was pending — they
        have moved on, exactly as R62 treats an Esc that lands in a different
        state. Also un-freezes `_click_guard`, which gates `?`, `/ commands`
        and `! bash` on this same flag."""
        if self._exit_confirm:
            self._exit_confirm = False
            if self._esc_armed == "exit":
                self._esc_armed = None
            self.app.invalidate()

    def _on_escape(self, app_exit) -> None:
        """Esc, in priority order: close an open completion/confirm menu;
        leave bash mode; cancel the in-flight request; confirm/dismiss a
        pending exit question; clear typed text; on an idle empty prompt,
        offer to quit.

        THE GENERIC RULE: any state that needs confirmation (cancel busy
        work, leave bash mode, quit) uses the SAME double-Esc-within-2s
        gesture — one press arms + shows a status-bar hint, the second press
        opens an explicit arrow-key Yes/No question (`_open_ui_menu`) the
        user picks from, same as any other menu in the app. A press after
        the window (or while in a DIFFERENT state) is a fresh first press,
        never a stale leftover confirm from an earlier, different Esc.
        `app_exit` is called to quit (injected so this is callable/testable
        without a real Application)."""
        buf = self.input.buffer
        import time as _t
        now = _t.monotonic()

        def _armed(kind: str) -> bool:
            return self._esc_armed == kind and now - self._esc_armed_at < 2

        def _arm(kind: str) -> None:
            self._esc_armed, self._esc_armed_at = kind, now

        if self._menu_options is not None:
            if self._menu_prompt == "Select model":
                # the model picker alone allows a bare Esc to cancel — it's a
                # picker over the CURRENT model, not a challenge that must be
                # answered, so backing out with no change is a valid outcome.
                # Every other select()/confirm menu still requires an
                # explicit pick (see test_menu_esc_is_noop_while_open).
                self._answers.put(None)
            # else: a challenge/confirm menu is open — require an explicit pick
        elif buf.complete_state:
            # R150d: this branch now sits where the docstring above always
            # said it did — BEFORE bash mode. Behind it, an Esc in bash mode
            # with `cd Doc<Tab>`'s PathCompleter popup open armed the
            # leave-bash gesture instead of dismissing the popup, and the
            # `elif buf.text` clear-the-draft branch was unreachable in bash
            # mode entirely. Closing an open popup is the narrower, more
            # local action, so it wins — same reasoning that puts the menu
            # check first.
            buf.cancel_completion()
        elif self._bash_mode:
            if _armed("bash"):
                self._esc_armed = None
                self._open_ui_menu("Leave bash mode?", [
                    ("leave", "Yes — leave, back to the prompt"),
                    ("stay", "No — stay in bash mode"),
                ], self._resolve_bash_leave_menu)
            elif buf.text:
                # in bash mode too, a typed-but-unwanted command clears
                # first; the leave-bash gesture is for an EMPTY prompt
                buf.reset()
            else:
                _arm("bash")
        elif self._busy:
            if _armed("cancel"):
                self._esc_armed = None
                self._open_ui_menu("Cancel this?", [
                    ("cancel", "Yes, cancel"),
                    ("continue", "No, keep going"),
                ], self._resolve_cancel_menu)
            else:
                _arm("cancel")
        elif self._exit_confirm:
            if _armed("exit"):
                self._exit_confirm = False
                self._esc_armed = None
                self._open_ui_menu("Quit Aurora?", [
                    ("yes", "Yes, quit"),
                    ("no", "No, stay"),
                ], lambda key: self._resolve_quit_menu(key, app_exit))
            else:
                _arm("exit")   # keeps _exit_confirm True, re-arms the window
        elif buf.text:
            buf.reset()
        elif self._question is None and not self._secret:
            self._exit_confirm = True
            _arm("exit")

    # ── layout ────────────────────────────────────────────────────────────
    def _build_app(self):
        self._chat_win = chat_win = Window(
            _ChatControl(self, text=self._render_fragments, focusable=False,
                         get_cursor_position=self._cursor, show_cursor=False),
            wrap_lines=True, style="class:chat")

        from prompt_toolkit.history import FileHistory
        def _prompt():
            # during a blocking ask, the challenge itself is the prompt —
            # the cursor lands right after "…[c]omment: " (no bottom-bar hop).
            # A select() menu is NOT here — it renders in its own window above
            # and the input line is collapsed to height 0. Same for /nano
            # (R110): the editor owns the chat area instead.
            if self._menu_options is not None or self._editor is not None:
                return []
            if self._question is not None:
                return ANSI(self._question).__pt_formatted_text__()
            if self._bash_mode:                  # `!` mode: $ instead of >
                return [("bold fg:ansigreen", "$ ")]
            return [("bold fg:ansicyan", "> ")]

        _ansi = re.compile(r"\x1b\[[0-9;]*m")

        def _input_height():
            # pin the field to its content (cap 8): a min/max RANGE lets the
            # HSplit stretch it to max whenever spare rows exist, opening a
            # blank gap between a challenge prompt and the chat above it.
            # Wrap-aware: a long challenge prompt on the first line (R50)
            # must not clip when it wraps at narrow widths.
            try:
                if not hasattr(self, "input"):
                    return Dimension(min=1, max=1, preferred=1)
                if self._menu_options is not None or self._editor is not None:
                    # a select() menu, or /nano (R110), owns the screen —
                    # collapse the input line so its "> " prompt isn't left
                    # dangling under it. Height 0 can upset prompt_toolkit's
                    # renderer, so keep a single invisible row and hide the
                    # prompt.
                    return Dimension.exact(1)
                cols = max(20, self.app.output.get_size().columns)
                if self._question is not None:
                    prompt_lines = _ansi.sub("", self._question).split("\n")
                else:
                    prompt_lines = [""]
                extra = len(prompt_lines) - 1    # embedded newlines
                plen = len(prompt_lines[-1]) if self._question else 2
                rows = extra
                for i, line in enumerate(self.input.document.lines):
                    # R171: plain char-count ceil-div undercounts against
                    # `wrap_lines=True`'s actual WORD wrap, which breaks
                    # before the column edge whenever a word wouldn't fit —
                    # a long word-heavy line at a narrow width wraps into
                    # more rows than len(line)/cols predicts, and the input
                    # box (min==max==n, no scrollback) then clips the
                    # cursor below the visible rows. `textwrap.wrap` with
                    # `break_long_words=True` mirrors that word-boundary
                    # behavior closely enough to size the box correctly.
                    content = (" " * plen if i == 0 else "") + line
                    if not content:
                        rows += 1
                        continue
                    wrapped = textwrap.wrap(
                        content, width=cols, break_long_words=True,
                        replace_whitespace=False, drop_whitespace=False)
                    rows += max(1, len(wrapped))
                n = min(max(rows, 1), 8)
                return Dimension(min=n, max=n, preferred=n)
            except Exception:
                return Dimension(min=1, max=1, preferred=1)

        self.input = TextArea(
            multiline=True, wrap_lines=True,
            height=_input_height,
            prompt=_prompt,
            password=Condition(lambda: self._secret),
            completer=_ModeCompleter(
                self, ui.SlashCompleter(self.engine.cfg.get("_base_dir"))),
            complete_while_typing=True,
            focus_on_click=True,   # a mouse click moves the input cursor
            history=FileHistory(str(aurora_home() / "input_history")),
            style="class:input")
        self.input.control.mouse_handler = self._input_mouse_handler(
            self.input.control.mouse_handler)

        # R142b: typing dismisses a pending quit question. One Esc on an idle
        # empty prompt sets `_exit_confirm`, and nothing used to clear it but
        # the next Enter — which then consumed that Enter as the ANSWER,
        # silently discarding whatever the user had typed (not even added to
        # the history to retype). R104 removed the line-2 hint, so the state
        # was invisible while it also froze `_click_guard`. `on_text_insert`
        # (not `on_text_changed`) is the right hook: it fires for real typing
        # and pastes but NOT for the programmatic `buffer.reset()` /
        # `buffer.text = draft` that `_submit` and the ask/menu teardowns do,
        # which would otherwise clear the flag before `_submit` can read it.
        self.input.buffer.on_text_insert += lambda _: self._typed_over_exit()

        # R110: /nano's editable buffer — built once, content swapped in on
        # open. Occupies the chat area (section 1) while self._editor is
        # set; self.input (section 2) collapses to a hidden single row, same
        # pattern already used for a select() menu owning the screen.
        self._editor_area = TextArea(
            multiline=True, wrap_lines=True, style="class:chat",
            scrollbar=True, line_numbers=True)
        self._editor_area.buffer.on_text_changed += \
            lambda _buf: (setattr(self, "_nano_dirty_cache", None),
                         setattr(self, "_nano_close_confirm", False))
        # R110 follow-up: mouse-wheel scroll over the editor replaces
        # Window's own default _scroll_up/_scroll_down (see _nano_wheel_scroll
        # docstring for why — its per-tick cursor-tracking heuristic is
        # broken for wrapped long lines, which silently caps how far you
        # can actually scroll back up).
        self._editor_area.window._scroll_up = \
            lambda: self._nano_wheel_scroll(up=True)
        self._editor_area.window._scroll_down = \
            lambda: self._nano_wheel_scroll(up=False)

        kb = KeyBindings()

        # R110: every REPL-muscle-memory binding below (Enter, space,
        # backspace, arrows, digits, `!`, Ctrl+J, Escape, PageUp/Down) reads
        # or writes `self.input.buffer`/scrolls the chat pane UNCONDITIONALLY
        # — it doesn't check what's actually focused. While /nano's editor
        # owns focus, that's not "harmless no-op": it hijacks the exact keys
        # needed to edit a file (a space typed while editing silently landed
        # in the hidden, empty `self.input` instead of the file; Enter could
        # submit whatever had silently accumulated there as a chat message).
        # `filter=_no_editor` disables each binding at the prompt_toolkit
        # level (not an in-body check) so the key falls through to the
        # focused editor's own default handling instead — confirmed against
        # a real Application: this is exactly the difference between "the
        # handler runs and no-ops" (still swallows the key) and "the
        # binding is ineligible" (prompt_toolkit tries the next match).
        _no_editor = Condition(lambda: self._editor is None)
        # R211: while a select() menu owns the input line, an EDITING key must
        # not reach the buffer underneath it. The `Keys.Any` catch-all below
        # is documented as the swallow, but it is a FALLBACK — prompt_toolkit
        # sorts `Keys.Any` bindings first and calls `matches[-1]`, so any more
        # specific binding beats it. Excluding those keys by FILTER (rather
        # than an early `return` in each body) is what actually hands the
        # keystroke to `Keys.Any`, i.e. it uses the resolution order instead of
        # fighting it. Menu-DRIVING keys — enter, arrows, escape, digits — are
        # deliberately not filtered: beating the swallow is their whole job.
        _no_menu = Condition(lambda: self._menu_options is None)

        def _submit(event, line: str):
            """Submit/answer the current input line under the normal single-
            line keymap; called by Enter and by Alt+Enter (multiline submit)."""
            buf = self.input.buffer
            if (line.strip() and self._question is None
                    and not self._secret and not self._exit_confirm):
                buf.append_to_history()           # up-arrow recall, persisted
            buf.reset()
            if self._exit_confirm:                # answer to "exit? [y/N]"
                self._exit_confirm = False
                if line.strip().lower() in ("y", "yes"):
                    event.app.exit()
                else:
                    self.append(dim("· staying\n"))
                return
            if self._question is not None:        # answer a blocking ask
                q = self._question               # echo Q+A into the transcript
                self.append(q + (dim(line) if not self._secret else dim("•••"))
                            + "\n")
                self._answers.put(line)
                return
            if self._bash_mode:                   # run locally, stay in bash mode
                if line.strip():
                    self.append(f"\n{GREEN}{BOLD}$ {RESET}{line}\n")
                    self.scroll_end()
                    self._inbox.put("!" + line)   # worker's `!` path runs bash
                return
            if not line.strip():
                return
            self.append(f"\n{CYAN}{BOLD}> {RESET}{line}\n")
            self.scroll_end()
            self._inbox.put(line)

        @kb.add("enter", filter=_no_editor)
        def _(event):
            if self._menu_options is not None:     # select()-mode menu
                self._resolve_menu(self._menu_index)
                return
            buf = self.input.buffer
            st = buf.complete_state
            if st and st.current_completion:      # menu open → accept entry
                buf.apply_completion(st.current_completion)
                return
            if self.engine.multiline:
                buf.insert_text("\n")
                return
            _submit(event, ui._expand_newlines(buf.text))

        @kb.add("escape", "enter", filter=_no_editor)
        def _(event):
            # Alt+Enter submits when in multiline mode; ignore otherwise
            if not self.engine.multiline:
                return
            _submit(event, ui._expand_newlines(self.input.buffer.text))

        @kb.add("escape", "m", filter=_no_editor)
        def _(event):
            self.engine.set_multiline(not self.engine.multiline)
            self.fe.notify(f"multiline {'ON (Enter newline, Alt+Enter submit)' if self.engine.multiline else 'OFF'}")

        @kb.add("space", filter=_no_editor & _no_menu)
        def _(event):
            buf = self.input.buffer
            if ui._expand_typed_newline(buf):
                return
            buf.insert_text(" ")

        @kb.add("c-j", filter=_no_editor & _no_menu)          # Ctrl+J newline
        def _(event):
            self.input.buffer.insert_text("\n")

        @kb.add("c-c", filter=_no_editor)
        def _(event):
            self.input.buffer.reset()             # Esc owns cancel, not Ctrl+C

        @kb.add("?", filter=Condition(self._click_guard))
        def _(event):
            # TUI: help is a toggle on an empty prompt, not a line submission.
            self._help_visible = not self._help_visible
            if not self._help_visible:
                self.app.layout.focus(self.input)
            self.app.invalidate()

        @kb.add("!", filter=_no_editor)
        def _(event):
            # `!` on an EMPTY prompt enters persistent bash mode ($); anywhere
            # else it's a literal `!`. Swallowed during a menu (like Keys.Any).
            buf = self.input.buffer
            if self._menu_options is not None:
                return
            if (not self._bash_mode and not buf.text and self._question is None
                    and not self._secret and not self._exit_confirm):
                self._bash_mode = True
                self.app.invalidate()
            else:
                buf.insert_text("!")

        @kb.add("backspace", filter=_no_editor & _no_menu)
        def _(event):
            # backspace on an empty `$` prompt leaves bash mode; else normal
            buf = self.input.buffer
            if self._bash_mode and not buf.text:
                self._bash_mode = False
                self.app.invalidate()
            elif buf.selection_state is not None:
                # a selection (e.g. the whole-draft selection from a
                # double-click, R135a) must be deleted wholesale, not
                # decremented one char before the cursor.
                buf.cut_selection()
            else:
                buf.delete_before_cursor(count=event.arg)

        @kb.add("escape", filter=_no_editor)
        def _(event):
            if self._help_visible:
                self._help_visible = False
                self.app.layout.focus(self.input)
                self.app.invalidate()
                return
            self._on_escape(event.app.exit)

        @kb.add("up", filter=_no_editor)
        def _(event):
            # select()-mode menu → move the pointer; completion menu open →
            # navigate it; an EMPTY prompt → recall history (REPL muscle
            # memory); otherwise move the cursor within the draft.
            #
            # R98: this used to key off cursor_position_row == 0 alone, so
            # up-arrow on line 2 of an unrelated multi-line draft (say, a
            # pasted error log) that happened to put the cursor back on row
            # 0 would jump straight into command history, discarding the
            # user's place in their own draft. History recall is now gated
            # on the DRAFT being empty, not on which row the cursor sits
            # on — an in-progress multi-line draft never gets clobbered by
            # a stray up-arrow, and going up from row 2 to row 1 of that
            # draft behaves like every other multi-line text field.
            if self._menu_options is not None:
                self._menu_index = (self._menu_index - 1) % len(self._menu_options)
                self.app.invalidate()
                return
            buf = self.input.buffer
            if buf.complete_state:
                buf.complete_previous()
            elif not buf.text:
                buf.history_backward()
            else:
                buf.cursor_up()

        @kb.add("down", filter=_no_editor)
        def _(event):
            # R98: same fix as "up", symmetrically — gated on an EMPTY
            # draft, not on which row the cursor is on.
            if self._menu_options is not None:
                self._menu_index = (self._menu_index + 1) % len(self._menu_options)
                self.app.invalidate()
                return
            buf = self.input.buffer
            if buf.complete_state:
                buf.complete_next()
            elif not buf.text:
                buf.history_forward()
            else:
                buf.cursor_down()

        def _digit_handler(n: int):
            def _(event):
                # select()-mode menu → jump straight to option n and confirm;
                # otherwise behave like ordinary self-insert
                if self._menu_options is not None:
                    # R150c: an OUT-OF-RANGE digit used to fall through to
                    # insert_text, straight into the buffer the menu is
                    # rendered on top of — invisible, since `_input_height`
                    # collapses the input line to one hidden row while a menu
                    # is open. `5` on a 2-option "Cancel this?" left a stray
                    # `5` in the prompt afterwards (`_open_ui_menu` has no
                    # save/restore of the draft, unlike `select_menu`). The
                    # Keys.Any fallback below exists to swallow exactly this,
                    # but a digit binding is more specific and wins.
                    if n <= len(self._menu_options):
                        self._resolve_menu(n - 1)
                    return
                self.input.buffer.insert_text(str(n))
            return _

        for _n in range(1, 10):
            kb.add(str(_n), filter=_no_editor)(_digit_handler(_n))

        @kb.add(Keys.Any, filter=Condition(lambda: self._menu_options is not None))
        def _(event):
            # while a select() menu owns the input line it is a pure chooser:
            # arrows/enter/esc/digits are bound above; every other key (letters,
            # space, backspace, paste) must be swallowed so nothing leaks into
            # the buffer rendered underneath the menu. Keys.Any is a fallback —
            # it only fires when no more-specific binding matched.
            pass

        @kb.add("pageup", filter=_no_editor)
        def _(event):
            self.scroll_by(-_PAGE_STEP)

        @kb.add("pagedown", filter=_no_editor)
        def _(event):
            self.scroll_by(_PAGE_STEP)

        @kb.add("escape", "end", filter=_no_editor)
        def _(event):
            self.scroll_end()

        def status():
            import time
            if self._editor is not None:
                # "type 2" toolbar (R110): while /nano owns the screen, the
                # status bar shows save/close actions instead of the usual
                # model/session-id/copy links + tooltip row
                return self._nano_status_fragments()
            try:
                s = self.engine.context_stats()
                used = f"{s.used / 1000:.1f}k" if s.used >= 1000 else str(s.used)
                # R135e/R168: the price is its own `│` section, bare, only
                # rendered when known (a local model reports none) — built
                # further down as its own clickable fragment now, not a
                # plain string spliced into the separator.
                warn = "  ⚠ context >80% — /compact?" if s.pct >= 80 else ""
                ml = " │ multiline" if self.engine.multiline else ""
                # R155: the session id is no longer rendered on the bar — it
                # moved into the copy picker, which reads it from
                # engine.session.id at pick time
                mode_txt = "bash mode" if self._bash_mode else "prompt mode"
                draft_tokens = ""
                if (not self._bash_mode and not self._secret
                        and self._question is None):
                    text = self.input.buffer.text
                    if text.strip():
                        draft_tokens = f"↑{ui.estimate_tokens(text)}"
                draft_part = f" - {draft_tokens}" if draft_tokens else ""
                mode_click = (self._leave_bash_mode_click() if self._bash_mode
                              else self._enter_bash_mode_click())
                frags = [("class:status", " "),
                         ("class:status.id", self._short_model(s.model, s.local_model),
                          self._open_model_picker())]
                if s.cost_known:
                    # R168: the price is now its own clickable/underlined
                    # link (same `class:status.id` every other status-bar
                    # button uses) — tapping it is /cost, same as tapping
                    # the ctx gauge below is /context.
                    frags += [("class:status", " │ "),
                              ("class:status.id", f"${s.cost_usd:.2f}",
                               self._cost_report_click())]
                frags += [
                         # R135c: the ctx gauge IS the cost-tree link — it
                         # already names what the tree breaks down, so a
                         # separate " cost tree" label was a second word for
                         # the same thing on a row that has to fit 80 cols
                         # (R134g). `│` separates it from the model name, the
                         # row's other click target, so the two read as two
                         # buttons rather than one run-on label.
                         ("class:status", " │ "),
                         ("class:status.id",
                          f"ctx {used}/{s.limit / 1000:.0f}k "
                          f"- {s.pct:.0f}%", self._cost_tree_click()),
                         ("class:status", f"{draft_part} │ "),
                         ("class:status.id", mode_txt, mode_click),
                         ("class:status", " │ "),
                         # R155: one button, a picker behind it — replaces
                         # "session id" / "copy last" / "copy all" / "copy
                         # selected". The selection-aware row still only
                         # appears when there IS a selection; it's now a menu
                         # row rather than a fifth button.
                         ("class:status.id", "copy",
                          self._copy_menu_click())]
                # R159: how many times this session has been folded, live.
                # Rendered only when it has happened — a permanent `⤵0` is
                # noise on the overwhelmingly common path, and the bar has to
                # fit 80 cols (R134g). Clickable: it asks whether to fold
                # again, which is the action you want the moment you notice
                # the number climbing.
                if s.compactions:
                    frags.append(("class:status", " │ "))
                    frags.append(("class:status.id", f"⤵{s.compactions}",
                                  self._compactions_click()))
                if self._has_checkpoints:
                    frags.append(("class:status", " │ "))
                    frags.append(("class:status.id", "undo",
                                  self._undo_click()))
                if ml:
                    frags.append(("class:status", ml))
                if warn:
                    frags.append(("class:status", warn))
            except Exception as e:
                # R212: still never crash the render — the status bar repaints
                # on a timer and an exception here would take the app down.
                # But the old fallback was SILENT: any bug in the block above
                # showed only as a bar that mysteriously read " aurora", with
                # nothing logged and nothing to go on. That is not
                # hypothetical — `MEMORY/bugs/20260715_120000_tui_status_token_
                # int_crash` records exactly such a crash
                # (`live_token_tag` passing a char count to a function wanting
                # a string), and this handler is what a repeat of it would
                # hide.
                #
                # Marked visibly, and recorded ONCE — the bar repaints several
                # times a second, so logging every frame would bury the
                # session log in duplicates of one bug.
                frags = [("class:status", " aurora ⚠")]
                detail = f"{type(e).__name__}: {e}"
                if detail != self._status_error:
                    self._status_error = detail
                    try:
                        self.engine.session.log("error", where="status_render",
                                                error=detail)
                    except Exception:
                        pass    # diagnostics must not become the failure
            # Line 1 is identity only (model/ctx/session id). Line 2 shows the
            # tooltips by default, but any live/transient status takes it over —
            # thinking, awaiting-answer, exit-confirm, and copy notices never
            # crowd line 1, and never coexist with the tooltips.
            # R137: a blank row between them — a terminal grid has no
            # fractional line-height, so "more space between the two status
            # lines" can only mean a full row, not a few extra pixels. The
            # status window grows from 2 to 3 rows to make room for it.
            frags.append(("", "\n\n"))
            msg, ts = self._sel_notice
            esc_pending = self._esc_armed is not None \
                and time.monotonic() - self._esc_armed_at < 2
            if esc_pending and self._menu_options is None and self._esc_armed != "exit":
                # R62: the first Esc arms the gesture — say what a second
                # press within the window will open a confirm for. R104:
                # NOT for "exit" — an idle empty prompt is unambiguous
                # enough on its own that the reminder was noise; the
                # cancel/leave-bash-mode hints stay, since those happen
                # mid-work where a heads-up is more likely to matter.
                what = {"cancel": "cancel this", "bash": "leave bash mode"}.get(
                    self._esc_armed, "confirm")
                frags.append(("class:status.busy", f" Esc again to {what}"))
            elif self._exit_confirm:
                pass   # armed window expired (or R104: the "exit" hint is
                # suppressed outright) — the gesture speaks for itself
            elif self._question is not None or self._menu_options is not None:
                hint = (" select one, or ESC to cancel"
                        if self._menu_prompt == "Select model" else " select one")
                frags.append(("class:status.busy", hint))
            elif msg and time.monotonic() - ts < 4:
                frags.append(("class:status.busy", f" ✂ {msg}",
                              self._dismiss_notice_click()))
            elif self._busy:
                frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self._spin % 10]
                secs = int(time.time() - self._busy_since)
                toks = self.fe.live_token_tag()
                tok_bit = f" │ {toks}" if toks else ""
                # R102: a running shell command gets its own label instead
                # of the generic phase word — see set_running_note
                phase_text = self._running_note or f"{self._phase or 'working'}…"
                frags.append(("class:status.busy",
                              f" {frame} {phase_text} "
                              f"{secs}s (Tap ESC twice to cancel){tok_bit}"))
            else:
                # split out of ui._FOOTER_HINT so "/ commands", the bash
                # toggle, and "? Help" are clickable — same effect as typing
                # the key. The bash toggle is "! bash" (enters bash mode) in
                # prompt mode, "> prompt" (leaves it) in bash mode; the rest
                # of the row is identical in both modes.
                if self._bash_mode:
                    toggle_text, toggle_handler = "> prompt", self._leave_bash_mode_click()
                else:
                    toggle_text, toggle_handler = "! bash", self._enter_bash_mode_click()
                frags.extend([
                    ("class:status.hint", " "),
                    ("class:status.hint", "/ commands", self._open_commands()),
                    ("class:status.hint", " · "),
                    ("class:status.hint", toggle_text, toggle_handler),
                    ("class:status.hint", " · "),
                    ("class:status.hint", "\\n/\\br newline",
                     self._insert_newline_click()),
                    ("class:status.hint", " · "),
                    ("class:status.hint", "Ctrl+J newline",
                     self._insert_newline_click()),
                    ("class:status.hint", " · "),
                    ("class:status.hint", "? Help", self._toggle_help_click()),
                    ("class:status.hint", " · Esc cancel/clear/exit"),
                ])
            return frags


        # R212: the renderer is a closure over this method's locals; bind it
        # so a test can drive its failure path directly.
        self._status_render = status
        from prompt_toolkit.layout import ConditionalContainer
        menu_active = Condition(lambda: self._menu_options is not None)
        help_active = Condition(lambda: self._help_visible)
        menu_win = ConditionalContainer(
            Window(FormattedTextControl(self._menu_fragments),
                   height=lambda: Dimension.exact(self._menu_height()),
                   style="class:menu"),
            filter=menu_active)
        chat_visible = Condition(
            lambda: not self._help_visible and self._editor is None)
        editor_visible = Condition(lambda: self._editor is not None)
        editor_win = ConditionalContainer(self._editor_area,
                                          filter=editor_visible)
        help_pane = ScrollablePane(
            Window(
                FormattedTextControl(lambda: self._help_text),
                style="class:help",
                wrap_lines=False),
            show_scrollbar=True)
        self._help_pane = help_pane
        root = FloatContainer(
            HSplit([
                ConditionalContainer(chat_win, filter=chat_visible),
                ConditionalContainer(help_pane, filter=help_active),
                editor_win,
                # a select() menu renders in its own window (multi-line, one
                # option per row) directly above the input line
                menu_win,
                # while a challenge owns the input line, drop the rule so the
                # question visually attaches to the approval box above it
                ConditionalContainer(
                    Window(height=1, char="─", style="class:separator"),
                    filter=Condition(lambda: self._question is None
                                     and self._menu_options is None)),
                self.input,
                Window(height=1, char="─", style="class:separator"),
                Window(FormattedTextControl(self._status_render), height=3,
                       style="class:status"),
            ]),
            floats=[Float(xcursor=True, ycursor=True,
                          content=_completions_menu())])

        # --debug tints the two non-interactive areas so their bounds are
        # obvious: chat (group 1) and status bar (group 3) both a red tint
        # (distinct shades so the two areas stay distinguishable). Terminals
        # have no alpha — bg is opaque hex — so these are a muted-but-visible
        # tint, not a real % opacity. The input line (group 2) stays untinted.
        chat_bg = " bg:#4a1010" if self._debug else ""
        status_bg = "#5c1414" if self._debug else "#1a2020"
        style = Style.from_dict({
            "chat":          f"noinherit{chat_bg}",
            "help":          "noinherit bg:#1a2020 fg:#9ab5b5",
            "separator":     "fg:#4a5c5c",
            "status":        f"fg:#9ab5b5 bg:{status_bg}",
            "status.busy":   f"fg:#e5a000 bg:{status_bg}",
            "status.hint":   f"fg:#4a5c5c bg:{status_bg}",
            "status.id":     f"fg:#9ab5b5 bg:{status_bg} underline",
            # prompt_toolkit's DEFAULT `menu` class is bg:#888888 (grey) — the
            # menu window's class:menu inherits it and it cascades under every
            # row. Reset it so the challenge menu has no background.
            "menu":          "noinherit",
            "menu.prompt":   "fg:#e5a000 bold",
            "menu.option":   "fg:#9ab5b5",
            # selected row is marked by the ❯ pointer + a bright bold fg, NO
            # background: a bg bar quantizes to muddy grey on non-truecolor
            # terminals (white text on grey), and the pointer already shows it
            "menu.selected": "fg:ansibrightcyan bold",
            "menu.hint":     "fg:#4a5c5c",
            "think.header":  "fg:#4a5c5c",
            "think.body":    "fg:#4a5c5c",
            "link":          "fg:ansibrightcyan underline",
            # the /command + model completion dropdown: prompt_toolkit's
            # default is a light-GREY bar (bg:#aaaaaa) — retheme it dark so it
            # matches the status bar instead of looking like a stray grey box
            "completion-menu":                    "bg:#1a2020 fg:#9ab5b5",
            "completion-menu.completion":         "bg:#1a2020 fg:#9ab5b5",
            "completion-menu.completion.current": "bg:#243232 fg:ansibrightcyan bold",
            "completion-menu.meta.completion":         "bg:#1a2020 fg:#4a5c5c",
            "completion-menu.meta.completion.current": "bg:#243232 fg:#9ab5b5",
            "scrollbar.background": "bg:#1a2020",
            "scrollbar.button":     "bg:#4a5c5c",
        })

        self.app = Application(
            layout=Layout(root, focused_element=self.input),
            key_bindings=kb, style=style,
            mouse_support=True, full_screen=True)
        # prompt_toolkit's default ttimeoutlen (0.5s) is how long it waits
        # after a lone Escape to see if more bytes follow (an Alt-sequence,
        # or one of our own "escape enter"/"escape m" bindings) before
        # firing the plain "escape" binding — so EVERY Esc tap in the
        # double-tap cancel/quit gesture ate this delay on top of the
        # intentional 2s confirm window, including the second tap that
        # opens the actual confirm menu. A locally-generated Alt-sequence
        # (both bytes from one physical keypress) is delivered to the
        # terminal driver's read() as a single burst, so even a near-zero
        # timeout still resolves it correctly in practice.
        self.app.ttimeoutlen = 0.001

    # ── worker: the session thread (all command/turn code runs here) ─────
    def _worker(self):
        engine, fe = self.engine, self.fe
        self._banner()

        import time as _t
        bp_text, bp_source = bootstrap.load(".")
        if bp_text and not engine.messages:
            first = next((l for l in bp_text.splitlines() if l.strip()), "")
            bp_url = bootstrap.source_url(".")
            self.append(f"{YELLOW}bootstrap prompt{RESET} [{bp_source}] "
                        + dim(f"{len(bp_text)} chars") + "\n"
                        + dim(f"  “{ui._short(first, 70)}”") + "\n")
            choice = ui._bootstrap_run_choice(bp_url)
            if choice in ("run", "download"):
                self._busy, self._busy_since, self._phase = True, _t.time(), "working"
                ui._run_bootstrap(engine, fe, redownload=(choice == "download"),
                                 sync=True)
                self._busy, self._phase = False, ""
                self._esc_armed = None

        import time
        while True:
            line = self._inbox.get()
            self._busy, self._busy_since, self._phase = True, time.time(), "working"
            try:
                if line.startswith("!"):          # local bash, no LLM (R10)
                    cmd = line[1:]
                    cd_target = self._bash_cd_target(cmd)
                    if cd_target is not None:
                        new_dir = os.path.normpath(os.path.join(
                            self._bash_cwd, os.path.expanduser(cd_target)))
                        if os.path.isdir(new_dir):
                            self._bash_cwd = new_dir
                            print(dim(f"(cwd: {new_dir})"))
                        else:
                            print(f"cd: no such file or directory: "
                                  f"{cd_target}")
                    elif cmd.strip() in ("clear", "cls"):
                        self.clear_screen()
                    else:
                        # R125c: route through tools._run_command_once (same
                        # as run_command/wait_until) instead of a bare
                        # subprocess.run — that gave bash-mode neither a
                        # timeout nor process-group ownership, so a hanging
                        # command (`!ssh host`, a REPL waiting on stdin)
                        # wedged this thread forever since _worker() is the
                        # sole consumer of the TUI's inbox queue.
                        from . import tools as _tools
                        out, code = _tools._run_command_once(cmd, self._bash_cwd)
                        if code is None:
                            out = (out + "\n" if out else "") + \
                                f"[timeout after {_tools.COMMAND_TIMEOUT}s]"
                        self._last_bash_output = out
                        self._last_bash_at = time.time()
                        if out.strip():
                            self.append_bash_output(out)
                        else:
                            print(dim("(no output)"))
                elif line.startswith("/"):
                    if not ui._handle_command(engine, fe, line):
                        break
                else:
                    # R96f: no thread wrapper — _worker is already off the
                    # main thread, and cancellation here is Esc-Esc's
                    # cancel_event, not Ctrl+C (see ui._run_turn's docstring)
                    ui._send_turn(engine, fe, line)
                    self._last_llm_at = time.time()
            except BaseException as e:            # never kill the session loop
                print(f"\n{RED}✗ {e.__class__.__name__}: {e}{RESET}")
            finally:
                self._busy, self._phase = False, ""
                self._esc_armed = None
                self.app.invalidate()
        # app.loop only exists once app.run() has started on the UI thread —
        # a '/quit' typed as the very first input can race that startup and
        # find None here, killing the worker and leaving the app running
        if self.app.loop is not None:
            self.app.loop.call_soon_threadsafe(self.app.exit)
        else:
            self.app.exit()

    def _banner(self):
        import os

        from . import __version__ as version

        engine = self.engine
        h = engine.provider_health()
        mark = f"{GREEN}✔{RESET}" if h["ok"] else f"{RED}✘{RESET}"
        info_lines = [f"{CYAN}{BOLD}Aurora{RESET} {dim(version)}",
                      f"  model    {BOLD}{engine.current.get('model')}{RESET}  "
                      f"{mark} {dim(h['detail'])}",
                      f"  cwd      {os.getcwd()}",
                      f"  session  {engine.session.id}"
                      + (dim(f"  ({len(engine.messages)} messages resumed)")
                         if engine.messages else "")]
        n_ext = len(ui._extension_tool_specs())
        if n_ext:
            info_lines.append(f"  extensions  {n_ext} tool"
                              f"{'s' if n_ext != 1 else ''} loaded"
                              + dim("  (/extensions for details)"))
        info_lines.append("")
        self.append("\n".join(info_lines) + "\n")

    # ── run ───────────────────────────────────────────────────────────────
    def run(self):
        from . import keystore
        keystore.set_prompter(self.fe.ask_secret)

        real_stdout, real_input = sys.stdout, builtins.input
        real_select = ui.select
        colors.IN_TUI = True
        sys.stdout = _ChatWriter(self)
        builtins.input = lambda prompt="": self.ask(str(prompt))
        ui.select = self.select_menu
        t = threading.Thread(target=self._worker, daemon=True)
        # app.run() below owns THIS thread as the UI event loop
        self._ui_thread = threading.current_thread()
        t.start()

        def _ticker():   # animates the spinner / elapsed seconds while busy
            import time
            while True:
                time.sleep(0.5)
                if self._busy:
                    self._spin += 1
                    try:
                        self.app.invalidate()
                    except Exception:
                        pass
        threading.Thread(target=_ticker, daemon=True).start()

        def _pre_run_hook():
            """Install a logging exception handler so uncaught event-loop
            errors (often swallowed by prompt_toolkit's alternate-screen
            dialog) are persisted with full tracebacks for debugging."""
            import asyncio
            import datetime
            import traceback
            loop = asyncio.get_event_loop()
            original = loop.get_exception_handler()

            def _log_and_forward(loop_, context):
                try:
                    log_path = aurora_home() / "tui_crash.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    # R171/I9: this is an accident log (crash tracebacks),
                    # not a record Aurora's "keep everything forever" policy
                    # (R20) was ever meant to cover — that's the session
                    # JSONL's job, by design. With no cap it grows without
                    # bound on a machine that crashes often. Truncate to the
                    # last 1MB before appending, same "keep the recent tail"
                    # shape as the session compaction/scrollback caps use
                    # elsewhere.
                    _CRASH_LOG_CAP = 1_000_000
                    try:
                        if log_path.stat().st_size > _CRASH_LOG_CAP:
                            data = log_path.read_bytes()[-_CRASH_LOG_CAP:]
                            log_path.write_bytes(data)
                    except OSError:
                        pass
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"\n--- {datetime.datetime.now().isoformat()} ---\n")
                        msg = context.get("message", "")
                        if msg:
                            f.write(msg + "\n")
                        exc = context.get("exception")
                        if exc:
                            traceback.print_exception(
                                type(exc), exc, exc.__traceback__, file=f)
                except Exception:
                    pass
                if original is not None:
                    original(loop_, context)
                else:
                    loop_.default_exception_handler(context)

            loop.set_exception_handler(_log_and_forward)

        try:
            self.app.run(pre_run=_pre_run_hook)
        finally:
            sys.stdout, builtins.input = real_stdout, real_input
            ui.select = real_select
            colors.IN_TUI = False
        print(f"\nResume this session with:\n  aurora --resume {self.engine.session.id}")
        print("bye")


def run(engine: Engine, debug: bool = False) -> None:
    Tui(engine, debug=debug).run()
