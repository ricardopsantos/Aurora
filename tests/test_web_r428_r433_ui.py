"""R428-R433 in the browser (1280 and 360 px): a long title leaves the pills whole, Edit models from the model menu,
the Settings panel, the Input tab, note tabs that open a note and drop empty ones."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)

LONG = "Filesystem Size Used Avail Use% Mounted on udev 16G 0 16G 0% /dev tmpfs 3.2G 2.6M 3.2G 1% /run " * 3


def _open(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)"); page.wait_for_timeout(300)
    return ctx, page, errors


def _side(page, sel):
    page.wait_for_function("document.getAnimations().length === 0")   # R451: let the sidebar finish sliding before asking if it is visible
    if not page.is_visible(sel):
        page.click("#sb-toggle")
    page.click(sel)


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r428_long_title_leaves_the_pills_whole(browser, app, w, h, mobile):
    ctx, page, errors = _open(browser, app, w, h, mobile)
    page.evaluate("t => { document.getElementById('chat-title').textContent = t; document.getElementById('ctx-txt').textContent = '1.9k / 66k · 3%'; }", LONG)
    page.wait_for_timeout(100)
    assert page.evaluate(OVERFLOW) <= 0
    for sel in ("#ctx", "#status") if not mobile else ("#ctx",):
        cut = page.evaluate("s => { const e = document.querySelector(s), b = e.getBoundingClientRect(); return [e.scrollWidth - e.clientWidth, b.right] }", sel)
        assert cut[0] <= 1 and cut[1] <= w, (sel, cut)
    assert page.evaluate("(() => { const e = document.getElementById('chat-title'); return e.scrollWidth > e.clientWidth })()")   # the title is the one cut
    assert not errors
    ctx.close()


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r430_r431_r432_menu_settings_and_input(browser, app, w, h, mobile):
    ctx, page, errors = _open(browser, app, w, h, mobile)
    page.click("#model"); page.wait_for_selector("#models-edit")
    page.click("#models-edit"); page.wait_for_function("document.getElementById('cfg-text').value.includes('providers:')")
    assert page.locator("#md-tabs .tab[aria-selected=true]").inner_text() == "config.yaml"
    page.click("#md-close"); page.wait_for_selector("#models", state="hidden")
    _side(page, "#settings-btn"); page.wait_for_selector("#settings.panel[open]")
    assert page.inner_text("#chat-title") == "Settings" and page.is_visible("#theme-sel") and page.is_visible("#token")
    assert page.evaluate(OVERFLOW) <= 0
    assert page.evaluate("document.querySelector('#settings .set-col').getBoundingClientRect().width") <= 560.5
    page.keyboard.press("Escape"); page.wait_for_selector("#settings", state="hidden")
    _side(page, "#files-btn"); page.wait_for_selector("#files.panel[open]")
    assert page.locator("#fl-tabs .tab[aria-selected=true]").inner_text() == "Input"
    page.wait_for_selector("#fl-list .empty"); assert page.inner_text("#fl-list") == "No input files."
    assert not errors
    ctx.close()


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r433_tabs_open_the_first_note_or_a_new_one(browser, app, w, h, mobile):
    call(app, "/api/notes/create", {"title": "p1", "text": "first", "type": "personal"})
    ctx, page, errors = _open(browser, app, w, h, mobile)
    _side(page, "#notes-btn"); page.wait_for_selector("#note-list .n")
    tab = lambda t: page.click(f"#note-tabs .tab[data-type={t}]")
    back = lambda: page.is_visible("#note-back") and page.click("#note-back")
    tab("work"); page.wait_for_function("document.getElementById('note-type').value === 'work' && !document.getElementById('note-edit').hidden")   # empty tab: a new note, open
    assert page.input_value("#note-title") == "" and page.locator("#note-type").input_value() == "work"
    types = lambda: sorted(n["type"] for n in call(app, "/api/notes")[1]["notes"])
    assert types() == ["personal", "work"]
    back(); tab("skills"); page.wait_for_function("document.getElementById('note-type').value === 'skills'")
    assert types() == ["personal", "skills"]                                     # the empty work note is gone
    page.fill("#note-title", "kept"); page.wait_for_timeout(1000)
    back(); tab("personal"); page.wait_for_function("document.getElementById('note-title').value === 'p1'")   # its first note opens
    assert types() == ["personal", "skills"]                                     # a note with a title stays
    assert not errors
    ctx.close()
