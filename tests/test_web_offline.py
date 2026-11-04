"""R396: offline support in the browser. Needs Playwright + Chromium; skipped where it is missing.
The fixture server is http://127.0.0.1, which browsers treat as a secure context, so the service worker runs."""
import pytest

pw = pytest.importorskip("playwright.sync_api")

from tests.test_web import app, call, types, wait_for  # noqa: E402,F401  (fixture)
from tests.test_web_mobile import browser  # noqa: E402,F401  (fixture)


def _open(browser, app, w=1280, h=800):
    ctx = browser.new_context(viewport={"width": w, "height": h})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('aurora_web_token', 't0k')")
    page.goto(app.base + "/")
    page.wait_for_selector("#model:not(:empty)")
    page.wait_for_timeout(300)
    return ctx, page, errors


def test_offline_tag_outbox_and_send_on_reconnect(browser, app):
    ctx, page, errors = _open(browser, app)
    assert page.is_hidden("#net")
    ctx.set_offline(True)
    page.evaluate("dispatchEvent(new Event('offline'))")
    assert "offline" in page.text_content("#net")
    page.fill("#text", "sent while offline")
    page.click("#go")
    page.wait_for_selector(".msg.pending")
    assert page.input_value("#text") == ""
    assert "1 waiting" in page.text_content("#net")
    assert "sent while offline" in page.evaluate("localStorage.getItem('aurora_web_outbox')")
    ctx.set_offline(False)
    page.evaluate("dispatchEvent(new Event('online'))")
    assert wait_for(lambda: "done" in types(app), 10)
    page.wait_for_selector(".msg.pending", state="detached")
    page.wait_for_function("document.querySelector('#net').hidden")
    assert page.evaluate("localStorage.getItem('aurora_web_outbox')") == "[]"
    assert types(app).count("user") == 1
    assert not errors, errors


def test_outbox_survives_a_reload_and_keeps_order(browser, app):
    ctx, page, errors = _open(browser, app)
    ctx.set_offline(True)
    for t in ("first", "second"):
        page.fill("#text", t); page.click("#go")
    page.wait_for_function("document.querySelectorAll('.msg.pending').length === 2")
    ctx.set_offline(False)
    page.evaluate("navigator.serviceWorker && navigator.serviceWorker.ready")
    # reload while the outbox still holds both: they are drawn again and sent in order
    page.route("**/api/send", lambda r: r.abort())
    page.reload(); page.wait_for_selector("#model:not(:empty)")
    page.wait_for_function("document.querySelectorAll('.msg.pending').length === 2")
    page.unroute("**/api/send")
    page.evaluate("dispatchEvent(new Event('online'))")
    assert wait_for(lambda: types(app).count("done") == 2, 15)
    texts = [e["text"] for e in app.s.events if e["type"] == "user"]
    assert texts == ["first", "second"]
    assert not errors, errors


def test_resend_after_a_lost_answer_runs_once(browser, app):
    """The request reaches the server but the answer is lost: the outbox resends with the same id, the server drops it."""
    ctx, page, errors = _open(browser, app)
    state = {"n": 0}

    def flaky(route):
        state["n"] += 1
        resp = route.fetch()               # the server gets it ...
        if state["n"] == 1:
            route.abort()                  # ... the page never hears back
        else:
            route.fulfill(response=resp)
    page.route("**/api/send", flaky)
    page.fill("#text", "exactly once"); page.click("#go")
    page.wait_for_selector(".msg.pending")
    page.evaluate("dispatchEvent(new Event('online'))")
    page.wait_for_selector(".msg.pending", state="detached", timeout=10000)
    assert wait_for(lambda: "done" in types(app), 10)
    page.wait_for_timeout(500)
    assert types(app).count("user") == 1 and state["n"] >= 2
    assert not errors, errors


def test_draft_survives_a_reload_and_clears_on_send(browser, app):
    ctx, page, errors = _open(browser, app)
    page.fill("#text", "half written thought")
    page.reload(); page.wait_for_selector("#model:not(:empty)")
    assert page.input_value("#text") == "half written thought"
    page.click("#go")
    assert wait_for(lambda: "done" in types(app), 10)
    page.reload(); page.wait_for_selector("#model:not(:empty)")
    assert page.input_value("#text") == ""
    assert not errors, errors


def test_other_actions_offline_say_so_instead_of_failing_silently(browser, app):
    ctx, page, errors = _open(browser, app)
    ctx.set_offline(True)
    page.evaluate("post('/api/plan', {on: true})")
    page.wait_for_selector(".msg.err")
    assert "offline" in page.text_content(".msg.err:last-child")
    assert not errors, errors


def test_page_and_last_chats_open_offline_via_the_service_worker(browser, app):
    call(app, "/api/send", {"text": "remember me"})
    assert wait_for(lambda: "done" in types(app), 10)
    ctx, page, errors = _open(browser, app)
    page.evaluate("navigator.serviceWorker.ready")
    page.reload(); page.wait_for_selector("#model:not(:empty)")   # now controlled: the shell and chats are cached
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    sid = call(app, "/api/state")[1]["session_id"]
    page.evaluate(f"fetch('/api/sessions/{sid}', {{headers: hdr()}})")
    page.wait_for_timeout(300)
    ctx.set_offline(True)
    page.reload()
    page.wait_for_selector("#text")                                  # the page itself opened offline
    r = page.evaluate(f"fetch('/api/sessions/{sid}', {{headers: hdr()}}).then(async r => [r.status, r.headers.get('X-Aurora-Offline'), (await r.json()).markdown])")
    assert r[0] == 200 and r[1] == "1" and "remember me" in r[2]
    st = page.evaluate("fetch('/api/state', {headers: hdr()}).then(() => 'answered', () => 'failed')")
    assert st == "failed"                                            # live state is never served from a cache
    assert not [e for e in errors if "Failed to fetch" not in e], errors


def test_text_typed_while_a_send_is_in_flight_is_kept(browser, app):
    """R396: the box is cleared only if it still holds what was sent (it used to wipe the next message)."""
    ctx, page, errors = _open(browser, app)
    import time
    page.route("**/api/send", lambda r: (time.sleep(0.8), r.continue_()))
    page.fill("#text", "the first one"); page.click("#go")
    page.wait_for_timeout(100)
    page.fill("#text", "typed meanwhile")
    assert wait_for(lambda: "done" in types(app), 10)
    page.wait_for_timeout(300)
    assert page.input_value("#text") == "typed meanwhile"
    assert page.evaluate("localStorage.getItem('aurora_web_draft')") == "typed meanwhile"
    assert not errors, errors
