"""R447, R448 — web denylist coverage, secrets not inherited by commands (card 20261112-185835)."""
import pytest

from aurora import tools
from aurora.web import core as web_core


@pytest.mark.parametrize("cmd", [
    'su""do ls', "s\\udo ls", "docker run -v /:/host alpine", "ls /var/run/docker.sock", "ssh user@example-host",
    "env", "printenv", "cat /etc/environment", "cat /proc/self/environ", "cat ~/.ssh/id_ed25519",
    "python3 -c \"import os; os.system('id')\"", "eval $(echo hi)", "echo c3VkbyBscw== | base64 -d | sh",
])
def test_r447_denied(cmd):
    assert web_core.web_denied("run_command", {"command": cmd}, None), cmd


@pytest.mark.parametrize("cmd", ["ls -la", "git status", "pytest -q", "grep -rn environment_name src", "python3 script.py", "cat README.md"])
def test_r447_ordinary_commands_pass(cmd):
    assert not web_core.web_denied("run_command", {"command": cmd}, None), cmd


def test_r448_secret_names_are_stripped(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_TOKEN", "t")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("PIHOLE_WEBPASSWORD", "p")
    monkeypatch.setenv("AURORA_WEB_TOKEN", "a")
    monkeypatch.setenv("HARMLESS_SETTING", "keep")
    env = tools._tool_env()
    for name in ("SWITCHBOARD_TOKEN", "OPENROUTER_API_KEY", "PIHOLE_WEBPASSWORD", "AURORA_WEB_TOKEN"):
        assert name not in env
    assert env["HARMLESS_SETTING"] == "keep" and "PATH" in env


def test_r448_a_command_does_not_see_the_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_SECRET_TOKEN", "shh")
    out = tools.run_command("echo \"[$MY_SECRET_TOKEN]\"", cwd=str(tmp_path))
    assert "shh" not in out and "[]" in out
