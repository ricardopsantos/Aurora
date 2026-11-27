"""R463: `remember` refuses over-long facts and secrets, never overwrites a concurrent edit, and picks one Memory note."""
from aurora.web import memory as m
from aurora.web import notes

from tests.test_web import app, call  # noqa: F401  (fixture)


def test_r463_too_long_is_refused_not_cut(app):
    out = m.remember(fact="y" * 301)
    assert "too long" in out and "301" in out
    assert m.remember(fact="y" * 300).startswith("saved")


def test_r463_secret_is_refused(app):
    out = m.remember(fact="my token is ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8")
    assert out.startswith("[remember: not saved") and "secret" in out
    assert not any(n["title"] == "Memory" for n in call(app, "/api/notes")[1]["notes"])


def test_r463_conflict_retries_once(app, monkeypatch):
    assert m.remember(fact="one").startswith("saved")
    real, calls = notes.save, []

    def racing(nid, title, text, base=None):
        calls.append(base)
        if len(calls) == 1:
            real(nid, title, "- one\n- typed in the editor\n")   # the user saves first
        return real(nid, title, text, base=base)

    monkeypatch.setattr(notes, "save", racing)
    assert m.remember(fact="two").startswith("saved")
    text = m._memory_note()["text"]
    assert "typed in the editor" in text and "- two" in text and len(calls) == 2


def test_r463_oldest_memory_note_wins(app):
    assert m.remember(fact="first").startswith("saved")
    nid, _ = notes.create("Memory", "- other\n", kind="personal")
    assert m.remember(fact="second").startswith("saved")
    assert "- second" in m._memory_note()["text"] and m._memory_note()["id"] != nid
