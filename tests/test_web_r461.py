"""R461: the address bar names the open chat (/?chat=<session id>); opening such a link opens that chat."""
import pathlib

from tests.test_web import app, call  # noqa: F401  (fixture)

JS = (pathlib.Path(__file__).resolve().parent.parent / "aurora/web/app.js").read_text()


def test_r461_page_puts_the_chat_in_the_address_and_opens_a_chat_link():
    assert "q.set('chat', id)" in JS and "history.replaceState" in JS
    assert "if (!LINK_CHAT) chatInAddress(id)" in JS                       # every session change updates it
    assert "if (LINK_CHAT) {" in JS and "post('/api/resume', { id: want })" in JS
    assert JS.index("if (LINK_CHAT) {") < JS.index("$('clear-open').checked && !inUse")   # a link wins over New chat on open


def test_r461_the_id_in_the_link_is_what_resume_takes(app):
    sid = call(app, "/api/state")[1]["session_id"]
    assert sid and all(c.isalnum() or c in "-_" for c in sid) and len(sid) <= 64
    assert call(app, "/api/resume", {"id": "nope"})[0] == 400
