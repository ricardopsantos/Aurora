"""R436/R437 in the browser (1280 and 360 px): the reply's action row, Notes opening on its first note, quick tab clicks."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)
from tests.test_web_r421 import _chat


def _open(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)"); page.wait_for_timeout(300)
    return ctx, page, errors


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r436_reply_actions_row(browser, app, w, h, mobile, tmp_path):
    _chat(app, tmp_path)
    ctx, page, errors = _open(browser, app, w, h, mobile)
    page.wait_for_selector("#log .acts .btn.act")
    labels = page.locator("#log .acts .btn").evaluate_all("bs => bs.map(b => b.getAttribute('aria-label'))")
    assert labels[:2] == ["Ask again for a new reply to your last message", "Change your last message and send it again"]
    if page.evaluate("'speechSynthesis' in window"):
        assert page.locator("#log .acts .speak").count() == 1 and page.locator("#log > .speak").count() == 0
    assert page.locator("#log .acts svg").count() == page.locator("#log .acts .btn").count()
    assert page.locator("#log .acts .btn span").first.is_visible() is (not mobile)    # a phone shows the icons only
    assert page.evaluate(OVERFLOW) <= 0 and not errors
    ctx.close()


def test_r437_notes_open_on_the_first_note_and_quick_tabs_stay_in_order(browser, app):
    call(app, "/api/notes/create", {"title": "p1", "text": "first", "type": "personal"})
    ctx, page, errors = _open(browser, app, 1280, 800, False)
    page.click("#notes-btn")
    page.click("#note-tabs .tab[data-type=work]"); page.click("#note-tabs .tab[data-type=skills]")   # at once, while p1 loads
    page.wait_for_function("document.getElementById('note-type').value === 'skills' && !document.getElementById('note-edit').hidden")
    page.wait_for_timeout(500)
    notes = call(app, "/api/notes")[1]["notes"]
    assert sorted((n["title"], n["type"]) for n in notes) == [("", "skills"), ("p1", "personal")]   # p1 untouched, one blank
    page.click("#note-tabs .tab[data-type=personal]")
    page.wait_for_function("document.getElementById('note-title').value === 'p1'")
    assert [n["title"] for n in call(app, "/api/notes")[1]["notes"]] == ["p1"]
    assert not errors
    ctx.close()
