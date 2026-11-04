"""R397: the model menu shows OpenRouter prices and a Refresh prices button (Playwright; skipped where it is missing)."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call, types, wait_for  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)
from tests.test_web_r397 import prices, A  # noqa: E402,F401  (fixture)


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_menu_shows_prices_and_refreshes(browser, app, prices, monkeypatch, w, h, mobile):
    monkeypatch.setattr(app.e, "_has_key", lambda p: True)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k')")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)"); page.wait_for_timeout(300)
    page.click("#model")
    page.wait_for_selector("#model-menu")
    txt = page.text_content("#model-menu")
    assert "$1 / $2 per M" in txt and "Refresh prices" in txt and "days ago" in txt
    page.click("#prices-refresh")
    page.wait_for_function("document.querySelector('#model-menu') && document.querySelector('#model-menu').textContent.includes('$1.5 / $2 per M')")
    assert "just now" in page.text_content("#prices-refresh")
    assert page.evaluate(OVERFLOW) <= 0
    assert page.evaluate("(() => { const r = document.querySelector('#model-menu').getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })()")
    page.wait_for_selector("text=prices refreshed")
    assert not errors, errors
