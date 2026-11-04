"""R405 in the browser: a text file picked with the paperclip becomes a 📎 chip, the box stays empty, Send carries it."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call, types, wait_for  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)
from tests.test_web_r401 import _seen  # noqa: E402


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r405_attached_file_is_a_chip_and_goes_raw_with_the_message(browser, app, w, h, mobile, tmp_path):
    seen = _seen(app)
    f = tmp_path / "plan.md"; f.write_text("# Plan\n- step one\n")
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    page.set_input_files("form#send input[type=file]", str(f))
    page.wait_for_selector("#chips .chip.file")
    assert page.locator("#chips .chip.file span").inner_text() == "plan.md"
    assert page.input_value("#text") == ""                    # never pasted into the box
    assert page.evaluate(OVERFLOW) <= 0
    page.fill("#text", "do step one"); page.click("#go")
    assert wait_for(lambda: "done" in types(app), 10)
    assert len(seen) == 1 and seen[0].startswith("[File: plan.md]\n# Plan\n- step one\n\n[End of file]\n\ndo step one\n[Attached file saved at: ")   # R409
    page.wait_for_function("!document.querySelector('#chips .chip.file')")   # once per message
    assert page.locator(".msg.user").last.inner_text().startswith("📎 plan.md")
    assert not errors


def _drop(page, selector, name, text):
    page.evaluate("""([sel, name, text]) => {
      const dt = new DataTransfer(); dt.items.add(new File([text], name, { type: 'text/plain' }));
      const el = document.querySelector(sel);
      for (const t of ['dragenter', 'dragover', 'drop']) el.dispatchEvent(new DragEvent(t, { dataTransfer: dt, bubbles: true, cancelable: true }));
    }""", [selector, name, text])


def test_r405_a_file_dropped_anywhere_on_the_page_attaches(browser, app):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    page.evaluate("""() => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'a.txt'));
      document.querySelector('#log').dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true, cancelable: true })); }""")
    assert page.is_visible("#dropall")                           # the overlay says where it will go
    _drop(page, "#log", "notes.txt", "raw\r\ntext\n")
    page.wait_for_selector("#chips .chip.file")
    assert page.is_hidden("#dropall") and page.locator("#chips .chip.file").count() == 1
    _drop(page, "form#send", "two.txt", "b")                      # the message bar still works, and only once
    page.wait_for_function("document.querySelectorAll('#chips .chip.file').length === 2")
    page.wait_for_timeout(300)
    assert page.locator("#chips .chip.file").count() == 2
    assert not errors
