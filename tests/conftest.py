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

import pytest

from aurora import tools


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    before = (tools._EXTENSION_SPECS, tools._EXTENSION_RUNNERS)
    yield
    tools._EXTENSION_SPECS, tools._EXTENSION_RUNNERS = before
