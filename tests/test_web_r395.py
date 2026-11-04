"""R395: every chat-organising endpoint takes only a real session id; the guard also sees dynamic imports."""
import ast
import pathlib

from tests.test_web import app, call, types, wait_for  # noqa: F401  (fixture)

CRAFTED = ("", "../../etc/passwd", "nope", "a" * 5000, "x.2")


def _live_sid(app):
    call(app, "/api/send", {"text": "first task"})
    assert wait_for(lambda: "done" in types(app))
    return call(app, "/api/state")[1]["session_id"]


def test_move_refuses_an_id_that_is_not_a_session(app):
    _live_sid(app)
    code, f = call(app, "/api/folder/create", {"name": "work"})
    fid = f["id"]
    for bad in CRAFTED:
        code, d = call(app, "/api/chat/move", {"chat": bad, "folder": fid})
        assert code != 200 and "no such session" in d.get("error", ""), bad
    folders = call(app, "/api/sessions")[1]
    assert all(s.get("folder") is None for s in folders["sessions"])
    from aurora.web import folders as store
    assert store.view()["folders"][0]["chats"] == []           # nothing junk was stored


def test_move_still_works_for_a_real_session(app):
    sid = _live_sid(app)
    fid = call(app, "/api/folder/create", {"name": "work"})[1]["id"]
    assert call(app, "/api/chat/move", {"chat": sid, "folder": fid})[0] == 200
    rows = call(app, "/api/sessions")[1]["sessions"]
    assert next(r for r in rows if r["id"] == sid)["folder"] == fid
    assert call(app, "/api/chat/move", {"chat": sid, "folder": ""})[0] == 200
    assert next(r for r in call(app, "/api/sessions")[1]["sessions"] if r["id"] == sid)["folder"] is None


def test_delete_rename_and_read_refuse_crafted_ids(app):
    _live_sid(app)
    for bad in CRAFTED:
        assert call(app, "/api/chat/delete", {"id": bad})[0] != 200, bad
        assert call(app, "/api/chat/rename", {"id": bad, "name": "x"})[0] != 200, bad
        assert call(app, "/api/resume", {"id": bad})[0] != 200, bad


def test_sessions_and_search_answer_from_the_facade(app):
    sid = _live_sid(app)
    rows = call(app, "/api/sessions")[1]["sessions"]
    assert rows[0]["id"] == sid and rows[0]["current"] and rows[0]["prompts"] == 1 and rows[0]["ts"] > 0
    hits = call(app, "/api/search?q=FIRST")[1]["sessions"]
    assert [h["id"] for h in hits] == [sid]


def test_web_has_no_dynamic_import_of_a_core_module():
    """importlib.import_module("aurora.session") or __import__("aurora.rewind") would slip past the AST import guard."""
    web = pathlib.Path(__file__).resolve().parent.parent / "aurora" / "web"
    bad = []
    for path in web.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                arg = node.args[0].value
                if fn in ("import_module", "__import__") and isinstance(arg, str) and \
                        (arg.startswith("aurora") or arg.startswith(".")) and not arg.endswith("service"):
                    bad.append(f"{path.name}:{node.lineno} {fn}({arg!r})")
    assert not bad, bad
