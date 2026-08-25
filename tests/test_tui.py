"""TUI unit tests — buffer, scroll math, question mode. The Application is
built but never run (no terminal needed)."""

import threading
import types
from pathlib import Path

import pytest
from prompt_toolkit.keys import Keys

from aurora import tui


def _press(t, key):
    """Invoke the registered key-binding handler for `key` directly — no
    real terminal/event loop is needed since these handlers never read
    anything off `event` besides what's already reachable via `self`
    (the enclosing Tui instance)."""
    for b in t.app.key_bindings.bindings:
        if b.keys == (key,):
            b.handler(None)
            return
    raise AssertionError(f"no binding registered for {key}")


class _FakeKeyEvent:
    """Minimal stand-in for prompt_toolkit's KeyPressEvent — enough for the
    bindings that read `event.arg` (the repeat count)."""
    arg = 1


def _press_with_arg(t, key):
    """`_press` with a real-enough event object, for bindings whose body
    touches `event`."""
    for b in t.app.key_bindings.bindings:
        if b.keys == (key,):
            b.handler(_FakeKeyEvent())
            return
    raise AssertionError(f"no binding registered for {key}")


def _type(t, text):
    """Insert text the way a keystroke does (so `on_text_insert` fires),
    minus `complete_while_typing` — the async completer wants a running
    event loop and the Application is built but never run in these tests."""
    from prompt_toolkit.filters import to_filter
    t.input.buffer.complete_while_typing = to_filter(False)
    t.input.buffer.auto_suggest = None
    t.input.buffer.insert_text(text)


def _press_enter(t):
    """Drive the real `enter` binding. Needs an event carrying `.app` (the
    exit-confirm branch calls `event.app.exit()`)."""
    class _Ev:
        arg = 1
        app = t.app
    for b in t.app.key_bindings.bindings:
        if b.keys == (Keys.ControlM,):
            b.handler(_Ev())
            return
    raise AssertionError("no binding registered for enter")


def test_merge_char_runs_matches_the_naive_implementation():
    """R148 rewrote this for speed (per-character string rebuild → one join
    per run). It is only allowed to be faster, never different — every
    linkify pass downstream depends on its exact output, and a fragment
    carrying a mouse handler (a 3-tuple) must still pass through unmerged."""
    import random

    from prompt_toolkit.formatted_text import ANSI

    def naive(frags):
        out = []
        for f in frags:
            if out and out[-1][0] == f[0] and len(out[-1]) == 2 and len(f) == 2:
                out[-1] = (f[0], out[-1][1] + f[1])
            else:
                out.append(f)
        return out

    random.seed(7)
    texts = ["", "x", "x" * 4096, "\x1b[2mdim\x1b[0mplain\x1b[31mred\x1b[0m",
             "\x1b[2m" + "y" * 4000 + "\x1b[0m", "a\x1b[31mb\x1b[0mc" * 300]
    for _ in range(50):
        texts.append("".join(
            random.choice(["\x1b[2m", "\x1b[0m", "\x1b[31m", ""])
            + "".join(random.choice("abc \n") for _ in range(random.randint(0, 30)))
            for _ in range(random.randint(0, 40))))
    for text in texts:
        frags = ANSI(text).__pt_formatted_text__()
        assert tui._merge_char_runs(frags) == naive(frags), repr(text[:40])

    h = (lambda e: None)
    for mixed in ([("class:a", "x"), ("class:a", "y"), ("class:a", "z", h),
                   ("class:a", "w"), ("class:b", "q")],
                  [("class:a", "x", h), ("class:a", "y"), ("class:a", "z")],
                  [], [("c", "only")]):
        assert tui._merge_char_runs(mixed) == naive(mixed)


def _mouse_up():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    return MouseEvent(position=Point(x=0, y=0), event_type=MouseEventType.MOUSE_UP,
                      button=MouseButton.LEFT, modifiers=frozenset())


class _Stats:
    model, used, limit, pct, cost_usd, session_id, cost_known = \
        "m", 1200, 64000, 2.0, 0, "s1", False
    compactions = 0          # R159: default "never folded" — the counter
    # fragment is only rendered when this is non-zero
    local_model = None       # only set for the "local" sentinel


class _FakeEngine:
    compactions = 0          # R159 (read by _compactions_click)
    runtime = {}
    cfg = {"_base_dir": None}
    messages = []
    multiline = False
    _last_resp = ""

    def context_stats(self):
        return _Stats()

    def cycle_model(self):
        return None

    def valid_models(self):
        return []

    def set_multiline(self, on):
        self.multiline = on

    def last_response(self):
        return self._last_resp

    def last_prompt(self):
        return ""


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
    """Any thread that ISN'T the UI event loop must be allowed to ask — the
    worker thread itself (R96f: it calls engine.send() directly, no nested
    per-turn thread) or, in the classic REPL, the wrapper thread
    ui._run_turn still spawns there. Regression: the guard once required the
    worker thread itself, which broke the first OpenRouter key prompt on the
    MacBook."""
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


def test_chat_writer_coalesces_a_print_statements_two_writes(t, monkeypatch):
    """R170k: print("hello") calls .write() TWICE (content, then the "\n"
    end) — each used to be its own append() (lock + app.invalidate()). Both
    writes of one print() must now land as ONE append() call."""
    calls = []
    monkeypatch.setattr(t, "append", lambda s: calls.append(s))
    w = tui._ChatWriter(t)
    w.write("hello")     # content — no newline yet, must NOT flush
    assert calls == []
    w.write("\n")         # print()'s separate end="\n" write
    assert calls == ["hello\n"]


def test_chat_writer_flushes_immediately_on_embedded_newline(t, monkeypatch):
    calls = []
    monkeypatch.setattr(t, "append", lambda s: calls.append(s))
    w = tui._ChatWriter(t)
    w.write("line one\nline two")   # a single multi-line write still flushes
    assert calls == ["line one\nline two"]


def test_chat_writer_flush_pushes_a_trailing_partial_line(t, monkeypatch):
    """flush() used to be a total no-op; a caller doing print(x, end="")
    for a progress indicator relies on flush() actually pushing what's
    buffered so far — same contract a real terminal gives a partial line."""
    calls = []
    monkeypatch.setattr(t, "append", lambda s: calls.append(s))
    w = tui._ChatWriter(t)
    w.write("50%")
    assert calls == []      # no newline yet — must stay buffered
    w.flush()
    assert calls == ["50%"]


def test_chat_writer_is_a_real_io_textiobase(t):
    """R171/B6: subclassing io.TextIOBase means any extension checking
    `isinstance(sys.stdout, io.IOBase)` sees a real file-like object, and
    `write()`'s return-chars-accepted contract matches a standard buffered
    stream instead of being an undocumented surprise."""
    import io
    w = tui._ChatWriter(t)
    assert isinstance(w, io.TextIOBase)
    assert w.writable()
    assert w.write("hi") == 2


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


def test_live_think_row_rebuilds_once_per_second_not_per_render(t):
    """R95j: a live think row disabled the transcript cache outright, so
    every render — the 0.5s ticker, but also every keystroke and status
    invalidate — rebuilt the whole scrollback for a clock that changes once
    a second."""
    # chunks over _MERGE_LIMIT stay separate entries — a real scrollback
    for i in range(5):
        t.append(f"line {i} " + "x" * 5000 + "\n")
    t.begin_think()                      # must come after: append() closes it
    live = len(t._chat) - 1

    rebuilds = []
    real = t._entry_fragments
    t._entry_fragments = lambda item, index: (rebuilds.append(index),
                                              real(item, index))[1]

    t._fragments()                       # first call populates the cache
    rebuilds.clear()
    for _ in range(20):                  # a burst within the same second
        t._fragments()
    # only the live row may re-render; the static scrollback never does
    assert all(idx == live for idx in rebuilds), rebuilds
    assert len(rebuilds) <= 1, f"{len(rebuilds)} rebuilds within one second"

    # when the displayed second changes, it DOES rebuild — the clock is live
    with t._lock:
        for item in t._chat:
            if isinstance(item, dict) and not item["done"]:
                item["t0"] -= 1.5
    rebuilds.clear()
    t._fragments()
    assert rebuilds == [live]

    t.finish_think()
    rebuilds.clear()
    t._fragments()
    assert rebuilds == [live]            # once: the header becomes "thought for"
    rebuilds.clear()
    for _ in range(5):
        t._fragments()
    assert rebuilds == []                # closed row: fully cached again


# ── R96b: the flatten must be incremental, not per-frame over the session ──
class _CountingCache(list):
    """Counts per-entry reads. Deliberately measures the OBSERVABLE work —
    how many entries a render touches — rather than hooking an internal, so
    the assertion is about complexity and holds against any implementation."""

    reads = 0

    def __getitem__(self, i):
        if isinstance(i, int):
            self.reads += 1
        return super().__getitem__(i)


def _count_entry_reads(t):
    t._cache = _CountingCache(t._cache)
    return t._cache


def _plain(frags):
    """(style, text) only — think rows carry a fresh click closure per build,
    so the handlers are never identical between two renders."""
    return [(f[0], f[1]) for f in frags]


def test_chat_flatten_is_incremental_not_quadratic(t):
    """R96b: the per-entry parse cache was already right, but the FLATTENED
    fragment list was dropped on every append — so each frame re-concatenated
    every fragment in the session (34ms/frame at 4MB). Appends land on the
    tail; the rebuild must resume from there."""
    n = 100
    cache = _count_entry_reads(t)
    for i in range(n):
        t.append(f"line {i} " + "x" * 5000 + "\n")   # over _MERGE_LIMIT
        t._fragments()                                # one frame per append

    assert len(t._chat) == n
    # each frame flattens the one changed entry; re-flattening the whole list
    # every frame is ~n²/2 entry reads (5050 here) instead of ~n
    assert cache.reads < 3 * n, \
        f"{cache.reads} entry reads for {n} appends — flatten is not incremental"


def test_incremental_flatten_matches_a_full_rebuild(t):
    """R96b's cache is only worth having if it is indistinguishable from the
    naive rebuild — including after a NON-tail entry changes."""
    for i in range(6):
        t.append(f"chunk {i} " + "y" * 5000 + "\n")
    t.begin_think()
    t.think_chunk("reasoning text")
    t.finish_think()
    t.append("after the think row\n")

    incremental = _plain(t._fragments())
    nlines = t._nlines

    def full():
        with t._lock:
            t._cache = [None] * len(t._chat)
            t._text_cache = None
            t._dirty_from = None
        return _plain(t._fragments())

    assert incremental == full()
    assert t._nlines == nlines

    # now dirty a NON-tail entry: expanding the think block changes its height
    think_idx = next(i for i, e in enumerate(t._chat) if isinstance(e, dict))
    assert think_idx < len(t._chat) - 1, "think row must not be the tail here"
    with t._lock:
        t._chat[think_idx]["open"] = True
        t._dirty(think_idx)
    reopened = _plain(t._fragments())
    reopened_nlines = t._nlines

    assert reopened != incremental          # it really did change
    assert reopened == full()               # ...and matches a clean rebuild
    assert t._nlines == reopened_nlines


def test_non_tail_dirty_only_reflattens_from_that_entry(t):
    """A think-row toggle at the head must not force a whole-session rebuild
    either — it re-flattens from that index on, not from zero."""
    for i in range(3):
        t.append(f"pre {i} " + "z" * 5000 + "\n")
    t.begin_think()
    t.think_chunk("some reasoning")
    t.finish_think()
    for i in range(20):
        t.append(f"post {i} " + "z" * 5000 + "\n")
    t._fragments()

    think_idx = next(i for i, e in enumerate(t._chat) if isinstance(e, dict))
    cache = _count_entry_reads(t)
    with t._lock:
        t._chat[think_idx]["open"] = True
        t._dirty(think_idx)
    t._fragments()
    # only the toggled row and what follows it — not the whole transcript
    assert cache.reads == len(t._chat) - think_idx
    assert cache.reads < len(t._chat)


# ── R96f: no redundant per-turn thread in the TUI's send path ─────────────
def test_send_turn_calls_engine_send_without_spawning_a_thread(monkeypatch):
    """R96f: ui._send_turn is what the TUI worker now calls directly for a
    plain turn — it must run engine.send() on the CALLING thread, not spawn
    one of its own. The classic REPL's Ctrl+C wrapper (ui._run_turn) still
    spawns a thread; this asserts the TUI's path specifically does not."""
    from aurora import ui
    calling_thread = threading.current_thread()
    seen = {}

    class _FakeEngine:
        def send(self, text, fe, bootstrap=False):
            seen["thread"] = threading.current_thread()

    class _FakeFE:
        cancel_event = threading.Event()

        def begin_turn(self):
            pass

        def end_turn(self):
            pass

    ui._send_turn(_FakeEngine(), _FakeFE(), "hello")
    assert seen["thread"] is calling_thread


def test_run_bootstrap_sync_mode_does_not_spawn_a_thread(tmp_path, monkeypatch):
    """R96f: ui._run_bootstrap(sync=True) — what the TUI worker calls — must
    reach engine.send on the calling thread too, not through _run_turn's
    Ctrl+C thread wrapper."""
    from aurora import bootstrap as bootstrap_mod
    from aurora import ui
    calling_thread = threading.current_thread()
    seen = {}

    monkeypatch.setattr(bootstrap_mod, "load", lambda cwd: ("do the thing", "test"))

    class _FakeSession:
        def log(self, *a, **k):
            pass

    class _FakeEngine:
        session = _FakeSession()

        def send(self, text, fe, bootstrap=False):
            seen["thread"] = threading.current_thread()
            seen["bootstrap"] = bootstrap

    class _FakeFE:
        cancel_event = threading.Event()

        def begin_turn(self):
            pass

        def end_turn(self):
            pass

    ui._run_bootstrap(_FakeEngine(), _FakeFE(), sync=True)
    assert seen["thread"] is calling_thread
    assert seen["bootstrap"] is True


def test_tui_worker_uses_send_turn_not_run_turn(t, monkeypatch):
    """Guards against a regression sliding back to the thread-wrapped call:
    the TUI's inbox-driven turn path must reach ui._send_turn, never
    ui._run_turn (which exists only for the classic REPL's Ctrl+C wrapper)."""
    from aurora import ui
    calls = []
    monkeypatch.setattr(ui, "_send_turn",
                        lambda *a, **k: calls.append("_send_turn"))
    monkeypatch.setattr(ui, "_run_turn",
                        lambda *a, **k: calls.append("_run_turn"))
    monkeypatch.setattr(t, "_banner", lambda: None)
    monkeypatch.setattr("aurora.bootstrap.load", lambda cwd: ("", None))
    monkeypatch.setattr(t.app, "exit", lambda: None)   # no real event loop here

    t._inbox.put("hello")
    t._inbox.put("/exit")
    # run the worker body directly (no real event loop needed for this path)
    th = threading.Thread(target=t._worker, daemon=True)
    th.start()
    th.join(timeout=2)
    assert not th.is_alive()
    assert calls == ["_send_turn"]


# ── R98: up/down only recall history from an EMPTY draft ──────────────────
# prompt_toolkit's history_backward()/history_forward() need a real running
# event loop to lazy-load FileHistory (Buffer.load_history_if_not_yet_loaded
# schedules a background task via get_app()), which this headless Tui
# fixture doesn't have. Rather than stand up a full asyncio Application just
# to exercise that unrelated machinery, these tests verify the actual
# DECISION R98 changed — does up/down call history_backward/forward, or
# cursor_up/cursor_down — via spies, and separately confirm cursor_up/down
# really do land on the adjacent row using the buffer's own real cursor
# movement (no history involved at all).
def test_up_arrow_moves_cursor_not_history_when_draft_is_non_empty(t, monkeypatch):
    """R98: the old logic keyed on cursor_position_row == 0 alone, so
    up-arrow on line 2 of an unrelated multi-line draft that happened to put
    the cursor back on row 0 after a previous cursor_up() jumped into
    command history, discarding the user's place in their own draft. It
    must move within the draft instead, as long as the draft has any text
    at all — regardless of which row the cursor is currently on."""
    buf = t.input.buffer
    buf.text = "line one\nline two"
    buf.cursor_position = len(buf.text)   # end of line two

    calls = []
    monkeypatch.setattr(buf, "history_backward", lambda *a, **k: calls.append("history"))
    monkeypatch.setattr(buf, "cursor_up", lambda *a, **k: calls.append("cursor"))

    _press(t, Keys.Up)
    assert calls == ["cursor"]

    buf.cursor_position = 0   # now on row 0, WITH the same non-empty draft
    calls.clear()
    _press(t, Keys.Up)
    assert calls == ["cursor"], \
        "non-empty draft on row 0 must still move the cursor, not recall history"


def test_up_arrow_recalls_history_only_when_the_draft_is_empty(t, monkeypatch):
    """The muscle-memory REPL behaviour must survive: an EMPTY prompt still
    recalls history on up-arrow."""
    buf = t.input.buffer
    assert buf.text == ""
    calls = []
    monkeypatch.setattr(buf, "history_backward", lambda *a, **k: calls.append("history"))
    monkeypatch.setattr(buf, "cursor_up", lambda *a, **k: calls.append("cursor"))

    _press(t, Keys.Up)
    assert calls == ["history"]


def test_down_arrow_moves_cursor_not_history_when_draft_is_non_empty(t, monkeypatch):
    buf = t.input.buffer
    buf.text = "line one\nline two"
    buf.cursor_position = 0

    calls = []
    monkeypatch.setattr(buf, "history_forward", lambda *a, **k: calls.append("history"))
    monkeypatch.setattr(buf, "cursor_down", lambda *a, **k: calls.append("cursor"))

    _press(t, Keys.Down)
    assert calls == ["cursor"]


def test_down_arrow_recalls_history_only_when_the_draft_is_empty(t, monkeypatch):
    buf = t.input.buffer
    assert buf.text == ""
    calls = []
    monkeypatch.setattr(buf, "history_forward", lambda *a, **k: calls.append("history"))
    monkeypatch.setattr(buf, "cursor_down", lambda *a, **k: calls.append("cursor"))

    _press(t, Keys.Down)
    assert calls == ["history"]


def test_up_arrow_really_moves_the_cursor_to_the_prior_line(t):
    """Confirms the actual cursor movement (not just which method gets
    called) — no history involved, so no event-loop dependency."""
    buf = t.input.buffer
    buf.text = "line one\nline two"
    buf.cursor_position = len(buf.text)
    assert buf.document.cursor_position_row == 1

    _press(t, Keys.Up)

    assert buf.document.cursor_position_row == 0
    assert buf.text == "line one\nline two"   # draft untouched, only the cursor moved


def test_down_arrow_really_moves_the_cursor_to_the_next_line(t):
    buf = t.input.buffer
    buf.text = "line one\nline two"
    buf.cursor_position = 0
    assert buf.document.cursor_position_row == 0

    _press(t, Keys.Down)

    assert buf.document.cursor_position_row == 1
    assert buf.text == "line one\nline two"


# ── R102: status bar shows what's actually running, not just "thinking…" ──
def test_running_note_set_on_command_tool_start_and_cleared_on_result(t):
    t.fe.on_tool_start("run_command", {"command": "npm test"})
    assert t._running_note == "running: npm test"
    t.fe.on_tool_result("run_command", "ok")
    assert t._running_note == ""


def test_running_note_uses_a_distinct_label_for_wait_until(t):
    t.fe.on_tool_start("wait_until", {"command": "curl -sf localhost:3000"})
    assert t._running_note == "waiting on: curl -sf localhost:3000"
    t.fe.on_tool_result("wait_until", "ok")
    assert t._running_note == ""


def test_running_note_is_untouched_by_non_command_tools(t):
    """A read/grep/edit is near-instant — naming every tool call would be
    status-bar churn, not a useful signal. Only run_command/wait_until get
    a note at all."""
    t._running_note = "should not change"
    t.fe.on_tool_start("read_file", {"path": "x.py"})
    assert t._running_note == "should not change"
    t.fe.on_tool_result("read_file", "contents")
    assert t._running_note == "should not change"


def test_running_note_is_truncated(t):
    long_cmd = "x" * 200
    t.fe.on_tool_start("run_command", {"command": long_cmd})
    assert len(t._running_note) <= len("running: ") + tui._RUNNING_NOTE_MAX
    assert t._running_note.endswith("…")


def test_running_note_collapses_embedded_newlines(t):
    t.fe.on_tool_start("run_command", {"command": "echo one\necho two"})
    assert "\n" not in t._running_note


def test_running_note_cleared_at_begin_turn_and_end_turn(t):
    """Safety net: a secret-challenge 'stop' mid-tool returns from run_turn
    without ever calling on_tool_result for that call — begin_turn/end_turn
    always fire exactly once per turn regardless, so both must clear it."""
    t.fe.on_tool_start("run_command", {"command": "npm test"})
    assert t._running_note
    t.fe.begin_turn()
    assert t._running_note == ""

    t.fe.on_tool_start("run_command", {"command": "npm test"})
    assert t._running_note
    t.fe.end_turn()
    assert t._running_note == ""


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
    _copy_via_menu(t, "selected")
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
    _copy_via_menu(t, "selected")
    assert copied["text"] == "two"


# ── R155: one "copy" button + a picker ──────────────────────────────────────
def _copy_via_menu(t, key):
    """Drive the copy picker the way a user does: click the status bar's
    "copy" button, then pick a row. Goes through `_resolve_menu` (not
    `_resolve_copy_menu` directly) so the UI-thread delivery path — the
    `_menu_on_select` callback rather than the answers queue — is exercised
    too."""
    t._copy_menu_click()(_mouse_up())
    keys = [k for k, _ in t._menu_options]
    assert key in keys, f"{key!r} not offered: {keys}"
    t._resolve_menu(keys.index(key))


def _copy_menu_keys(t):
    t._copy_menu_click()(_mouse_up())
    keys = [k for k, _ in t._menu_options]
    t._resolve_menu(keys.index("cancel"))     # close it again
    return keys


# ── "copy last" — LLM response vs. bash-mode command output (R109) ────────
def _patched_clipboard(monkeypatch):
    copied = {}
    monkeypatch.setattr("aurora.clipboard.copy",
                        lambda s: copied.update(text=s) or "OSC52")
    return copied


def test_copy_last_with_nothing_yet(t, monkeypatch):
    copied = _patched_clipboard(monkeypatch)
    _copy_via_menu(t, "last")
    assert not copied
    assert t._sel_notice[0] == "nothing to copy yet"


def test_copy_last_copies_llm_response_when_no_bash_output(t, monkeypatch):
    copied = _patched_clipboard(monkeypatch)
    t.engine._last_resp = "the answer"
    _copy_via_menu(t, "last")
    assert copied["text"] == "the answer"
    assert "raw response copied" in t._sel_notice[0]


def test_copy_last_copies_bash_output_when_no_llm_response(t, monkeypatch):
    copied = _patched_clipboard(monkeypatch)
    t._last_bash_output = "total 0\ndrwxr-xr-x  script"
    t._last_bash_at = 100.0
    _copy_via_menu(t, "last")
    assert copied["text"] == "total 0\ndrwxr-xr-x  script"
    assert "command output copied" in t._sel_notice[0]


def test_copy_last_prefers_whichever_is_more_recent(t, monkeypatch):
    copied = _patched_clipboard(monkeypatch)
    t.engine._last_resp = "the answer"
    t._last_llm_at = 50.0
    t._last_bash_output = "ls output"
    t._last_bash_at = 100.0            # bash happened AFTER the LLM turn
    _copy_via_menu(t, "last")
    assert copied["text"] == "ls output"

    t._last_bash_at = 10.0             # ...and now BEFORE it
    _copy_via_menu(t, "last")
    assert copied["text"] == "the answer"


def test_new_drag_drops_pending_frozen_selection(t):
    t.append("one two three\n")
    t.sel_begin((0, 0))
    t.sel_drag((0, 3))
    t.sel_finish()
    assert t._sel_frozen is not None
    t.sel_begin((0, 4))               # starting a fresh drag…
    assert t._sel_frozen is None      # …drops the old pending selection


# ── prompt-field selection: double-click + "copy selected" (R135a/b) ──────
def _click_input(t, x=0, y=0, ev=None):
    # set_app: BufferControl.mouse_handler reaches for the *ambient* app, not
    # t.app — without it prompt_toolkit hands back its "No layout specified"
    # dummy and focus lookups fail
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    with set_app(t.app):
        return t.input.control.mouse_handler(MouseEvent(
            position=Point(x=x, y=y),
            event_type=ev or MouseEventType.MOUSE_UP,
            button=MouseButton.LEFT, modifiers=frozenset()))


def _focus_input(t):
    t.app.layout.focus(t.input)


def test_double_click_in_the_prompt_selects_the_whole_draft(t):
    """R135a: prompt_toolkit's default is select-the-word-under-the-cursor;
    a draft is one thought you replace or copy wholesale."""
    _focus_input(t)
    t.input.buffer.text = "explain this bug to me"
    t.input.buffer.cursor_position = 3
    _click_input(t)                    # 1st click — no selection yet
    assert t.input.buffer.selection_state is None
    _click_input(t)                    # 2nd, inside the window → select all
    assert t._input_sel_text() == "explain this bug to me"


def test_two_slow_clicks_in_the_prompt_are_not_a_double_click(t, monkeypatch):
    """The gesture is time-boxed — two unrelated clicks a second apart must
    not silently select (and then let the next keystroke replace) the draft."""
    _focus_input(t)
    t.input.buffer.text = "keep me"
    now = [1000.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: now[0])
    _click_input(t)
    now[0] += tui._DOUBLE_CLICK_S + 0.1
    _click_input(t)
    assert t.input.buffer.selection_state is None


def test_copy_selected_copies_the_prompt_selection(t, monkeypatch):
    """R135b: the button serves the prompt field too, not just the chat."""
    copied = _patched_clipboard(monkeypatch)
    _focus_input(t)
    t.input.buffer.text = "draft text"
    _click_input(t)
    _click_input(t)                   # double-click → whole draft selected
    _copy_via_menu(t, "selected")
    assert copied["text"] == "draft text"
    assert t.input.buffer.selection_state is None   # button clears it
    assert t.input.buffer.text == "draft text"      # …without eating the draft


def test_backspace_after_double_click_deletes_the_whole_selection(t):
    """R138x: backspace is custom-bound (bash-mode-exit-on-empty) and used to
    call delete_before_cursor() unconditionally, ignoring an active selection
    — so after a double-click selected the whole draft (R135a), backspace
    only erased one character instead of the selection."""
    _focus_input(t)
    t.input.buffer.text = "draft text"
    _click_input(t)
    _click_input(t)                   # double-click → whole draft selected
    assert t.input.buffer.selection_state is not None
    # a real event is needed here, not _press's None: the else-branch this
    # test is about reads event.arg (the repeat count)
    _press_with_arg(t, Keys.ControlH)
    assert t.input.buffer.text == ""
    assert t.input.buffer.selection_state is None


def test_copy_selected_row_is_offered_only_when_there_is_a_selection(t):
    """R155: the selection-aware option moved from a fifth status-bar button
    into a menu row, but the predicate behind it is unchanged — a prompt
    selection must surface it, or the copy path is unreachable."""
    _focus_input(t)
    assert "selected" not in _copy_menu_keys(t)
    t.input.buffer.text = "hello"
    _click_input(t)
    _click_input(t)
    assert "selected" in _copy_menu_keys(t)


def test_the_status_bar_shows_one_copy_button_not_four(t):
    """R155: "session id", "copy last", "copy all" and "copy selected" were
    four separate fixed-width buttons on a bar that kept running out of
    room."""
    bar = "".join(f[1] for f in _status_frags(t))
    assert "copy" in bar
    for gone in ("copy last", "copy all", "session id", "copy selected"):
        assert gone not in bar, gone


def test_opening_the_copy_menu_does_not_eat_the_draft(t, monkeypatch):
    """`_open_ui_menu` resets the input buffer, which clears the TEXT and not
    just the selection — so the picker would cost the user a half-typed
    prompt. The button it replaced only ever dropped the selection."""
    _patched_clipboard(monkeypatch)
    _focus_input(t)
    t.input.buffer.text = "half-typed prompt"
    _copy_via_menu(t, "cancel")
    assert t.input.buffer.text == "half-typed prompt"
    t.input.buffer.text = "another draft"
    _copy_via_menu(t, "id")
    assert t.input.buffer.text == "another draft"


def test_input_height_accounts_for_word_wrap_not_just_char_count(t, monkeypatch):
    """R171/B5: a plain ceil-div on char count ignores `wrap_lines=True`'s
    actual WORD wrap, which breaks before the column edge whenever a word
    wouldn't fit whole — undercounting rows on a narrow terminal and
    clipping the cursor below the visible input box (no scrollback there)."""
    class _Size:
        columns = 20
    monkeypatch.setattr(t.app.output, "get_size", lambda: _Size())
    # crafted so word-boundary wrapping needs one more row than a plain
    # char-count ceil-div over the same (cols=20, "> " prompt) predicts
    t.input.buffer.text = "aaa aaaaaaaaaaaaaaa aaaaaaaaaaaaaaaa aaa aaaaaaaaaaaaaaaaa"
    height_fn = t.input.window.height
    dim = height_fn()
    assert dim.preferred == 4    # old ceil-div formula would have said 3


def test_open_ui_menu_preserves_a_half_typed_draft_generically(t):
    """R171/B4: R155/R159 fixed this per-caller (copy menu, compactions
    click) but `_open_ui_menu` itself still did an unconditional
    `buffer.reset()` — every Esc-Esc confirm (cancel/leave-bash/quit) opens
    through it with no draft save of its own. The fix moves the save/restore
    into `_open_ui_menu`/`_resolve_menu` so EVERY caller gets it for free,
    not just the ones that opted in."""
    _focus_input(t)
    t.input.buffer.text = "half-typed prompt"
    fired = []
    t._open_ui_menu("Quit Aurora?", [("yes", "Yes"), ("no", "No")],
                    lambda key: fired.append(key))
    assert t.input.buffer.text == ""    # reset while the menu is open
    t._resolve_menu(1)                  # "no"
    assert fired == ["no"]
    assert t.input.buffer.text == "half-typed prompt"


def test_copy_menu_defaults_to_the_selection_when_there_is_one(t):
    """If you just selected text and clicked copy, that's the intent — Enter
    should do it. Order stays fixed so the digit shortcuts don't shift."""
    _focus_input(t)
    t._copy_menu_click()(_mouse_up())
    assert t._menu_options[t._menu_index][0] == "last"   # no selection
    t._resolve_menu([k for k, _ in t._menu_options].index("cancel"))
    t.input.buffer.text = "hello"
    _click_input(t)
    _click_input(t)
    t._copy_menu_click()(_mouse_up())
    assert t._menu_options[t._menu_index][0] == "selected"
    assert [k for k, _ in t._menu_options][:3] == ["last", "session", "id"]


def test_copy_menu_reports_a_selection_that_vanished(t, monkeypatch):
    """The selection is captured at click time; if it's gone by the time the
    row is picked, say so instead of silently copying nothing."""
    copied = _patched_clipboard(monkeypatch)
    t.append("alpha beta\n")
    t.sel_begin((0, 0))
    t.sel_drag((0, 5))
    t.sel_finish()
    t._copy_menu_click()(_mouse_up())
    t._copy_menu_sel = ""             # e.g. a redraw dropped it
    t._resolve_menu([k for k, _ in t._menu_options].index("selected"))
    assert not copied
    assert t._sel_notice[0] == "selection is gone"


def test_rendering_the_button_does_not_destroy_the_prompt_selection(t):
    """`Buffer.copy_selection()` drops the selection as a side effect, and the
    status bar re-renders every tick — reading the text to decide whether to
    show the button must not be what clears it."""
    _focus_input(t)
    t.input.buffer.text = "hello"
    _click_input(t)
    _click_input(t)
    for _ in range(3):
        _status_frags(t)
    assert t._input_sel_text() == "hello"


def test_only_one_selection_is_live_at_a_time(t):
    """"copy selected" is a single button — it must never be ambiguous about
    which pane it copies, so each new selection drops the other."""
    _focus_input(t)
    t.input.buffer.text = "draft"
    _click_input(t)
    _click_input(t)
    t.append("alpha beta\n")
    t.sel_begin((0, 0))               # a chat drag…
    t.sel_drag((0, 5))
    t.sel_finish()
    assert t._input_sel_text() == ""  # …drops the prompt selection
    _click_input(t)
    _click_input(t)                   # and a prompt double-click…
    assert t._sel_frozen is None      # …drops the frozen chat one


def test_a_plain_drag_in_the_prompt_drops_a_frozen_chat_selection(t):
    """R135f: R135b's "only one selection is ever live" invariant was only
    enforced on the double-click branch — a plain click-drag inside the
    prompt (MOUSE_DOWN then MOUSE_UP, no double-click) skipped it entirely,
    so a frozen chat selection stayed lit up (and copyable) alongside a
    fresh prompt one."""
    from prompt_toolkit.mouse_events import MouseEventType
    t.append("alpha beta\n")
    t.sel_begin((0, 0))
    t.sel_drag((0, 5))
    t.sel_finish()
    assert t._sel_frozen is not None
    _focus_input(t)
    _click_input(t, ev=MouseEventType.MOUSE_DOWN)   # a plain click/drag start
    assert t._sel_frozen is None


def test_third_click_after_a_double_click_does_not_reselect_everything(t, monkeypatch):
    """R135f: firing the double-click used to leave `_input_click_at` set to
    that very click's timestamp, so a third click shortly after (meant to
    place the cursor) paired with it and re-selected the whole draft instead
    of moving the cursor.

    The headless test Application never runs a real render pass, so
    `BufferControl`'s own MOUSE_UP handling is inert here (its position
    translation needs `_last_get_processed_line`, which only gets set by an
    actual render) — a real MOUSE_DOWN's `buffer.exit_selection()` is
    simulated by hand to stand in for what a real terminal would do before
    the third click's MOUSE_UP arrives. What this isolates and proves is the
    actual fix: whether OUR wrapper re-fires the select-all branch a second
    time, which it must not."""
    _focus_input(t)
    t.input.buffer.text = "explain this bug to me"
    now = [1000.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: now[0])
    _click_input(t)                     # 1st click
    now[0] += 0.05
    _click_input(t)                     # 2nd, within window → selects all
    assert t._input_sel_text() == "explain this bug to me"
    assert t._input_click_at == 0.0, "the firing click must be consumed"
    t.input.buffer.exit_selection()     # stand-in for a real MOUSE_DOWN
    now[0] += 0.05                      # well within the double-click window
    _click_input(t, x=3)                # 3rd click — meant to place the cursor
    assert t._input_sel_text() == "", \
        "third click re-selected everything instead of leaving the cursor placed"


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


# ── R96c: _overlay must not rebuild fragments fully outside the selection ──
def test_overlay_returns_untouched_fragments_by_identity():
    """R96c: fragments fully outside the (start, end) range are supposed to
    'pass through untouched' — this asserts that literally (`is`, not `==`),
    which only holds if they're sliced rather than rebuilt one at a time.
    Runs on every frame while a selection is live/frozen; a drag invalidates
    on every mouse-move, so untouched fragments dominate a long transcript."""
    before = ("class:x", "before\n")
    crossed = ("class:y", "crossed text\n")
    after = ("class:z", "after\n")
    frags = [before, crossed, after]
    out = tui._overlay(frags, (1, 2), (1, 5))
    assert out[0] is before
    assert out[-1] is after
    assert out[1] is not crossed        # this one really was re-split


def _overlay_naive(frags, start, end):
    """The pre-R96c implementation: append() every untouched fragment one at
    a time instead of slicing. Kept here only so the regression test can
    measure against the actual old behaviour, not a guessed bound."""
    out = []
    y, x = 0, 0
    for f in frags:
        style, text = f[0], f[1]
        nl = text.count("\n")
        ey, ex = (y + nl, len(text) - text.rfind("\n") - 1) if nl \
            else (y, x + len(text))
        if (ey, ex) <= start or (y, x) >= end:
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


def test_overlay_is_faster_than_the_per_fragment_rebuild():
    """R96c's actual claim: same linear walk, far cheaper per untouched
    fragment (a slice instead of a Python-level append loop). Compares
    directly against the pre-fix implementation rather than an arbitrary
    threshold, on a transcript large enough for the constant-factor
    difference to dominate interpreter noise."""
    import time
    frags = [("", "line of ordinary chat output\n")] * 40_000

    t0 = time.perf_counter()
    for _ in range(10):
        naive = _overlay_naive(frags, (100, 1), (101, 1))
    naive_ms = (time.perf_counter() - t0) / 10 * 1000

    t1 = time.perf_counter()
    for _ in range(10):
        fast = tui._overlay(frags, (100, 1), (101, 1))
    fast_ms = (time.perf_counter() - t1) / 10 * 1000

    assert [(f[0], f[1]) for f in fast] == [(f[0], f[1]) for f in naive]
    assert fast_ms < naive_ms / 3, \
        f"naive={naive_ms:.2f}ms fast={fast_ms:.2f}ms — no longer meaningfully faster"


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


def test_approve_offers_an_explain_choice(monkeypatch):
    """R103: "e" is a plain pass-through answer at this layer — the
    explain-then-reask LOOP lives in agent.py, which has provider access;
    ui.approve() just needs to offer and return the choice."""
    from aurora import ui
    fe = ui.TerminalFrontend()
    answers = iter(["e"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    key, note = fe.approve("run_command", {"command": "ls"}, "")
    assert key == "e" and note == ""


def test_approve_shows_wait_until_command_and_then(monkeypatch, capsys):
    """Bug fix: wait_until used to fall through to the generic branch (which
    only ever shows `path`, empty for wait_until) — the polled command, and
    now its optional `then` follow-up, never appeared at the approval
    prompt at all."""
    from aurora import ui
    fe = ui.TerminalFrontend()
    answers = iter(["y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    fe.approve("wait_until", {"command": "curl -sf localhost:8080/health",
                              "then": "curl -s localhost:8080/version"}, "")
    out = capsys.readouterr().out
    assert "curl -sf localhost:8080/health" in out
    assert "curl -s localhost:8080/version" in out


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


def test_secret_challenge_v_toggles_masking_then_reasks(monkeypatch, capsys):
    """The 'v' choice must re-render the challenge with the token
    masked/shown and ask again — never a terminal answer on its own, same
    shape as the approval gate's 'e'xplain loop."""
    from aurora import secrets as secretscan
    from aurora import ui
    fe = ui.TerminalFrontend()
    text = f"export AWS_ACCESS_KEY_ID={_AWS1}"
    matches = secretscan.scan(text)
    answers = iter(["v", "keep"])
    monkeypatch.setattr(ui, "select", lambda *_a, **_k: next(answers))
    result = fe.secret_challenge("tool:read_file", matches, source_text=text)
    assert result == "keep"
    out = capsys.readouterr().out
    assert out.count("possible secret detected") == 2   # rendered twice: before/after toggle
    assert _AWS1 in out              # first (unmasked) render shows it
    assert "AWS access key hidden" in out   # second (masked) render doesn't


_AWS1 = "AKIA" + "IOSFODNN7EXAMPLE"


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
    def fake_select(prompt, options, **kw):
        # **kw: R214 added `eof_key`, which `confirm` now passes. This test is
        # about OPTION ORDER, so it tolerates the widened signature rather
        # than re-pinning it — but the assertion below keeps the EOF answer
        # honest, since "no" is the whole point of that parameter here.
        seen["options"], seen["kw"] = options, kw
        return options[0][0]                 # pick the default (first, Enter)
    monkeypatch.setattr(ui, "select", fake_select)
    assert ui.confirm("Run it?") is True                       # [Y/n]: Yes first
    assert seen["options"][0] == ("y", "Yes")
    assert ui.confirm("Evict?", default_yes=False) is False     # [y/N]: No first
    assert seen["options"][0] == ("n", "No")
    # R214: whatever the default, an unattended confirm must answer no
    assert seen["kw"].get("eof_key") == "n"


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


# ── R218: select_menu must accept everything ui.select accepts ─────────────
def test_select_menu_accepts_the_eof_key_ui_select_takes(t):
    """`run()` swaps `ui.select` for `select_menu`, so a parameter added to
    one and not the other is a TypeError at the approval gate — which is what
    R214's `eof_key` was until R218. Fails with `TypeError: select_menu() got
    an unexpected keyword argument 'eof_key'` without the fix."""
    got = {}

    def worker():
        got["key"] = t.select_menu("Approve?", _OPTS, eof_key="n")

    th = threading.Thread(target=worker)
    th.start()
    while t._menu_options is None:
        pass
    t._resolve_menu(0)
    th.join(timeout=2)
    assert got["key"] == "y"       # a real pick still wins over eof_key


def test_a_dismissed_menu_answers_eof_key_when_one_was_named(t):
    """Dismissal is the TUI's "the answer never came", so an approval that
    named a safe answer gets it instead of None — a None would fall through
    the gate's key comparisons as neither approve nor deny."""
    got = {}

    def worker():
        got["key"] = t.select_menu("Approve?", _OPTS, eof_key="n")

    th = threading.Thread(target=worker)
    th.start()
    while t._menu_options is None:
        pass
    t._answers.put(None)           # as the dismiss-click path does
    th.join(timeout=2)
    assert got["key"] == "n"


def test_a_dismissed_menu_without_an_eof_key_still_returns_none(t):
    """`/model` and the other pickers rely on None meaning "no change"."""
    got = {}

    def worker():
        got["key"] = t.select_menu("Select model", _OPTS)

    th = threading.Thread(target=worker)
    th.start()
    while t._menu_options is None:
        pass
    t._answers.put(None)
    th.join(timeout=2)
    assert got["key"] is None


# ── R188: menu rows are clickable, like the status bar's buttons ───────────
def test_menu_rows_carry_a_mouse_handler(t):
    t._menu_prompt, t._menu_options = "Approve?", _OPTS
    t._menu_index = 0
    try:
        frags = t._menu_fragments()
        # the prompt line and the hint stay plain 2-tuples; every option row
        # fragment carries a third element (the handler), same shape the
        # status bar's clickable fragments use
        rows = [f for f in frags if len(f) == 3]
        assert rows, "no clickable fragments in the menu"
        assert all(callable(f[2]) for f in rows)
        assert "click" in frags[-1][1]      # the hint advertises it
    finally:
        t._menu_prompt = t._menu_options = None


def test_clicking_a_menu_row_picks_that_row(t):
    """A click commits to the row, as Enter does — there is no hover-only
    state in a terminal. Goes through `_resolve_menu`, so it works for a
    blocking `select_menu()` too (see the roundtrip test)."""
    got = {}

    def worker():
        got["key"] = t.select_menu("Approve?", _OPTS)

    th = threading.Thread(target=worker)
    th.start()
    while t._menu_options is None:
        pass
    t._menu_row_click(1)(_mouse_up())      # click the SECOND row
    th.join(timeout=2)
    assert got["key"] == "n"
    assert t._menu_options is None


def test_a_menu_row_ignores_everything_but_mouse_up(t):
    """Mirrors every other click handler here: a MOUSE_DOWN or a scroll must
    not resolve the menu, or a drag over the pane would answer it."""
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import (MouseButton, MouseEvent,
                                             MouseEventType)
    t._menu_prompt, t._menu_options = "Approve?", _OPTS
    t._menu_index = 0
    try:
        for ev in (MouseEventType.MOUSE_DOWN, MouseEventType.SCROLL_UP):
            t._menu_row_click(1)(MouseEvent(
                position=Point(x=0, y=0), event_type=ev,
                button=MouseButton.LEFT, modifiers=frozenset()))
            assert t._menu_options is not None, f"{ev} resolved the menu"
    finally:
        t._menu_prompt = t._menu_options = None


def test_a_worker_menu_opened_over_an_esc_confirm_still_gets_its_answer(t):
    """R142a: the Esc-Esc confirms (_open_ui_menu, UI thread) and the blocking
    challenges (select_menu, worker thread) share ONE menu slot but resolve
    differently, and _resolve_menu checks _menu_on_select FIRST. select_menu
    overwrote _menu_prompt/_menu_options without clearing that callback, so
    the answer to ITS menu was delivered to the Esc confirm's resolver and
    never reached _answers — the worker blocked forever, mid-turn, with no
    way out but quitting."""
    got = {}
    fired = []
    # a turn is running and the user taps Esc-Esc → "Cancel this?" opens
    t._open_ui_menu("Cancel this?", [("cancel", "Yes"), ("no", "No")],
                    lambda key: fired.append(key))
    assert t._menu_on_select is not None

    def worker():                      # …then the agent hits an approval gate
        got["key"] = t.select_menu("Approve?", _OPTS)

    # daemon: on the PRE-fix code this thread never wakes, and a non-daemon
    # one would hang the whole suite at exit instead of failing the assert
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    while t._menu_prompt != "Approve?":
        pass
    t._resolve_menu(1)                 # the user answers the APPROVAL menu
    th.join(timeout=2)
    assert not th.is_alive(), "worker never woke — select_menu deadlocked"
    assert got["key"] == "n"           # the answer reached its real caller
    assert fired == []                 # and was NOT fed to the Esc resolver


def test_opening_the_editor_closes_the_help_overlay(t, tmp_path):
    """R150b: help and the editor both occupy the chat area but their
    visibility filters are independent, so `?` then `/nano <file>` drew BOTH,
    splitting the screen — and help was then undismissable, since `?` and its
    click target need `self._editor is None` while Escape is
    `filter=_no_editor`."""
    f = tmp_path / "notes.md"
    f.write_text("hello\n")
    t._help_visible = True
    t.open_nano(f, check_busy=False)
    assert t._editor is not None
    assert not t._help_visible


def test_an_out_of_range_digit_does_not_leak_into_the_hidden_buffer(t):
    """R150c: a digit past the menu's option count fell through to
    insert_text, into the buffer the menu is drawn over — invisible, because
    the input line collapses to one hidden row while a menu is open. The
    Keys.Any fallback exists to swallow exactly this, but a digit binding is
    more specific and wins."""
    t._open_ui_menu("Cancel this?", [("cancel", "Yes"), ("no", "No")],
                    lambda key: None)
    for b in t.app.key_bindings.bindings:
        if b.keys == ("5",):
            b.handler(None)
            break
    else:
        raise AssertionError("no binding for digit 5")
    assert t.input.buffer.text == ""
    assert t._menu_options is not None      # menu untouched, still awaiting


def test_esc_closes_a_completion_popup_before_arming_leave_bash(t):
    """R150d: the bash-mode branch sat ahead of the completion branch,
    contradicting _on_escape's own documented priority. In bash mode with
    `cd Doc<Tab>`'s popup open, Esc armed the leave-bash gesture instead of
    dismissing the popup."""
    from prompt_toolkit.buffer import CompletionState
    from prompt_toolkit.completion import Completion

    t._bash_mode = True
    _type(t, "cd Doc")
    buf = t.input.buffer
    # a real CompletionState — cancel_completion() drives it, so a stub
    # without go_to_index() would only prove the stub is wrong
    buf.complete_state = CompletionState(
        original_document=buf.document,
        completions=[Completion("Documents", start_position=-3)])
    assert buf.complete_state
    t._on_escape(lambda: None)
    assert buf.complete_state is None       # popup dismissed…
    assert t._esc_armed != "bash"           # …and the gesture NOT armed
    assert t._bash_mode                     # still in bash mode


def test_esc_in_bash_mode_clears_a_typed_command_before_arming(t):
    """The `elif buf.text` clear-the-draft branch was unreachable in bash
    mode, so Esc on an unwanted shell command never cleared it."""
    t._bash_mode = True
    _type(t, "rm -rf something")
    t._on_escape(lambda: None)
    assert t.input.buffer.text == ""
    assert t._esc_armed != "bash"
    assert t._bash_mode


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

def test_update_menu_labels_repaints_open_menu_without_moving_the_cursor(t):
    # /model's background price refresh (feature request, 2026-08-03) lands
    # after the menu is already on screen; the fresh price has to appear
    # without the user reopening the picker, and without the highlighted row
    # sliding out from under them mid-keystroke.
    t._menu_prompt, t._menu_options, t._menu_index = "Select model", [
        ("1", "vendor/a  [$] $1/$2 per M"), ("2", "local  [free]")], 1
    assert t.update_menu_labels("Select model", [
        ("1", "vendor/a  [$] $9/$18 per M"), ("2", "local  [free]")]) is True
    text = "".join(f[1] for f in t._menu_fragments())
    assert "$9/$18 per M" in text and "$1/$2" not in text
    assert t._menu_index == 1               # cursor stayed put
    t._resolve_menu(1)
    assert t._answers.get() == "2"          # index → key mapping unchanged


def test_update_menu_labels_refuses_to_relabel_a_different_menu(t):
    # the callback arrives from a network thread: by then the user may have
    # picked a model and be sitting in an approval menu, and overwriting THAT
    # menu's rows with model names would mislabel what Enter approves.
    t._menu_prompt, t._menu_options, t._menu_index = "Approve?", _OPTS, 0
    assert t.update_menu_labels(
        "Select model", [("1", "vendor/a")]) is False
    assert t._menu_options == _OPTS
    # right prompt, but the option keys moved on (config changed under it)
    t._menu_prompt = "Select model"
    assert t.update_menu_labels(
        "Select model", [("1", "vendor/a"), ("2", "vendor/b")]) is False
    assert t._menu_options == _OPTS
    # and no menu open at all is not something to paint into
    t._menu_prompt = t._menu_options = None
    assert t.update_menu_labels("Select model", [("1", "vendor/a")]) is False


def test_model_picker_esc_cancels(t):
    # Only the model picker allows a bare Esc to back out with no change —
    # same "no change" outcome as a second click on the status bar's model
    # name (see _open_model_picker).
    t._menu_prompt, t._menu_options, t._menu_index = "Select model", _OPTS, 0
    t._on_escape(lambda: None)
    assert t._answers.get() is None


def _status_line2(t):
    from prompt_toolkit.layout.controls import FormattedTextControl
    ctrl = next(c for c in t.app.layout.find_all_controls()
                if isinstance(c, FormattedTextControl)
                # R155: anchored on the ctx gauge, not the (removed) session-id
                # button — a label that only one control ever renders
                and any("ctx " in f[1] for f in
                        (c.text() if callable(c.text) else c.text)))
    text = "".join(f[1] for f in ctrl.text())
    # R137: a blank row sits between line 1 and line 2 now (status window
    # grew 2 rows -> 3), so the tooltip/hint content is index 2, not 1.
    return text.split("\n")[2].strip()


def _status_line1(t):
    from prompt_toolkit.layout.controls import FormattedTextControl
    ctrl = next(c for c in t.app.layout.find_all_controls()
                if isinstance(c, FormattedTextControl)
                # R155: anchored on the ctx gauge, not the (removed) session-id
                # button — a label that only one control ever renders
                and any("ctx " in f[1] for f in
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


def test_status_hint_select_one_for_generic_menu(t):
    t._menu_prompt, t._menu_options, t._menu_index = "Approve?", _OPTS, 0
    assert _status_line2(t) == "select one"


def test_status_hint_mentions_esc_for_model_picker(t):
    t._menu_prompt, t._menu_options, t._menu_index = "Select model", _OPTS, 0
    assert _status_line2(t) == "select one, or ESC to cancel"


def test_copy_all_click_queues_the_command_instead_of_working_inline(t, monkeypatch):
    """R129: the "copy all" button's work is UNBOUNDED — it parses the whole
    session JSONL (which only grows, R20) and then spawns a clipboard
    subprocess with a 5s timeout, all from a mouse handler on the UI
    event-loop thread. It now queues `/copy-all` for the worker instead,
    same as every other status-bar link that blocks or does real work (R89).

    Asserted by proving neither expensive call happens on the calling
    thread: a failure here is a frozen UI, which no output assertion would
    catch."""
    from aurora import clipboard
    from aurora import session as sessions

    called = []
    monkeypatch.setattr(sessions, "export_markdown",
                        lambda *a, **k: called.append("export") or "")
    monkeypatch.setattr(clipboard, "copy",
                        lambda *a, **k: called.append("clipboard") or "x")

    _copy_via_menu(t, "session")

    assert t._inbox.get_nowait() == "/copy-all"
    assert called == [], f"ran on the UI thread: {called}"
    # echoed into the chat, same feedback shape as the agentic-report click
    assert any("/copy-all" in e for e in t._chat if isinstance(e, str))


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


def _mode_label_frag(t):
    from prompt_toolkit.layout.controls import FormattedTextControl
    ctrl = next(c for c in t.app.layout.find_all_controls()
                if isinstance(c, FormattedTextControl)
                # R155: anchored on the ctx gauge, not the (removed) session-id
                # button — a label that only one control ever renders
                and any("ctx " in f[1] for f in
                        (c.text() if callable(c.text) else c.text)))
    return next(f for f in ctrl.text() if f[1] in ("prompt mode", "bash mode"))


def test_click_mode_label_enters_bash_mode(t):
    frag = _mode_label_frag(t)
    assert frag[1] == "prompt mode"
    frag[2](_mouse_up())
    assert t._bash_mode is True


def test_click_mode_label_leaves_bash_mode(t):
    t._bash_mode = True
    t.input.buffer.document = t.input.buffer.document.__class__("some typed command")
    frag = _mode_label_frag(t)
    assert frag[1] == "bash mode"
    frag[2](_mouse_up())
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
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    ctrl = tui._SafeCompletionsMenuControl()

    class _Buf: complete_state = None
    class _App: current_buffer = _Buf()
    monkeypatch.setattr(tui, "get_app", lambda: _App())

    ev = MouseEvent(position=Point(x=0, y=0), event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT, modifiers=frozenset())
    assert ctrl.mouse_handler(ev) is None      # no AssertionError


def test_bash_mode_cd_persists_across_commands(t, tmp_path, monkeypatch):
    # regression: subprocess.run's own `cd` only affects that throwaway
    # child process, so a second `!` command used to land back in the
    # original directory — `cd` must be intercepted and tracked ourselves.
    from aurora import ui
    (tmp_path / "script").mkdir()
    monkeypatch.setattr(t, "_banner", lambda: None)
    monkeypatch.setattr("aurora.bootstrap.load", lambda cwd: ("", None))
    monkeypatch.setattr(t.app, "exit", lambda: None)
    monkeypatch.setattr(ui, "_send_turn", lambda *a, **k: None)

    t._bash_cwd = str(tmp_path)
    t._inbox.put("!cd script")
    t._inbox.put("!pwd")
    t._inbox.put("/exit")
    th = threading.Thread(target=t._worker, daemon=True)
    th.start()
    th.join(timeout=2)
    assert not th.is_alive()
    assert t._bash_cwd == str(tmp_path / "script")


def test_bash_mode_cd_to_missing_dir_reports_error(t, monkeypatch):
    from aurora import ui
    monkeypatch.setattr(t, "_banner", lambda: None)
    monkeypatch.setattr("aurora.bootstrap.load", lambda cwd: ("", None))
    monkeypatch.setattr(t.app, "exit", lambda: None)
    monkeypatch.setattr(ui, "_send_turn", lambda *a, **k: None)

    before = t._bash_cwd
    t._inbox.put("!cd does-not-exist")
    t._inbox.put("/exit")
    th = threading.Thread(target=t._worker, daemon=True)
    th.start()
    th.join(timeout=2)
    assert not th.is_alive()
    assert t._bash_cwd == before


def test_bash_mode_command_is_timeout_and_process_group_safe(t, monkeypatch):
    # R125c regression: bash mode used a bare subprocess.run(shell=True)
    # with no timeout and no process-group ownership, unlike run_command/
    # wait_until — a hung `!` command wedged _worker (the TUI's sole inbox
    # consumer) forever. It must now go through tools._run_command_once,
    # the same hardened path.
    from aurora import tools as _tools
    from aurora import ui
    calls = []
    monkeypatch.setattr(_tools, "_run_command_once",
                        lambda command, workdir, timeout=None:
                        (calls.append((command, workdir)) or ("hi", 0)))
    monkeypatch.setattr(t, "_banner", lambda: None)
    monkeypatch.setattr("aurora.bootstrap.load", lambda cwd: ("", None))
    monkeypatch.setattr(t.app, "exit", lambda: None)
    monkeypatch.setattr(ui, "_send_turn", lambda *a, **k: None)

    t._inbox.put("!echo hi")
    t._inbox.put("/exit")
    th = threading.Thread(target=t._worker, daemon=True)
    th.start()
    th.join(timeout=2)
    assert not th.is_alive()
    assert calls == [("echo hi", t._bash_cwd)]


def test_bash_mode_reports_a_timed_out_command(t, monkeypatch):
    from aurora import tools as _tools
    from aurora import ui
    monkeypatch.setattr(_tools, "_run_command_once",
                        lambda command, workdir, timeout=None: ("partial", None))
    monkeypatch.setattr(t, "_banner", lambda: None)
    monkeypatch.setattr("aurora.bootstrap.load", lambda cwd: ("", None))
    monkeypatch.setattr(t.app, "exit", lambda: None)
    monkeypatch.setattr(ui, "_send_turn", lambda *a, **k: None)

    t._inbox.put("!sleep 999")
    t._inbox.put("/exit")
    th = threading.Thread(target=t._worker, daemon=True)
    th.start()
    th.join(timeout=2)
    assert not th.is_alive()
    assert "timeout after" in t._last_bash_output


def test_bash_mode_clear_wipes_scrollback(t, monkeypatch):
    # regression (R116): `clear`/`cls` in bash mode used to fall through to
    # subprocess.run, whose captured stdout (an ANSI escape blob, since
    # there's no real tty) got dumped into the transcript instead of
    # actually clearing anything.
    from aurora import ui
    monkeypatch.setattr(t, "_banner", lambda: None)
    monkeypatch.setattr("aurora.bootstrap.load", lambda cwd: ("", None))
    monkeypatch.setattr(t.app, "exit", lambda: None)
    monkeypatch.setattr(ui, "_send_turn", lambda *a, **k: None)

    t.append("some prior chat output\n")
    t._fragments()                      # force-populate _text_cache, like a real render
    assert t._chat
    t._inbox.put("!clear")
    t._inbox.put("/exit")
    th = threading.Thread(target=t._worker, daemon=True)
    th.start()
    th.join(timeout=2)
    assert not th.is_alive()
    assert t._chat == []
    assert t._cache == []
    # regression: clear_screen() used to reset _chat/_cache but leave
    # _text_cache (what _fragments() actually returns to the renderer)
    # untouched, so the pre-clear screen kept rendering forever.
    assert t._fragments() == []


def test_bash_cd_target_parsing():
    from aurora.tui import Tui
    assert Tui._bash_cd_target("cd foo") == "foo"
    assert Tui._bash_cd_target("cd") == "~"
    assert Tui._bash_cd_target("ls -la") is None
    assert Tui._bash_cd_target("cd foo && ls") is None
    assert Tui._bash_cd_target("cdfoo") is None       # not a "cd" word


def test_bash_cd_target_handles_unquoted_spaces(t):
    # bug fix: cd only ever takes one path argument, so an unquoted
    # multi-word remainder is the target (a real folder name with a
    # space), not "too many arguments" — this is exactly what
    # PathCompleter's Tab-completion inserts (it does not escape spaces),
    # so this was reachable just by typing `cd Del<Tab>` in bash mode
    from aurora.tui import Tui
    assert Tui._bash_cd_target("cd Delete Latter") == "Delete Latter"
    assert Tui._bash_cd_target("cd foo bar") == "foo bar"


def test_bash_cd_target_strips_matching_quotes():
    from aurora.tui import Tui
    assert Tui._bash_cd_target('cd "Delete Latter"') == "Delete Latter"
    assert Tui._bash_cd_target("cd 'Delete Latter'") == "Delete Latter"


def test_bash_mode_cd_into_folder_with_space(t, tmp_path, monkeypatch):
    from aurora import ui
    (tmp_path / "Delete Latter").mkdir()
    monkeypatch.setattr(t, "_banner", lambda: None)
    monkeypatch.setattr("aurora.bootstrap.load", lambda cwd: ("", None))
    monkeypatch.setattr(t.app, "exit", lambda: None)
    monkeypatch.setattr(ui, "_send_turn", lambda *a, **k: None)

    t._bash_cwd = str(tmp_path)
    t._inbox.put("!cd Delete Latter")
    t._inbox.put("/exit")
    th = threading.Thread(target=t._worker, daemon=True)
    th.start()
    th.join(timeout=2)
    assert not th.is_alive()
    assert t._bash_cwd == str(tmp_path / "Delete Latter")


# ── /nano — built-in editor (R110) ─────────────────────────────────────────
def test_nano_opens_valid_file(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    assert t._editor is not None
    assert t._editor["path"] == p
    assert t._editor_area.buffer.text == "hello"
    assert not t._nano_dirty()


def test_nano_opens_shell_script(t, tmp_path):
    p = tmp_path / "deploy.sh"
    p.write_text("#!/usr/bin/env bash\necho hi\n")
    t.open_nano(p)
    assert t._editor is not None
    assert t._editor["path"] == p


def test_nano_status_button_order(t, tmp_path):
    # close/save come before page up/page down (per explicit ordering request)
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    labels = [f[1] for f in t._nano_status_fragments() if f[1].strip() not in ("", "·")]
    assert labels.index("close") < labels.index("page up") < labels.index("page down")

    t._editor_area.buffer.text = "hello world"
    labels = [f[1] for f in t._nano_status_fragments() if f[1].strip() not in ("", "·")]
    assert (labels.index("save") < labels.index("save and close")
            < labels.index("close") < labels.index("page up") < labels.index("page down"))


def test_nano_status_actions_on_line1_underlined_filename_on_line2_plain(t, tmp_path):
    p = tmp_path / "sub" / "notes.txt"
    p.parent.mkdir()
    p.write_text("hello")
    t.open_nano(p)
    frags = t._nano_status_fragments()
    # R137: the separator is "\n\n" (a blank row) now that the status window
    # is 3 rows, not a bare "\n"
    nl = next(i for i, f in enumerate(frags) if f[1] == "\n\n")
    line1, line2 = frags[:nl], frags[nl + 1:]

    line1_labels = {f[1] for f in line1}
    assert {"close", "page up", "page down"} <= line1_labels
    for f in line1:
        if f[1].strip() and f[1].strip() != "·":
            assert f[0] == "class:status.id", f  # underlined, tappable

    line2_text = "".join(f[1] for f in line2)
    assert "notes.txt" in line2_text
    assert str(p) not in line2_text          # bare filename only, no path
    assert all(f[0] != "class:status.id" for f in line2)  # not underlined


def test_nano_refuses_bad_extension(t, tmp_path, capsys):
    p = tmp_path / "script.py"
    p.write_text("print(1)")
    t.open_nano(p)
    assert t._editor is None
    assert "unsupported file type" in capsys.readouterr().out


def test_nano_refuses_missing_file(t, tmp_path, capsys):
    t.open_nano(tmp_path / "ghost.txt")
    assert t._editor is None
    assert "no such file" in capsys.readouterr().out


def test_nano_refuses_oversized_file(t, tmp_path, capsys):
    p = tmp_path / "big.txt"
    p.write_bytes(b"x" * (tui._NANO_MAX_BYTES + 1))
    t.open_nano(p)
    assert t._editor is None
    assert "too large" in capsys.readouterr().out


def test_nano_edit_marks_dirty(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._editor_area.buffer.text = "hello world"
    assert t._nano_dirty()


def test_nano_save_writes_and_clears_dirty(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._editor_area.buffer.text = "hello world"
    t._nano_save_click()(_mouse_up())
    assert p.read_text() == "hello world"
    assert not t._nano_dirty()
    assert t._editor is not None          # save keeps the editor open


def test_nano_close_while_clean_closes_on_the_first_click(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._nano_close_click()(_mouse_up())
    assert t._editor is None


def test_nano_close_while_dirty_requires_a_second_click_to_discard(t, tmp_path):
    """R125c: closing over unsaved edits must not be a single silent click
    — it's the only way out of the editor and had no confirm at all before.
    First click arms it (editor stays open, nothing written); second click
    actually discards and closes."""
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._editor_area.buffer.text = "hello world"

    t._nano_close_click()(_mouse_up())     # first click: arms, doesn't close
    assert t._editor is not None
    assert t._nano_close_confirm
    assert p.read_text() == "hello"

    t._nano_close_click()(_mouse_up())     # second click: actually closes
    assert t._editor is None
    assert p.read_text() == "hello"        # discarded, not written


def test_nano_editing_after_arming_close_disarms_it(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._editor_area.buffer.text = "hello world"
    t._nano_close_click()(_mouse_up())     # arm
    assert t._nano_close_confirm
    t._editor_area.buffer.text = "hello world!"   # further edit disarms it
    assert not t._nano_close_confirm


def test_nano_saving_after_arming_close_disarms_it(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._editor_area.buffer.text = "hello world"
    t._nano_close_click()(_mouse_up())     # arm
    assert t._nano_close_confirm
    t._nano_save_click()(_mouse_up())
    assert not t._nano_close_confirm


def test_nano_save_and_close(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._editor_area.buffer.text = "hello world"
    t._nano_save_close_click()(_mouse_up())
    assert t._editor is None
    assert p.read_text() == "hello world"


def test_nano_status_shows_only_close_when_clean(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    frags = t._nano_status_fragments()
    labels = [f[1] for f in frags]
    assert "close" in labels
    assert "save" not in labels
    assert "save and close" not in labels


def test_nano_status_shows_save_buttons_when_dirty(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._editor_area.buffer.text = "hello world"
    frags = t._nano_status_fragments()
    labels = [f[1] for f in frags]
    assert "save" in labels
    assert "save and close" in labels


def test_nano_status_always_shows_page_up_down(t, tmp_path):
    # scrolling isn't a save/close action — must be present clean OR dirty
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    assert {"page up", "page down"} <= {f[1] for f in t._nano_status_fragments()}
    t._editor_area.buffer.text = "hello world"
    assert {"page up", "page down"} <= {f[1] for f in t._nano_status_fragments()}


def test_nano_status_shows_cursor_line_and_dirty_mark(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("a\nb\nc\n")
    t.open_nano(p)
    doc = t._editor_area.buffer.document
    t._editor_area.buffer.cursor_position = doc.translate_row_col_to_index(2, 0)
    line1 = "".join(f[1] for f in t._nano_status_fragments()
                    if f[0] in ("class:status", "class:status.id"))
    assert "line 3/4" in line1
    assert "[modified]" not in line1

    t._editor_area.buffer.text = "a\nCHANGED\nc\n"
    line1 = "".join(f[1] for f in t._nano_status_fragments()
                    if f[0] in ("class:status", "class:status.id"))
    assert "[modified]" in line1


def test_nano_editor_has_line_numbers(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    from prompt_toolkit.layout.margins import NumberedMargin
    assert any(isinstance(m, NumberedMargin)
               for m in t._editor_area.window.left_margins)


def test_nano_page_click_handlers_call_scroll_page_functions(t, tmp_path, monkeypatch):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    calls = []
    from prompt_toolkit.key_binding.bindings import scroll as pt_scroll
    monkeypatch.setattr(pt_scroll, "scroll_page_up", lambda event: calls.append("up"))
    monkeypatch.setattr(pt_scroll, "scroll_page_down", lambda event: calls.append("down"))
    t._nano_page_down_click()(_mouse_up())
    t._nano_page_up_click()(_mouse_up())
    assert calls == ["down", "up"]


def test_nano_page_click_noop_when_editor_not_open(t, tmp_path, monkeypatch):
    from prompt_toolkit.key_binding.bindings import scroll as pt_scroll
    calls = []
    monkeypatch.setattr(pt_scroll, "scroll_page_down", lambda event: calls.append("down"))
    t._nano_page_down_click()(_mouse_up())     # no editor open — must not call through
    assert calls == []


def test_nano_page_down_then_up_scrolls_editor_window(tmp_path):
    # end-to-end against a real running Application: prompt_toolkit's
    # scroll_page_up/down need an actual render pass (render_info) to know
    # the window's visible line range, so this can't be verified with the
    # headless `t` fixture alone.
    import asyncio

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    p = tmp_path / "notes.txt"
    p.write_text("\n".join(f"line {i}" for i in range(200)))

    with create_pipe_input() as inp:
        tui_ = tui.Tui(_FakeEngine())
        tui_.app.input = inp
        tui_.app.output = DummyOutput()
        tui_.open_nano(p)

        async def main():
            fut = asyncio.ensure_future(tui_.app.run_async())
            await asyncio.sleep(0.1)
            assert tui_._editor_area.window.vertical_scroll == 0
            tui_._nano_page_down_click()(_mouse_up())
            await asyncio.sleep(0.05)
            down_scroll = tui_._editor_area.window.vertical_scroll
            tui_._nano_page_up_click()(_mouse_up())
            await asyncio.sleep(0.05)
            up_scroll = tui_._editor_area.window.vertical_scroll
            tui_.app.exit()
            await fut
            return down_scroll, up_scroll

        down_scroll, up_scroll = asyncio.run(main())
        assert down_scroll > 0            # scrolled forward
        assert up_scroll < down_scroll    # and back up again


def test_nano_rapid_page_up_clicks_reach_the_top(tmp_path):
    # regression: scroll_page_up/down compute against Window.render_info,
    # which only updates on an actual render pass — app.invalidate() alone
    # just SCHEDULES one, so clicking faster than a redraw can keep up (the
    # obvious way to reach the top of a long file quickly) made every click
    # after the first compute against the SAME stale render_info: each one
    # only nudged the cursor up by a single line instead of a full page,
    # so reaching row 0 took dozens of clicks instead of a handful.
    # _nano_scroll must force a synchronous redraw so rapid clicks each see
    # fresh render_info.
    import asyncio

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    p = tmp_path / "notes.txt"
    p.write_text("\n".join(f"line {i}" for i in range(200)))

    with create_pipe_input() as inp:
        tui_ = tui.Tui(_FakeEngine())
        tui_.app.input = inp
        tui_.app.output = DummyOutput()
        tui_.open_nano(p)

        async def main():
            fut = asyncio.ensure_future(tui_.app.run_async())
            await asyncio.sleep(0.1)
            buf = tui_._editor_area.buffer
            buf.cursor_position = len(buf.text)
            await asyncio.sleep(0.05)
            # RAPID clicks, deliberately no await/yield between them — no
            # chance for a scheduled invalidate() to actually redraw
            for _ in range(10):
                tui_._nano_page_up_click()(_mouse_up())
            row = buf.document.cursor_position_row
            tui_.app.exit()
            await fut
            return row

        row = asyncio.run(main())
        assert row == 0     # 10 page-ups over 200 lines must reach the top


def test_nano_wheel_scroll_up_reaches_the_true_top_on_wrapped_lines(tmp_path):
    # regression: reported as "I can scroll full down, but only to about
    # half [way] when going back up" on a file with long lines that wrap
    # (ARCHITECTURE.md's own 260-char lines at 80 columns). Window's
    # DEFAULT wheel-scroll (_scroll_up/_scroll_down) decides whether to
    # move the cursor along with the view via a screen-position heuristic
    # that prompt_toolkit's own source admits is incomplete for wrapped
    # lines; when it fails to track the cursor, every subsequent render's
    # "keep cursor visible" pass drags the view back down mid-scroll,
    # capping how far up you can actually get. _nano_wheel_scroll (which
    # replaces the Window's default _scroll_up/_scroll_down at construction
    # time) must not have this problem: it derives the target line from
    # the same wrap-aware first_visible_line()/last_visible_line()
    # translation the (already-correct) page up/down buttons use.
    import asyncio

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    p = tmp_path / "notes.md"
    # long lines that force wrapping at DummyOutput's 80-column width
    p.write_text("\n".join(f"paragraph {i} " + "x" * 200 for i in range(60)))

    with create_pipe_input() as inp:
        tui_ = tui.Tui(_FakeEngine())
        tui_.app.input = inp
        tui_.app.output = DummyOutput()
        tui_.open_nano(p)

        async def main():
            fut = asyncio.ensure_future(tui_.app.run_async())
            await asyncio.sleep(0.1)
            w = tui_._editor_area.window
            buf = tui_._editor_area.buffer
            for _ in range(400):
                w._scroll_down()                # via the installed override
            down_scroll = w.vertical_scroll
            for _ in range(400):
                w._scroll_up()
            up_scroll, up_row = w.vertical_scroll, buf.document.cursor_position_row
            tui_.app.exit()
            await fut
            return down_scroll, up_scroll, up_row

        down_scroll, up_scroll, up_row = asyncio.run(main())
        assert down_scroll > 0
        assert up_scroll == 0      # must reach the TRUE top, not partway
        assert up_row == 0


def test_strip_dangerous_escapes_removes_osc52_clipboard_hijack():
    """R170l: OSC 52 (clipboard write) is BEL-terminated; must be removed
    entirely, surrounding text kept."""
    payload = "before\x1b]52;c;bG9va2F0dGhpcw==\x07after"
    assert tui._strip_dangerous_escapes(payload) == "beforeafter"


def test_strip_dangerous_escapes_removes_st_terminated_osc():
    """OSC can also be terminated by ST (ESC \\) instead of BEL."""
    payload = "before\x1b]0;window title\x1b\\after"
    assert tui._strip_dangerous_escapes(payload) == "beforeafter"


def test_strip_dangerous_escapes_removes_dcs():
    payload = "before\x1bPsome dcs payload\x1b\\after"
    assert tui._strip_dangerous_escapes(payload) == "beforeafter"


def test_strip_dangerous_escapes_leaves_plain_csi_color_codes_alone():
    """CSI (\\x1b[...m) is how ANSI colors work — must survive untouched,
    only OSC/DCS/APC/PM/SOS are stripped."""
    payload = "\x1b[31mred text\x1b[0m"
    assert tui._strip_dangerous_escapes(payload) == payload


def test_append_bash_output_strips_dangerous_escapes(t):
    t.append_bash_output("before\x1b]52;c;bG9va2F0dGhpcw==\x07after\n")
    stored = next(e for e in t._chat if isinstance(e, dict)
                  and e.get("kind") == "bash_output")
    assert "\x1b]52" not in stored["text"]
    assert "before" in stored["text"] and "after" in stored["text"]


# ── R171/S1: unterminated OSC/DCS must also be stripped ────────────────────
def test_strip_dangerous_escapes_removes_unterminated_osc52():
    """A crashed binary, a truncated capture, or a deliberately malformed
    payload can emit an OSC 52 with NO terminator at all — the BEL/ST
    alternatives both require one, so this used to pass straight through."""
    payload = "before\x1b]52;c;CLIPBOARDPAYLOAD"
    out = tui._strip_dangerous_escapes(payload)
    assert "\x1b]52" not in out
    assert out == "before"


def test_strip_dangerous_escapes_removes_unterminated_dcs():
    payload = "before\x1bPunterminated dcs body"
    out = tui._strip_dangerous_escapes(payload)
    assert "\x1bP" not in out
    assert out == "before"


# ── R198: an ESC inside the payload defeated every alternative ─────────────
def test_an_esc_inside_an_osc_payload_does_not_smuggle_it_through():
    """R198: every alternative's body excluded ESC (`[^\\x1b]*`), including
    the two R171 added for unterminated sequences — which therefore could
    never reach their `\\Z` anchor once another ESC intervened. No
    alternative matched at the introducer, so the scan advanced PAST it,
    stripped only the INNER sequence, and left the outer one live:

        "\\x1b]52;c;PAY" + "\\x1b]0;t\\x07" + "LOAD"  ->  "\\x1b]52;c;PAYLOAD"

    A still-open OSC 52 clipboard write survived the sanitizer whose whole
    job is removing it. Fails without the fix on every payload below."""
    for payload in ("\x1b]52;c;PAY\x1b]0;title\x07LOAD",     # embedded OSC
                    "\x1b]52;c;PAY\x1b[0mLOAD",              # embedded CSI
                    "\x1b]52;c;A\x1b]52;c;B",                # two open OSCs
                    "\x1bP0;1|payload\x1b[0mtail"):          # DCS + inner CSI
        out = tui._strip_dangerous_escapes(payload)
        assert "\x1b]" not in out and "\x1bP" not in out, \
            f"{payload!r} smuggled an introducer through as {out!r}"


def test_r195_fix_does_not_over_strip_terminated_sequences():
    """R198 guard against over-correcting: the unterminated alternatives are
    greedy to end-of-text, so ordering is load-bearing. A properly terminated
    sequence must still match the earlier, narrower alternative and take only
    ITSELF, leaving following text alone."""
    assert tui._strip_dangerous_escapes(
        "\x1b]0;my title\x07hello world") == "hello world"
    # an OSC 8 hyperlink pair brackets its text; both halves go, text stays
    assert tui._strip_dangerous_escapes(
        "\x1b]8;;http://x\x1b\\text\x1b]8;;\x1b\\") == "text"
    # and colours are still untouched
    assert tui._strip_dangerous_escapes(
        "a\x1b[31mred\x1b[0mb") == "a\x1b[31mred\x1b[0mb"


def test_append_strips_dangerous_escapes_from_llm_text(t):
    """R170l only stripped bash_output — a compromised/prompt-injected model
    can put an OSC 52 clipboard write in its own reply just as easily as a
    subprocess can, and `append()` is the path that reply renders through."""
    t.append("before\x1b]52;c;bG9va2F0dGhpcw==\x07after")
    assert "\x1b]52" not in t._chat[-1]


def test_nano_click_in_bash_output_opens_editor(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("bash-opened")
    t._bash_cwd = str(tmp_path)
    t.append_bash_output("notes.txt\n")
    frags = t._fragments()
    handler = next(f[2] for f in frags if f[1] == "notes.txt")
    handler(_mouse_up())
    assert t._editor is not None
    assert t._editor_area.buffer.text == "bash-opened"


def test_nano_click_in_bash_output_resolves_against_bash_cwd(t, tmp_path):
    # regression companion to R107: a filename click must resolve against
    # the TRACKED bash cwd, not the process's real cwd
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "notes.txt").write_text("in sub")
    t._bash_cwd = str(sub)
    t.append_bash_output("notes.txt\n")
    frags = t._fragments()
    handler = next(f[2] for f in frags if f[1] == "notes.txt")
    handler(_mouse_up())
    assert t._editor["path"] == sub / "notes.txt"


def test_nano_click_refuses_while_menu_is_active(t, tmp_path):
    # regression: the chat pane's visibility isn't gated on
    # self._menu_options, only on self._editor/help — so a filename click
    # was reachable while e.g. /model's picker was open. Opening the editor
    # there made Enter/arrows/digits (needed to resolve the menu)
    # ineligible via filter=_no_editor, permanently deadlocking the worker
    # thread on select_menu()'s blocking self._answers.get().
    p = tmp_path / "notes.txt"
    p.write_text("hi")
    t._bash_cwd = str(tmp_path)
    t.append_bash_output("notes.txt\n")
    t._menu_prompt, t._menu_options, t._menu_index = "Select model", [("a", "A")], 0
    frags = t._fragments()
    handler = next(f[2] for f in frags if f[1] == "notes.txt")
    handler(_mouse_up())
    assert t._editor is None               # refused, didn't open
    assert t._menu_options is not None     # menu untouched, still resolvable


def test_nano_open_nano_refuses_during_question(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hi")
    t._question = "some blocking question: "
    t.open_nano(p)
    assert t._editor is None


def test_nano_open_nano_refuses_while_worker_busy(t, tmp_path):
    # regression: a background LLM turn/bash command with no menu/question
    # active YET can still reach one moments later (e.g. a tool-call
    # approval gate) — self._busy is a broader, simpler guard than trying
    # to enumerate every individual blocking state at open time.
    p = tmp_path / "notes.txt"
    p.write_text("hi")
    t._busy = True
    t.open_nano(p)
    assert t._editor is None


def test_nano_command_opens_file_despite_worker_busy_flag(t, tmp_path):
    # regression (R117): the worker sets self._busy = True for a line
    # BEFORE dispatching it, so `/nano <file>` — reached via
    # ui._handle_command on the worker thread — always found itself
    # "busy" and refused to open anything. check_busy=False on that call
    # site fixes it; check_busy still defaults to True for the OTHER entry
    # point (a mouse click on the UI thread racing an unrelated command).
    from aurora import ui
    p = tmp_path / "notes.txt"
    p.write_text("hi")
    fe = type("FE", (), {"_tui": t})()
    t._busy = True
    ui._handle_command(object(), fe, f"/nano {p}")
    assert t._editor is not None
    assert t._editor["path"] == p


def test_nano_path_completion_offered_in_prompt_mode(t, tmp_path, monkeypatch):
    # regression (R117): SlashCompleter.get_completions returns nothing once
    # the text has a space in it, so `/nano <partial><Tab>` never offered a
    # single path — unlike `cd` in bash mode, which gets PathCompleter.
    from prompt_toolkit.document import Document

    from aurora import ui
    (tmp_path / "notes.txt").write_text("hi")
    monkeypatch.chdir(tmp_path)
    completer = tui._ModeCompleter(t, ui.SlashCompleter(None))
    doc = Document("/nano no")
    completions = list(completer.get_completions(doc, None))
    # PathCompleter's .text is just the completed suffix (e.g. "tes.txt"
    # for "no"), not the whole filename — reconstruct to check the match.
    assert any(("no" + c.text) == "notes.txt" for c in completions)


def test_filenames_not_linkified_outside_bash_output(t, tmp_path):
    # only bash-mode command output gets filename-click — plain chat text
    # (e.g. the LLM mentioning a filename) must stay plain
    t.append("see notes.txt for details\n")
    frags = t._fragments()
    assert not any(f[1] == "notes.txt" and len(f) > 2 for f in frags)


def test_url_click_works_after_char_run_merge_fix(t):
    # regression: ANSI(...).__pt_formatted_text__() emits one fragment per
    # character, so URL_RE could never match against a single-char fragment
    # until fragments are merged back into same-style runs first
    t.append("visit https://example.com now\n")
    frags = t._fragments()
    link = next((f for f in frags if len(f) > 2), None)
    assert link is not None
    assert link[1] == "https://example.com"


def test_nano_filename_regex_does_not_match_longer_token_prefix():
    # regression: "notes.txtbak"/"archive.txt.bak" must NOT linkify as
    # "notes.txt"/"archive.txt" — nothing forced the match to consume the
    # rest of the token, so a wrong (or worse, coincidentally real but
    # unrelated) file could get opened instead
    found = tui._NANO_FILENAME_RE.findall(
        "notes.txtbak config.ymlbak archive.txt.bak plain.txt done")
    assert found == ["plain.txt"]


def test_nano_refuses_when_already_open(t, tmp_path, capsys):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("A")
    b.write_text("B")
    t.open_nano(a)
    t.open_nano(b)
    assert t._editor["path"] == a               # still editing the first
    out = capsys.readouterr().out
    assert "already editing" in out and str(a) in out


def test_nano_open_reports_decode_error_instead_of_raising(t, tmp_path, capsys):
    p = tmp_path / "bad.txt"
    p.write_bytes(b"\xff\xfe\x00\x01not valid utf-8 \xfa")
    t.open_nano(p)
    assert t._editor is None
    assert "can't open" in capsys.readouterr().out


def test_nano_save_reports_write_error_instead_of_raising(t, tmp_path, monkeypatch, capsys):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._editor_area.buffer.text = "hello world"

    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(type(p), "write_text", _boom)
    ok = t._nano_save()
    assert ok is False
    assert "can't save" in capsys.readouterr().out
    assert t._nano_dirty()                       # not silently marked clean


def test_nano_save_and_close_keeps_editor_open_on_failed_save(t, tmp_path, monkeypatch):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    t._editor_area.buffer.text = "hello world"
    monkeypatch.setattr(type(p), "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    t._nano_save_close_click()(_mouse_up())
    assert t._editor is not None                  # did NOT close/discard
    assert p.read_text() == "hello"                # nothing written either


def test_nano_dirty_cache_invalidated_on_edit_and_reset_on_save(t, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    t.open_nano(p)
    assert t._nano_dirty_cache is False            # set clean right on open
    t._editor_area.buffer.text = "hello world"
    assert t._nano_dirty_cache is None             # invalidated by the edit
    assert t._nano_dirty() is True                 # recomputes...
    assert t._nano_dirty_cache is True              # ...and caches the result
    t._nano_save()
    assert t._nano_dirty_cache is False             # save resets it directly


def test_nano_focus_hops_thread_when_opened_off_the_ui_thread(t, tmp_path, monkeypatch):
    # /nano reaches open_nano() from the WORKER thread; layout.focus() has
    # no documented thread-safety contract of its own (unlike app.invalidate,
    # which every worker-thread caller already relies on), so it must be
    # dispatched via call_soon_threadsafe when called off the UI thread —
    # same mechanism already used for /quit's app.exit().
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    calls = []
    monkeypatch.setattr(t.app, "layout", type(t.app.layout)(t.app.layout.container))
    monkeypatch.setattr(t.app.layout, "focus", lambda *_a, **_k: calls.append("direct"))
    fake_loop = type("L", (), {"call_soon_threadsafe": lambda self, fn: (calls.append("threadsafe"), fn())})()
    monkeypatch.setattr(t.app, "loop", fake_loop, raising=False)
    other_thread = threading.Thread(target=lambda: None)
    other_thread.start()
    other_thread.join()
    t._ui_thread = other_thread                   # pretend caller is off-thread
    t.open_nano(p)
    assert calls == ["threadsafe", "direct"]       # hopped, then focused

    t._editor = None                               # reset for a same-thread open
    calls.clear()
    t._ui_thread = threading.current_thread()
    t.open_nano(p)
    assert calls == ["direct"]                     # no hop needed on the UI thread


def test_editing_keys_reach_the_editor_not_the_hidden_input():
    # regression: every REPL-muscle-memory global binding (space, enter,
    # backspace, arrows, digits, `!`) used to act on self.input.buffer
    # UNCONDITIONALLY, regardless of what was actually focused — while
    # /nano owned focus, typing a space silently vanished into the hidden
    # input buffer instead of the file, Enter could submit whatever had
    # piled up there as a stray chat message, and arrow keys couldn't
    # navigate the file at all. filter=_no_editor must let these fall
    # through to the focused editor's own default key handling instead.
    import asyncio

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as inp:
        eng = _FakeEngine()
        tui_ = tui.Tui(eng)
        tui_.app.input = inp
        tui_.app.output = DummyOutput()
        p = _tmp_nano_file()
        tui_.open_nano(p)

        async def main():
            fut = asyncio.ensure_future(tui_.app.run_async())
            await asyncio.sleep(0.05)
            inp.send_text(" wor1d\r!more")
            await asyncio.sleep(0.05)
            tui_.app.exit()
            await fut

        asyncio.run(main())
        assert tui_.input.buffer.text == ""        # nothing leaked in here
        assert tui_._inbox.empty()                 # nothing submitted either
        assert tui_._bash_mode is False             # "!" didn't toggle bash mode
        assert "wor1d" in tui_._editor_area.buffer.text
        assert "more" in tui_._editor_area.buffer.text
        p.unlink()


def _tmp_nano_file():
    import tempfile
    d = tempfile.mkdtemp()
    p = Path(d) / "scratch.txt"
    p.write_text("x")
    return p


def test_bash_mode_completes_paths_not_slash_commands(t, tmp_path):
    (tmp_path / "Xxxanadu").mkdir()
    t._bash_mode = True
    t._bash_cwd = str(tmp_path)
    from prompt_toolkit.document import Document
    doc = Document("cd Xxx")
    completions = list(t.input.completer.get_completions(doc, None))
    assert any(c.text == "anadu" for c in completions)


def test_prompt_mode_still_completes_slash_commands(t):
    t._bash_mode = False
    from prompt_toolkit.document import Document
    doc = Document("/mod")
    completions = list(t.input.completer.get_completions(doc, None))
    assert any("model" in c.text for c in completions)


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
    # R170d: cursor must land at the END of the restored draft, where the
    # user actually left off — not at 0, where buffer.reset() put it
    assert t.input.buffer.cursor_position == len("half-typed thought")


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
    # R170d: cursor must land at the END of the restored draft, not at 0
    assert t.input.buffer.cursor_position == len("half-typed thought")


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


# ── bash_output entries must not break think-row bookkeeping (R110 regr.) ──
# R110 introduced a SECOND dict chat-entry kind ({"kind": "bash_output"}),
# but every "is this an open think row?" check elsewhere in this file
# assumed ANY dict entry was a think row and indexed straight into
# item["done"] — a real production crash: KeyError: 'done' inside
# _close_think_locked, reported live after a `!` bash command was followed
# by an LLM turn (bash_output entry precedes a think entry in self._chat).
def test_bash_output_entry_does_not_crash_finish_think(t):
    t.append_bash_output("some ls output\n")
    t.fe.begin_turn()
    t.begin_think(live=False)
    t.think_chunk("reasoning...")
    t.finish_think()                              # used to raise KeyError
    kinds = [e.get("kind") for e in t._chat if isinstance(e, dict)]
    assert kinds == ["bash_output", "think"]
    think = next(e for e in t._chat if e.get("kind") == "think")
    assert think["done"] is True


def test_bash_output_entry_does_not_crash_live_clock_key(t):
    t.append_bash_output("some ls output\n")
    t.begin_think(live=True)
    key = t._live_clock_key()                     # used to raise KeyError
    assert key == (0,)
    frags = t._fragments()                         # exercises the same path
    assert frags


# ── R171/P3: _live_clock_key stops walking the whole scrollback ───────────
def test_live_clock_key_uses_tracked_open_rows_not_a_full_scan(t):
    """Before the fix, `_live_clock_key` did `for item in self._chat` every
    render — O(scrollback) to find rows that are almost always 0 or 1.
    `_open_think_items` is now maintained at create/close time instead, so
    it should track exactly the currently-open rows without a scan."""
    for i in range(500):
        t.append(f"line {i}\n")
    assert t._live_clock_key() == ()
    assert t._open_think_items == []
    t.begin_think(live=True)
    assert len(t._open_think_items) == 1
    key = t._live_clock_key()
    assert len(key) == 1
    t.finish_think()
    assert t._open_think_items == []
    assert t._live_clock_key() == ()


# ── R62: the first Esc shows an "Esc again to …" hint on status line 2 ─────
def test_esc_armed_shows_cancel_hint(t):
    t._busy = True
    t._on_escape(lambda: None)                  # 1st press arms
    assert "Esc again to cancel this" in _status_line2(t)


def test_esc_armed_shows_no_quit_hint(t):
    """R104: the quit variant's status-bar hint was removed on request — an
    idle empty prompt is unambiguous enough on its own. The gesture itself
    (double-Esc opens the Yes/No quit menu) is unchanged; only the line-2
    reminder text during the arm window is gone. Cancel/leave-bash-mode
    keep their hints (see the tests above/below) — this is quit-only."""
    t._on_escape(lambda: None)                  # idle empty prompt: arms exit
    assert t._exit_confirm and t._esc_armed == "exit"   # gesture still arms
    assert "Esc again" not in _status_line2(t)          # but no line-2 text


def test_esc_armed_shows_leave_bash_hint(t):
    t._bash_mode = True
    t._on_escape(lambda: None)
    assert "Esc again to leave bash mode" in _status_line2(t)


def test_typing_dismisses_a_pending_exit_confirm(t):
    """R142b: one Esc on an idle empty prompt arms the quit question, and
    NOTHING cleared it except the next Enter. R104 removed the line-2 hint,
    so the state was invisible: the user typed a real message and Enter fed
    it to the quit question instead of sending it — discarded outright, not
    even recallable with up-arrow."""
    t._on_escape(lambda: None)                  # idle empty prompt: arms exit
    assert t._exit_confirm
    _type(t, "explain this traceback")
    assert not t._exit_confirm, "typing must dismiss the pending quit question"


def test_a_message_typed_after_a_stray_esc_is_actually_sent(t):
    """The symptom the fix is really about: the message must reach the
    worker, not vanish into `· staying`."""
    t._on_escape(lambda: None)
    _type(t, "explain this traceback")
    _press_enter(t)
    assert t._inbox.get_nowait() == "explain this traceback"


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


def test_cost_tree_click_queues_the_command_instead_of_working_inline(t, monkeypatch):
    """R134: the "cost tree" link walks the whole session JSONL, which only
    grows (R20). Doing that in a mouse handler freezes the UI event loop —
    the same defect R129 fixed for "copy all", so it gets the same shape.

    Proven by showing the render never happens on the calling thread; an
    output assertion alone would not catch a frozen UI."""
    from aurora import ctxtree

    called = []
    monkeypatch.setattr(ctxtree, "render",
                        lambda *a, **k: called.append("render") or "")

    t._cost_tree_click()(_mouse_up())

    assert t._inbox.get_nowait() == "/context"
    assert called == [], f"ran on the UI thread: {called}"
    assert any("/context" in e for e in t._chat if isinstance(e, str))


def test_cost_report_click_queues_the_command_instead_of_working_inline(
        t, monkeypatch):
    """R168: same shape as `_cost_tree_click` — `/cost` reads every session
    log on the machine (R20's unbounded logs), so it must not run on the
    UI thread either."""
    from aurora import ui

    called = []
    monkeypatch.setattr(ui, "_cost_report", lambda *a, **k: called.append("report") or "")

    t._cost_report_click()(_mouse_up())

    assert t._inbox.get_nowait() == "/cost"
    assert called == [], f"ran on the UI thread: {called}"
    assert any("/cost" in e for e in t._chat if isinstance(e, str))


def _status_frags(t):
    """Line 1+2 of the status bar, as the app would render them."""
    from prompt_toolkit.layout.controls import FormattedTextControl
    for ctrl in t.app.layout.find_all_controls():
        if isinstance(ctrl, FormattedTextControl) and callable(ctrl.text):
            got = ctrl.text()
            if any("ctx " in f[1] for f in got):
                return got
    raise AssertionError("no status bar found")


def test_status_bar_ctx_gauge_is_the_cost_tree_link(t):
    """R135c: R134 spelled the link out as a separate " cost tree" label next
    to the price. The ctx gauge already names what the tree breaks down, so
    the gauge IS the link now — one word less on a row that must fit 80 cols
    (R134g). It has to be its OWN fragment or it can't carry a handler (R56)."""
    frags = _status_frags(t)
    labels = [f[1] for f in frags]
    assert not any("cost tree" in s for s in labels), \
        "the separate 'cost tree' label should be gone"
    i = next(i for i, s in enumerate(labels) if s.startswith("ctx "))
    assert frags[i][0] == "class:status.id" and len(frags[i]) == 3
    # still sits on the price it explains — _Stats reports cost_known False
    # here, so the preceding fragment is the (empty) price slot
    assert "$" in labels[i - 1] or labels[i - 1].strip() == "│"


def test_status_bar_separates_the_model_from_the_ctx_gauge(t):
    """R135c: the two click targets on line 1 read as two buttons, not one
    run-on label — and the separator never doubles up when the price slot is
    empty (local models report no cost, the case _Stats reports)."""
    frags = _status_frags(t)
    line1 = "".join(f[1] for f in frags).split("\n")[0]
    assert " │ ctx " in line1, line1
    assert "│  │" not in line1, f"empty price left a doubled separator: {line1}"


def test_status_bar_gives_the_price_its_own_bare_section(t, monkeypatch):
    """R135e: the price is a `│` section of its own, with no parentheses —
    they said "aside" about the one number on the row that's real money."""
    monkeypatch.setattr(_Stats, "cost_known", True)
    monkeypatch.setattr(_Stats, "cost_usd", 1.5)
    line1 = "".join(f[1] for f in _status_frags(t)).split("\n")[0]
    assert " │ $1.50 │ ctx " in line1, line1
    assert "($" not in line1, f"price is still parenthesised: {line1}"


def test_status_bar_price_is_clickable_and_runs_cost(t, monkeypatch):
    """R168: tapping the `$` price is /cost, same as tapping the ctx gauge
    is /context — it needs its own fragment (with a handler) to be
    clickable at all (R56), and `class:status.id` is what makes it render
    underlined, same as every other status-bar link."""
    monkeypatch.setattr(_Stats, "cost_known", True)
    monkeypatch.setattr(_Stats, "cost_usd", 1.5)
    frags = _status_frags(t)
    price = next(f for f in frags if f[1] == "$1.50")
    assert price[0] == "class:status.id"   # underlined, same class as every link
    assert len(price) == 3                 # has a click handler

    price[2](_mouse_up())
    assert t._inbox.get_nowait() == "/cost"


def test_status_bar_undo_button_hidden_until_a_checkpoint_exists(t):
    """Feature request (2026-07-27): the `undo` button appears once the
    FIRST file has been changed this session, not before — `_has_checkpoints`
    starts False and only `on_tool_result` (a mutating tool) can flip it."""
    labels = [f[1] for f in _status_frags(t)]
    assert not any(s == "undo" for s in labels)
    t._has_checkpoints = True
    labels = [f[1] for f in _status_frags(t)]
    assert any(s == "undo" for s in labels)


def test_status_bar_undo_click_queues_the_command(t):
    """The click itself does no git plumbing and shows no detail — it just
    queues `/undo`, whose OWN handler (`ui._undo_cmd`) previews the affected
    paths and confirms against those (see the incident this replaced: a
    generic status-bar confirm with no file list let a click meant to undo
    one file silently revert a different, unrelated batch of work instead —
    see `_undo_click`'s docstring)."""
    t._has_checkpoints = True
    undo = next(f for f in _status_frags(t) if f[1] == "undo")
    assert undo[0] == "class:status.id" and len(undo) == 3   # clickable
    undo[2](_mouse_up())
    assert t._inbox.get_nowait() == "/undo"


def test_on_tool_result_flips_has_checkpoints_only_for_mutating_tools(t, monkeypatch, tmp_path):
    from aurora import tui as tui_mod
    monkeypatch.setattr(tui_mod.rewind, "undo_preview",
                        lambda cwd=".": ("uncommitted", ["x"]))
    fe = tui_mod.TuiFrontend(t)
    fe.on_tool_result("read_file", "x")
    assert not t._has_checkpoints          # read-only tool never checks
    fe.on_tool_result("write_file", "ok")
    assert t._has_checkpoints


def test_model_name_and_ctx_gauge_run_their_own_commands(t):
    """R135c: tapping the model name is /model, tapping the ctx gauge is
    /context — the two links must not share a handler."""
    frags = _status_frags(t)
    model = next(f for f in frags if len(f) == 3)   # first link on the row
    ctx = next(f for f in frags if f[1].startswith("ctx "))

    model[2](_mouse_up())
    assert t._inbox.get_nowait() == "/model"
    ctx[2](_mouse_up())
    assert t._inbox.get_nowait() == "/context"


def test_status_bar_drops_the_vendor_prefix_from_the_model(t):
    """R134g: line 1 is identity (R56) and had grown past 80 columns with a
    real model id, where prompt_toolkit clips it and the rightmost links stop
    being clickable. The vendor prefix disambiguates nothing on screen."""
    assert t._short_model("moonshotai/kimi-k2.7-code") == "kimi-k2.7-code"
    assert t._short_model("local") == "local"           # nothing to drop


def test_status_bar_names_the_loaded_local_model(t):
    """"local" alone doesn't say which model is serving — show it when the
    live name is known, still without the vendor prefix."""
    assert t._short_model("local", "unsloth/Qwen3-30B") == "local: Qwen3-30B"
    assert t._short_model("local", None) == "local"     # not known (yet)


def test_status_bar_keeps_the_vendor_prefix_when_it_disambiguates(t):
    """...unless two configured models share a short name, in which case a
    status bar that can't tell you which one you're on is worse than a long
    one."""
    t.engine.models = [{"model": "vendor-a/kimi-k2"},
                       {"model": "vendor-b/kimi-k2"}]
    assert t._short_model("vendor-a/kimi-k2") == "vendor-a/kimi-k2"
    t.engine.models = [{"model": "vendor-a/kimi-k2"}, {"model": "local"}]
    assert t._short_model("vendor-a/kimi-k2") == "kimi-k2"


# ── R152: scrollback is bounded ────────────────────────────────────────────
def _chat_lines(t):
    """Counted here rather than via t._entry_lines so the cap assertions fail
    on unbounded growth on the pre-R152 code, not on a missing attribute."""
    total = 0
    for e in t._chat:
        text = e if isinstance(e, str) else (e.get("text") or "")
        total += text.count("\n") + 1
    return total


def test_scrollback_is_capped_and_keeps_the_newest_content(t):
    """R152: `_chat`/`_cache` only ever grew — nothing but bash-mode `clear`
    ever emptied them. prompt_toolkit's create_content is linear in fragment
    count and runs before its own cache, every frame, so a long session
    degraded steadily. Trim the OLDEST; the newest must survive intact."""
    for i in range(4000):
        t.append(f"line {i}\n" * 5)
    assert _chat_lines(t) <= 12_000   # the cap, plus slack for the estimate
    # the most recent output is still there, in order
    text = "".join(e for e in t._chat if isinstance(e, str))
    assert "line 3999" in text
    assert "line 0\n" not in text          # the oldest is gone


def test_eviction_leaves_a_consistent_renderable_transcript(t):
    """The cap is worthless if trimming corrupts the render. `_offsets` and
    `_text_cache` are absolute, so they must be rebuilt — a stale offset would
    index into the wrong fragment or past the end."""
    for i in range(3000):
        t.append(f"entry {i}\n" * 6)
    frags = t._fragments()                 # full re-flatten after eviction
    assert frags
    assert len(t._offsets) == len(t._chat)
    assert len(t._cache) == len(t._chat)
    # _nlines must agree with what was actually flattened
    assert t._nlines == sum(f[1].count("\n") for f in frags if len(f) >= 2)
    # and it renders again cleanly (cache fast path) without raising
    assert t._fragments() is not None


def test_eviction_invalidates_a_selection_rather_than_misreporting_it(t):
    """Selections are absolute (line, col) coords, so trimming shifts them.
    Dropping them is correct; silently keeping them would make "copy selected"
    return whatever text now sits at those coordinates."""
    t.append("alpha\nbeta\ngamma\n")
    t._fragments()
    t.sel_begin((0, 0))
    t.sel_drag((1, 4))
    t.sel_finish()
    assert t._sel_frozen is not None
    for i in range(3000):
        t.append(f"filler {i}\n" * 6)
    assert t._sel_frozen is None and t._sel is None


def test_no_eviction_for_an_ordinary_session(t):
    """A normal session must be untouched — this is a backstop, not a policy
    that trims what anyone actually scrolls back through."""
    for i in range(50):
        t.append(f"turn {i}: a few lines of output\n" * 4)
    n = len(t._chat)
    assert n > 0
    assert "turn 0" in "".join(e for e in t._chat if isinstance(e, str))
    assert _chat_lines(t) < 10_000


def test_eviction_never_drops_the_entry_still_being_written(t):
    """think_chunk/append hold a live reference to `_chat[-1]` and keep
    appending into it, so the newest entry must never be evicted."""
    for i in range(3000):
        t.append(f"bulk {i}\n" * 6)
    t.think_chunk("thinking hard ")
    last = t._chat[-1]
    for i in range(3000):
        t.append(f"more {i}\n" * 6)
        t.think_chunk("x")
    assert t._chat, "everything was evicted"
    # whatever is last is a real entry and still writable
    t.think_chunk("final")
    assert isinstance(t._chat[-1], (str, dict))
    del last


# ── R204: the reasoning stream is model output too ─────────────────────────
def test_think_chunks_are_sanitized_like_the_reply(t):
    """R204: R171 added escape-stripping to `append()` reasoning that "a
    compromised or prompt-injected model can put an OSC 52 clipboard write in
    its own reply just as easily as a subprocess can". That argument is about
    the MODEL, not about which of its two output channels carried it — and
    `think_chunk` (the `reasoning_content` stream) was left raw, so a payload
    there reached `_chat` intact and went out with /copy-all.

    Fails without the fix: the row holds the raw OSC 52."""
    payload = "reasoning\x1b]52;c;bG9va2F0dGhpcw==\x07 continues"
    t.begin_think()
    t.think_chunk(payload)
    t.finish_think()
    row = next(e for e in t._chat
               if isinstance(e, dict) and e.get("kind") == "think")
    assert "\x1b]52" not in row["text"]
    assert "reasoning" in row["text"] and "continues" in row["text"]


def test_a_think_payload_split_across_chunks_still_does_not_survive(t):
    """R204: stripping per chunk cannot see a sequence split at a chunk
    boundary. It is covered anyway — R198's unterminated alternative is
    greedy to end-of-text, so the partial introducer goes as it arrives and
    the continuation lands as ordinary text — and the close-time pass makes
    that no longer depend on another rule's greediness."""
    t.begin_think()
    t.think_chunk("thinking\x1b]52;c;AA")
    t.think_chunk("BB\x07 and on")
    t.finish_think()
    row = next(e for e in t._chat
               if isinstance(e, dict) and e.get("kind") == "think")
    assert "\x1b]52" not in row["text"]
    assert "thinking" in row["text"]


def test_closing_a_think_row_re_sanitizes_the_whole_text(t):
    """R204: the close-time pass is the backstop. Written directly into the
    row (bypassing `think_chunk`'s own strip) so it is THIS pass being
    tested, not the per-chunk one."""
    t.begin_think()
    row = next(e for e in t._chat
               if isinstance(e, dict) and e.get("kind") == "think")
    row["text"] = "raw\x1b]52;c;SMUGGLED\x07 tail"
    t.finish_think()
    assert "\x1b]52" not in row["text"]
    assert "raw" in row["text"] and "tail" in row["text"]


# ── R205: eviction's line total is memoised, not re-scanned ────────────────
def test_the_line_count_memo_never_drifts_from_a_fresh_recompute(t):
    """R205: the memo is only safe if it is invalidated everywhere an entry's
    text changes. `_dirty(i)` is that point and is already called at all of
    them, but this pins it: after every kind of mutation the tui supports,
    the memoised total must equal a from-scratch recompute."""
    def fresh():
        return sum(t._entry_lines(e) for e in t._chat)

    t.append("one\ntwo\n")
    assert t._total_lines() == fresh()
    t.append("merged into the same entry\n")          # merge path
    assert t._total_lines() == fresh()
    t.append("x" * (t._MERGE_LIMIT + 10) + "\n")      # new-entry path
    assert t._total_lines() == fresh()
    t.append_bash_output("bash\noutput\n")
    assert t._total_lines() == fresh()
    t.begin_think()
    t.think_chunk("thinking\nmore\n")                 # mutates in place
    assert t._total_lines() == fresh()
    t.finish_think()
    assert t._total_lines() == fresh()
    t.clear_screen()
    assert t._total_lines() == fresh()


def test_the_memo_stays_parallel_to_chat_across_eviction(t):
    """R205: `_line_counts` is indexed by entry, so it has to be trimmed in
    lockstep with `_chat` — a length drift would silently misprice every
    entry after the split, and eviction is the one place both are trimmed."""
    big = "y" * (t._MERGE_LIMIT + 10) + "\n"
    for _ in range(60):
        t.append(big)
    assert len(t._line_counts) == len(t._chat) == len(t._cache)
    t._SCROLLBACK_MAX_LINES, t._SCROLLBACK_KEEP_LINES = 20, 10
    t.append(big)
    with t._lock:
        t._evict_locked()
    assert len(t._line_counts) == len(t._chat) == len(t._cache)
    assert t._total_lines() == sum(t._entry_lines(e) for e in t._chat)


# ── R211: editing keys leaked past the menu's Keys.Any swallow ─────────────
def _resolved_press(t, key, event=None):
    """Press a key the way prompt_toolkit really would — pick among the
    bindings that MATCH and whose filter passes, then call `matches[-1]`.
    `_press` above calls a handler directly and so cannot see precedence,
    which is the entire subject of these tests."""
    kb = t.app.key_bindings
    matches = [b for b in kb.get_bindings_for_keys((key,)) if b.filter()]
    assert matches, f"no active binding for {key}"
    matches[-1].handler(event)
    return matches[-1]


def test_editing_keys_do_not_leak_into_the_draft_during_a_menu(t):
    """R211: the `Keys.Any` binding is documented as swallowing "every other
    key (letters, space, backspace, paste) … so nothing leaks into the buffer
    rendered underneath the menu". Two of the four keys it NAMES leaked.

    `Keys.Any` is a fallback: prompt_toolkit sorts Any-bindings first and
    calls `matches[-1]`, so every more specific binding beats it. `space`,
    `c-j` and `backspace` were bound specifically with no menu filter, so
    while an approval menu was open a space appended to the draft, Ctrl+J
    added a newline to it, and backspace edited it.

    Fails without the fix: the draft grows a trailing space."""
    _type(t, "my draft")
    t._menu_options, t._menu_prompt = ["yes", "no"], "Approve?"
    _resolved_press(t, " ")
    _resolved_press(t, Keys.ControlJ)
    assert t.input.buffer.text == "my draft"


def test_backspace_during_a_menu_cannot_drop_bash_mode(t):
    """R211, the sharper half: backspace on an empty `$` prompt leaves bash
    mode. With a menu open that fired anyway — so a stray backspace while
    answering "Approve this command?" silently changed a mode the user never
    touched.

    Fails without the fix: bash mode is off."""

    class _Ev:
        arg = 1

    t._bash_mode = True
    t._menu_options, t._menu_prompt = ["yes", "no"], "Approve?"
    _resolved_press(t, Keys.Backspace, _Ev())
    assert t._bash_mode is True


def test_menu_driving_keys_still_beat_the_swallow(t):
    """R211 guard against over-correcting: enter, arrows, escape and digits
    are SUPPOSED to outrank `Keys.Any` — driving the menu is their job. Only
    the editing keys were filtered."""
    t._menu_options, t._menu_prompt = ["yes", "no"], "Approve?"
    for key in (Keys.ControlM, Keys.Up, Keys.Down, Keys.Escape, "1"):
        kb = t.app.key_bindings
        matches = [b for b in kb.get_bindings_for_keys((key,)) if b.filter()]
        assert matches and matches[-1].keys != (Keys.Any,), \
            f"{key} no longer reaches the menu"


def test_editing_keys_are_unaffected_with_no_menu_open(t):
    """R211: the filter is menu-scoped — with no menu, space and backspace
    edit exactly as before."""

    class _Ev:
        arg = 1

    _type(t, "ab")
    _resolved_press(t, " ")
    assert t.input.buffer.text == "ab "
    _resolved_press(t, Keys.Backspace, _Ev())
    assert t.input.buffer.text == "ab"


# ── R212: a status-render failure was silent ───────────────────────────────
def test_a_status_render_failure_is_reported_once(t, tmp_path, monkeypatch):
    """R212: the status block is wrapped in `except Exception` so a render
    error cannot take the app down — correct, since the bar repaints on a
    timer. But the fallback was SILENT: any bug in it showed only as a bar
    that mysteriously read " aurora", with nothing logged.

    Not hypothetical. `MEMORY/bugs/20260715_120000_tui_status_token_int_crash`
    records exactly such a crash (`live_token_tag` handing a char count to a
    function wanting a string) — this handler is what a repeat would hide.

    Reported once, not per frame: the bar repaints several times a second and
    logging each one would bury the session log in duplicates of one bug.

    Fails without the fix: no marker, no `_status_error`, no record."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora.session import Session
    t.engine.session = Session()

    def _boom():
        raise TypeError("object of type 'int' has no len()")

    t.engine.context_stats = _boom
    for _ in range(5):
        frags = t._status_render()

    assert "⚠" in "".join(f[1] for f in frags)
    assert t._status_error == "TypeError: object of type 'int' has no len()"
    errors = [r for r in t.engine.session.iter_records()
              if r.get("event") == "error" and r.get("where") == "status_render"]
    assert len(errors) == 1, f"logged {len(errors)} times across 5 renders"


def test_a_healthy_status_bar_is_unmarked(t):
    """R212 guard: the marker must mean something. A normal render carries no
    warning sign and leaves `_status_error` unset."""
    frags = t._status_render()
    assert "⚠" not in "".join(f[1] for f in frags[:4])
    assert t._status_error is None


def test_a_status_render_failure_never_propagates(t, tmp_path, monkeypatch):
    """R212: the whole point of the handler survives the change — the bar
    repaints on a timer, so raising here would take the session down. Even a
    failure while LOGGING the failure must not escape."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    t.engine.context_stats = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    t.engine.session = types.SimpleNamespace(
        log=lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    frags = t._status_render()          # must not raise
    assert "⚠" in "".join(f[1] for f in frags)
