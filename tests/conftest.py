"""Shared pytest fixtures.

`tools._EXTENSION_SPECS`/`_EXTENSION_RUNNERS` (R119) are process-global
mutable state, set by every `Engine()` construction. Without this, a test
that builds an Engine with extensions configured and forgets to reset them
(or fails before reaching its own cleanup) leaks specs/runners into every
later test that calls `tools.specs()`/`tools.run_tool()` — flaky failures
that depend on test ORDER, the worst kind to debug. Autouse + session-wide
so every test gets a clean slate on entry and exit, structurally, instead
of relying on each test remembering its own `tools.set_extensions([], {})`
in a `finally`."""

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("AURORA_KEYRING_TIMEOUT", "1")   # R325: never hang a suite

from aurora import tools


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    before = (tools._EXTENSION_SPECS, tools._EXTENSION_RUNNERS)
    yield
    tools._EXTENSION_SPECS, tools._EXTENSION_RUNNERS = before


@pytest.fixture(autouse=True)
def _reset_process_wide_hooks():
    """A web turn wires `keystore._prompter` to the page's ask_secret (which answers ""), and R475 keeps the command
    sandbox in module state: both are per process in real use (web or terminal, never both), so no test may leak
    them. Before this, any web turn that ran ahead of test_core's key-picker test made it fail."""
    from aurora import keystore
    before = keystore._prompter
    sb = (tools._SANDBOX, tools.SANDBOX_WANTED, tools.SANDBOX_PROBLEM)
    yield
    keystore._prompter = before
    tools._SANDBOX, tools.SANDBOX_WANTED, tools.SANDBOX_PROBLEM = sb


@pytest.fixture(autouse=True)
def _reset_web_guard():
    """R450: `WebSession` switches the web path guard on for the process; no test may leak it."""
    from aurora import guard
    guard.WEB = False
    yield
    guard.WEB = False


class _MemoryKeyring:
    """An in-memory stand-in for the `keyring` module, so no test ever reaches
    the machine's real SecretService (which can block on an unlock prompt)."""

    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


@pytest.fixture(autouse=True)
def _isolated_keyring(monkeypatch):
    """R325: every test gets a fresh in-memory keyring (tests that need a
    specific fake still install their own over it), and the 'stuck' flag a
    blocked keyring sets never leaks from one test to the next."""
    import sys

    from aurora import keystore
    monkeypatch.setitem(sys.modules, "keyring", _MemoryKeyring())
    monkeypatch.setattr(keystore, "_keyring_stuck", False)
    yield


@pytest.fixture
def box_home_root(tmp_path):
    """Where a command-box test puts its fake home. On macOS the box may write the per-user temp dir
    (pytest's tmp_path lives there), so a home under it would look writable: use a folder under $HOME."""
    import sys
    if sys.platform != "darwin":
        yield tmp_path
        return
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp(prefix=".aurora-boxtest-", dir=Path.home())).resolve()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def pytest_configure(config):
    config.addinivalue_line("markers", "python_loop: this test is about the Python loop itself; never route it to Rust")


# ── R507: with AURORA_CORE=rust, a test that calls agent.run_turn directly runs it on the Rust loop too ─────────────
class _LoopOnlyEngine:
    """What rustcore.run_turn needs from an engine, for a bare agent.run_turn call: the caller's history list (grown in
    place), its system prompt and cap, a config naming the model, and the caller's provider object (bridged)."""

    class _Stats:
        limit = 0

    class _Log:
        def log(self, *a, **k):
            pass

    def __init__(self, cfg, provider, messages, system, max_iterations):
        self.config_path, self.messages, self.system, self.max_iterations = str(cfg), messages, system, max_iterations
        self.runtime, self.compactions, self.session = {}, 0, self._Log()
        self._provider_for = lambda *a, **k: provider

    def context_stats(self):
        return self._Stats()


def on_rust_loop() -> bool:
    """R508: a test's turns run on the Rust loop unless AURORA_CORE=python (or there is no aurora-rs)."""
    from aurora import rustcore
    return os.environ.get("AURORA_CORE", "").strip().lower() != "python" and rustcore.binary() is not None


@pytest.fixture(autouse=True)
def _run_turn_on_the_rust_loop(monkeypatch, tmp_path_factory, request):
    if not on_rust_loop() or request.node.get_closest_marker("python_loop"):
        yield
        return
    from aurora import agent, rustcore
    real = agent.run_turn
    made = []

    def run_turn(provider, model, messages, system, cb, max_iterations, tools_enabled):
        if not on_rust_loop() or (cb.plan_mode and cb.plan_mode()):
            # a test that switched to the Python core, or plan mode (rustcore.unsupported): Python's loop
            return real(provider, model, messages, system, cb, max_iterations, tools_enabled)
        redact = "true" if cb.secret_challenge is not None else "false"   # how a bare call switches scanning on
        cfg = tmp_path_factory.mktemp("loop") / "config.yaml"
        cfg.write_text("providers:\n  p:\n    type: openai\n    base_url: http://127.0.0.1:9/v1\n"
                       f"models:\n  - provider: p\n    model: {json.dumps(model or 'm')}\n    tools: true\n"
                       f"runtime:\n  sandbox: off\n  redact_secrets: {redact}\n", encoding="utf-8")
        e = _LoopOnlyEngine(cfg, provider, messages, system, max_iterations)
        made.append(e)
        return rustcore.run_turn(e, provider, model or "m", cb, tools_enabled)

    run_turn.python_loop = real
    monkeypatch.setenv("AURORA_NO_LOCAL_CONFIG", "1")   # the shim's config is complete; never the machine's overlay
    monkeypatch.setattr(agent, "run_turn", run_turn)
    monkeypatch.setattr(rustcore, "_PY_RUN_TURN", run_turn)      # the engine's own turns are not "a replaced loop"
    yield
    for e in made:
        if getattr(e, "_rust_core", None):
            e._rust_core.close()


# R507: no test may change the real Aurora home (a test once overwrote the user's allowlist)
def _real_home_state():
    home = Path(os.path.expanduser("~/.aurora"))
    if not home.is_dir():
        return {}
    return {p.name: p.stat().st_mtime_ns for p in (home / "allowlist.yaml", home / "denylist.yaml") if p.is_file()}


_REAL_HOME_AT_START = _real_home_state()


def pytest_sessionfinish(session, exitstatus):
    changed = sorted(k for k, v in _real_home_state().items() if _REAL_HOME_AT_START.get(k) != v)
    if changed:
        print(f"\nFAILED: tests changed the real ~/.aurora: {', '.join(changed)}")
        session.exitstatus = 1
