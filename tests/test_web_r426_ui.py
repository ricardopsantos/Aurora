"""R426 in the browser: tapping "idle" wakes the model; Save as note makes a note AND downloads the chat (1280, 360 px)."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from aurora.providers.base import TurnResult  # noqa: E402
from tests.test_web import app, call  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser  # noqa: E402,F401  (fixture)
from tests.test_web_r426 import Sleepy  # noqa: E402


@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (360, 640, True)])
def test_r426_idle_wakes_and_save_as_note_downloads(browser, app, w, h, mobile):
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile, accept_downloads=True)
    page = ctx.new_page(); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)")
    app.state["prov"] = Sleepy([TurnResult(text="h", stop_reason="end"), TurnResult(text="Paris is the capital", stop_reason="end")])
    page.wait_for_function("document.getElementById('status').textContent === 'idle'")
    if page.is_visible("#status"):
        page.click("#status")                                             # "idle" on the top bar
    else:                                                                 # a phone hides it: re-pick the model instead
        page.click("#model"); page.locator("#model-menu button").nth(1).click()   # entry 1 is `local`
    page.wait_for_selector(".msg.note:has-text('awake: qwen3-9b.gguf')")
    page.fill("#text", "capital of France?"); page.click("#go")
    page.wait_for_selector(".acts button:has-text('Save as note')")
    with page.expect_download() as dl:
        page.click(".acts button:has-text('Save as note')")
    assert dl.value.suggested_filename.endswith(".html")
    page.wait_for_selector(".msg.note:has-text('saved as a note')")
    assert len(call(app, "/api/notes")[1]["notes"]) == 1
    assert not errors
    ctx.close()
