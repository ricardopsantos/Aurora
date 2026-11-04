"""R407: only the chat column takes dropped files. R408: the Uploads dialog lists and deletes saved uploads."""
from pathlib import Path

from tests.test_web import app, call  # noqa: F401  (fixture)

WEB = Path(__file__).resolve().parent.parent / "aurora/web"


def test_r407_drop_zone_is_the_chat_column():
    js = (WEB / "app.js").read_text()
    assert "const zone = document.querySelector('.main')" in js
    assert "zone.addEventListener('drop'" in js
    assert "\n  addEventListener('dragenter'" not in js   # no longer on the whole window


def test_r408_list_and_delete(app):
    from aurora.web import attach
    p = Path(attach.save_original("a b.png", b"x"))
    st, d = call(app, "/api/files")
    assert st == 200 and [u["name"] for u in d["files"]] == [p.name] and d["files"][0]["size"] == 1
    assert call(app, "/api/files/delete", {"name": "../config.yaml"})[0] == 404
    assert call(app, "/api/files/delete", {"name": p.name})[0] == 200 and not p.exists()
    assert call(app, "/api/files/delete", {"name": p.name})[0] == 404
    assert call(app, "/api/files")[1]["files"] == []


def test_r408_page_has_the_uploads_dialog():
    html = (WEB / "index_v2.html").read_text()
    assert 'id="files-btn"' in html and '<dialog id="files"' in html
