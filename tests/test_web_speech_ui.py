"""R400: the Language setting (English by default) and Read aloud, in the browser. Playwright; skipped where it is missing.
speechSynthesis is replaced by a recorder, so the test does not depend on the voices installed on the machine."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call, types, wait_for  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)
from aurora.providers.base import TurnResult  # noqa: E402
from tests.test_core import FakeProvider  # noqa: E402


class Speaker(FakeProvider):
    """Streams the whole reply, like a real model (the core fake streams only "chunk ")."""
    def turn(self, model, messages, system, tools_, on_text, cancel):
        r = self.results.pop(0); self.calls += 1
        on_text(r.text)
        return r

STUB = """
window.__said = []; window.__cancels = 0;
const fake = { speaking: false, getVoices: () => [{lang: 'en-GB', name: 'E'}, {lang: 'pt-PT', name: 'P'}],
  speak(u) { window.__said.push({text: u.text, lang: u.lang, voice: u.voice && u.voice.name}); setTimeout(() => u.onend && u.onend(), 5); },
  cancel() { window.__cancels++; }, onvoiceschanged: null };
Object.defineProperty(window, 'speechSynthesis', {value: fake});
window.SpeechSynthesisUtterance = function (t) { this.text = t; };
localStorage.setItem('aurora_web_token', 't0k');
"""
REPLY = "Here is the answer. It has two sentences.\n\n```\nrm -rf /tmp/x\n```\n\nVisit https://example.com for more."


def _open(browser, app, w=1280, h=800, mobile=False, extra=""):
    app.state["prov"] = Speaker([TurnResult(text=REPLY, stop_reason="end") for _ in range(4)])
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(STUB + extra)
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)"); page.wait_for_timeout(300)
    return page, errors


def _settings(page):
    if not page.is_visible("#settings-btn"):
        page.click("#sb-toggle")
    page.click("#settings-btn"); page.wait_for_selector("#voice-lang", state="visible")   # R431: Settings panel


def _close(page):
    if page.is_visible("#settings"):
        page.keyboard.press("Escape"); page.wait_for_selector("#settings", state="hidden")


def _turn(page, app, n=1):
    page.fill("#text", "question"); page.click("#go")
    assert wait_for(lambda: types(app).count("done") >= n, 10)
    page.wait_for_selector(f".speak >> nth={n - 1}")


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_language_defaults_to_english_and_the_reply_is_read_in_it(browser, app, w, h, mobile):
    page, errors = _open(browser, app, w, h, mobile)
    assert page.input_value("#voice-lang") == "en-GB"
    assert not page.is_checked("#read-aloud")
    _turn(page, app)
    assert page.evaluate("__said.length") == 0                    # no auto-read unless switched on
    page.click(".speak")
    page.wait_for_function("__said.length > 0")
    said = page.evaluate("__said")
    text = " ".join(s["text"] for s in said)
    assert all(s["lang"] == "en-GB" and s["voice"] == "E" for s in said)
    assert "Here is the answer" in text and "rm -rf" not in text and "https://" not in text and "link" in text
    assert page.evaluate(OVERFLOW) <= 0
    assert not errors, errors


def test_changing_the_language_is_remembered_and_used(browser, app):
    page, errors = _open(browser, app)
    _settings(page); page.select_option("#voice-lang", "pt-PT")
    page.reload(); page.wait_for_selector("#model:not(:empty)")
    assert page.input_value("#voice-lang") == "pt-PT"
    _close(page)
    _turn(page, app)
    page.click(".speak")
    page.wait_for_function("__said.length > 0")
    assert {s["lang"] for s in page.evaluate("__said")} == {"pt-PT"}
    assert not errors, errors


def test_read_aloud_switch_speaks_new_replies_only(browser, app):
    page, errors = _open(browser, app)
    _settings(page); page.check("#read-aloud"); _close(page)
    _turn(page, app)
    page.wait_for_function("__said.length > 0")
    n = page.evaluate("__said.length")
    page.reload(); page.wait_for_selector("#model:not(:empty)"); page.wait_for_timeout(800)
    assert page.evaluate("__said.length") == 0                    # a replayed log is not read again
    assert page.is_checked("#read-aloud") and n > 0
    assert not errors, errors


def test_tapping_again_stops(browser, app):
    page, errors = _open(browser, app, extra="window.speechSynthesis.speak = function (u) { window.__said.push({text: u.text}); };")
    _turn(page, app)
    page.click(".speak")
    assert page.get_attribute(".speak", "aria-pressed") == "true"
    page.click(".speak")
    assert page.get_attribute(".speak", "aria-pressed") == "false" and page.evaluate("__cancels") >= 2
    assert not errors, errors


def test_the_microphone_sends_the_language(browser, app, monkeypatch):
    monkeypatch.setenv("AURORA_WEB_STT_URL", "http://stt.invalid")
    page, errors = _open(browser, app)
    _settings(page); page.select_option("#voice-lang", "de-DE")
    seen = []
    page.route("**/api/transcribe*", lambda r: (seen.append(r.request.url), r.fulfill(status=200, body='{"text":"hallo"}', content_type="application/json")))
    page.evaluate("fetch('/api/transcribe?lang=' + encodeURIComponent(voiceLang().split('-')[0]), {method: 'POST', body: 'x', headers: hdr()})")
    page.wait_for_timeout(300)
    assert seen and seen[0].endswith("/api/transcribe?lang=de")
    assert not errors, errors
