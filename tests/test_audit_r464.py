"""R464-R468: deep-dive audit fixes (2026-11-24), one requirement each."""

import os
import re
import stat
import tempfile

from aurora import bashmode, tools


# ── R464: bash mode's carried-state file is in a private directory ──────────

def test_r464_state_file_lives_in_a_private_directory(monkeypatch, tmp_path):
    seen = {}
    real = tools._run_command_once

    def spy(script, cwd, **kw):
        path = re.search(r"'?(\S*?aurora-bash-[^\s']*)'? 2>/dev/null", script).group(1)
        d = os.path.dirname(path)
        st = os.stat(d)
        seen.update(path=path, dir=d, mode=stat.S_IMODE(st.st_mode), uid=st.st_uid, exists=os.path.exists(path))
        return real(script, cwd, **kw)
    monkeypatch.setattr(tools, "_run_command_once", spy)
    res, new = bashmode.run("cd .. && export R464=yes", bashmode.initial_state(str(tmp_path)))
    assert res.code == 0
    assert new.env.get("R464") == "yes" and new.cwd == str(tmp_path.parent)    # carry-over still works
    assert seen["dir"] != tempfile.gettempdir()          # never a bare name in the shared temp dir
    assert seen["mode"] == 0o700 and seen["uid"] == os.getuid()
    assert not seen["exists"]                            # the trap creates it
    assert not os.path.exists(seen["dir"])               # removed afterwards



# ── R465: the web viewer stops reading a huge session at its cap ────────────

def test_r465_capped_export_is_the_same_head_and_stops_early(monkeypatch, tmp_path):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora import session as S
    s = S.Session("r465sess0001")
    for i in range(400):
        s.log("user", text=f"q{i} " + "q" * 300)
        s.log("tool", name="run_command", args={"command": "ls"}, output="o" * 2000, chars=5000)
        s.log("assistant", text=f"a{i} " + "a" * 600, model="m")
    full = S.export_markdown(s.id)
    cap = 50_000
    seen = []
    real = S.Session.iter_records

    def counting(self, events=None):
        for r in real(self, events):
            seen.append(1)
            yield r
    monkeypatch.setattr(S.Session, "iter_records", counting)
    head = S.export_markdown(s.id, cap)
    assert len(head) > cap and full.startswith(head)
    assert len(seen) < 100                       # 1200 records in the log; it stopped near the cap
    assert S.export_markdown(s.id, 10 ** 9) == full


def test_r465_web_viewer_still_says_it_cut(monkeypatch, tmp_path):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora import session as S
    from aurora.web import core
    s = S.Session("r465sess0002")
    for i in range(200):
        s.log("user", text="q" * 2000)
    monkeypatch.setattr(core, "MAX_TRANSCRIPT", 10_000)

    class E:
        session = S.Session("someoneelse1")
    ws = core.WebSession.__new__(core.WebSession)
    ws.engine, ws.busy = E(), False
    md = ws.session_markdown(s.id)
    assert md.startswith(S.export_markdown(s.id)[:10_000]) and md.endswith("[… transcript cut at 10000 characters]")


# ── R467: `remember` really uses the oldest Memory note ─────────────────────

def test_r467_oldest_memory_note_wins_whatever_the_ids(monkeypatch, tmp_path):
    import uuid as _uuid
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora.web import memory as m
    from aurora.web import notes
    ids = iter(["f" * 32, "0" * 32])                       # the newer note sorts first by id
    monkeypatch.setattr(_uuid, "uuid4", lambda: _uuid.UUID(next(ids)))
    clock = iter([1000.0, 2000.0])                        # one reading per note (R577)
    monkeypatch.setattr(notes, "_now", lambda: next(clock, 3000.0))
    old, _ = notes.create("Memory", "- kept here\n")
    new, _ = notes.create("memory", "")
    assert (old, new) == ("f" * 12, "0" * 12)
    assert [n["created"] for n in notes.listing()] == [2000.0, 1000.0]
    assert m.remember(fact="a new fact").startswith("saved")
    assert "- a new fact" in notes.get(old)["text"] and notes.get(new)["text"] == ""


# ── R469: the web denylist's glob expansion is bounded ──────────────────────

def test_r469_a_deep_glob_is_not_walked_for_long(monkeypatch, tmp_path):
    import time
    from aurora.web import core
    d = tmp_path
    for a in range(40):
        for b in range(40):
            (d / f"a{a}" / f"b{b}").mkdir(parents=True)
    t = time.monotonic()
    assert core._glob_expanded(f"ls {d}/*/*/*/zz", budget_s=0) == ("", False)          # out of time
    monkeypatch.setattr(core, "GLOB_MAX_FRONTIER", 100)
    assert core._glob_expanded(f"ls {d}/*/*/*/zz", budget_s=5) == ("", False)          # too many paths at one level
    assert time.monotonic() - t < 1.0
    out, done = core._glob_expanded(f"cat {d}/a1?/b3", budget_s=5)
    assert done and sorted(out.split("\n")) == [f"{d}/a1{k}/b3" for k in range(10)]


def test_r469_glob_matches_like_iglob(tmp_path):
    import glob
    from aurora.web import core
    for n in ("x.py", "y.py", ".hidden.py", "z.txt"):
        (tmp_path / n).write_text("")
    for pat in ("*.py", ".*.py", "?.py", "[xz].*"):
        got = core._bounded_glob(f"{tmp_path}/{pat}", float("inf"), limit=99)
        assert got[1] and sorted(got[0]) == sorted(glob.glob(f"{tmp_path}/{pat}")), pat


def test_r469_an_unchecked_glob_is_asked_even_with_auto_approve(monkeypatch):
    from aurora.web import core
    monkeypatch.setattr(core, "_glob_expanded", lambda text, budget_s=None: ("", False))
    assert core.web_check("run_command", {"command": "ls /*/*/*"}, []) == ("", False)
    asked = []

    class S:
        auto = True
        def emit(self, *a, **k): pass
        def ask(self, tool, args, diff): asked.append(tool); return "n"
    fe = core.WebFrontend.__new__(core.WebFrontend)
    fe.s, fe._lock, fe._buf, fe._timer, fe._kind, fe._size = S(), __import__("threading").Lock(), [], None, "", 0
    assert fe.approve("run_command", {"command": "ls /*/*/*"}, "") == "n" and asked == ["run_command"]
    monkeypatch.setattr(core, "_glob_expanded", lambda text, budget_s=None: ("", True))
    assert fe.approve("run_command", {"command": "ls /tmp/*"}, "") == "y"
