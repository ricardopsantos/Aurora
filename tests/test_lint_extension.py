"""Tests for R121: the bundled lint_check extension
(aurora/extensions_bundled/lint_extension.py)."""

import subprocess

from aurora.extensions_bundled import lint_extension as lint


def test_lint_check_reports_missing_file(tmp_path):
    out = lint.lint_check(str(tmp_path / "nope.py"))
    assert "no such file" in out


def test_lint_check_rejects_non_python_files(tmp_path):
    f = tmp_path / "data.json"
    f.write_text("{}")
    out = lint.lint_check(str(f))
    assert "unsupported file type" in out


def test_lint_check_falls_back_to_syntax_check_without_ruff(tmp_path, monkeypatch):
    monkeypatch.setattr(lint.shutil, "which", lambda name: None)
    f = tmp_path / "ok.py"
    f.write_text("x = 1\n")
    out = lint.lint_check(str(f))
    assert "no syntax errors" in out
    assert "ruff not installed" in out


def test_lint_check_fallback_catches_a_real_syntax_error(tmp_path, monkeypatch):
    monkeypatch.setattr(lint.shutil, "which", lambda name: None)
    f = tmp_path / "broken.py"
    f.write_text("def f(:\n    pass\n")
    out = lint.lint_check(str(f))
    assert "syntax error" in out


def test_lint_check_uses_ruff_when_available(tmp_path, monkeypatch):
    f = tmp_path / "ok.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint.shutil, "which", lambda name: "/usr/bin/ruff")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "ruff"
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(lint.subprocess, "run", fake_run)
    out = lint.lint_check(str(f))
    assert out == "ruff: no issues found"


def test_lint_check_surfaces_ruff_findings(tmp_path, monkeypatch):
    f = tmp_path / "messy.py"
    f.write_text("import os\n")
    monkeypatch.setattr(lint.shutil, "which", lambda name: "/usr/bin/ruff")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="messy.py:1:8: F401 'os' imported but unused\n",
            stderr="")

    monkeypatch.setattr(lint.subprocess, "run", fake_run)
    out = lint.lint_check(str(f))
    assert "F401" in out


def test_lint_check_never_raises_on_a_lint_tool_crash(tmp_path, monkeypatch):
    f = tmp_path / "ok.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint.shutil, "which", lambda name: "/usr/bin/ruff")

    def raising_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 15)

    monkeypatch.setattr(lint.subprocess, "run", raising_run)
    out = lint.lint_check(str(f))
    assert "lint error" in out


# ── bundled-extension wiring: the tool is really discoverable ──────────────
def test_lint_check_is_registered_as_a_bundled_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  local: {type: openai, base_url: x}\n"
                  "models:\n  - {provider: local, model: m}\n")
    from aurora import tools
    from aurora.engine import Engine
    Engine(str(cfg))
    try:
        names = [s["name"] for s in tools.specs()]
        assert "lint_check" in names
    finally:
        tools.set_extensions([], {})   # don't leak into later tests
