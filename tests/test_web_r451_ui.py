"""R451 in the browser: a phone's closed sidebar is closed from the first paint (no slide on load)."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser  # noqa: E402,F401  (fixture)


def test_r451_phone_sidebar_never_slides_on_load(browser, app):
    ctx = browser.new_context(viewport={"width": 360, "height": 640}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    # record every margin the sidebar has on its way to being closed: with no slide there is only the final one
    page.add_init_script("""window.__m = [];
      (function tick() { const a = document.getElementById('side'); if (a) window.__m.push(getComputedStyle(a).marginLeft);
        if (window.__m.length < 40) requestAnimationFrame(tick); })();""")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    page.wait_for_function("!document.body.classList.contains('sb-init')")
    assert not page.is_visible("#files-btn")                       # closed, so Playwright's visibility check agrees at once
    assert set(page.evaluate("window.__m")) <= {"0px", "-340px"}, page.evaluate("window.__m")   # a jump from the parse-time 0 to closed, never a slide
    page.click("#sb-toggle")                                       # and opening still slides (the class is gone again)
    page.wait_for_selector("#files-btn", state="visible")
    ctx.close()


def test_r451_toggle_still_animates_after_load():
    css = open("aurora/web/index_v2.html").read()
    assert "body.sb-init #side" in css and "transition:margin-left .22s ease" in css
