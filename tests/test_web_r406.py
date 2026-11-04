"""R406: an attached picture is also saved, unchanged, as a file, and its path goes with the message so tools can open it."""
import io
import json
import urllib.request

from tests.test_web import TOKEN, app, call, types, wait_for  # noqa: F401  (fixture)
from tests.test_web_r401 import _seen


def _upload(app, name, data):
    req = urllib.request.Request(app.base + "/api/attach", data=data, method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "X-Filename": name})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _jpeg_with_exif():
    from PIL import Image
    im = Image.new("RGB", (40, 30), "red")
    ex = Image.Exif()
    ex[0x010F] = "TestCam"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=ex.tobytes())
    return buf.getvalue()


def test_r406_picture_is_saved_byte_for_byte_and_its_path_reaches_the_model(app, monkeypatch):
    from aurora.web.core import WebSession
    monkeypatch.setattr(WebSession, "vision", lambda self, probe=False: True)
    raw = _jpeg_with_exif()
    d = _upload(app, "my photo.jpg", raw)
    assert d["kind"] == "image" and open(d["path"], "rb").read() == raw     # original kept, EXIF included
    seen = _seen(app)
    assert call(app, "/api/send", {"text": "make it black and white", "images": [d["data_url"]], "image_paths": [d["path"]]})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    assert seen == [f"make it black and white\n[Attached picture saved at: {d['path']}]"]


def test_r406_only_paths_this_server_saved_are_accepted(app, tmp_path):
    other = tmp_path / "x.png"
    other.write_bytes(b"x")
    for bad in ([str(other)], ["/etc/passwd"], "x", [1]):
        assert call(app, "/api/send", {"text": "t", "image_paths": bad})[0] == 400


def test_r406_page_sends_the_paths():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "aurora/web/app.js").read_text()
    assert "image_paths: attached.map(f => f.path)" in js and "image_paths: item.image_paths" in js
