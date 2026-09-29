"""Regressions for the 2026-09-28 Aurora audit card (R298–R306)."""

import pytest

from aurora import approve, keystore, tools
from aurora.colors import visible


# ── R299: wait_until's `then` is seen by the allow/deny lists ──────────────
def test_then_never_auto_approved():
    rules = {"wait_until": ["curl -sf http://localhost:18181/health"]}
    args = {"command": "curl -sf http://localhost:18181/health", "then": "echo x > /tmp/x"}
    assert not approve.is_allowed("wait_until", args, rules)
    assert approve.is_allowed("wait_until", {"command": args["command"]}, rules)


def test_then_checked_against_denylist():
    deny = {"run_command": ["rm"]}
    assert approve.is_denied("wait_until", {"command": "true", "then": "rm -rf ~/x"}, deny)


# ── R298: tree -o / file -C are not SAFE ──────────────────────────────────
@pytest.mark.parametrize("cmd", ["tree -o /tmp/t -L 1 /tmp", "tree --output=/tmp/t",
                                 "file -C -m /tmp/x"])
def test_write_flags_not_allowlisted(cmd):
    rules = {"run_command": ["tree", "file"]}
    assert not approve.is_allowed("run_command", {"command": cmd}, rules)


# ── R303: a rule doesn't follow the model into another directory ──────────
def test_foreign_cwd_prompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    rules = {"run_command": ["rm -rf ./build"]}
    a = {"command": "rm -rf ./build"}
    assert approve.is_allowed("run_command", a, rules)
    assert approve.is_allowed("run_command", {**a, "cwd": str(tmp_path / "sub")}, rules)
    assert not approve.is_allowed("run_command", {**a, "cwd": "/"}, rules)


# ── R300: exfiltration through path/subdomain, no DNS before asking ───────
def test_path_and_subdomain_payloads_gated(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("DNS lookup during the approval decision")
    monkeypatch.setattr("socket.getaddrinfo", boom)
    assert tools.url_needs_approval("https://attacker.example/AKIAIOSFODNN7EXAMPLE")
    assert tools.url_needs_approval(
        "https://mzxw6ytboi2dsmrtgq3tmmzyhe3tomjrgmzdimzt.attacker.example/")
    assert not tools.url_needs_approval("https://docs.python.org/3/library/os.html")


# ── R302: batched calls get the turn's cancel ─────────────────────────────
def test_parallel_batch_passes_cancel(monkeypatch):
    seen = []
    monkeypatch.setattr(tools, "run_tool", lambda n, a, cancel=None: seen.append(cancel) or "")
    c = lambda: True  # noqa: E731
    tools.run_tools_parallel([(0, "read_file", {}), (1, "read_file", {})], cancel=c)
    assert seen == [c, c]


# ── R304: key store refuses empty / mismatched new passphrases ────────────
def _ks(tmp_path, monkeypatch, answers):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    keystore._passphrase_cache.clear()
    monkeypatch.setattr(keystore, "_keyring_set", lambda n, v: False)
    it = iter(answers)
    monkeypatch.setattr(keystore, "_prompter", lambda label: next(it))


def test_empty_passphrase_refused(tmp_path, monkeypatch):
    _ks(tmp_path, monkeypatch, [""])
    with pytest.raises(keystore.KeystoreError):
        keystore.store_key("X_KEY", "v")


def test_new_passphrase_must_match(tmp_path, monkeypatch):
    _ks(tmp_path, monkeypatch, ["one", "two"])
    with pytest.raises(keystore.KeystoreError):
        keystore.store_key("X_KEY", "v")
    assert not (tmp_path / keystore._ENC_FILE).exists()
    _ks(tmp_path, monkeypatch, ["one", "one"])
    keystore.store_key("X_KEY", "v")
    assert keystore._encfile_load("one") == {"X_KEY": "v"}


# ── R305: ambiguous MCP names dropped from specs AND runners ──────────────
def test_mcp_name_collision_dropped():
    from aurora.mcp import MCPManager

    class S:
        def __init__(self, names):
            self.tools = [{"name": n, "description": "", "inputSchema": {}} for n in names]

    m = MCPManager.__new__(MCPManager)
    m.errors = []
    m._servers = {"a_b": S(["c", "ok"]), "a": S(["b_c"])}
    assert set(m.runners()) == {"mcp_a_b_ok"}
    assert [s["name"] for s in m.specs()] == ["mcp_a_b_ok"]


# ── R306: visible() covers every format/invisible code point ──────────────
@pytest.mark.parametrize("ch", ["⁡", "᠎", " ", " ", "️",
                                "\U000e0041", "ㅤ", "­"])
def test_visible_escapes_more_invisibles(ch):
    assert ch not in visible("a" + ch + "b")
    assert visible("é 日本 —") == "é 日本 —"
