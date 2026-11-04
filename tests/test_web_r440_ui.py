"""R440: Notes Edit continues lists on Enter and indents them with Tab; the note list previews drop the Markdown marks."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser  # noqa: E402,F401  (fixture)


def test_r440_list_keys_and_plain_previews(browser, app):
    call(app, "/api/notes/create", {"title": "Shop", "text": "- [ ] milk **now**\n- [x] eggs", "type": "personal"})
    page = browser.new_page(viewport={"width": 1280, "height": 800}); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    page.click("#notes-btn"); page.wait_for_selector("#note-list .n")
    assert "☐ milk now ☑ eggs" in page.locator("#note-list .n").first.inner_text()
    page.click("#note-new"); page.wait_for_selector("#note-edit:not([hidden])")
    t = page.locator("#note-text"); t.click()
    page.keyboard.type("- [x] one"); page.keyboard.press("Enter"); page.keyboard.type("two")
    page.keyboard.press("Tab"); page.keyboard.press("Enter"); page.keyboard.type("three")
    page.keyboard.press("Shift+Tab"); page.keyboard.press("Enter"); page.keyboard.press("Enter"); page.keyboard.type("after")
    page.keyboard.press("Enter"); page.keyboard.type("1. a"); page.keyboard.press("Enter"); page.keyboard.type("b")
    assert t.input_value() == "- [x] one\n  - [ ] two\n- [ ] three\nafter\n1. a\n2. b"   # Enter on an empty item clears it, the line stays
    assert not errors
    page.close()


def test_r440_md_plain(browser, app):
    page = browser.new_page(); page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    got = page.evaluate("""() => [mdPlain('# Title > quote 1. one - two [link](https://x.y) `code` ~~old~~ *it* snake_case_x'), mdPlain('')]""")
    assert got == ["Title quote one two link code old it snake_case_x", ""]
    page.close()
