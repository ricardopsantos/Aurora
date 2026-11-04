"""R422: notes have a type (Personal, Work, Skills); old notes are Personal; a note moves between types."""
import json

from tests.test_web import app, call  # noqa: F401  (fixture)


def test_r422_old_notes_are_personal_and_types_filter(app):
    from aurora.web import notes
    old, _ = notes.create("old", "x")
    p = notes._dir() / "index.json"; idx = json.loads(p.read_text()); idx[old].pop("type"); p.write_text(json.dumps(idx))
    w = call(app, "/api/notes/create", {"title": "job", "text": "y", "type": "work"})[1]["id"]
    s = call(app, "/api/notes/create", {"title": "mail", "text": "z", "type": "skills"})[1]["id"]
    ids = lambda t: [n["id"] for n in call(app, "/api/notes?type=" + t)[1]["notes"]]
    assert ids("personal") == [old] and ids("work") == [w] and ids("skills") == [s]
    assert len(call(app, "/api/notes")[1]["notes"]) == 3 and call(app, "/api/notes/" + old)[1]["type"] == "personal"
    assert call(app, "/api/notes/create", {"title": "x", "type": "other"})[0] == 400


def test_r422_move_keeps_updated_and_order(app):
    nid = call(app, "/api/notes/create", {"title": "a", "text": "b"})[1]["id"]
    before = call(app, "/api/notes/" + nid)[1]["updated"]
    assert call(app, "/api/notes/type", {"id": nid, "type": "skills"})[0] == 200
    n = call(app, "/api/notes/" + nid)[1]
    assert n["type"] == "skills" and n["updated"] == before
    assert call(app, "/api/notes/type", {"id": nid, "type": "nope"})[0] == 400
    assert call(app, "/api/notes/type", {"id": "0" * 12, "type": "work"})[0] == 404
