"""R421 in the browser: a chat row's Download asks brief or full, then saves one HTML file (1280 and 360 px)."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)
from tests.test_web_r421 import _chat  # noqa: E402


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r421_download_dialog(browser, app, w, h, mobile, tmp_path):
    _chat(app, tmp_path)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile, accept_downloads=True)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    if mobile and not page.is_visible("#sessions"):
        page.click("#sb-toggle")
    row = page.locator("#sessions .chat").first
    row.wait_for()
    if mobile:
        row.locator(".cm").click()
    else:
        row.hover()
    assert page.evaluate(OVERFLOW) <= 0
    row.locator(".cd").click()
    page.wait_for_selector("#export[open]")
    page.check("#export input[value=full]")
    with page.expect_download() as dl:
        page.click("#ex-yes")
    d = dl.value
    assert d.suggested_filename.startswith("aurora-") and d.suggested_filename.endswith("-full.html")
    text = open(d.path()).read()
    assert "pondering" in text and "is 42" in text
    assert not errors
    ctx.close()
