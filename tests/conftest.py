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

import os

import pytest

os.environ.setdefault("AURORA_KEYRING_TIMEOUT", "1")   # R325: never hang a suite

from aurora import tools


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    before = (tools._EXTENSION_SPECS, tools._EXTENSION_RUNNERS)
    yield
    tools._EXTENSION_SPECS, tools._EXTENSION_RUNNERS = before


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
