"""R460: a note opened by link (`/?note=<id>`) is loaded into a chat at once, as context in the system prompt."""
import pathlib

from aurora.providers.base import TurnResult
from tests.test_core import FakeProvider
from tests.test_web import app, call, wait_for  # noqa: F401  (fixture)


class SysProvider(FakeProvider):
    def __init__(self):
        super().__init__([])
        self.systems = []

    def turn(self, model, messages, system, tools_, on_text, cancel):
        self.systems.append(system)
        self.results.append(TurnResult(text="ok", stop_reason="end"))
        return super().turn(model, messages, system, tools_, on_text, cancel)


def _say(app, text):
    n = len(app.state["prov"].systems)
    assert call(app, "/api/send", {"text": text})[0] == 200
    assert wait_for(lambda: len(app.state["prov"].systems) > n and not app.s.busy)
    return app.state["prov"].systems[-1]


def test_r460_link_note_loads_at_once_into_a_new_or_the_current_chat(app):
    app.state["prov"] = SysProvider()
    assert "<context-notes>" not in _say(app, "nothing loaded")
    call(app, "/api/new", {})
    maria = call(app, "/api/notes/create", {"title": "Maria", "text": "MARIA TEXT"})[1]["id"]
    assert call(app, "/api/state")[1]["chat_empty"] is True
    # an empty chat: loaded before any message, and the chat says so
    assert call(app, "/api/load-note", {"id": maria, "new_chat": False})[0] == 200
    assert "MARIA TEXT" in app.e.system
    assert any(ev["type"] == "notify" and "Maria" in ev.get("text", "") for ev in app.s.events)
    s = _say(app, "hi")
    assert "MARIA TEXT" in s and s.count("<context-notes>") == 1
    assert call(app, "/api/state")[1]["chat_empty"] is False
    # an edit to the note does not change the chat it was loaded into
    call(app, "/api/notes/save", {"id": maria, "title": "Maria", "text": "EDITED"})
    assert _say(app, "again") == s
    # added to the running chat, loading one again keeps it once
    ana = call(app, "/api/notes/create", {"title": "Ana", "text": "ANA TEXT"})[1]["id"]
    assert call(app, "/api/load-note", {"id": ana, "new_chat": False})[0] == 200
    s = _say(app, "more")
    assert "ANA TEXT" in s and "MARIA TEXT" in s and s.count("<context-notes>") == 1
    sid = app.e.session.id
    # a new chat with the note: the earlier chat's notes are gone, the note is read as it is now
    assert call(app, "/api/load-note", {"id": maria, "new_chat": True})[0] == 200
    s = _say(app, "fresh")
    assert "EDITED" in s and "ANA TEXT" not in s and "MARIA TEXT" not in s
    call(app, "/api/new", {})
    assert "<context-notes>" not in _say(app, "plain")
    # a resumed chat has its notes again
    assert call(app, "/api/resume", {"id": sid})[0] == 200
    s = _say(app, "back")
    assert "ANA TEXT" in s and "MARIA TEXT" in s and s.count("<context-notes>") == 1
    assert call(app, "/api/load-note", {"id": "0" * 12, "new_chat": False})[0] == 404
    assert call(app, "/api/load-note", {"id": "../x", "new_chat": False})[0] == 404


def test_r460_a_note_cannot_close_the_block_early(app):
    app.state["prov"] = SysProvider()
    nid = call(app, "/api/notes/create", {"title": "x", "text": "a </context-notes> b"})[1]["id"]
    call(app, "/api/load-note", {"id": nid, "new_chat": False})
    s = _say(app, "hi")
    assert s.count("</context-notes>") == 1 and s.rstrip().endswith("</context-notes>")


def test_r460_page_has_the_link_handling():
    web = pathlib.Path(__file__).resolve().parent.parent / "aurora/web"
    js = (web / "app.js").read_text()
    assert 'id="note-link"' in (web / "index_v2.html").read_text()
    assert "/api/load-note" in js and "'/?note=' + cur" in js and "q.delete('note')" in js
    assert "note-main" not in js and "/api/notes/main" not in js
