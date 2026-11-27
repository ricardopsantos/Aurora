"""R315 — AURORA_CONFIG picks the config `aurora` starts with."""
import pytest

from aurora import __main__ as entry


def test_env_config_wins_over_the_repo_config(tmp_path, monkeypatch):
    mine = tmp_path / "pi.yaml"
    mine.write_text("models: []\n")
    monkeypatch.setenv("AURORA_CONFIG", str(mine))
    assert entry._default_config() == str(mine)


def test_unset_keeps_the_repo_config(monkeypatch, tmp_path):
    # config.yaml is untracked: a fresh checkout has none, so give AURORA_HOME one
    (tmp_path / "config.yaml").write_text("models: []\n")
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.delenv("AURORA_CONFIG", raising=False)
    assert entry._default_config().endswith("config.yaml")


def test_env_pointing_at_nothing_stops_instead_of_falling_back(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(SystemExit) as e:
        entry._default_config()
    assert "AURORA_CONFIG is not a file" in str(e.value)
