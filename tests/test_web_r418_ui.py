"""R418 in the browser: Notes and Files take the chat's place, New Chat reads Back to chat, leaving saves the note."""
import os

import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)


def _open(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    return ctx, page, errors


def _side(page, mobile, btn):
    if mobile and not page.is_visible(btn):
        page.click("#sb-toggle")
    page.click(btn)


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r418_notes_and_files_fill_the_chat_area(browser, app, w, h, mobile):
    ctx, page, errors = _open(browser, app, w, h, mobile)
    shots = os.environ.get("AURORA_SHOTS")
    _side(page, mobile, "#notes-btn")
    page.wait_for_selector("#notes.panel[open]")
    assert not page.is_visible("#log") and not page.is_visible("#send")
    assert page.evaluate("getComputedStyle(document.getElementById('notes')).position") == "static"
    assert page.evaluate("document.querySelector('.main').contains(document.getElementById('notes'))")
    assert "Back to chat" in page.locator("#new-chat").inner_text()
    assert page.locator("#chat-title").inner_text() == "Notes"
    page.click("#note-new"); page.wait_for_selector("#note-edit:not([hidden])")
    page.fill("#note-title", "Kept"); page.fill("#note-text", "typed just before leaving")
    page.wait_for_timeout(300)
    assert page.locator("#chat-title").inner_text() == "Notes"                # a chat-list refresh must not take it back
    assert page.evaluate(OVERFLOW) <= 0
    if shots:
        page.screenshot(path=f"{shots}/r418-notes-{w}.png")
    _side(page, mobile, "#new-chat")                                     # Back to chat: saves first
    page.wait_for_selector("#log", state="visible")
    assert not page.is_visible("#notes")
    assert "New Chat" in page.locator("#new-chat").inner_text()
    notes = call(app, "/api/notes")[1]["notes"]
    assert any(n["title"] == "Kept" for n in notes)
    nid = next(n["id"] for n in notes if n["title"] == "Kept")
    assert call(app, "/api/notes/" + nid)[1]["text"] == "typed just before leaving"

    _side(page, mobile, "#files-btn")
    page.wait_for_selector("#files.panel[open]")
    assert not page.is_visible("#log") and "Back to chat" in page.locator("#new-chat").inner_text()
    assert page.evaluate(OVERFLOW) <= 0
    if shots:
        page.screenshot(path=f"{shots}/r418-files-{w}.png")
    _side(page, mobile, "#notes-btn")                                   # Files -> Notes swaps the panel
    page.wait_for_selector("#notes.panel[open]")
    assert not page.is_visible("#files")
    _side(page, mobile, "#new-chat")
    page.wait_for_selector("#log", state="visible")
    assert errors == []
    ctx.close()


def test_r418_queue_row_has_a_remove_button_and_turn_line_shows(browser, app):
    ctx, page, errors = _open(browser, app, 1280, 800, False)
    page.fill("#text", "hello"); page.click("#go")
    page.wait_for_selector(".msg.reply")
    page.wait_for_selector(".acts")
    assert page.locator(".acts").inner_text().count("Save as note") == 1
    page.click(".acts >> text=Save as note"); page.wait_for_selector(".acts >> text=Saved")
    assert any(n.get("title") for n in call(app, "/api/notes")[1]["notes"])
    page.evaluate("handle({id: 99990, type: 'queued', text: 'waiting one', position: 1, t: 0})")
    assert page.is_visible("#queue .q-item .qx")
    page.evaluate("handle({id: 99991, type: 'queue_cancel', text: 'waiting one', position: 1, t: 0})")
    assert page.is_hidden("#queue")
    assert errors == []
    ctx.close()
