"""R400: one Language setting (English by default) for the microphone and for reading replies aloud. Server side here."""
import json
import shutil
import urllib.request

import pytest

from aurora.web import server as web_server
from tests.test_web import TOKEN, app, call, types, wait_for  # noqa: F401  (fixture)


@pytest.fixture
def whisper(app, monkeypatch):
    """A fake whisper behind AURORA_WEB_STT_URL that records which language it was asked for."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg needed")
    monkeypatch.setenv("AURORA_WEB_STT_URL", "http://stt.invalid")
    seen = []
    real = urllib.request.urlopen

    class R:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=None):
        if isinstance(req, urllib.request.Request) and req.full_url.startswith("http://stt.invalid"):
            body = req.data
            lang = body.split(b'name="language"\r\n\r\n')[1].split(b"\r\n")[0].decode()
            seen.append(lang)
            return R(json.dumps({"text": f"said in {lang}"}).encode())
        return real(req, timeout=timeout)
    monkeypatch.setattr(web_server.urllib.request, "urlopen", fake)
    # R533: the server reaches the speech server through build_opener(ProxyHandler({})).open, not urlopen
    real_build = urllib.request.build_opener

    def build(*handlers):
        opener = real_build(*handlers)
        real_open = opener.open
        opener.open = lambda req, data=None, timeout=None: (
            fake(req, timeout) if isinstance(req, urllib.request.Request) and req.full_url.startswith("http://stt.invalid")
            else real_open(req, data, timeout) if timeout is not None else real_open(req, data))
        return opener
    monkeypatch.setattr(web_server.urllib.request, "build_opener", build)
    return seen


def _wav(tmp_path):
    import subprocess
    p = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=0.3", str(p)], check=True)
    return p.read_bytes()


def _post(app, path, data):
    req = urllib.request.Request(app.base + path, data=data, method="POST", headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read())


def test_voice_input_is_english_by_default(app, whisper, tmp_path):
    code, d = _post(app, "/api/transcribe", _wav(tmp_path))
    assert code == 200 and whisper == ["en"] and d["text"] == "said in en"


@pytest.mark.parametrize("q,want", [("pt", "pt"), ("PT", "pt"), ("de", "de"), ("xx", "en"), ("en%0Ainjected", "en"), ("", "en")])
def test_voice_input_uses_the_page_language_from_an_allowlist(app, whisper, tmp_path, q, want):
    _post(app, "/api/transcribe?lang=" + q, _wav(tmp_path))
    assert whisper == [want]


def test_page_script_wires_the_setting():
    js = web_server.SCRIPT.read_text()
    assert "'/api/transcribe?lang=' + encodeURIComponent(voiceLang().split('-')[0])" in js
    assert "return 'en-GB';" in js                                      # the default when nothing is chosen
    offered = {v.split("-")[0] for v in __import__("re").findall(r"\['([a-z]{2}-[A-Z]{2})',", js)}
    assert offered == web_server.STT_LANGS                               # the page offers exactly what the server accepts
