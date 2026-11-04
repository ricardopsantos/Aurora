"""R422 in the browser: Personal / Work / Skills tabs, a new note takes the tab's type, the select moves it (1280, 360 px)."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r422_note_tabs(browser, app, w, h, mobile):
    call(app, "/api/notes/create", {"title": "Mum's number", "text": "123"})
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    if mobile and not page.is_visible("#notes-btn"):
        page.click("#sb-toggle")
    page.click("#notes-btn"); page.wait_for_selector("#note-list .n")
    assert page.locator("#note-tabs .tab[aria-selected=true]").inner_text() == "Personal"
    page.click("#note-tabs .tab[data-type=skills]"); page.wait_for_function("document.getElementById('note-type').value === 'skills' && !document.getElementById('note-edit').hidden")   # R433: an empty tab opens a new note
    page.fill("#note-title", "Send mail"); page.wait_for_timeout(1000)
    assert page.evaluate(OVERFLOW) <= 0
    assert page.locator("#note-type").input_value() == "skills"
    page.select_option("#note-type", "work")
    page.wait_for_selector("#note-tabs .tab[data-type=work][aria-selected=true]", state="attached")
    page.click("#note-back") if page.is_visible("#note-back") else None
    page.fill("#note-search", "number")                                  # a search looks in every type
    page.wait_for_selector("#note-list .n:has-text(\"Mum's number\")", state="attached")
    assert "Personal" in page.locator("#note-list .n").first.inner_text()
    notes = {n["title"]: n["type"] for n in call(app, "/api/notes")[1]["notes"]}
    assert notes == {"Mum's number": "personal", "Send mail": "work"}
    assert not errors
    ctx.close()
