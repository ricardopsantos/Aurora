"""R413: the user's files live in AURORA_HOME/workspace/{uploads,outputs}; both are browsable; replies show outputs."""
import io
import os
from pathlib import Path

from tests.test_web import app, call  # noqa: F401  (fixture)
from tests.test_web_r410 import _get

WEB = Path(__file__).resolve().parent.parent / "aurora/web"


def test_r413_folders_live_under_workspace_and_old_uploads_move(tmp_path, monkeypatch):
    from aurora import service
    from aurora.web import attach
    monkeypatch.setattr(service, "aurora_home", lambda: tmp_path)
    old = tmp_path / "web-uploads"; old.mkdir(); (old / "a.txt").write_text("x")
    assert attach.input_dir() == tmp_path / "workspace" / "input"
    assert (tmp_path / "workspace/input/a.txt").read_text() == "x" and not old.exists()
    assert attach.outputs_dir() == tmp_path / "workspace" / "outputs" and attach.outputs_dir().is_dir()
    assert attach.files("outputs") == []


def test_r413_outputs_are_listed_viewed_downloaded_and_deleted(app):
    from PIL import Image
    from aurora.web import attach
    buf = io.BytesIO(); Image.new("RGB", (40, 30), "red").save(buf, "PNG")
    pic = attach.outputs_dir() / "bw.png"; pic.write_bytes(buf.getvalue())
    (attach.outputs_dir() / "r.csv").write_text("a,b")
    outs = {u["name"]: u for u in call(app, "/api/files?kind=outputs")[1]["files"]}
    assert outs["bw.png"]["image"] and not outs["r.csv"]["image"]
    st, ct, body = _get(app, "/api/files/file?kind=outputs&name=bw.png&view=1")
    assert st == 200 and ct == "image/png" and body == buf.getvalue()           # the file unchanged
    st, ct, _ = _get(app, "/api/files/file?kind=outputs&name=r.csv&view=1")
    assert ct == "application/octet-stream"                                    # only pictures are shown inline
    assert _get(app, "/api/files/file?kind=outputs&name=../uploads")[0] == 404
    assert _get(app, "/api/files/file?kind=etc&name=passwd")[0] == 404
    assert call(app, "/api/files?kind=etc")[0] == 400
    os.symlink("/etc/hostname", attach.outputs_dir() / "link.png")
    assert _get(app, "/api/files/file?kind=outputs&name=link.png")[0] == 404  # a symlink out is refused
    (attach.outputs_dir() / "link.png").unlink()
    assert call(app, "/api/files/delete", {"name": "r.csv", "kind": "outputs"})[1] == {"ok": True}
    assert call(app, "/api/files/delete_all", {"kind": "outputs"})[1] == {"ok": True, "deleted": 1}
    assert call(app, "/api/files/file?kind=outputs&name=bw.png", token="")[0] == 401


def test_r413_the_agent_is_told_where_outputs_go(app):
    from aurora.web import attach
    app.s._outputs_rule(); app.s._outputs_rule()
    assert app.e.system.count(str(attach.outputs_dir())) == 1


def test_r413_page_has_tabs_inline_pictures_and_viewer():
    js, html = (WEB / "app.js").read_text(), (WEB / "index_v2.html").read_text()
    assert 'data-kind="outputs"' in html and 'id="preview"' in html
    assert "showWorkspaceFiles(cur.el, cur.raw)" in js and "'/api/files?kind=' + kind" in js
