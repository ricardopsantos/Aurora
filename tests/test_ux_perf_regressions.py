"""Regression tests for the 2026-09-24 performance/UX pass (R294+). Each was
measured or reproduced against the unfixed code first."""

import pytest

from aurora import tui
from tests.test_tui import _FakeEngine


def _t():
    t_ = tui.Tui(_FakeEngine())
    t_.app.invalidate = lambda *a: None
    return t_


# ── R294: input-box sizing stops at its 8-row cap ──────────────────────────
def test_input_height_does_not_wrap_the_whole_draft(monkeypatch):
    import textwrap

    from prompt_toolkit.layout.dimension import to_dimension
    t_ = _t()
    t_.input.text = "a pasted line of code with words\n" * 5000
    calls = []
    real = textwrap.wrap
    monkeypatch.setattr(textwrap, "wrap",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    d = to_dimension(t_.input.window.height)
    assert d.preferred == 8
    assert len(calls) <= 8, f"wrapped {len(calls)} lines for a capped box"


def test_input_height_still_counts_short_drafts():
    from prompt_toolkit.layout.dimension import to_dimension
    t_ = _t()
    t_.input.text = "one\ntwo\nthree"
    assert to_dimension(t_.input.window.height).preferred == 3


# ── R295: a sleeping (idle-unloaded) server is healthy, not "schema changed"
def _health_with_props(tmp_path, monkeypatch, props):
    import httpx

    from tests.test_core import _mk_engine
    e = _mk_engine(tmp_path, monkeypatch)
    e.current = {"model": "local", "provider": "local"}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return props
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    prov = e._provider_for(e.current)
    monkeypatch.setattr(prov, "pick_endpoint", lambda cache_ok=True: prov.base_url)
    return e._provider_health_uncached()


def test_sleeping_server_reports_asleep_not_schema_change(tmp_path, monkeypatch):
    h = _health_with_props(tmp_path, monkeypatch, {
        "default_generation_settings": {}, "model_path": None, "sleeping": True})
    assert h["ok"] is True
    assert "asleep" in h["detail"] and "schema" not in h["detail"]


def test_awake_server_still_reports_model_and_ctx(tmp_path, monkeypatch):
    h = _health_with_props(tmp_path, monkeypatch, {
        "default_generation_settings": {"n_ctx": 65536},
        "model_path": "/m/qwen.gguf"})
    assert h == {"ok": True, "detail": "qwen.gguf ready, ctx 65536"}


# ── R296: large-file exclusion persists, and costs one cheap scan ──────────
def _tree_files(root):
    import subprocess

    from aurora import rewind
    return subprocess.run(["git", "--git-dir", str(rewind._gitdir(root.resolve())),
                           "ls-tree", "-r", "--name-only", "HEAD"],
                          capture_output=True, text=True).stdout.split()


@pytest.fixture
def small_cap(tmp_path, monkeypatch):
    from aurora import rewind
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(rewind, "MAX_CHECKPOINT_FILE_BYTES", 1000)
    root = tmp_path / "p"
    root.mkdir()
    return root


def test_big_file_stays_excluded_across_checkpoints(small_cap):
    from aurora import rewind
    root = small_cap
    (root / "big.bin").write_bytes(b"0" * 5000)
    for i in range(4):
        (root / "a.py").write_text(str(i))
        rewind.checkpoint(f"c{i}", cwd=str(root))
        assert "big.bin" not in _tree_files(root), f"snapshotted at checkpoint {i}"


def test_a_file_that_shrinks_is_snapshotted_again(small_cap):
    from aurora import rewind
    root = small_cap
    (root / "big.bin").write_bytes(b"0" * 5000)
    rewind.checkpoint("c0", cwd=str(root))
    (root / "big.bin").write_bytes(b"small")
    rewind.checkpoint("c1", cwd=str(root))
    assert "big.bin" in _tree_files(root)


def test_an_already_tracked_file_that_grows_is_dropped(small_cap):
    from aurora import rewind
    root = small_cap
    (root / "data.bin").write_bytes(b"tiny")
    rewind.checkpoint("c0", cwd=str(root))
    assert "data.bin" in _tree_files(root)
    (root / "data.bin").write_bytes(b"0" * 5000)
    (root / "a.py").write_text("x")
    rewind.checkpoint("c1", cwd=str(root))
    assert "data.bin" not in _tree_files(root)


def test_one_status_scan_per_checkpoint(small_cap, monkeypatch):
    from aurora import rewind
    root = small_cap
    (root / "a.py").write_text("1")
    rewind.checkpoint("c0", cwd=str(root))
    seen = []
    real = rewind._git
    monkeypatch.setattr(rewind, "_git",
                        lambda wt, *a, **k: seen.append(a[0]) or real(wt, *a, **k))
    (root / "a.py").write_text("2")
    rewind.checkpoint("c1", cwd=str(root))
    assert "ls-files" not in seen and seen.count("status") == 1
