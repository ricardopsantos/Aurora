"""R402: the sidebar Chats list folds on a tap of its header, remembered across reloads; a search unfolds it. Playwright."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call, types, wait_for  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r402_chats_section_folds_and_remembers(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem(\"aurora_web_token\", \"t0k\");")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    if mobile:
        page.click("#sb-toggle")
    page.wait_for_selector("#chats-cap", state="visible")
    assert page.is_visible("#sessions") and page.get_attribute("#chats-cap", "aria-expanded") == "true"
    page.click("#chats-cap")
    assert page.is_hidden("#sessions") and page.get_attribute("#chats-cap", "aria-expanded") == "false"
    page.reload(); page.wait_for_selector("#model:not(:empty)")
    if mobile:
        page.click("#sb-toggle")
    page.wait_for_selector("#chats-cap", state="visible")
    assert page.is_hidden("#sessions")                       # remembered
    page.fill("#search", "x")
    assert page.is_visible("#sessions")                      # a search shows its results
    page.fill("#search", ""); page.click("#chats-cap"); page.click("#chats-cap")
    page.focus("#chats-cap"); page.keyboard.press("Enter")
    assert page.get_attribute("#chats-cap", "aria-expanded") == "false"
    assert page.evaluate(OVERFLOW) <= 0
    assert not errors
