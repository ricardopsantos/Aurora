"""TUI unit tests — buffer, scroll math, question mode. The Application is
built but never run (no terminal needed)."""

import threading

import pytest

from aurora import tui


def _mouse_up():
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
    from prompt_toolkit.data_structures import Point
    return MouseEvent(position=Point(x=0, y=0), event_type=MouseEventType.MOUSE_UP,
                      button=MouseButton.LEFT, modifiers=frozenset())


class _Stats:
    model, used, limit, pct, cost_usd, session_id, cost_known = \
        "m", 1200, 64000, 2.0, 0, "s1", False


class _FakeEngine:
    runtime = {}
    cfg = {"_base_dir": None}
    messages = []
    multiline = False

    def context_stats(self):
        return _Stats()

    def cycle_model(self):
        return None

    def valid_models(self):
        return []

    def set_multiline(self, on):
        self.multiline = on


@pytest.fixture
def t(monkeypatch):
    # Application() builds fine headless; only .run() needs a terminal
    return tui.Tui(_FakeEngine())


def test_append_and_line_count(t):
    t.append("hello\nworld\n")
    t._fragments()
    assert t._nlines == 2
    assert t._follow


def test_scroll_up_unfollows_and_end_refollows(t):
    t.append("x\n" * 50)
    t._fragments()
    t.scroll_by(-5)
    assert not t._follow
    assert t._cursor().y == 45
    t.scroll_by(-100)          # clamp at top
    assert t._cursor().y == 0
    t.scroll_end()
    assert t._follow
    assert t._cursor().y == t._nlines


def test_scroll_down_to_bottom_refollows(t):
    t.append("x\n" * 20)
    t._fragments()
    t.scroll_by(-3)
    assert not t._follow
    t.scroll_by(3)
    assert t._follow


def test_question_mode_roundtrip(t):
    got = {}

    def worker():
        got["answer"] = t.ask("approve? [y/N]:")

    th = threading.Thread(target=worker)
    th.start()
    while t._question is None:   # wait for the ask to arm
        pass
    t._answers.put("y")
    th.join(timeout=2)
    assert got["answer"] == "y"
    assert t._question is None


def test_ask_from_ui_thread_raises_not_deadlocks(t):
    """builtins.input is monkeypatched to ask() while the TUI runs; a call
    from the UI event-loop thread could never be answered (that thread IS
    the answerer) — it must raise, not block forever."""
    t._ui_thread = threading.current_thread()  # pretend WE are the event loop
    with pytest.raises(RuntimeError, match="deadlock"):
        t.ask("q?")


def test_ask_from_any_non_ui_thread_is_answered(t):
    """Both the session worker AND nested threads it spawns (ui._run_turn
    runs engine.send in one — mid-turn key prompts arrive from there) must
    be allowed to ask. Regression: the guard once required the worker thread
    itself, which broke the first OpenRouter key prompt on the MacBook."""
    t._ui_thread = threading.Thread(target=lambda: None)  # some other thread
    got = {}

    def nested_turn_thread():
        got["answer"] = t.ask("Enter OPENROUTER_API_KEY: ")

    th = threading.Thread(target=nested_turn_thread)
    th.start()
    while t._question is None:
        pass
    t._answers.put("sk-or-xyz")
    th.join(timeout=2)
    assert got["answer"] == "sk-or-xyz"


def test_chat_writer_feeds_chat(t):
    w = tui._ChatWriter(t)
    w.write("abc")
    w.flush()
    assert "abc" in "".join(t._chat)
    assert w.isatty()


def test_think_entry_collapsed_then_toggle(t):
    t.think_chunk("step one ")
    t.think_chunk("step two")
    frags = t._fragments()
    text = "".join(f[1] for f in frags)
    assert "thinking…" in text and "step one" not in text   # collapsed
    idx, entry = next((i, e) for i, e in enumerate(t._chat) if isinstance(e, dict))
    with t._lock:
        entry["open"] = True
        t._dirty(idx)
    text = "".join(f[1] for f in t._fragments())
    assert "step one step two" in text                       # expanded
    t.finish_think()
    text = "".join(f[1] for f in t._fragments())
    assert "thought for" in text and "s —" in text   # timed header


def test_begin_think_row_without_text_is_timed_not_clickable(t):
    t.begin_think()
    frags = t._fragments()
    header = next(f for f in frags if "thinking…" in f[1])
    assert "s" in header[1] and len(header) == 2     # timed, no click handler
    t.finish_think()
    text = "".join(f[1] for f in t._fragments())
    assert "thought for" in text and "click" not in text


def test_begin_think_is_idempotent_per_request(t):
    t.begin_think()
    t.begin_think()
    assert sum(isinstance(e, dict) for e in t._chat) == 1
    t.think_chunk("x")                               # lands in the same row
    assert sum(isinstance(e, dict) for e in t._chat) == 1


def test_ask_prompt_becomes_input_prompt_not_chat(t):
    got = {}

    def worker():
        got["answer"] = t.ask("approve? [y]es / [c]omment:")

    th = threading.Thread(target=worker)
    th.start()
    while t._question is None:
        pass
    assert t._question.startswith("approve?")        # question armed inline
    assert not any("approve?" in e for e in t._chat if isinstance(e, str))
    t._answers.put("y")
    th.join(timeout=2)
    assert got["answer"] == "y"


def test_think_header_has_click_handler(t):
    t.think_chunk("x")
    frags = t._fragments()
    header = next(f for f in frags if "thinking" in f[1])
    assert len(header) == 3 and callable(header[2])


# ── drag-select → auto-copy (R48) ─────────────────────────────────────────
def test_drag_select_freezes_range_and_offers_copy_button(t, monkeypatch):
    copied = {}
    monkeypatch.setattr("aurora.clipboard.copy",
                        lambda s: copied.update(text=s) or "OSC52")
    t.append("alpha beta\ngamma delta\n")
    t.sel_begin((0, 6))               # "beta"
    t.sel_drag((1, 5))                # …through "gamma"
    assert t._sel == ((0, 6), (1, 5))
    assert t.sel_finish() is True     # drag → swallowed click
    assert not copied                 # not copied yet — needs an explicit tap
    assert t._sel is None             # live drag cleared…
    assert t._sel_frozen == ((0, 6), (1, 5))  # …but stays frozen/highlighted
    # tapping "copy selected" does the actual copy
    handler = t._copy_selected()
    handler(_mouse_up())
    assert copied["text"] == "beta\ngamma"
    assert "copied" in t._sel_notice[0]
    assert t._sel_frozen is None      # button disappears after copying


def test_backwards_drag_normalizes(t, monkeypatch):
    copied = {}
    monkeypatch.setattr("aurora.clipboard.copy",
                        lambda s: copied.update(text=s) or "OSC52")
    t.append("one two three\n")
    t.sel_begin((0, 7))
    t.sel_drag((0, 4))                # dragged right-to-left
    t.sel_finish()
    t._copy_selected()(_mouse_up())
    assert copied["text"] == "two"


def test_new_drag_drops_pending_frozen_selection(t):
    t.append("one two three\n")
    t.sel_begin((0, 0))
    t.sel_drag((0, 3))
    t.sel_finish()
    assert t._sel_frozen is not None
    t.sel_begin((0, 4))               # starting a fresh drag…
    assert t._sel_frozen is None      # …drops the old pending selection


def test_plain_click_is_not_a_copy(t):
    t.append("hello\n")
    t.sel_begin((0, 2))
    assert t.sel_finish() is False    # no drag → falls through to fragments


def test_overlay_reverses_only_selection(t):
    frags = [("bold", "ab\ncd"), ("", "ef\n")]
    out = tui._overlay(frags, (0, 1), (1, 1))   # "b\nc"
    joined = "".join(f[1] for f in out)
    assert joined == "ab\ncdef\n"               # text unchanged
    rev = "".join(f[1] for f in out if "reverse" in f[0])
    assert rev == "b\nc"
    assert ("bold", "a") == (out[0][0], out[0][1])


def test_osc52_never_writes_to_redirected_stdout(monkeypatch):
    """Inside the TUI sys.stdout is the chat pane (isatty()=True!) — an OSC52
    write there renders as visible garbage. It must go to /dev/tty or the
    real process stdout, never sys.stdout."""
    import io
    from aurora import clipboard

    class TtyLike(io.StringIO):
        def isatty(self):
            return True

    fake_out = TtyLike()
    monkeypatch.setattr("sys.stdout", fake_out)
    real_open = open
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(
        OSError("no tty")) if a and a[0] == "/dev/tty" else real_open(*a, **k))
    clipboard._osc52("secret")
    assert fake_out.getvalue() == ""   # chat pane stayed clean


def test_local_session_prefers_os_clipboard_tool(monkeypatch):
    """Terminal.app drops OSC52 silently — locally the OS tool must win."""
    from aurora import clipboard
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr(clipboard, "_local_tool", lambda t: "pbcopy")
    monkeypatch.setattr(clipboard, "_osc52",
                        lambda t: (_ for _ in ()).throw(AssertionError(
                            "OSC52 must not be tried when a tool worked")))
    assert clipboard.copy("x") == "pbcopy"


def test_ssh_session_prefers_osc52(monkeypatch):
    from aurora import clipboard
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    monkeypatch.setattr(clipboard, "_local_tool",
                        lambda t: (_ for _ in ()).throw(AssertionError(
                            "remote-side tool must not preempt OSC52")))
    monkeypatch.setattr(clipboard, "_osc52", lambda t: True)
    assert "OSC52" in clipboard.copy("x")


def test_approve_comment_choice_prompts_for_guidance(monkeypatch):
    from aurora import ui
    fe = ui.TerminalFrontend()
    # select() reads the menu choice ("c"); approve() then reads free-text
    # guidance via a plain input() call
    answers = iter(["c", "please use rsync instead"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    key, note = fe.approve("run_command", {"command": "cp -r a b"}, "")
    assert (key, note) == ("c", "please use rsync instead")


def test_approve_menu_accepts_number_or_key(monkeypatch):
    from aurora import ui
    fe = ui.TerminalFrontend()
    answers = iter(["1"])                  # "1" == first option == "y"
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    key, _ = fe.approve("run_command", {"command": "ls"}, "")
    assert key == "y"

    answers = iter(["y"])                  # the raw key also works
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    key, _ = fe.approve("run_command", {"command": "ls"}, "")
    assert key == "y"


def test_ask_continue_comment_choice_is_guidance(monkeypatch):
    from aurora import ui
    fe = ui.TerminalFrontend()
    answers = iter(["c", "focus on the tests"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    go_on, note = fe.ask_continue(20)
    assert go_on and note == "focus on the tests"


def test_confirm_is_a_numbered_menu_with_default_first(monkeypatch):
    from aurora import ui
    seen = {}
    def fake_select(prompt, options):
        seen["options"] = options
        return options[0][0]                 # pick the default (first, Enter)
    monkeypatch.setattr(ui, "select", fake_select)
    assert ui.confirm("Run it?") is True                       # [Y/n]: Yes first
    assert seen["options"][0] == ("y", "Yes")
    assert ui.confirm("Evict?", default_yes=False) is False     # [y/N]: No first
    assert seen["options"][0] == ("n", "No")


# ── select() arrow-key menu (TUI) ─────────────────────────────────────────
_OPTS = [("y", "Yes"), ("n", "No"), ("c", "Comment")]


def test_select_menu_roundtrip_returns_chosen_key(t):
    got = {}

    def worker():
        got["key"] = t.select_menu("Approve?", _OPTS)

    th = threading.Thread(target=worker)
    th.start()
    while t._menu_options is None:     # wait for the menu to arm
        pass
    assert t._menu_index == 0          # first option highlighted by default
    t._resolve_menu(1)                 # as the "down,down,enter" path would
    th.join(timeout=2)
    assert got["key"] == "n"
    assert t._menu_options is None      # torn down after the answer

def test_select_menu_pointer_tracks_index(t):
    # a label can carry raw ANSI colour (e.g. /model's tags), so a row is now
    # several fragments, not one — group fragments into rows by the newlines
    # they contain, then check styles per row instead of per fragment.
    t._menu_prompt, t._menu_options, t._menu_index = "Pick", _OPTS, 2
    frags = t._menu_fragments()
    text = "".join(f[1] for f in frags)
    assert "❯ 3. Comment" in text                    # pointer on current index
    assert "1. Yes" in text and "2. No" in text      # every option on its row
    assert t._menu_height() == len(_OPTS) + 2        # prompt + options + hint

    rows: list[list[tuple]] = [[]]
    for style, frag_text in ((f[0], f[1]) for f in frags):
        for j, part in enumerate(frag_text.split("\n")):
            if j > 0:
                rows.append([])
            if part:
                rows[-1].append((style, part))

    def _row_for(marker: str) -> list[tuple]:
        # ANSI() fragments plain text char-by-char (no escapes to group on),
        # so check the ROW'S JOINED text, not any single fragment's text
        return next(r for r in rows if marker in "".join(txt for _, txt in r))

    assert any(s == "class:menu.selected" for s, _ in _row_for("Comment"))
    assert any(s == "class:menu.option" for s, _ in _row_for("1. Yes"))
    assert not any(s == "class:menu.selected" for s, _ in _row_for("1. Yes"))

def test_resolve_menu_echoes_label_into_transcript(t):
    t._menu_prompt, t._menu_options, t._menu_index = "Approve?", _OPTS, 0
    t._resolve_menu(2)
    assert t._answers.get() == "c"
    assert any("→ Comment" in e for e in t._chat if isinstance(e, str))

def test_menu_esc_is_noop_while_open(t):
    # Esc must NOT resolve an open challenge/confirm menu — the user always
    # picks explicitly (arrow keys + Enter, or a number key).
    t._menu_prompt, t._menu_options, t._menu_index = "Approve?", [
        ("y", "Yes"), ("a", "Always"), ("n", "No"), ("s", "Stop"),
        ("c", "Comment")], 0
    t._on_escape(lambda: None)
    assert t._menu_options is not None
    assert t._answers.empty()


def _status_line2(t):
    from prompt_toolkit.layout.controls import FormattedTextControl
    ctrl = next(c for c in t.app.layout.find_all_controls()
                if isinstance(c, FormattedTextControl)
                and any("session " in f[1] for f in
                        (c.text() if callable(c.text) else c.text)))
    text = "".join(f[1] for f in ctrl.text())
    return text.split("\n")[1].strip()


def _status_line1(t):
    from prompt_toolkit.layout.controls import FormattedTextControl
    ctrl = next(c for c in t.app.layout.find_all_controls()
                if isinstance(c, FormattedTextControl)
                and any("session " in f[1] for f in
                        (c.text() if callable(c.text) else c.text)))
    text = "".join(f[1] for f in ctrl.text())
    return text.split("\n")[0].strip()


def test_draft_token_estimate_shown_while_typing(t):
    t.input.buffer.text = "hello world this is a test prompt"  # 35 chars
    assert "- ↑8" in _status_line1(t)


def test_draft_token_estimate_hidden_when_empty(t):
    t.input.buffer.text = "   "
    assert "↑" not in _status_line1(t)


def test_draft_token_estimate_hidden_in_bash_mode(t):
    t._bash_mode = True
    t.input.buffer.text = "ls -la"
    assert "↑" not in _status_line1(t)


def test_status_hint_row_in_prompt_mode(t):
    assert _status_line2(t) == (
        "/ commands · ! bash · \\n/\\br newline · Ctrl+J newline · "
        "? Help · Esc cancel/clear/exit")


def test_status_hint_row_in_bash_mode(t):
    t._bash_mode = True
    assert _status_line2(t) == (
        "/ commands · > prompt · \\n/\\br newline · Ctrl+J newline · "
        "? Help · Esc cancel/clear/exit")


def test_click_dismisses_copy_notice_early(t):
    import time
    t._sel_notice = ("whole chat copied — OSC52", time.monotonic())
    assert "whole chat copied" in _status_line2(t)
    t._dismiss_notice_click()(_mouse_up())
    assert t._sel_notice == ("", 0.0)
    assert _status_line2(t) == (
        "/ commands · ! bash · \\n/\\br newline · Ctrl+J newline · "
        "? Help · Esc cancel/clear/exit")


def test_click_prompt_leaves_bash_mode(t):
    t.append("x\n")
    t._bash_mode = True
    t.input.buffer.document = t.input.buffer.document.__class__("some typed command")
    handler = t._leave_bash_mode_click()
    handler(_mouse_up())
    assert t._bash_mode is False
    assert t.input.buffer.text == ""


# ── generic double-Esc-within-2s gesture (cancel/bash-exit/quit) ──────────
def test_double_esc_opens_cancel_confirm_menu(t):
    # 2nd Esc doesn't cancel directly — it opens an explicit Yes/No question,
    # same arrow-key menu as everywhere else in the app
    t._busy = True
    t._on_escape(lambda: None)                          # 1st: arms
    assert t._esc_armed == "cancel"
    assert not t.fe.cancel_event.is_set()
    t._on_escape(lambda: None)                          # 2nd: opens the menu
    assert t._esc_armed is None
    assert not t.fe.cancel_event.is_set()                # not yet — needs a menu pick
    assert t._menu_prompt == "Cancel this?"
    assert [k for k, _ in t._menu_options] == ["cancel", "continue"]

    t._resolve_menu(1)   # "No, keep going"
    assert not t.fe.cancel_event.is_set() and t._menu_options is None

    # re-arm and pick "Yes, cancel" this time
    t._busy = True
    t._on_escape(lambda: None)
    t._on_escape(lambda: None)
    t._resolve_menu(0)   # "Yes, cancel"
    assert t.fe.cancel_event.is_set()


def test_double_esc_opens_quit_confirm_menu(t):
    # 2nd Esc doesn't quit directly — it opens an explicit Yes/No question,
    # same arrow-key menu as everywhere else in the app
    calls = []
    t._on_escape(lambda: calls.append("exit"))          # 1st: arms + exit_confirm
    assert t._exit_confirm and t._esc_armed == "exit"
    t._on_escape(lambda: calls.append("exit"))          # 2nd: opens the menu
    assert calls == []                                    # not yet — needs a menu pick
    assert not t._exit_confirm and t._esc_armed is None   # armed state consumed
    assert t._menu_prompt == "Quit Aurora?"
    assert [k for k, _ in t._menu_options] == ["yes", "no"]

    t._resolve_menu(1)   # "No, stay"
    assert calls == [] and t._menu_options is None

    # re-arm and pick "Yes" this time
    t._on_escape(lambda: calls.append("exit"))
    t._on_escape(lambda: calls.append("exit"))
    t._resolve_menu(0)   # "Yes, quit"
    assert calls == ["exit"]


def test_double_esc_opens_leave_bash_confirm_menu(t):
    t._bash_mode = True
    t._on_escape(lambda: None)                            # 1st: arms
    assert t._bash_mode and t._esc_armed == "bash"
    t._on_escape(lambda: None)                            # 2nd: opens the menu
    assert t._bash_mode                                    # not left yet
    assert t._esc_armed is None
    assert t._menu_prompt == "Leave bash mode?"
    assert [k for k, _ in t._menu_options] == ["leave", "stay"]

    t._resolve_menu(1)   # "stay"
    assert t._bash_mode and t._menu_options is None

    t._on_escape(lambda: None)
    t._on_escape(lambda: None)
    t._resolve_menu(0)   # "leave"
    assert not t._bash_mode


def test_esc_armed_window_expires_instead_of_confirming(t):
    # a second Esc long after the first must re-arm, not confirm — a stray
    # press minutes later must never silently cancel/quit/leave bash mode
    t._busy = True
    t._on_escape(lambda: None)
    assert t._esc_armed == "cancel"
    t._esc_armed_at -= 3   # simulate >2s having passed
    calls = []
    t._on_escape(lambda: calls.append("cancelled"))
    assert t._esc_armed == "cancel"          # re-armed, a fresh "first press"
    assert not t.fe.cancel_event.is_set()    # NOT confirmed


def test_esc_confirm_is_reset_when_state_changes(t):
    # arming "bash" then leaving bash mode some OTHER way (not Esc) must not
    # let a later, unrelated Esc silently confirm a stale pending action
    t._bash_mode = True
    t._on_escape(lambda: None)
    assert t._esc_armed == "bash"
    t._bash_mode = False   # e.g. via backspace-on-empty, not Esc
    t._busy = True
    t._on_escape(lambda: None)   # must ARM "cancel" fresh, not confirm anything
    assert t._esc_armed == "cancel"
    assert not t.fe.cancel_event.is_set()


def test_bash_mode_toggle_run_and_exit(t):
    import time
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    with create_pipe_input() as pipe:
        t.app.input, t.app.output = pipe, DummyOutput()
        th = threading.Thread(target=t.app.run, daemon=True)
        th.start()
        time.sleep(0.3)
        pipe.send_text("!")                    # empty prompt → bash mode
        time.sleep(0.15)
        assert t._bash_mode and t.input.buffer.text == ""
        pipe.send_text("ls\r")                 # command + Enter
        time.sleep(0.15)
        assert t._inbox.get_nowait() == "!ls"  # worker's `!` path runs it
        assert t._bash_mode                     # stays in bash mode
        pipe.send_text("\x7f")                 # backspace on empty → exit
        time.sleep(0.15)
        assert not t._bash_mode
        pipe.send_text("x!")                   # `!` mid-text is literal
        time.sleep(0.15)
        assert t.input.buffer.text == "x!" and not t._bash_mode
        t.app.exit()
        time.sleep(0.1)


def test_completion_menu_ignores_stray_mouse_when_no_completion(monkeypatch):
    # prompt_toolkit crashes if a MOUSE_UP hits the completion menu while
    # complete_state is None (stray click after returning to the window). The
    # guarded control must swallow it instead of asserting.
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
    from prompt_toolkit.data_structures import Point
    ctrl = tui._SafeCompletionsMenuControl()

    class _Buf: complete_state = None
    class _App: current_buffer = _Buf()
    monkeypatch.setattr(tui, "get_app", lambda: _App())

    ev = MouseEvent(position=Point(x=0, y=0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert ctrl.mouse_handler(ev) is None      # no AssertionError


def test_challenge_menu_has_no_background(t):
    # MUST check the merged renderer style, not t.app.style: prompt_toolkit's
    # default `menu` class is bg:#888888 (grey), which cascades under every
    # menu row unless we override the base `menu` class. (t.app.style omits the
    # defaults, so it hid this regression.)
    merged = t.app._merged_style
    for cls in ("class:menu class:menu.option",
                "class:menu class:menu.selected",
                "class:menu class:menu.prompt",
                "class:menu class:menu.hint"):
        assert merged.get_attrs_for_style_str(cls).bgcolor == "", cls


def test_completion_menu_is_not_grey(t):
    # prompt_toolkit's default completion dropdown is a light-grey bar; we
    # retheme it dark so it doesn't read as a stray grey box
    bg = t.app._merged_style.get_attrs_for_style_str(
        "class:completion-menu.completion").bgcolor
    assert bg and bg not in ("aaaaaa", "888888")


def test_select_menu_preserves_and_restores_input_draft(t):
    t.input.buffer.text = "half-typed thought"
    got = {}

    def worker():
        got["key"] = t.select_menu("Approve?", _OPTS)

    th = threading.Thread(target=worker)
    th.start()
    while t._menu_options is None:
        pass
    assert t.input.buffer.text == ""    # draft cleared so it can't bleed under the menu
    t._resolve_menu(0)
    th.join(timeout=2)
    assert got["key"] == "y"
    assert t.input.buffer.text == "half-typed thought"  # restored after the menu


def test_ask_preserves_and_restores_input_draft(t):
    t.input.buffer.text = "half-typed thought"
    got = {}

    def worker():
        got["answer"] = t.ask("approve? [y/N]:")

    th = threading.Thread(target=worker)
    th.start()
    while t._question is None:
        pass
    assert t.input.buffer.text == ""    # draft cleared so it can't bleed into the answer
    t._answers.put("y")
    th.join(timeout=2)
    assert got["answer"] == "y"
    assert t.input.buffer.text == "half-typed thought"  # restored after the ask


def test_short_transcript_bottom_anchors(t):
    t.append("hello\n")
    t._fragments()

    class _RI:
        window_height = 12

    t._chat_win.render_info = _RI()
    pad = t._pad()
    assert pad == 12 - t._nlines - 1
    frags = t._render_fragments()
    assert frags[0][1] == "\n" * pad            # top padding, content at bottom
    assert t._cursor().y == pad + t._nlines


def test_full_pane_has_no_padding(t):
    t.append("x\n" * 50)
    t._fragments()

    class _RI:
        window_height = 12

    t._chat_win.render_info = _RI()
    assert t._pad() == 0


def test_exit_command_in_completer():
    from aurora import ui
    assert "exit" in ui.COMMAND_INFO and "quit" in ui.COMMAND_INFO


def test_tool_only_round_closes_the_think_row(t):
    """A round that ends in tool calls without text never fires on_text —
    the first plain print (tool start) must close the live row, or its
    clock runs forever and the render cache stays disabled all session."""
    t.begin_think()
    assert t._open_think
    t.append("⚙ run_command(...)\n")             # tool output arrives
    entry = next(e for e in t._chat if isinstance(e, dict))
    assert entry["done"] and not t._open_think
    text = "".join(f[1] for f in t._fragments())
    assert "thought for" in text and "thinking…" not in text


def test_closed_rows_reenable_the_render_cache(t):
    t.begin_think()
    t.append("out\n")                            # closes the row
    first = t._fragments()
    assert t._fragments() is first               # cache hit — no rebuild


# ── R62: the first Esc shows an "Esc again to …" hint on status line 2 ─────
def test_esc_armed_shows_cancel_hint(t):
    t._busy = True
    t._on_escape(lambda: None)                  # 1st press arms
    assert "Esc again to cancel this" in _status_line2(t)


def test_esc_armed_shows_quit_hint(t):
    t._on_escape(lambda: None)                  # idle empty prompt: arms exit
    assert "Esc again to quit" in _status_line2(t)


def test_esc_armed_shows_leave_bash_hint(t):
    t._bash_mode = True
    t._on_escape(lambda: None)
    assert "Esc again to leave bash mode" in _status_line2(t)


def test_esc_hint_expires_back_to_tooltips(t):
    t._on_escape(lambda: None)
    t._esc_armed_at -= 3                        # age the arm past the 2s window
    t._exit_confirm = False                     # user typed/state moved on
    assert _status_line2(t).startswith("/ commands")


# ── R48: drag-select coords vs. the bottom-anchor top pad ──────────────────
def test_drag_select_accounts_for_top_pad(t, monkeypatch):
    # short transcript in a tall window: content is top-padded, so mouse rows
    # arrive offset by the pad — the copied text must NOT be
    t.append("alpha\nbeta\ngamma\n")
    t._fragments()
    monkeypatch.setattr(t, "_pad", lambda: 5)
    t.sel_begin((6, 0))                   # visual row 6 == text row 1 ("beta")
    t.sel_drag((6, 4))
    assert t._sel == ((1, 0), (1, 4))     # stored unpadded
    assert t.sel_finish() is True
    assert t._sel_text(t._sel_frozen) == "beta"


def test_drag_select_render_overlay_shifts_back_by_pad(t, monkeypatch):
    t.append("alpha\nbeta\n")
    t._fragments()
    monkeypatch.setattr(t, "_pad", lambda: 3)
    t.sel_begin((4, 0))
    t.sel_drag((4, 4))
    frags = t._render_fragments()
    sel_text = "".join(s for style, s, *_ in frags if "reverse" in style)
    assert sel_text == "beta"
