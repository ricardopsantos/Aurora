"""R458 (#22): the page can tell a turn another device started from its own."""
import pathlib

from tests.test_web import app, types, wait_for  # noqa: F401  (fixture)


def test_user_event_and_status_carry_the_sending_client(app):
    app.s.busy = True                                  # queue behind a running turn
    assert app.s.send("from the phone", client_id="phone-1") == (True, "")
    assert app.s._queue[0][4] == "phone-1"
    app.s.busy = False
    app.s._start_queued(app.s._queue.pop(0))
    assert app.s.busy_client == "phone-1"
    assert wait_for(lambda: "done" in types(app))
    assert "busy_client" in app.s.state()
    users = [ev for ev in app.s.events if ev["type"] == "user"]
    assert users[-1]["client"] == "phone-1"


def test_direct_send_records_the_client(app):
    assert app.s.send("hello", client_id="desk-7") == (True, "")
    assert app.s.busy_client == "desk-7"
    assert wait_for(lambda: "done" in types(app))


def test_page_labels_a_turn_from_another_device():
    js = (pathlib.Path(__file__).resolve().parent.parent / "aurora/web/app.js").read_text()
    assert "busy on another device" in js
    assert "setBusy(true, fromOther(ev.client))" in js
    assert "mySent.add(item.id)" in js


def test_typed_commands_say_where_to_go_instead():
    from aurora.web.core import typed_command_refusal
    assert "Undo item" in typed_command_refusal("/undo")
    assert "Plan mode in Settings" in typed_command_refusal("/plan on")
    msg = typed_command_refusal("/fallback on")
    assert "only in the terminal" in msg and "⋯ menu" in msg
    assert "only in the terminal" in typed_command_refusal("/")
