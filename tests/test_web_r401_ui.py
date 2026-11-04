"""R401 in the browser: the # picker and Notes' Send to chat button attach a note chip; Send carries it. Playwright; skipped where missing."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call, types, wait_for  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)
from tests.test_web_r401 import _seen  # noqa: E402

VIEWS = [(1280, 800, False), (360, 640, True)]


def _open(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)"); page.wait_for_timeout(300)
    return page, errors


@pytest.mark.parametrize("w,h,mobile", VIEWS)
def test_r401_hash_picker_attaches_a_note_and_send_carries_it(browser, app, w, h, mobile):
    seen = _seen(app)
    call(app, "/api/notes/create", {"title": "Deploy rules", "text": "run the tests"})
    call(app, "/api/notes/create", {"title": "Groceries", "text": "milk"})
    page, errors = _open(browser, app, w, h, mobile)
    page.click("#text"); page.keyboard.type("explain #depl")
    page.wait_for_selector("#npick button")
    assert page.locator("#npick button").count() == 1
    assert page.evaluate(OVERFLOW) <= 0                  # no sideways scroll with the picker open
    if mobile:
        page.tap("#npick button")
    else:
        page.keyboard.press("Enter")                    # picks; does not send
    assert page.is_hidden("#npick")
    assert page.input_value("#text") == "explain "
    assert page.locator("#chips .chip.note").inner_text().startswith("📝")
    assert "user" not in types(app)
    page.click("#go")
    assert wait_for(lambda: "done" in types(app), 10)
    assert seen == ["[Note: Deploy rules]\nrun the tests\n[End of note]\n\nexplain"]
    page.wait_for_function("!document.querySelector('#chips .chip.note')")   # once per message
    page.click("#text"); page.keyboard.type("x #zzz"); page.wait_for_selector("#npick p")      # no match is said, not silent
    page.keyboard.press("Escape"); assert page.is_hidden("#npick")
    assert not errors


@pytest.mark.parametrize("w,h,mobile", VIEWS)
def test_r401_notes_send_to_chat_button_attaches_the_open_note(browser, app, w, h, mobile):
    call(app, "/api/notes/create", {"title": "Brief", "text": "context here"})
    page, errors = _open(browser, app, w, h, mobile)
    if mobile:
        page.click("#sb-toggle"); page.wait_for_selector("#notes-btn", state="visible")
    page.click("#notes-btn"); page.wait_for_selector("#note-list .n")
    page.click("#note-list .n"); page.wait_for_selector("#note-send", state="visible")
    page.click("#note-send")
    page.wait_for_selector("#notes", state="hidden")
    assert page.locator("#chips .chip.note span").inner_text() == "Brief"
    page.click("#chips .chip.note button")              # removable before sending
    assert page.locator("#chips .chip.note").count() == 0
    assert not errors
