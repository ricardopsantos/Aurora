"""R466: deleting a chat on the page revokes its share links; expired links are dropped (audit, 2026-11-24)."""
import time

from aurora.web import folders, shares
from tests.test_web import app, call  # noqa: F401  (fixture)
from tests.test_web_r418 import _get, _turn


def _shared_then_left(app):  # noqa: F811
    _turn(app, "private words")
    sid = app.e.session.id
    code, d = call(app, "/api/share", {"id": sid})
    assert code == 200 and _get(app, d["path"])[0] == 200
    assert call(app, "/api/new", {})[0] == 200         # the open chat cannot be deleted
    return sid, d


def test_deleting_a_chat_revokes_its_share_link(app):  # noqa: F811
    sid, d = _shared_then_left(app)
    assert call(app, "/api/chat/delete", {"id": sid})[0] == 200
    code, _, body = _get(app, d["path"])
    assert code == 404 and "private words" not in body
    assert shares.info(d["token"]) is None


def test_deleting_a_folder_revokes_the_links_of_the_chats_it_hides(app):  # noqa: F811
    sid, d = _shared_then_left(app)
    fid = call(app, "/api/folder/create", {"name": "F"})[1]["id"]
    assert call(app, "/api/chat/move", {"chat": sid, "folder": fid})[0] == 200
    assert call(app, "/api/folder/delete", {"id": fid})[0] == 200
    assert _get(app, d["path"])[0] == 404 and shares.info(d["token"]) is None


def test_a_chat_hidden_before_the_fix_is_not_served(app):  # noqa: F811
    sid, d = _shared_then_left(app)
    folders.hide_chat(sid)                              # what an older server did: hidden, link kept
    assert shares.info(d["token"]) is not None
    assert _get(app, d["path"])[0] == 404


def test_expired_links_do_not_fill_the_store(app, monkeypatch):  # noqa: F811
    _turn(app, "hello")
    past = int(time.time()) - 10
    shares._save({f"{i:022d}": {"session": "gone", "upto": 1, "expires": past} for i in range(shares.MAX_SHARES)})
    code, d = call(app, "/api/share", {"id": app.e.session.id})
    assert code == 200, d
    assert list(shares._load()) == [d["token"]]
