"""The one deliberate exception to the rest of the suite's no-network rule
(see test_core.py/test_finish.py docstrings) — exercises /bootstrap set
<url> against a real, stable URL (this repo's own MAIN_PROMPT.md, mirrored
in the AgenticContext repo) instead of a mocked fetch_url. Skips instead of
failing if the network isn't reachable from this environment."""

import httpx
import pytest

from aurora import bootstrap

REAL_URL = ("https://raw.githubusercontent.com/ricardopsantos/AgenticContext/"
           "refs/heads/main/MAIN_PROMPT.md")


def _require_network():
    try:
        httpx.get(REAL_URL, timeout=10)
    except httpx.HTTPError as e:
        pytest.skip(f"network unreachable: {e}")


def test_fetch_url_downloads_real_content():
    _require_network()
    text = bootstrap.fetch_url(REAL_URL)
    assert "Session bootstrap" in text


def test_is_url_recognizes_the_real_url():
    assert bootstrap.is_url(REAL_URL)


def test_set_from_real_url_then_refresh(tmp_path, monkeypatch):
    _require_network()
    monkeypatch.setattr(bootstrap, "_global_path",
                        lambda: tmp_path / "bootstrap.md")
    bootstrap.save(bootstrap.fetch_url(REAL_URL), source_url=REAL_URL)
    text, source = bootstrap.load(tmp_path)
    assert "Session bootstrap" in text
    assert bootstrap.source_url(tmp_path) == REAL_URL

    refreshed = bootstrap.refresh_from_source(tmp_path)
    assert refreshed is not None
    new_text, path = refreshed
    assert "Session bootstrap" in new_text
    assert path == tmp_path / "bootstrap.md"
    # sidecar URL survives the refresh, unchanged
    assert bootstrap.source_url(tmp_path) == REAL_URL
