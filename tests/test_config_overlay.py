"""R316 — $AURORA_HOME/config.local.yaml is merged over the shared config."""
from aurora.config import load_config

BASE = """providers:
  local:
    type: openai
    base_url:
    - https://host.example/v1
    api_key_env: LLAMA_API_KEY
models:
- provider: local
  model: local
"""


def _home(tmp_path, monkeypatch, overlay=None):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AURORA_HOME", str(home))
    if overlay is not None:
        (home / "config.local.yaml").write_text(overlay)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(BASE)
    return cfg


def test_overlay_replaces_the_url_list_and_keeps_other_keys(tmp_path, monkeypatch):
    cfg = _home(tmp_path, monkeypatch,
                "providers:\n  local:\n    base_url:\n    - http://127.0.0.1:18181/v1\n")
    local = load_config(cfg)["providers"]["local"]
    assert local["base_url"] == ["http://127.0.0.1:18181/v1"]
    assert local["api_key_env"] == "LLAMA_API_KEY"
    assert local["type"] == "openai"


def test_overlay_can_add_a_provider(tmp_path, monkeypatch):
    cfg = _home(tmp_path, monkeypatch,
                "providers:\n  extra:\n    type: openai\n    base_url: http://x/v1\n")
    providers = load_config(cfg)["providers"]
    assert set(providers) == {"local", "extra"}


def test_no_overlay_file_changes_nothing(tmp_path, monkeypatch):
    cfg = _home(tmp_path, monkeypatch)
    assert load_config(cfg)["providers"]["local"]["base_url"] == ["https://host.example/v1"]


def test_empty_overlay_file_is_ignored(tmp_path, monkeypatch):
    cfg = _home(tmp_path, monkeypatch, "")
    assert load_config(cfg)["models"] == [{"provider": "local", "model": "local"}]
