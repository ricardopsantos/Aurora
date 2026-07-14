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
import queue
import re
import subprocess
import sys
import threading

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (Dimension, Float, FloatContainer,
                                   HSplit, Layout, ScrollablePane, Window)
from prompt_toolkit.application import get_app
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu, CompletionsMenuControl
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from . import bootstrap, ui
from .colors import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW, dim
from .engine import Engine

_SCROLL_STEP = 3          # wheel ticks are per-notch; keep it gentle
_PAGE_STEP = 10


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


def _overlay(frags, start, end):
    """Re-style the (start..end) content range (tuple-compared (line, col)
    positions) in reverse video. Splits only the fragments the selection
    crosses; everything fully outside passes through untouched."""
    out = []
    y, x = 0, 0
    for f in frags:
        style, text = f[0], f[1]
        nl = text.count("\n")
        ey, ex = (y + nl, len(text) - text.rfind("\n") - 1) if nl \
            else (y, x + len(text))
        if (ey, ex) <= start or (y, x) >= end:   # fully outside
            out.append(f)
            y, x = ey, ex
            continue
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
        out += [((style + " reverse") if ins else style, s, *f[2:])
                for ins, s in segs]
    return out


class _ChatWriter:
    """A file-like stdout: every write lands in the chat pane. The worker
    thread is the only printer; prompt_toolkit renders through its own
    Output object, so the redirect never touches the UI's escape codes."""

    def __init__(self, tui):
        self._tui = tui

    def write(self, s):
        if s:
            self._tui.append(s)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return True   # colors.py and friends keep emitting ANSI


class TuiFrontend(ui.TerminalFrontend):
    """TerminalFrontend already writes everything through sys.stdout /
    input(), which the TUI redirects — overridden here: ask_secret (hidden
    input in the pinned field) and thinking (collapsed clickable block
    instead of an inline marker, à la Copilot)."""

    def __init__(self, tui, **kw):
        super().__init__(**kw)
        self._tui = tui

    def ask_secret(self, label: str) -> str:
        return self._tui.ask(label, secret=True)

    def begin_turn(self) -> None:
        super().begin_turn()
        self._tui.set_phase("thinking")

    def on_request(self) -> None:
        # every LLM request (each tool round too) gets its own timed row in
        # the chat, mirroring the toolbar's phase+elapsed — even for models
        # that never stream thinking tokens
        self._tui.set_phase("thinking")
        self._tui.begin_think(live=self.show_thinking)

    def end_turn(self) -> None:
        super().end_turn()
        self._tui.finish_think()

    def on_think(self, chunk: str) -> None:
        self.think_buffer += chunk            # /think still works
        self._tui.think_chunk(chunk, live=self.show_thinking)

    def on_text(self, chunk: str) -> None:
        self._tui.finish_think()              # answer starts → close the block
        self._tui.set_phase("generating")
        self._think_marker_shown = False      # never let the marker logic fire
        super().on_text(chunk)


class Tui:
    def __init__(self, engine: Engine, debug: bool = False):
        self.engine = engine
        self._debug = debug          # --debug: tint chat green, status bar pink
        self._chat: list = []        # str | think dict
        self._cache: list = []       # per-entry (fragments, nlines) | None
        self._text_cache = None
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
        self._bash_mode = False      # `!` on an empty prompt → persistent bash
        self._exit_confirm = False   # Esc while idle → "exit? [y/N]"
        # generic double-Esc-within-2s confirm gesture, shared by cancel/
        # bash-exit/quit: kind of the pending action ("cancel"|"bash"|"exit")
        # + when it was armed, so a stale/expired first press never counts
        self._esc_armed: str | None = None
        self._esc_armed_at = 0.0
        self._busy = False           # a turn/command is running in the worker
        self._phase = ""             # thinking / generating / working
        self._busy_since = 0.0
        self._spin = 0
        self._ui_thread: threading.Thread | None = None
        self._sel_anchor: tuple | None = None   # (line, col) of MOUSE_DOWN
        self._sel: tuple | None = None           # normalized ((y,x),(y,x)) — live drag
        self._sel_frozen: tuple | None = None    # finished selection, stays
        # highlighted and offers "copy selected" on the status bar until
        # copied or a new drag starts
        self._sel_notice: tuple = ("", 0.0)      # ("copied …", monotonic ts)
        self._open_think = False     # a live (undone) think row exists
        self._saved_draft = ""       # input text preserved across challenges
        self._help_text = ANSI(ui.HELP).__pt_formatted_text__()
        self._help_visible = False

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
    _MERGE_LIMIT = 4096

    def _dirty(self, i: int) -> None:
        self._cache[i] = None
        self._text_cache = None

    def append(self, s: str) -> None:
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
                self._text_cache = None
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
            if isinstance(last, dict) and not last["done"]:
                return                      # this request's row already exists
            self._chat.append({"kind": "think", "text": "", "open": live,
                               "done": False, "t0": time.monotonic(), "dt": 0})
            self._cache.append(None)
            self._text_cache = None
            self._open_think = True
        self.app.invalidate()

    def think_chunk(self, chunk: str, live: bool = False) -> None:
        """Grow the current request's thinking block (create it if on_request
        didn't, e.g. under a plain frontend test); collapsed by default — a
        click on its header expands it."""
        with self._lock:
            last = self._chat[-1] if self._chat else None
            if not (isinstance(last, dict) and not last["done"]):
                import time
                last = {"kind": "think", "text": "", "open": live,
                        "done": False, "t0": time.monotonic(), "dt": 0}
                self._chat.append(last)
                self._cache.append(None)
                self._open_think = True
            last["text"] += chunk
            self._dirty(len(self._chat) - 1)
        self.app.invalidate()

    def _close_think_locked(self) -> None:
        """Close EVERY open think row (normally at most one; sweeping all of
        them guarantees no stuck live clock can survive — a single leaked row
        would disable the render cache for the whole session)."""
        import time
        if not self._open_think:
            return
        for i, item in enumerate(self._chat):
            if isinstance(item, dict) and not item["done"]:
                item["done"] = True
                item["dt"] = time.monotonic() - item.get("t0", time.monotonic())
                self._dirty(i)
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

    def _entry_fragments(self, item, index):
        if isinstance(item, str):
            return ANSI(item).__pt_formatted_text__()
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

    def _fragments(self):
        with self._lock:
            # a live think row's header carries a running clock — as long as
            # one exists, rebuild every render (the 0.5s ticker drives it)
            if self._open_think:
                self._text_cache = None
            if self._text_cache is None:
                out: list = []
                total = 0
                for i, item in enumerate(self._chat):
                    cached = self._cache[i]
                    if isinstance(item, dict) and not item["done"]:
                        cached = None        # live clock — never trust cache
                    if cached is None:
                        frags = self._entry_fragments(item, i)
                        nl = sum(f[1].count("\n") for f in frags)
                        cached = self._cache[i] = (frags, nl)
                    out += cached[0]
                    total += cached[1]
                self._nlines = total
                self._text_cache = out
            return self._text_cache

    # ── drag-select → "copy selected" button (R48) ─────────────────────────
    def sel_begin(self, pos: tuple) -> None:
        self._sel_frozen = None   # a fresh drag drops any pending selection
        self._sel_anchor, self._sel = pos, None
        self.app.invalidate()

    def sel_drag(self, pos: tuple):
        if self._sel_anchor is None:
            return NotImplemented
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

    def _copy_selected(self):
        """Click handler for the "copy selected" status-bar button — copies
        the frozen drag-selection and clears it."""
        def handler(mouse_event):
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return
            sel = self._sel_frozen
            if sel is None:
                return
            text = self._sel_text(sel)
            self._sel_frozen = None
            if text.strip():
                from . import clipboard
                import time
                how = clipboard.copy(text)
                self._sel_notice = (f"copied {len(text)} chars — {how}",
                                    time.monotonic())
            self.app.invalidate()
        return handler

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
        return frags if sel is None else _overlay(frags, *sel)

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

    # ── question mode (blocking asks from the worker thread) ─────────────
    def ask(self, prompt: str = "", secret: bool = False) -> str:
        # The UI (event-loop) thread is the one that DELIVERS answers, so an
        # ask() from it (e.g. via the builtins.input monkeypatch) can never
        # be answered — it deadlocks silently. Fail loudly instead. Any other
        # thread is fine: the worker, or the nested turn thread _run_turn
        # spawns (a key prompt mid-turn arrives from there).
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
            # async completer that insert_text() would trigger
            self.input.buffer.text = self._saved_draft
            self._saved_draft = ""
            self.app.invalidate()

    def select_menu(self, prompt: str, options: list[tuple[str, str]],
                    default_index: int | None = None) -> str:
        """Arrow-key menu, rendered in place of the input prompt (see `ask`
        for the same blocking-from-worker-thread contract). `default_index`
        only sets which row starts highlighted (e.g. the current model in
        `/model`) — Enter always requires the user to actually press it on
        that row, so this carries none of the classic REPL's blank-Enter
        safety concern (see `ui.select`)."""
        if (self._ui_thread is not None
                and threading.current_thread() is self._ui_thread):
            raise RuntimeError(
                "select_menu() called from the TUI event-loop thread — this "
                "would deadlock; route it through the session worker")
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
                self._saved_draft = ""
            self.app.invalidate()

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
        if self._menu_options is None:
            return []
        frags = [("class:menu.prompt", self._menu_prompt + "\n")]
        for i, (_, label) in enumerate(self._menu_options):
            selected = i == self._menu_index
            row_style = "class:menu.selected" if selected else "class:menu.option"
            frags.append((row_style, f" ❯ {i + 1}. " if selected else f"   {i + 1}. "))
            for style, text, *_rest in ANSI(label).__pt_formatted_text__():
                frags.append((f"{row_style} {style}".strip(), text))
            frags.append((row_style, "\n"))
        frags.append(("class:menu.hint", " ↑/↓ move · Enter select · number to jump"))
        return frags

    def _menu_height(self) -> int:
        # prompt + one row per option + hint row
        return (len(self._menu_options) + 2) if self._menu_options else 0

    def _copy_session_id(self, session_id: str):
        """Click handler for the session id in the status bar — copies it to
        the clipboard (SSH-safe, same path as drag-select copy)."""
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                import time
                from . import clipboard
                how = clipboard.copy(session_id)
                self._sel_notice = (f"session id copied — {how}",
                                    time.monotonic())
                self.app.invalidate()
        return handler

    def _copy_raw_response(self):
        """Click handler for the "copy last" button in the status bar —
        copies the last turn's RAW response to the clipboard, thinking
        included (the model's reasoning, if any, followed by its final
        answer). Same text as `/copy-last`; unlike `/copy`, which copies the
        answer only. Shared logic lives in `ui._raw_last_response_text`."""
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                import time
                from . import clipboard
                raw = ui._raw_last_response_text(self.engine, self.fe)
                if not raw:
                    self._sel_notice = ("nothing to copy yet", time.monotonic())
                else:
                    how = clipboard.copy(raw)
                    self._sel_notice = (f"raw response copied — {how}",
                                        time.monotonic())
                self.app.invalidate()
        return handler

    def _copy_all_chat(self):
        """Click handler for the "copy all" button in the status bar —
        copies the whole session transcript (questions + answers, no
        thinking) to the clipboard. Same text as `/copy-all`. Shared logic
        lives in `ui._all_chat_text`."""
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                import time
                from . import clipboard
                text = ui._all_chat_text(self.engine)
                if not text.strip():
                    self._sel_notice = ("nothing to copy yet", time.monotonic())
                else:
                    how = clipboard.copy(text)
                    self._sel_notice = (f"whole chat copied — {how}",
                                        time.monotonic())
                self.app.invalidate()
        return handler

    def _click_guard(self) -> bool:
        """Shared eligibility check for the line-2 hint buttons (/ commands,
        ! bash / > prompt, ? Help) — the input line must be free (empty, no
        challenge, no open menu, no exit-confirm) for a click to act. Valid
        in both prompt and bash mode — only the entering-bash-mode click
        additionally requires NOT already being in bash mode (see
        _enter_bash_mode_click)."""
        return (self.input.document.text == "" and self._question is None
                and not self._secret and self._menu_options is None
                and not self._exit_confirm)

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
            self.app.invalidate()
            cb(key)
        else:
            self._answers.put(key)

    def _open_ui_menu(self, prompt: str, options: list[tuple[str, str]], on_select) -> None:
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
        self._menu_prompt, self._menu_options, self._menu_index = prompt, options, 0
        self._menu_on_select = on_select
        if hasattr(self, "input"):
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
            pass  # a challenge/confirm menu is open — require an explicit pick
        elif self._bash_mode:
            if _armed("bash"):
                self._esc_armed = None
                self._open_ui_menu("Leave bash mode?", [
                    ("leave", "Yes — leave, back to the prompt"),
                    ("stay", "No — stay in bash mode"),
                ], self._resolve_bash_leave_menu)
            else:
                _arm("bash")
        elif buf.complete_state:
            buf.cancel_completion()
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
        from .paths import aurora_home
        def _prompt():
            # during a blocking ask, the challenge itself is the prompt —
            # the cursor lands right after "…[c]omment: " (no bottom-bar hop).
            # A select() menu is NOT here — it renders in its own window above.
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
            if not hasattr(self, "input"):
                return Dimension(min=1, max=1, preferred=1)
            if self._menu_options is not None:
                # a select() menu owns the screen — collapse the input line so
                # its "> " prompt isn't left dangling under the choices
                return Dimension.exact(0)
            try:
                cols = max(20, self.app.output.get_size().columns)
            except Exception:
                cols = 80
            if self._question is not None:
                prompt_lines = _ansi.sub("", self._question).split("\n")
            else:
                prompt_lines = [""]
            extra = len(prompt_lines) - 1        # embedded newlines in the prompt
            plen = len(prompt_lines[-1]) if self._question else 2
            rows = extra
            for i, line in enumerate(self.input.document.lines):
                w = len(line) + (plen if i == 0 else 0)
                rows += max(1, -(-w // cols))     # ceil-div, min 1 per line
            n = min(max(rows, 1), 8)
            return Dimension(min=n, max=n, preferred=n)

        self.input = TextArea(
            multiline=True, wrap_lines=True,
            height=_input_height,
            prompt=_prompt,
            password=Condition(lambda: self._secret),
            completer=ui.SlashCompleter(self.engine.cfg.get("_base_dir")),
            complete_while_typing=True,
            focus_on_click=True,   # a mouse click moves the input cursor
            history=FileHistory(str(aurora_home() / "input_history")),
            style="class:input")

        kb = KeyBindings()

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

        @kb.add("enter")
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

        @kb.add("escape", "enter")
        def _(event):
            # Alt+Enter submits when in multiline mode; ignore otherwise
            if not self.engine.multiline:
                return
            _submit(event, ui._expand_newlines(self.input.buffer.text))

        @kb.add("escape", "m")
        def _(event):
            self.engine.set_multiline(not self.engine.multiline)
            self.fe.notify(f"multiline {'ON (Enter newline, Alt+Enter submit)' if self.engine.multiline else 'OFF'}")

        @kb.add("space")
        def _(event):
            buf = self.input.buffer
            if ui._expand_typed_newline(buf):
                return
            buf.insert_text(" ")

        @kb.add("c-j")                            # Ctrl+J newline
        def _(event):
            self.input.buffer.insert_text("\n")

        @kb.add("c-c")
        def _(event):
            self.input.buffer.reset()             # Esc owns cancel, not Ctrl+C

        @kb.add("?", filter=Condition(self._click_guard))
        def _(event):
            # TUI: help is a toggle on an empty prompt, not a line submission.
            self._help_visible = not self._help_visible
            if not self._help_visible:
                self.app.layout.focus(self.input)
            self.app.invalidate()

        @kb.add("!")
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

        @kb.add("backspace")
        def _(event):
            # backspace on an empty `$` prompt leaves bash mode; else normal
            buf = self.input.buffer
            if self._bash_mode and not buf.text:
                self._bash_mode = False
                self.app.invalidate()
            else:
                buf.delete_before_cursor(count=event.arg)

        @kb.add("escape")
        def _(event):
            if self._help_visible:
                self._help_visible = False
                self.app.layout.focus(self.input)
                self.app.invalidate()
                return
            self._on_escape(event.app.exit)

        @kb.add("up")
        def _(event):
            # select()-mode menu → move the pointer; completion menu open →
            # navigate it; on the first input row → recall history (REPL
            # muscle memory); otherwise move the cursor
            if self._menu_options is not None:
                self._menu_index = (self._menu_index - 1) % len(self._menu_options)
                self.app.invalidate()
                return
            buf = self.input.buffer
            if buf.complete_state:
                buf.complete_previous()
            elif buf.document.cursor_position_row == 0:
                buf.history_backward()
            else:
                buf.cursor_up()

        @kb.add("down")
        def _(event):
            if self._menu_options is not None:
                self._menu_index = (self._menu_index + 1) % len(self._menu_options)
                self.app.invalidate()
                return
            buf = self.input.buffer
            if buf.complete_state:
                buf.complete_next()
            elif buf.document.cursor_position_row == buf.document.line_count - 1:
                buf.history_forward()
            else:
                buf.cursor_down()

        def _digit_handler(n: int):
            def _(event):
                # select()-mode menu → jump straight to option n and confirm;
                # otherwise behave like ordinary self-insert
                if self._menu_options is not None and n <= len(self._menu_options):
                    self._resolve_menu(n - 1)
                else:
                    self.input.buffer.insert_text(str(n))
            return _

        for _n in range(1, 10):
            kb.add(str(_n))(_digit_handler(_n))

        @kb.add(Keys.Any, filter=Condition(lambda: self._menu_options is not None))
        def _(event):
            # while a select() menu owns the input line it is a pure chooser:
            # arrows/enter/esc/digits are bound above; every other key (letters,
            # space, backspace, paste) must be swallowed so nothing leaks into
            # the buffer rendered underneath the menu. Keys.Any is a fallback —
            # it only fires when no more-specific binding matched.
            pass

        @kb.add("pageup")
        def _(event):
            self.scroll_by(-_PAGE_STEP)

        @kb.add("pagedown")
        def _(event):
            self.scroll_by(_PAGE_STEP)

        @kb.add("escape", "end")
        def _(event):
            self.scroll_end()

        def status():
            import time
            try:
                s = self.engine.context_stats()
                used = f"{s.used / 1000:.1f}k" if s.used >= 1000 else str(s.used)
                cost = f" │ ${s.cost_usd:.2f}" if s.cost_usd else ""
                warn = "  ⚠ context >80% — /compact?" if s.pct >= 80 else ""
                ml = " │ multiline" if self.engine.multiline else ""
                sid = str(s.session_id)
                # session id is its own fragment so a click can copy it
                mode_txt = "bash mode" if self._bash_mode else "prompt mode"
                frags = [("class:status",
                          f" {s.model} │ ctx {used}/{s.limit / 1000:.0f}k "
                          f"({s.pct:.0f}%){cost} │ {mode_txt} │ "),
                         ("class:status.id", f"session {sid}",
                          self._copy_session_id(sid)),
                         ("class:status", " │ "),
                         ("class:status.id", "copy last",
                          self._copy_raw_response()),
                         ("class:status", " │ "),
                         ("class:status.id", "copy all",
                          self._copy_all_chat())]
                if self._sel_frozen is not None:
                    frags.append(("class:status", " │ "))
                    frags.append(("class:status.id", "copy selected",
                                  self._copy_selected()))
                if ml:
                    frags.append(("class:status", ml))
                if warn:
                    frags.append(("class:status", warn))
            except Exception:
                frags = [("class:status", " aurora")]
            # Line 1 is identity only (model/ctx/session id). Line 2 shows the
            # tooltips by default, but any live/transient status takes it over —
            # thinking, awaiting-answer, exit-confirm, and copy notices never
            # crowd line 1, and never coexist with the tooltips.
            frags.append(("", "\n"))
            msg, ts = self._sel_notice
            esc_pending = self._esc_armed is not None \
                and time.monotonic() - self._esc_armed_at < 2
            if self._exit_confirm:
                pass   # no status-bar tip here — the double-Esc gesture speaks for itself
            elif self._question is not None or self._menu_options is not None:
                frags.append(("class:status.busy", " select one"))
            elif msg and time.monotonic() - ts < 4:
                frags.append(("class:status.busy", f" ✂ {msg}",
                              self._dismiss_notice_click()))
            elif self._busy:
                frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self._spin % 10]
                secs = int(time.time() - self._busy_since)
                cancel_hint = "Tap ESC twice to cancel" if esc_pending else "Tap ESC twice to cancel"
                frags.append(("class:status.busy",
                              f" {frame} {self._phase or 'working'}… "
                              f"{secs}s ({cancel_hint})"))
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

        from prompt_toolkit.layout import ConditionalContainer
        menu_active = Condition(lambda: self._menu_options is not None)
        help_active = Condition(lambda: self._help_visible)
        menu_win = ConditionalContainer(
            Window(FormattedTextControl(self._menu_fragments),
                   height=lambda: Dimension.exact(self._menu_height()),
                   style="class:menu"),
            filter=menu_active)
        chat_visible = Condition(lambda: not self._help_visible)
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
                Window(FormattedTextControl(status), height=2,
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

    # ── worker: the session thread (all command/turn code runs here) ─────
    def _worker(self):
        engine, fe = self.engine, self.fe
        self._banner()

        import time as _t
        bp_text, bp_source = bootstrap.load(".")
        if bp_text and not engine.messages:
            first = next((l for l in bp_text.splitlines() if l.strip()), "")
            self.append(f"{YELLOW}bootstrap prompt{RESET} [{bp_source}] "
                        + dim(f"{len(bp_text)} chars") + "\n"
                        + dim(f"  “{ui._short(first, 70)}”") + "\n")
            if ui.confirm("Run the bootstrap prompt?"):
                self._busy, self._busy_since, self._phase = True, _t.time(), "working"
                ui._run_bootstrap(engine, fe)
                self._busy, self._phase = False, ""
                self._esc_armed = None

        import time
        while True:
            line = self._inbox.get()
            self._busy, self._busy_since, self._phase = True, time.time(), "working"
            try:
                if line.startswith("!"):          # local bash, no LLM (R10)
                    r = subprocess.run(line[1:], shell=True, text=True,
                                       capture_output=True)
                    out = (r.stdout or "") + (r.stderr or "")
                    print(out if out.strip() else dim("(no output)"))
                elif line.startswith("/"):
                    if not ui._handle_command(engine, fe, line):
                        break
                else:
                    ui._run_turn(engine, fe, line)
            except BaseException as e:            # never kill the session loop
                print(f"\n{RED}✗ {e.__class__.__name__}: {e}{RESET}")
            finally:
                self._busy, self._phase = False, ""
                self._esc_armed = None
                self.app.invalidate()
        self.app.loop.call_soon_threadsafe(self.app.exit)

    def _banner(self):
        import os
        from . import __version__ as version
        from . import logo

        engine = self.engine
        h = engine.provider_health()
        mark = f"{GREEN}✔{RESET}" if h["ok"] else f"{RED}✘{RESET}"
        info_lines = [f"{CYAN}{BOLD}Aurora{RESET} {dim('v' + version)}",
                      f"  model    {BOLD}{engine.current.get('model')}{RESET}  "
                      f"{mark} {dim(h['detail'])}",
                      f"  cwd      {os.getcwd()}",
                      f"  session  {engine.session.id}"
                      + (dim(f"  ({len(engine.messages)} messages resumed)")
                         if engine.messages else ""),
                      dim("  /help · /model · ? help · --man manual"), ""]

        logo_path = logo.resolve_logo(engine.cfg)
        logo_lines: list[str] = []
        logo_w = 0
        if logo_path:
            try:
                logo_lines = logo.render(logo_path, max_rows=10, max_cols=22)
                logo_w = logo.visible_width(logo_lines[0]) if logo_lines else 0
            except Exception as e:
                info_lines.append(dim(f"  logo: {e}"))

        merged: list[str] = []
        for i in range(max(len(logo_lines), len(info_lines))):
            left = logo_lines[i] if i < len(logo_lines) else " " * logo_w
            right = info_lines[i] if i < len(info_lines) else ""
            merged.append(f"{left}  {right}")
        self.append("\n".join(merged) + "\n")

    # ── run ───────────────────────────────────────────────────────────────
    def run(self):
        from . import keystore
        keystore.set_prompter(self.fe.ask_secret)

        real_stdout, real_input = sys.stdout, builtins.input
        real_select = ui.select
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
        try:
            self.app.run()
        finally:
            sys.stdout, builtins.input = real_stdout, real_input
            ui.select = real_select
        print(f"\nResume this session with:\n  aurora --resume {self.engine.session.id}")
        print("bye")


def run(engine: Engine, debug: bool = False) -> None:
    Tui(engine, debug=debug).run()
