"""R462: `remember` keeps facts in the Memory note; a page link can load several notes (/?note=a,b)."""
import pathlib

from tests.test_web import app, call  # noqa: F401  (fixture)


def test_r462_remember_appends_to_one_memory_note(app):
    from aurora import tools
    from aurora.web import memory as m
    assert "remember" in [s["name"] for s in tools.specs()] and not tools.needs_approval("remember", {"fact": "x"})
    assert m.remember(fact="  Ana's email is ana@x.pt ").startswith("saved")
    assert m.remember(fact="Prefers short replies").startswith("saved")
    assert m.remember(fact="prefers   short replies").startswith("already")
    assert m.remember(fact=" ").startswith("[remember: nothing")
    mem = [n for n in call(app, "/api/notes")[1]["notes"] if n["title"] == "Memory"]
    assert len(mem) == 1
    assert call(app, "/api/notes/" + mem[0]["id"])[1]["text"] == "- Ana's email is ana@x.pt\n- Prefers short replies\n"
    assert len(m.remember(fact="y" * 999)) < 400


def test_r462_link_with_several_notes():
    js = (pathlib.Path(__file__).resolve().parent.parent / "aurora/web/app.js").read_text()
    assert "id.split(',')" in js and ".slice(0, 5)" in js
    assert "new_chat: fresh && k === 0" in js            # only the first may start a new chat
