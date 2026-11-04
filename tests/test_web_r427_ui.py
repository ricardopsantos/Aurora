"""R427 in the browser: Edit models (R430: in the model menu) is a raw editor; Validate shows errors at their line; Save reloads the models."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser, OVERFLOW  # noqa: E402,F401  (fixture)


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r427_raw_editor(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    page.click("#model"); page.click("#models-edit"); page.wait_for_function("document.getElementById('cfg-text').value.length > 0")
    assert not page.is_visible("#md-tabs .tab[data-view='config.local.yaml']")
    text = page.input_value("#cfg-text")
    assert "providers:" in text and page.locator("#cfg-gutter").inner_text().strip().startswith("1")
    assert page.evaluate(OVERFLOW) <= 0
    page.fill("#cfg-text", text.replace("provider: local, tools: true}", "provider: ghost, tools: true}"))
    page.click("#cfg-check"); page.wait_for_selector("#cfg-msg.bad")
    assert "ghost" in page.inner_text("#cfg-msg")
    page.fill("#cfg-text", "models: [\n"); page.click("#cfg-save"); page.wait_for_selector("#cfg-msg.bad")
    assert "line" in page.inner_text("#cfg-msg")
    page.fill("#cfg-text", text + "  - {model: fresh-one, provider: local}\n")
    page.click("#cfg-check"); page.wait_for_selector("#cfg-msg.ok")
    page.click("#cfg-save"); page.wait_for_function("document.getElementById('cfg-msg').textContent.startsWith('Saved')")
    assert [m["model"] for m in app.e.models][-1] == "fresh-one"
    page.fill("#cfg-text", text + "# unsaved\n")
    page.keyboard.press("Escape"); page.wait_for_selector("#confirm[open]")        # edited: asks first
    page.click("#cf-yes"); page.wait_for_selector("#models", state="hidden")   # R429: no list to go back to; it closes
    assert not errors
    ctx.close()
