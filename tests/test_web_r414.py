"""R414: Files (was Uploads) previews pictures, PDFs, markdown, text/code, office files, audio and video."""
import io
import shutil
import zipfile
from pathlib import Path

import pytest

from tests.test_web import app, call  # noqa: F401  (fixture)
from tests.test_web_r410 import _get

WEB = Path(__file__).resolve().parent.parent / "aurora/web"


def test_r414_preview_kinds():
    from aurora.web.attach import preview_kind as k
    assert [k(n) for n in ("a.PNG", "b.pdf", "c.md", "d.docx", "e.mp3", "f.webm", "g.py", "h.csv", "i.svg", "j.html")] == \
        ["image", "pdf", "markdown", "document", "audio", "video", "text", "text", "text", "text"]


def test_r414_text_previews(app):
    from aurora.web import attach
    d = attach.outputs_dir()
    (d / "n.md").write_text("# Title\n\n- a\n")
    (d / "c.py").write_text("print(1)\n")
    (d / "x.svg").write_text("<svg><script>alert(1)</script></svg>")
    (d / "b.bin").write_bytes(b"\0\1\2")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   '<w:body><w:p><w:r><w:t>hello doc</w:t></w:r></w:p></w:body></w:document>')
    (d / "w.docx").write_bytes(buf.getvalue())
    q = lambda n: call(app, f"/api/files/file?kind=outputs&name={n}&text=1")
    assert q("n.md")[1] == {"kind": "markdown", "text": "# Title\n\n- a\n", "cut": False}
    assert q("c.py")[1]["kind"] == "text"
    assert "hello doc" in q("w.docx")[1]["text"]
    assert q("b.bin")[0] == 415
    st, ct, _ = _get(app, "/api/files/file?kind=outputs&name=x.svg&view=1")
    assert ct == "application/octet-stream"                       # svg is never served as a document in this origin
    assert (d / "n.md").read_text() == "# Title\n\n- a\n"           # previews never change the file


def test_r414_pdf_audio_video_served_with_their_type(app):
    from aurora.web import attach
    d = attach.outputs_dir()
    for n in ("s.mp3", "v.mp4", "p.pdf"):
        (d / n).write_bytes(b"x")
    assert _get(app, "/api/files/file?kind=outputs&name=s.mp3&view=1")[1] == "audio/mpeg"
    assert _get(app, "/api/files/file?kind=outputs&name=v.mp4&view=1")[1] == "video/mp4"
    assert _get(app, "/api/files/file?kind=outputs&name=p.pdf&view=1")[1] == "application/pdf"
    assert {u["name"]: u["preview"] for u in call(app, "/api/files?kind=outputs")[1]["files"]}["p.pdf"] == "pdf"


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="poppler not installed")
def test_r414_pdf_thumbnail_is_its_first_page(app):
    from PIL import Image
    from aurora.web import attach
    buf = io.BytesIO(); Image.new("RGB", (200, 280), "white").save(buf, "PDF")
    (attach.outputs_dir() / "doc.pdf").write_bytes(buf.getvalue())
    st, ct, body = _get(app, "/api/files/file?kind=outputs&name=doc.pdf&thumb=1")
    assert st == 200 and ct == "image/png" and max(Image.open(io.BytesIO(body)).size) == 96


def test_r414_page_is_files_with_previews():
    js, html = (WEB / "app.js").read_text(), (WEB / "index_v2.html").read_text()
    assert "</svg> Files</button>" in html and 'id="fl-h">Files<' in html and 'id="preview"' in html
    assert "async function previewFile(kind, name, stepping)" in js and "'&text=1'" in js and "file-card" in js
