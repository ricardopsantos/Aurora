"""R403: notes are Markdown; Edit shows the source, View the formatted note, and a task box ticked in View saves. Playwright."""
import json
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call, types, wait_for  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)

SRC = "# Plan\n\nSome **bold** and `code`.\n\n- [ ] buy milk\n- [x] call Ana\n- plain item\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\nrm -rf x\n```"


def _open(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    if mobile:
        page.click("#sb-toggle")
    page.click("#notes-btn"); page.wait_for_selector("#note-list")
    return page, errors


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r403_note_opens_in_view_renders_markdown_and_task_ticks_save(browser, app, w, h, mobile):
    nid = call(app, "/api/notes/create", {"title": "Plan", "text": SRC})[1]["id"]
    page, errors = _open(browser, app, w, h, mobile)
    page.click("#note-list .n"); page.wait_for_selector("#note-view", state="visible")
    assert page.is_hidden("#note-text") and page.inner_text("#note-mode") == "Edit"
    v = page.locator("#note-view")
    assert v.locator("h1").inner_text() == "Plan" and v.locator("strong").inner_text() == "bold"
    assert v.locator("table td").count() == 2 and v.locator("pre code").inner_text() == "rm -rf x"
    assert v.locator("script").count() == 0
    boxes = v.locator("li.task input")
    assert boxes.count() == 2 and not boxes.nth(0).is_checked() and boxes.nth(1).is_checked()
    assert page.evaluate(OVERFLOW) <= 0
    boxes.nth(0).click()
    assert wait_for(lambda: "- [x] buy milk" in call(app, f"/api/notes/{nid}")[1]["text"], 5)
    assert page.locator("#note-view li.task input").nth(0).is_checked()
    page.click("#note-mode")                                  # back to the source
    assert page.is_visible("#note-text") and page.is_hidden("#note-view") and page.inner_text("#note-mode") == "View"
    assert "- [x] buy milk" in page.input_value("#note-text")
    page.fill("#note-text", "## Changed"); page.click("#note-mode")
    assert page.locator("#note-view h2").inner_text() == "Changed"
    assert not errors


def test_r403_empty_or_new_note_opens_in_edit_and_ctrl_e_toggles(browser, app):
    page, errors = _open(browser, app, 1280, 800, False)
    page.click("#note-new"); page.wait_for_selector("#note-text", state="visible")
    assert page.is_hidden("#note-view") and page.inner_text("#note-mode") == "View"
    page.click("#note-text"); page.keyboard.type("- [ ] one")
    page.keyboard.press("Control+e")
    assert page.is_visible("#note-view") and page.locator("#note-view li.task").count() == 1
    page.keyboard.press("Control+e")
    assert page.is_visible("#note-text")
    assert not errors


def test_r403_chat_replies_show_task_boxes_read_only():
    import pathlib
    from aurora.web import server as web_server
    js = (pathlib.Path(web_server.__file__).parent / "app.js").read_text()
    assert "if (opts && opts.task) cb.onchange" in js and "else cb.disabled = true" in js
