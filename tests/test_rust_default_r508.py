"""R508 — the Rust loop is the default; `runtime.core: python` keeps Python's; a turn
the core cannot take falls back quietly unless the user asked for Rust."""

import pytest

from aurora import rustcore
from tests.test_core import FakeProvider, _FEQuiet, _mk_engine
from tests.test_rust_compat_r482 import aurora_rs  # noqa: F401
from aurora.providers.base import TurnResult


class _FE(_FEQuiet):
    def __init__(self):
        self.notes = []

    def notify(self, m):
        self.notes.append(m)


@pytest.fixture
def eng(tmp_path, monkeypatch, aurora_rs):
    monkeypatch.delenv("AURORA_CORE", raising=False)
    monkeypatch.setenv("AURORA_RS", aurora_rs)
    e = _mk_engine(tmp_path, monkeypatch)
    e.redact_secrets = False
    monkeypatch.setattr(e, "_provider_for", lambda *a, **k: FakeProvider([TurnResult(text="hi", stop_reason="end")]))
    yield e
    if getattr(e, "_rust_core", None):
        e._rust_core.close()


def test_rust_is_the_default_and_python_can_be_kept(eng, monkeypatch):
    assert rustcore.wanted(eng)
    eng.send("q", _FE())
    assert eng._rust_core is not None                   # the turn ran on the Rust core
    eng.runtime["core"] = "python"
    assert not rustcore.wanted(eng)
    monkeypatch.setenv("AURORA_CORE", "rust")
    assert rustcore.wanted(eng)


@pytest.mark.parametrize("asked", [False, True])
def test_a_turn_the_core_cannot_take_falls_back_quietly_unless_rust_was_asked_for(eng, asked):
    if asked:
        eng.runtime["core"] = "rust"
    eng.plan_mode = True
    fe = _FE()
    eng.send("q", fe)
    told = [n for n in fe.notes if "runs on the Python core" in n]
    assert bool(told) == asked
    assert getattr(eng, "_rust_core", None) is None
