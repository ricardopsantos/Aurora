"""R404 in the browser: drag a note by its grip (mouse on a desktop, touch on a phone) or Alt+arrows. Playwright."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call, wait_for  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)


def _open(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    if mobile:
        page.click("#sb-toggle")
    page.click("#notes-btn"); page.wait_for_selector("#note-list .n >> nth=2")
    return page, errors


def _server(app):
    return [n["title"] for n in call(app, "/api/notes")[1]["notes"]]


def _titles(page):
    return page.eval_on_selector_all("#note-list .n b", "els => els.map(e => e.textContent)")


def _drag(page, mobile, src, dst):
    g = page.locator("#note-list .n .grip").nth(src).bounding_box()
    t = page.locator("#note-list .n").nth(dst).bounding_box()
    x, y0, y1 = g["x"] + g["width"] / 2, g["y"] + g["height"] / 2, t["y"] + t["height"] - 4
    if not mobile:
        page.mouse.move(x, y0); page.mouse.down()
        for k in range(1, 11):
            page.mouse.move(x, y0 + (y1 - y0) * k / 10)
        page.mouse.up()
        return
    cdp = page.context.new_cdp_session(page)
    pt = lambda y: [{"x": x, "y": y}]
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pt(y0)})
    for k in range(1, 11):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": pt(y0 + (y1 - y0) * k / 10)})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r404_drag_a_note_down_and_the_order_is_saved(browser, app, w, h, mobile):
    for t in ("C", "B", "A"):
        call(app, "/api/notes/create", {"title": t, "text": t})
    page, errors = _open(browser, app, w, h, mobile)
    assert _titles(page) == ["A", "B", "C"]
    assert page.evaluate(OVERFLOW) <= 0
    _drag(page, mobile, 0, 2)                                  # A to the bottom
    assert wait_for(lambda: _server(app) == ["B", "C", "A"], 5), _server(app)
    assert _titles(page) == ["B", "C", "A"]
    assert page.is_hidden("#note-edit") or not mobile          # a drag is not a tap: the note did not open on a phone
    if not mobile:
        page.focus("#note-list .n >> nth=2"); page.keyboard.press("Alt+ArrowUp")
        assert wait_for(lambda: _server(app) == ["B", "A", "C"], 5)
    assert not errors
