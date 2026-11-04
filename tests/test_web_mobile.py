"""R392 — the Chat page on a small phone (320×568) and with the keyboard up (360×330): no sideways scrolling,
and Menu, Send and Stop stay on screen while busy, with Notes open and with the live panel open.
Needs Playwright + Chromium (`pip install playwright && playwright install chromium`); skipped where it is missing."""
import threading

import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call, types, wait_for  # noqa: E402,F401  (fixture)

OVERFLOW = "document.documentElement.scrollWidth - innerWidth"


def _on_screen(page, sel):
    return page.evaluate("""s => { const e = document.querySelector(s); if (!e || e.hidden) return 'hidden';
        const r = e.getBoundingClientRect(), vw = innerWidth, vh = (window.visualViewport || window).height || innerHeight;
        return r.width > 0 && r.left >= 0 && r.right <= vw + 0.5 && r.top >= 0 && r.bottom <= vh + 0.5; }""", sel)


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"no chromium for playwright: {e}")
        yield b
        b.close()


def _open(browser, app, w, h):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k'); localStorage.setItem('aurora_web_clear_open', '0')")
    page.goto(app.base + "/")
    page.wait_for_selector("#model:not(:empty)")
    page.wait_for_timeout(300)
    return page, errors


def _busy(app):
    gate = threading.Event()
    prov = app.state["prov"]
    orig = prov.turn
    prov.turn = lambda *a, **k: (gate.wait(10), orig(*a, **k))[1]
    assert call(app, "/api/send", {"text": "a long running request " + "word " * 30})[0] == 200
    return gate


def test_320_idle_busy_notes(browser, app):
    page, errors = _open(browser, app, 320, 568)
    assert page.evaluate(OVERFLOW) <= 0
    assert _on_screen(page, "#ctx") is True and _on_screen(page, "#go") is True   # R431: no ••• menu; the context pill ends the bar
    gate = _busy(app)
    page.wait_for_selector("#stop:not([hidden])")
    page.wait_for_timeout(1200)                          # the status counter grows ("writing… 1s")
    assert page.evaluate(OVERFLOW) <= 0, "busy header wider than the phone (U1)"
    for sel in ("#ctx", "#go", "#stop"):
        assert _on_screen(page, sel) is True, sel
    page.click("#live-line") if _on_screen(page, "#live-line") is True else None
    page.wait_for_timeout(100)
    for sel in ("#go", "#stop"):
        assert _on_screen(page, sel) is True, f"{sel} pushed off by the live panel (U4)"
    gate.set()
    assert wait_for(lambda: "done" in types(app))
    page.evaluate("document.getElementById('notes').showModal()")
    page.wait_for_timeout(100)
    assert page.evaluate(OVERFLOW) <= 0
    assert page.evaluate("document.getElementById('notes').getBoundingClientRect().right") <= 320.5, "Notes wider than the phone (U2)"
    assert _on_screen(page, "#note-close") is True
    assert not errors, errors


def test_keyboard_up_360x330(browser, app):
    page, errors = _open(browser, app, 360, 330)
    page.focus("#text")
    page.wait_for_timeout(100)
    assert page.evaluate(OVERFLOW) <= 0
    for sel in ("#go", "#sb-toggle"):
        assert _on_screen(page, sel) is True, sel
    page.click("#cmd-btn")
    page.wait_for_timeout(100)
    assert page.evaluate("""() => { const m = document.getElementById('cmd-menu').getBoundingClientRect();
        return m.top >= 0 && m.bottom <= innerHeight + 0.5; }"""), "Commands menu off-screen with the keyboard up (U3)"
    page.keyboard.press("Escape")
    gate = _busy(app)
    page.wait_for_selector("#stop:not([hidden])")
    page.focus("#text")
    for sel in ("#go", "#stop"):
        assert _on_screen(page, sel) is True, sel
    gate.set()
    assert not errors, errors


def test_inputs_are_16px_on_touch(browser, app):
    page, _ = _open(browser, app, 320, 568)
    small = page.evaluate("""() => [...document.querySelectorAll('input:not([type=checkbox]),select,textarea')]
        .filter(e => parseFloat(getComputedStyle(e).fontSize) < 16).map(e => e.id)""")
    assert small == [], small                           # iOS zooms on focus below 16 px (U6)
