"""R396: offline support, server side: an outbox resend never runs a message twice; the PWA files are served."""
import json
import re
import urllib.request

from aurora.web import server as web_server
from tests.test_web import app, call, types, wait_for  # noqa: F401  (fixture)


def test_same_client_id_is_accepted_once(app):
    code, d = call(app, "/api/send", {"text": "only once", "client_id": "abc-123"})
    assert code == 200 and d == {"ok": True}
    assert wait_for(lambda: "done" in types(app))
    code, d = call(app, "/api/send", {"text": "only once", "client_id": "abc-123"})
    assert code == 200 and d.get("duplicate") is True
    assert types(app).count("user") == 1


def test_different_ids_and_no_id_both_send(app):
    for body in ({"text": "a", "client_id": "id-1"}, {"text": "b", "client_id": "id-2"}, {"text": "c"}, {"text": "c"}):
        assert call(app, "/api/send", body)[0] == 200
    assert wait_for(lambda: types(app).count("done") == 4, 10)
    assert types(app).count("user") == 4


def test_a_refused_message_does_not_burn_its_id(app):
    assert call(app, "/api/send", {"text": "/help", "client_id": "same"})[0] == 400   # commands are refused
    code, d = call(app, "/api/send", {"text": "real text", "client_id": "same"})
    assert code == 200 and not d.get("duplicate")


def test_bad_client_ids_are_ignored_not_trusted(app):
    for cid in (123, "", "x" * 65, ["a"]):
        assert call(app, "/api/send", {"text": "hi", "client_id": cid})[0] == 200
    assert wait_for(lambda: types(app).count("done") == 4, 10)


def test_remembered_ids_are_capped(app, monkeypatch):
    from aurora.web import core
    monkeypatch.setattr(core, "MAX_SENT_IDS", 3)
    for i in range(6):
        app.s._sent_ids[f"x{i}"] = None
    call(app, "/api/send", {"text": "t", "client_id": "new"})
    assert len(app.s._sent_ids) == 3 and "new" in app.s._sent_ids and "x0" not in app.s._sent_ids


def _get(app, path):
    with urllib.request.urlopen(app.base + path, timeout=5) as r:
        return r.status, dict(r.headers), r.read()


def test_service_worker_manifest_and_icon_are_served_without_a_token(app):
    code, h, body = _get(app, "/sw.js")
    assert code == 200 and h["Content-Type"].startswith("text/javascript") and h["Cache-Control"] == "no-store"
    assert b"/api/stream" not in re.sub(rb"//.*", b"", body).split(b"READ_ONLY")[1].split(b"]")[0]
    code, h, body = _get(app, "/manifest.webmanifest")
    m = json.loads(body)
    assert code == 200 and m["start_url"] == "/" and m["display"] == "standalone" and m["icons"][0]["src"] == "/icon-192.png"


def test_every_page_links_the_manifest_and_the_package_ships_the_files():
    for page in (web_server.PAGE, web_server.PAGE_PAPER, web_server.PAGE_V2):
        assert b'<link rel="manifest" href="/manifest.webmanifest">' in page.read_bytes()
    import pathlib
    py = (pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    for f in ("sw.js", "manifest.webmanifest", "favicon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        assert f'"{f}"' in py


def test_service_worker_never_caches_live_or_mutating_requests():
    sw = web_server.SW.read_text()
    assert "req.method !== 'GET'" in sw
    ro = sw.split("const READ_ONLY = [")[1].split("];")[0]
    for live in ("stream", "state", "approve", "send", "usage"):
        assert live not in ro, live


def test_r398_favicon_is_the_sidebar_logo(app):
    """R398: every page's tab icon is /favicon.png, a square PNG of the sidebar's browser-tab logo (the WebP in index_v2)."""
    import base64
    import io
    from PIL import Image
    for page in (web_server.PAGE, web_server.PAGE_PAPER, web_server.PAGE_V2):
        html = page.read_text()
        assert html.count('rel="icon"') == 1 and '<link rel="icon" type="image/png" href="/favicon.png">' in html
    for path in ("/favicon.png", "/favicon.ico"):
        code, h, body = _get(app, path)
        assert code == 200 and h["Content-Type"] == "image/png" and "max-age" in h["Cache-Control"]
    fav = Image.open(io.BytesIO(body)).convert("RGB")
    assert fav.width == fav.height
    logo = re.search(r"data:image/webp;base64,([A-Za-z0-9+/=]+)", web_server.PAGE_V2.read_text()).group(1)
    src = Image.open(io.BytesIO(base64.b64decode(logo))).convert("RGB")
    ox, oy = (fav.width - src.width) // 2, (fav.height - src.height) // 2
    assert fav.crop((ox, oy, ox + src.width, oy + src.height)).tobytes() == src.tobytes()   # same pixels, only padded


def test_r399_home_screen_icons_are_the_logo(app):
    """R399: the manifest and apple-touch-icon use the sidebar logo (images/base_logo.png) at real sizes, on its own background."""
    import io
    import pathlib
    from PIL import Image
    m = json.loads(_get(app, "/manifest.webmanifest")[2])
    icons = {(i["src"], i["sizes"], i["purpose"]) for i in m["icons"]}
    assert icons == {("/icon-192.png", "192x192", "any"), ("/icon-512.png", "512x512", "any"),
                     ("/icon-maskable-512.png", "512x512", "maskable")}
    assert m["background_color"] == m["theme_color"] == "#0d0d0e"
    for page in (web_server.PAGE, web_server.PAGE_PAPER, web_server.PAGE_V2):
        assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in page.read_text()
    src = Image.open(pathlib.Path(__file__).resolve().parent.parent / "images" / "base_logo.png").convert("RGB")
    bg = src.getpixel((2, 2))
    for path, n in (("/icon-192.png", 192), ("/icon-512.png", 512), ("/icon-maskable-512.png", 512), ("/apple-touch-icon.png", 180)):
        code, h, body = _get(app, path)
        assert code == 200 and h["Content-Type"] == "image/png", path
        im = Image.open(io.BytesIO(body)).convert("RGB")
        assert im.size == (n, n), path
        assert all(max(abs(a - b) for a, b in zip(im.getpixel(c), bg)) <= 2 for c in ((0, 0), (n - 1, 0), (0, n - 1), (n - 1, n - 1))), path   # no box
        light = sum(1 for p in im.getdata() if sum(p) > 450) / (n * n)
        assert 0.02 < light < 0.3, path                                  # the cream line art is there
    mask = Image.open(io.BytesIO(_get(app, "/icon-maskable-512.png")[2])).convert("L")
    r = 512 * 0.4                                                        # Android's safe zone: a centred circle, 80 % across
    outside = [mask.getpixel((x, y)) for x in range(0, 512, 4) for y in range(0, 512, 4) if (x - 256) ** 2 + (y - 256) ** 2 > r * r]
    assert max(outside) < 120                                            # nothing of the face is cut off by the mask
    assert _get_status(app, "/icon.svg") == 404


def _get_status(app, path):
    import urllib.error
    try:
        return _get(app, path)[0]
    except urllib.error.HTTPError as e:
        return e.code
