"""R428-R433 (card aurora-web-v2, 2026-11-04): top bar, YAML-only Edit models in the model menu, Settings panel,
Uploads renamed Input everywhere, note tabs open a note."""
import os
from pathlib import Path

from tests.test_web import app, call  # noqa: F401  (fixture)
from tests.test_web_r406 import _upload
from tests.test_web_r410 import _get

WEB = Path(__file__).resolve().parent.parent / "aurora/web"


def _page():
    return (WEB / "index_v2.html").read_text(), (WEB / "app.js").read_text()


def test_r428_pills_keep_their_size_the_title_gives_way():
    html, _ = _page()
    assert "#chat-title{" in html and "flex:1 1 0}" in html.split("#chat-title{", 1)[1].split("\n", 1)[0]
    assert "#ctx{display:inline-flex;align-items:center;gap:6px;cursor:pointer;flex:none}" in html
    assert "#status{cursor:pointer;flex:none}" in html


def test_r429_models_dialog_is_yaml_only():
    html, js = _page()
    dlg = html.split('<dialog id="models"', 1)[1].split("</dialog>", 1)[0]
    for gone in ('id="md-list"', 'id="md-form"', 'id="md-add"', 'data-view="list"', 'id="md-desc"'):
        assert gone not in dlg, gone
    assert 'data-view="config.yaml" aria-selected="true"' in dlg and 'id="cfg-save"' in dlg
    assert "/api/models/config/add" not in js and "md-form" not in js


def test_r430_edit_models_and_refresh_prices_live_in_the_model_menu():
    html, js = _page()
    assert 'id="models-btn"' not in html
    assert "eb.id = 'models-edit'" in js and "openModels()" in js and "rb.id = 'prices-refresh'" in js


def test_r431_settings_is_a_panel_opened_from_the_sidebar():
    html, js = _page()
    assert 'id="menu-btn"' not in html and "<aside" not in html
    side = html.split('<nav id="side"', 1)[1].split("</nav>", 1)[0]
    assert side.rstrip().endswith('Settings</button>') and 'id="settings-btn"' in side
    dlg = html.split('<dialog id="settings"', 1)[1].split("</dialog>", 1)[0]
    for i in ("theme-sel", "md-on", "hide-tools", "auto-on", "plan-on", "clear-open", "temp-on", "ctx-detail", "sid", "usage-btn", "token", "token-save", "ver-code"):
        assert f'id="{i}"' in dlg, i
    assert '<option value="auto">System</option>' in dlg                       # the Design Language's theme names
    assert "#settings.panel[open]" in html and "max-width:560px" in html
    assert "openPanel(dlg, 'Settings')" in js and "window.openSettings('token')" in js


def test_r432_input_folder_moves_old_uploads_and_links_the_old_path(tmp_path, monkeypatch):
    from aurora import service
    from aurora.web import attach
    monkeypatch.setattr(service, "aurora_home", lambda: tmp_path)
    old = tmp_path / "workspace/uploads"; old.mkdir(parents=True); (old / "a.jpg").write_bytes(b"x")
    web = tmp_path / "web-uploads"; web.mkdir(); (web / "b.txt").write_text("y")
    d = attach.input_dir()
    assert d == tmp_path / "workspace" / "input" and attach.KINDS == ("input", "outputs")
    assert sorted(f.name for f in d.iterdir()) == ["a.jpg", "b.txt"] and not web.exists()
    assert old.is_symlink() and os.readlink(old) == "input"                   # an older chat's path still opens
    assert (old / "a.jpg").read_bytes() == b"x" and attach.is_input(str(old / "a.jpg"))
    assert attach.input_dir() == d                                             # a second call changes nothing


def test_r432_api_and_page_say_input(app):
    from aurora.web import attach
    import base64
    jpg = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
    d = _upload(app, "p.jpg", jpg)
    assert Path(d["path"]).parent == attach.input_dir()
    st, body = call(app, "/api/files")
    assert st == 200 and body["kind"] == "input" and [f["path"] for f in body["files"]] == [d["path"]]
    assert _get(app, "/api/files/file?kind=input&name=" + Path(d["path"]).name)[0] == 200
    assert call(app, "/api/files?kind=uploads")[0] == 400 and call(app, "/api/uploads")[0] == 404
    assert call(app, "/api/files/delete_all", {"kind": "input"})[1] == {"ok": True, "deleted": 1}
    assert "saved unchanged in the input folder" in attach.media_text("audio", "a.mp3", 1, "/x/a.mp3")
    html, js = _page()
    assert 'data-kind="input" aria-selected="true">Input</button>' in html and 'id="files-btn"' in html
    assert "upload" not in html.lower()
    assert "m[1] === 'uploads' ? 'input'" in js


def test_r433_note_tabs_open_a_note_and_drop_empty_ones():
    _, js = _page()
    assert "const switchTab = t => serial(" in js and "await dropIfEmpty(true)" in js
    assert "api('/api/notes/create', { title: '', text: '', type: t })" in js
    assert "const closeNotes = async () => { await dropIfEmpty(false);" in js
    assert "async function leave() { await dropIfEmpty(false);" in js
