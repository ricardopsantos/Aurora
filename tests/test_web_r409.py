"""R409: attached documents are saved and listed too; the drop overlay goes away after a drop on the message bar."""
import json
import urllib.request
from pathlib import Path

from tests.test_web import TOKEN, app, call, types, wait_for  # noqa: F401  (fixture)
from tests.test_web_r401 import _seen

WEB = Path(__file__).resolve().parent.parent / "aurora/web"


def _upload(app, name, data):
    req = urllib.request.Request(app.base + "/api/attach", data=data, method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "X-Filename": name})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_r409_text_file_is_saved_listed_and_its_path_reaches_the_model(app):
    d = _upload(app, "notes.txt", b"hello\n")
    assert d["kind"] == "text" and open(d["path"], "rb").read() == b"hello\n"
    assert [u["path"] for u in call(app, "/api/files")[1]["files"]] == [d["path"]]
    seen = _seen(app)
    assert call(app, "/api/send", {"text": "sum it", "files": [{"name": d["name"], "text": d["text"], "path": d["path"]}]})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    assert seen[0].endswith(f"sum it\n[Attached file saved at: {d['path']}]")


def test_r409_a_foreign_file_path_is_refused(app):
    assert call(app, "/api/send", {"text": "t", "files": [{"name": "a", "text": "b", "path": "/etc/passwd"}]})[0] == 400


def test_r409_overlay_hidden_on_any_drop_in_the_chat_column():
    assert "zone.addEventListener('drop', () => setTimeout(off), true)" in (WEB / "app.js").read_text()
