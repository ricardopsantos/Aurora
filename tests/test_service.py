"""R394/R395: the front-end facade (aurora/service.py) and the session scans behind it."""
import inspect
import json
import os

import pytest

from aurora import service, session as core_session
from aurora.providers.base import ProviderError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    from aurora import paths
    d = paths.sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    core_session._PROMPTS.clear(); core_session._FIRST.clear(); core_session._SEARCH.clear()
    return d


def _log(d, name, texts, mtime):
    p = d / name
    p.write_text("".join(json.dumps({"event": "user", "text": t}) + "\n" for t in texts))
    os.utime(p, (mtime, mtime))
    return p


# ── the contract ────────────────────────────────────────────────────────
def test_every_exported_name_exists_and_provider_error_is_the_core_class():
    for name in service.__all__:
        assert hasattr(service, name), name
    assert service.ProviderError is ProviderError
    assert set(service.__all__) >= {"session_exists", "list_sessions", "search_sessions", "load_state", "save_state",
                                    "set_secret_prompter", "checkpoints", "diff_since", "undo", "aurora_home", "version"}


def test_facade_signatures_are_stable():
    """Front ends call these by keyword/position; a change here is a contract change (update ARCHITECTURE.md)."""
    sig = {n: list(inspect.signature(getattr(service, n)).parameters) for n in service.__all__
           if callable(getattr(service, n)) and n not in ("ProviderError", "ModelEditError")}   # exception classes
    assert sig["list_sessions"] == ["limit"] and sig["search_sessions"] == ["query", "limit", "counts"]
    assert sig["session_exists"] == ["sid"] and sig["save_state"] == ["values"] and sig["diff_since"] == ["ref"]


def test_delegation_happens_at_call_time(monkeypatch):
    monkeypatch.setattr(core_session, "list_sessions", lambda limit=20: [("x", "now", f"patched {limit}")])
    assert service.list_sessions(3) == [("x", "now", "patched 3")]
    from aurora import config
    monkeypatch.setattr(config, "load_state", lambda: {"k": 1})
    assert service.load_state() == {"k": 1}


def test_version_is_a_string():
    assert isinstance(service.version(), str) and service.version()


# ── session_exists: exact ids only (R359), one stat not a scan (R395) ──
def test_session_exists_exact_ids_only(home):
    _log(home, "abc123def456.jsonl", ["hi"], 1e9)
    _log(home, "abc123def456.2.jsonl", ["more"], 1e9 + 1)
    assert service.session_exists("abc123def456")
    for bad in ("abc123", "abc123def456.2", "../abc123def456", "abc123def456/", "", None, 5,
                "..", "a" * 65, "abc123def456.jsonl", "ABC123DEF456"):
        assert service.session_exists(bad) is False, bad


def test_session_exists_falls_back_to_the_list_for_an_unusual_id(home):
    _log(home, "odd.name.jsonl", ["hi"], 1e9)        # not a plain id, but a real base log
    assert service.session_exists("odd.name")
    assert not service.session_exists("odd")


def test_session_exists_does_not_scan_the_directory_for_a_plain_id(home, monkeypatch):
    _log(home, "abc.jsonl", ["hi"], 1e9)
    monkeypatch.setattr(core_session, "_base_session_ids", lambda: pytest.fail("scanned the whole directory"))
    assert service.session_exists("abc") and not service.session_exists("nope")


def test_a_directory_named_like_a_log_is_not_a_session(home):
    (home / "dir.jsonl").mkdir()
    assert not service.session_exists("dir")


# ── one-scan mtimes (R395) ──────────────────────────────────────────────
def test_session_mtimes_groups_rotated_parts_and_keeps_the_newest(home):
    _log(home, "aaa.jsonl", ["a"], 1000)
    _log(home, "aaa.2.jsonl", ["a2"], 3000)
    _log(home, "aaa.10.jsonl", ["a10"], 2000)        # R276: part 10 is a part, not a session
    _log(home, "bbb.jsonl", ["b"], 2500)
    (home / "notes.txt").write_text("x")
    assert core_session._session_mtimes() == {"aaa": 3000, "bbb": 2500}
    assert core_session._session_mtimes() == {s: core_session._latest_mtime(s) for s in ("aaa", "bbb")}


def test_session_mtimes_with_no_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "missing"))
    assert core_session._session_mtimes() == {}
    assert core_session.list_sessions() == [] and core_session.latest_session_id() is None


def test_list_and_search_order_by_the_newest_part_without_probing_each_session(home, monkeypatch):
    _log(home, "old.jsonl", ["old task"], 1000)
    _log(home, "old.2.jsonl", ["still going"], 9000)  # rotated: its newest part makes it the freshest
    _log(home, "mid.jsonl", ["mid task"], 5000)
    assert core_session.list_sessions()[0][2] == "old task"   # cold: reads each listed log once for its first prompt
    with monkeypatch.context() as m:
        m.setattr(core_session, "_parts_for", lambda *a: pytest.fail("a warm listing probed parts one by one"))
        m.setattr(core_session, "_latest_mtime", lambda *a: pytest.fail("stat'ed per session"))
        assert [r[0] for r in core_session.list_sessions()] == ["old", "mid"]
        assert core_session.latest_session_id() == "old"
    assert [r[0] for r in core_session.search_sessions("task")] == ["old", "mid"]
    assert core_session.search_sessions("task", limit=1)[0][0] == "old"


def test_list_sessions_respects_the_limit(home):
    for i in range(30):
        _log(home, f"s{i:02d}.jsonl", [f"task {i}"], 1000 + i)
    rows = service.list_sessions(5)
    assert [r[0] for r in rows] == ["s29", "s28", "s27", "s26", "s25"]


def test_search_finds_a_hit_in_a_rotated_part(home):
    _log(home, "rot.jsonl", ["nothing here"], 1000)
    _log(home, "rot.2.jsonl", ["the needle is here"], 2000)
    hits = service.search_sessions("NEEDLE")
    assert [h[0] for h in hits] == ["rot"] and "needle" in hits[0][3]


def test_secret_prompter_routes_to_the_keystore(monkeypatch):
    from aurora import keystore
    got = []
    monkeypatch.setattr(keystore, "set_prompter", got.append)
    fn = lambda q: "k"                                # noqa: E731
    service.set_secret_prompter(fn)
    assert got == [fn]
