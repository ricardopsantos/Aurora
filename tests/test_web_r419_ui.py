"""R419 in the browser: the file preview has Prev/Next (buttons and arrow keys, wrapping), and Esc steps back one layer."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r419_prev_next_wrap_and_esc_chain(browser, app, w, h, mobile):
    from aurora.web import attach
    d = attach.outputs_dir()
    for n in ("a.txt", "b.txt", "c.txt"):
        (d / n).write_text("file " + n)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    if mobile and not page.is_visible("#files-btn"):
        page.click("#sb-toggle")
    page.click("#files-btn"); page.click("#fl-tabs .tab[data-kind=outputs]")
    page.wait_for_selector("#fl-list .fl-row >> nth=2")
    order = [t.strip() for t in page.locator("#fl-list .fl-name").all_inner_texts()]
    page.click(f".fl-row:has-text('{order[0]}') .fl-open")
    page.wait_for_selector("#pv-next:visible")
    body = lambda: page.locator("#pv-body pre").inner_text()
    page.wait_for_function("document.querySelector('#pv-body pre')")
    assert body() == "file " + order[0]
    assert page.evaluate(OVERFLOW) <= 0
    page.click("#pv-next"); page.wait_for_function(f"document.getElementById('pv-h').textContent === '{order[1]}'")
    page.wait_for_function(f"(document.querySelector('#pv-body pre')||{{}}).textContent === 'file {order[1]}'")
    page.click("#pv-prev"); page.click("#pv-prev")                       # wraps from the first to the last
    page.wait_for_function(f"document.getElementById('pv-h').textContent === '{order[2]}'")
    page.keyboard.press("ArrowRight")                                    # and from the last to the first
    page.wait_for_function(f"document.getElementById('pv-h').textContent === '{order[0]}'")
    page.keyboard.press("ArrowLeft")
    page.wait_for_function(f"document.getElementById('pv-h').textContent === '{order[2]}'")
    page.keyboard.press("Escape")                                        # 1st Esc: the preview only
    page.wait_for_selector("#preview", state="hidden")
    assert page.is_visible("#files") and "Back to chat" in page.locator("#new-chat").inner_text()
    page.keyboard.press("Escape")                                        # 2nd Esc: back to the chat
    page.wait_for_selector("#log", state="visible")
    assert "New Chat" in page.locator("#new-chat").inner_text()
    if mobile and not page.is_visible("#notes-btn"):
        page.click("#sb-toggle")
    page.click("#notes-btn"); page.wait_for_selector("#notes.panel[open]")
    page.click("#note-new"); page.wait_for_selector("#note-edit:not([hidden])")
    page.fill("#note-title", "EscKept"); page.focus("#note-text")
    page.keyboard.press("Escape")                                        # Esc in Notes: saves and goes back
    page.wait_for_selector("#log", state="visible")
    from tests.test_web import call
    assert any(n["title"] == "EscKept" for n in call(app, "/api/notes")[1]["notes"])
    assert not errors
    ctx.close()
