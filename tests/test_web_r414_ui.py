"""R414 in the browser: Files lists outputs with type icons and previews markdown, PDF and pictures, at desktop and phone width."""
import io
import os

import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r414_files_dialog_previews(browser, app, w, h, mobile):
    from PIL import Image
    from aurora.web import attach
    d = attach.outputs_dir()
    (d / "notes.md").write_text("# Hello\n\n- one\n")
    buf = io.BytesIO(); Image.new("RGB", (200, 280), "white").save(buf, "PDF"); (d / "doc.pdf").write_bytes(buf.getvalue())
    buf = io.BytesIO(); Image.new("RGB", (60, 40), "red").save(buf, "PNG"); (d / "pic.png").write_bytes(buf.getvalue())
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    if mobile and not page.is_visible("#files-btn"):
        page.click("#sb-toggle")
    page.click("#files-btn"); page.click("#fl-tabs .tab[data-kind=outputs]")
    page.wait_for_selector("#fl-list .fl-row >> nth=2")
    assert page.locator("#fl-h").inner_text() == "Files"
    assert "Markdown" in page.locator(".fl-row", has_text="notes.md").inner_text()
    assert page.evaluate(OVERFLOW) <= 0
    shots = os.environ.get("AURORA_SHOTS")
    if shots:
        page.wait_for_timeout(500); page.screenshot(path=f"{shots}/files-{w}.png")
    page.click(".fl-row:has-text('notes.md') .fl-open"); page.wait_for_selector("#pv-body h1")
    assert page.locator("#pv-body h1").inner_text() == "Hello"
    if shots:
        page.screenshot(path=f"{shots}/md-{w}.png")
    page.click("#pv-close"); page.click(".fl-row:has-text('doc.pdf') .fl-open"); page.wait_for_selector("#pv-body iframe.pv-pdf")
    page.click("#pv-close"); page.click(".fl-row:has-text('pic.png') .fl-open"); page.wait_for_selector("#pv-body img.pv-image")
    assert not errors
