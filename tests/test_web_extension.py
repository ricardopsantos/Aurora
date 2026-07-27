"""R157: web_search/web_fetch as a BUNDLED extension rather than an engine
module wired in by name.

The behaviour that must survive the move is the `runtime.web_search` toggle.
It used to be enforced engine-side by `tools.specs(include_web)`; it is now
`register()`'s job, and a static SPEC/RUNNERS extension would have silently
ignored it — so that is what these tests pin down.
"""

import importlib.util
import pathlib

import pytest

from aurora import tools

_MOD = (pathlib.Path(__file__).resolve().parent.parent
        / "aurora" / "extensions_bundled" / "web_extension.py")


def _load():
    spec = importlib.util.spec_from_file_location("web_extension_under_test", _MOD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine(tmp_path, monkeypatch, runtime: str = ""):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  local: {type: openai, base_url: x}\n"
                   "models:\n  - {provider: local, model: m}\n" + runtime)
    from aurora.engine import Engine
    return Engine(str(cfg))


# ── the move itself ────────────────────────────────────────────────────────
def test_web_tools_are_loaded_by_default(tmp_path, monkeypatch):
    """'Installed by default' is the whole point: a config that says nothing
    about web_search still gets both tools, exactly as before the move."""
    _engine(tmp_path, monkeypatch)
    try:
        names = [s["name"] for s in tools.specs()]
        assert "web_search" in names and "web_fetch" in names
    finally:
        tools.set_extensions([], {})


def test_web_tools_are_runnable_through_the_extension_path(tmp_path, monkeypatch):
    """Registered specs are worthless if the runner didn't come with them —
    `run_tool` must resolve these through _EXTENSION_RUNNERS now, not through
    the builtin table they used to live in."""
    eng = _engine(tmp_path, monkeypatch)
    del eng
    try:
        out = tools.run_tool("web_fetch", {"url": "http://127.0.0.1:9/nope"})
        assert "web_fetch error" in out      # reached the runner, failed to connect
        assert "no such tool" not in out.lower()
    finally:
        tools.set_extensions([], {})


# ── the toggle that had to survive ─────────────────────────────────────────
def test_web_search_false_contributes_no_tools(tmp_path, monkeypatch):
    """`runtime.web_search: false` must still hide both tools from the model.
    A static SPEC/RUNNERS extension would load unconditionally and quietly
    turn this flag into a no-op."""
    _engine(tmp_path, monkeypatch, "runtime:\n  web_search: false\n")
    try:
        names = [s["name"] for s in tools.specs()]
        assert "web_search" not in names and "web_fetch" not in names
    finally:
        tools.set_extensions([], {})


def test_register_honours_the_flag_directly():
    """Same rule at the unit level, independent of Engine wiring."""
    module = _load()
    on = module.register(type("E", (), {"web": True})())
    off = module.register(type("E", (), {"web": False})())
    assert [s["name"] for s in on[0]] == ["web_search", "web_fetch"]
    assert set(on[1]) == {"web_search", "web_fetch"}
    assert off == ([], {})


def test_register_defaults_to_on_for_an_engine_without_the_attribute():
    """Extensions are handed whatever object the host passes; a missing `web`
    must mean "enabled", matching `runtime.web_search`'s own default of true."""
    module = _load()
    specs, runners = module.register(object())
    assert [s["name"] for s in specs] == ["web_search", "web_fetch"]
    assert set(runners) == {"web_search", "web_fetch"}


# ── the engine no longer knows these tools by name ─────────────────────────
def test_engine_half_no_longer_imports_websearch():
    """The point of the move: no engine module may reach for a `websearch`
    module any more — it doesn't exist, and re-adding one would quietly
    restore the special-casing this change removed."""
    pkg = pathlib.Path(__file__).resolve().parent.parent / "aurora"
    assert not (pkg / "websearch.py").exists()
    offenders = [p.name for p in pkg.glob("*.py")
                 if "websearch" in p.read_text()]
    assert not offenders, f"engine modules still reference websearch: {offenders}"


def test_specs_takes_no_include_web_argument():
    """`tools.specs()` lost its `include_web` parameter — the flag is the
    extension's business now. Passing one must be an error, not silently
    ignored, so a stale caller fails loudly."""
    with pytest.raises(TypeError):
        tools.specs(False)
