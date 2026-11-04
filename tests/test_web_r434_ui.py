"""R434: the Edit models line numbers run the whole height of the editor and stay level with the text when it scrolls."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser  # noqa: E402,F401  (fixture)


@pytest.mark.parametrize("w,h,mobile", [(1280, 1300, False), (360, 640, True)])
def test_r434_gutter_fills_the_editor_and_follows_the_scroll(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    page.click("#model"); page.click("#models-edit"); page.wait_for_function("document.getElementById('cfg-text').value.length > 0")
    page.evaluate("() => { const t = document.getElementById('cfg-text'); t.value = Array.from({length: 120}, (_, i) => 'k' + (i + 1) + ': v').join('\\n'); t.dispatchEvent(new Event('input')); }")
    g, t = page.evaluate("['cfg-gutter','cfg-text'].map(i => { const r = document.getElementById(i).getBoundingClientRect(); return [r.top, r.bottom]; })")
    assert abs(g[0] - t[0]) <= 1 and abs(g[1] - t[1]) <= 1, (g, t)          # same height as the text box
    page.evaluate("document.getElementById('cfg-text').scrollTop = 100000; document.getElementById('cfg-text').dispatchEvent(new Event('scroll'))")
    off = page.evaluate("(() => { const g = document.getElementById('cfg-gutter'), t = document.getElementById('cfg-text'); return [g.scrollTop, t.scrollTop, g.scrollHeight - g.clientHeight, t.scrollHeight - t.clientHeight]; })()")
    assert abs(off[0] - off[1]) <= 1 and abs(off[2] - off[3]) <= 2, off      # the last number sits beside the last line
    ctx.close()
