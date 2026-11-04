"""R401: a note is sent to Aurora as context with one message (Notes "Send to chat" button or the # picker)."""
import pathlib
import types as pytypes

from aurora.providers.base import TurnResult
from aurora.web import server as web_server
from tests.test_core import FakeProvider
from tests.test_web import app, call, types, wait_for  # noqa: F401  (fixture)


def _seen(app, n=4):
    app.state["prov"] = FakeProvider([TurnResult(text=f"r{i}", stop_reason="end") for i in range(n)])
    prov, seen = app.state["prov"], []
    orig = prov.turn

    def turn(model, messages, *a, **k):
        seen.append(messages[-1]["content"])
        return orig(model, messages, *a, **k)
    prov.turn = turn
    return seen


def _note(app, title, text):
    code, d = call(app, "/api/notes/create", {"title": title, "text": text})
    assert code == 200
    return d["id"]


def _users(app):
    return [e["text"] for e in app.s.events if e["type"] == "user"]


def test_r401_note_text_reaches_the_model_and_the_chat_shows_only_the_title(app):
    seen = _seen(app)
    nid = _note(app, "Deploy rules", "always run the tests")
    assert call(app, "/api/send", {"text": "what are the rules?", "note_ids": [nid]})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    assert seen == ["[Note: Deploy rules]\nalways run the tests\n[End of note]\n\nwhat are the rules?"]
    assert _users(app) == ["📝 Deploy rules\nwhat are the rules?"]


def test_r401_several_notes_in_order_duplicates_once_and_no_text_needed(app):
    seen = _seen(app)
    a, b = _note(app, "A", "one"), _note(app, "", "two")
    assert call(app, "/api/send", {"text": "", "note_ids": [a, b, a]})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    assert seen[0] == "[Note: A]\none\n[End of note]\n\n[Note: Untitled]\ntwo\n[End of note]\n\n(see the attached notes)"
    assert _users(app) == ["📝 A, Untitled\n(see the attached notes)"]


def test_r401_bad_or_unknown_note_ids_are_refused(app):
    nid = _note(app, "x", "y")
    assert call(app, "/api/send", {"text": "hi", "note_ids": ["0123456789ab"]})[0] == 400
    assert call(app, "/api/send", {"text": "hi", "note_ids": ["../etc/passwd"]})[0] == 400
    assert call(app, "/api/send", {"text": "hi", "note_ids": nid})[0] == 400
    assert call(app, "/api/send", {"text": "hi", "note_ids": [nid] * 6})[0] == 400
    assert "user" not in types(app)


def test_r401_a_note_too_big_for_the_remaining_context_is_refused_not_cut(app, monkeypatch):
    nid = _note(app, "big", "word " * 4000)
    monkeypatch.setattr(app.e, "context_stats", lambda: pytypes.SimpleNamespace(used=500, limit=2000, local_model="", compactions=0))
    code, d = call(app, "/api/send", {"text": "hi", "note_ids": [nid]})
    assert code == 400 and "context" in d["error"]
    assert "user" not in types(app)


def test_r401_edit_keeps_the_notes_and_regenerate_shows_titles(app):
    seen = _seen(app)
    nid = _note(app, "N", "body")
    call(app, "/api/send", {"text": "first", "note_ids": [nid]})
    assert wait_for(lambda: types(app).count("done") == 1)
    assert call(app, "/api/retry", {"text": "📝 N\nsecond"})[0] == 200        # the page edits what it showed
    assert wait_for(lambda: types(app).count("done") == 2)
    assert call(app, "/api/retry", {})[0] == 200
    assert wait_for(lambda: types(app).count("done") == 3)
    assert seen[1] == seen[2] == "[Note: N]\nbody\n[End of note]\n\nsecond"
    assert _users(app)[1:] == ["📝 N\nsecond", "📝 N\nsecond"]


def test_r401_queued_message_keeps_its_notes(app):
    import threading
    seen, gate = _seen(app), threading.Event()
    prov = app.state["prov"]
    inner = prov.turn

    def turn(*a, **k):
        if len(seen) == 0:
            gate.wait(3)
        return inner(*a, **k)
    prov.turn = turn
    nid = _note(app, "Q", "ctx")
    call(app, "/api/send", {"text": "one"})
    assert wait_for(lambda: app.s.busy)
    assert call(app, "/api/send", {"text": "two", "note_ids": [nid]})[0] == 200
    assert [e["text"] for e in app.s.events if e["type"] == "queued"] == ["📝 Q\ntwo"]
    gate.set()
    assert wait_for(lambda: types(app).count("done") == 2)
    assert seen == ["one", "[Note: Q]\nctx\n[End of note]\n\ntwo"]


def test_r401_page_has_send_button_hash_picker_and_note_chips():
    d = pathlib.Path(web_server.__file__).parent
    v2, js = (d / "index_v2.html").read_text(), (d / "app.js").read_text()
    assert 'id="note-send"' in v2 and "Send to chat as context" in v2
    assert "note_ids: noteChips.map(n => n.id)" in js and "note_ids: item.note_ids || []" in js
    assert "#npick" in js and "/(^|\\s)#([^\\s#]{0,40})$/" in js
    assert "at most ' + NOTE_MAX + ' notes per message'" in js
