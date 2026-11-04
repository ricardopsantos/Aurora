"""R410: Uploads shows a thumbnail for pictures, downloads a file again, and deletes all at once."""
import io
import urllib.request
from pathlib import Path

from tests.test_web import TOKEN, app, call  # noqa: F401  (fixture)

WEB = Path(__file__).resolve().parent.parent / "aurora/web"


def _get(app, path):
    req = urllib.request.Request(app.base + path, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, None, b""


def test_r410_thumbnail_download_and_delete_all(app):
    from PIL import Image
    from aurora.web import attach
    buf = io.BytesIO(); Image.new("RGB", (800, 600), "blue").save(buf, "JPEG")
    pic = Path(attach.save_original("p.jpg", buf.getvalue()))
    doc = Path(attach.save_original("d.txt", b"text"))
    ups = {u["name"]: u for u in call(app, "/api/files")[1]["files"]}
    assert ups[pic.name]["image"] is True and ups[doc.name]["image"] is False
    st, ct, body = _get(app, f"/api/files/file?name={pic.name}&thumb=1")
    assert st == 200 and ct == "image/png" and max(Image.open(io.BytesIO(body)).size) == 96
    assert pic.read_bytes() == buf.getvalue()                       # the saved original is untouched
    assert _get(app, f"/api/files/file?name={doc.name}")[2] == b"text"
    assert _get(app, f"/api/files/file?name={doc.name}&thumb=1")[0] == 404
    assert _get(app, "/api/files/file?name=../config.yaml")[0] == 404
    assert call(app, "/api/files/delete_all", {})[1] == {"ok": True, "deleted": 2}
    assert call(app, "/api/files")[1]["files"] == [] and not pic.exists()


def test_r410_page_has_download_and_delete_all():
    js, html = (WEB / "app.js").read_text(), (WEB / "index_v2.html").read_text()
    assert "/api/files/delete_all" in js and "downloadFile(k, u.name, nm.textContent)" in js and 'id="fl-all"' in html
