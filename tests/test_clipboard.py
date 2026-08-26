"""Tests for aurora.clipboard — previously untested despite being the
security-relevant path model output reaches the real OS clipboard through
(R171/S4)."""

import base64
import subprocess

from aurora import clipboard

# ── _local_tool ──────────────────────────────────────────────────────────

def test_local_tool_uses_first_available_tool(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which",
                        lambda t: f"/usr/bin/{t}" if t == "wl-copy" else None)
    calls = []
    monkeypatch.setattr(clipboard.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    assert clipboard._local_tool("hi") == "wl-copy"
    assert calls == [["wl-copy"]]


def test_local_tool_tries_pbcopy_before_wl_copy_before_xclip(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(clipboard.subprocess, "run", lambda cmd, **kw: None)
    assert clipboard._local_tool("hi") == "pbcopy"


def test_local_tool_falls_through_a_failing_tool_to_the_next(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda t: f"/usr/bin/{t}")

    def flaky_run(cmd, **kw):
        if cmd[0] == "pbcopy":
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(clipboard.subprocess, "run", flaky_run)
    assert clipboard._local_tool("hi") == "wl-copy"


def test_local_tool_returns_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda t: None)
    assert clipboard._local_tool("hi") is None


def test_local_tool_returns_none_when_every_tool_fails(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda t: f"/usr/bin/{t}")

    def always_fail(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(clipboard.subprocess, "run", always_fail)
    assert clipboard._local_tool("hi") is None


def test_local_tool_passes_text_as_encoded_stdin(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which",
                        lambda t: "/usr/bin/pbcopy" if t == "pbcopy" else None)
    captured = {}

    def capture_run(cmd, input=None, **kw):
        captured["input"] = input

    monkeypatch.setattr(clipboard.subprocess, "run", capture_run)
    clipboard._local_tool("héllo")
    assert captured["input"] == "héllo".encode()


# ── copy(): dispatch logic ───────────────────────────────────────────────

def _no_ssh(monkeypatch):
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)


def _over_ssh(monkeypatch):
    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)


def test_copy_prefers_local_tool_when_not_over_ssh(monkeypatch):
    _no_ssh(monkeypatch)
    monkeypatch.setattr(clipboard, "_local_tool", lambda t: "pbcopy")
    monkeypatch.setattr(clipboard, "_osc52",
                        lambda t: (_ for _ in ()).throw(
                            AssertionError("osc52 must not run when a local tool works")))
    assert clipboard.copy("hi") == "pbcopy"


def test_copy_falls_back_to_osc52_when_no_local_tool_and_not_over_ssh(monkeypatch):
    _no_ssh(monkeypatch)
    monkeypatch.setattr(clipboard, "_local_tool", lambda t: None)
    monkeypatch.setattr(clipboard, "_osc52", lambda t: True)
    assert clipboard.copy("hi") == "OSC52 (terminal)"


def test_copy_reports_failure_when_nothing_works_locally(monkeypatch):
    _no_ssh(monkeypatch)
    monkeypatch.setattr(clipboard, "_local_tool", lambda t: None)
    monkeypatch.setattr(clipboard, "_osc52", lambda t: False)
    assert clipboard.copy("hi") == "failed — no clipboard method available"


def test_copy_over_ssh_prefers_osc52_never_tries_local_tool_first(monkeypatch):
    _over_ssh(monkeypatch)
    monkeypatch.setattr(
        clipboard, "_local_tool",
        lambda t: (_ for _ in ()).throw(
            AssertionError("local tool must not run first over SSH")))
    monkeypatch.setattr(clipboard, "_osc52", lambda t: True)
    assert clipboard.copy("hi") == "OSC52 (terminal)"


def test_copy_over_ssh_falls_back_to_remote_side_local_tool(monkeypatch):
    _over_ssh(monkeypatch)
    monkeypatch.setattr(clipboard, "_osc52", lambda t: False)
    monkeypatch.setattr(clipboard, "_local_tool", lambda t: "xclip")
    assert clipboard.copy("hi") == "xclip (remote side!)"


def test_copy_over_ssh_everything_fails(monkeypatch):
    _over_ssh(monkeypatch)
    monkeypatch.setattr(clipboard, "_osc52", lambda t: False)
    monkeypatch.setattr(clipboard, "_local_tool", lambda t: None)
    assert clipboard.copy("hi") == "failed — no clipboard method available"


def test_copy_detects_ssh_via_ssh_connection_var_too(monkeypatch):
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 1 5.6.7.8 22")
    monkeypatch.setattr(
        clipboard, "_local_tool",
        lambda t: (_ for _ in ()).throw(
            AssertionError("local tool must not run first when SSH_CONNECTION is set")))
    monkeypatch.setattr(clipboard, "_osc52", lambda t: True)
    assert clipboard.copy("hi") == "OSC52 (terminal)"


# ── _osc52 ───────────────────────────────────────────────────────────────

def test_osc52_writes_to_dev_tty_when_available(monkeypatch):
    class _FakeTty:
        def __init__(self):
            self.written = []

        def write(self, s):
            self.written.append(s)

        def flush(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    ft = _FakeTty()
    monkeypatch.setattr(clipboard, "open", lambda path, mode: ft, raising=False)
    assert clipboard._osc52("hello") is True
    assert len(ft.written) == 1
    assert ft.written[0].startswith("\x1b]52;c;")
    assert ft.written[0].endswith("\x07")


def test_osc52_falls_back_to_real_stdout_when_no_dev_tty(monkeypatch):
    def boom(path, mode):
        raise OSError("no such device")

    monkeypatch.setattr(clipboard, "open", boom, raising=False)

    class _FakeStdout:
        isatty_val = True
        written = []

        def isatty(self):
            return self.isatty_val

        def write(self, s):
            self.written.append(s)

        def flush(self):
            pass

    fake = _FakeStdout()
    monkeypatch.setattr(clipboard.sys, "__stdout__", fake)
    assert clipboard._osc52("hello") is True
    assert fake.written and fake.written[0].startswith("\x1b]52;c;")


def test_osc52_fails_cleanly_when_stdout_is_not_a_tty(monkeypatch):
    def boom(path, mode):
        raise OSError("no such device")

    monkeypatch.setattr(clipboard, "open", boom, raising=False)

    class _NotATty:
        def isatty(self):
            return False

    monkeypatch.setattr(clipboard.sys, "__stdout__", _NotATty())
    assert clipboard._osc52("hello") is False


def test_osc52_fails_cleanly_when_real_stdout_is_none(monkeypatch):
    def boom(path, mode):
        raise OSError("no such device")

    monkeypatch.setattr(clipboard, "open", boom, raising=False)
    monkeypatch.setattr(clipboard.sys, "__stdout__", None)
    assert clipboard._osc52("hello") is False


def test_osc52_payload_round_trips_through_base64():
    """The escape sequence's payload must be exactly the base64 of the
    input text, so a compliant terminal reconstructs it byte-for-byte."""
    captured = {}

    class _FakeTty:
        def write(self, s):
            captured["seq"] = s

        def flush(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import unittest.mock
    with unittest.mock.patch.object(clipboard, "open",
                                    lambda path, mode: _FakeTty(), create=True):
        clipboard._osc52("hello world")
    payload = captured["seq"][len("\x1b]52;c;"):-1]  # strip prefix + trailing BEL
    assert base64.b64decode(payload) == b"hello world"


def test_osc52_truncates_large_payload_to_valid_base64():
    """The 100_000-byte truncation cap is a magic number with no comment
    tying it to base64's 4-byte-group alignment — if it were ever changed
    to a value not divisible by 4, the truncated payload would decode to
    garbage on the last group. Pin the current value's safety AND catch a
    future edit that breaks it."""
    text = "x" * 200_000   # base64-encodes to ~266KB, well past the cap
    full_b64 = base64.b64encode(text.encode())
    assert len(full_b64) > 100_000   # sanity: this text actually needs truncation

    captured = {}

    class _FakeTty:
        def write(self, s):
            captured["seq"] = s

        def flush(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import unittest.mock
    with unittest.mock.patch.object(clipboard, "open",
                                    lambda path, mode: _FakeTty(), create=True):
        clipboard._osc52(text)
    payload = captured["seq"][len("\x1b]52;c;"):-1]
    assert len(payload) == 100_000
    # must be valid, decodable base64 — not a corrupted mid-group truncation
    decoded = base64.b64decode(payload)
    assert decoded == text.encode()[:len(decoded)]


# ── R235: OSC52 truncation must be reported, never silent ────────────────

def test_osc52_would_truncate_is_false_for_small_text():
    assert clipboard._osc52_would_truncate("hello") is False


def test_osc52_would_truncate_is_true_past_the_cap():
    # 100_000 base64 bytes carry 75_000 raw bytes; one more raw byte spills.
    assert clipboard._osc52_would_truncate("x" * 75_001) is True
    assert clipboard._osc52_would_truncate("x" * 75_000) is False


def test_copy_reports_truncation_when_osc52_drops_the_tail(monkeypatch):
    """R235: a session export copied over SSH used to report a clean
    'OSC52 (terminal)' while only ~75KB of it reached the clipboard — the
    user pasted a silently-cut file with nothing to notice."""
    monkeypatch.setenv("SSH_TTY", "/dev/pts/0")
    monkeypatch.setattr(clipboard, "_osc52", lambda t: True)
    result = clipboard.copy("x" * 200_000)
    assert "TRUNCATED" in result


def test_copy_does_not_claim_truncation_for_a_small_payload(monkeypatch):
    monkeypatch.setenv("SSH_TTY", "/dev/pts/0")
    monkeypatch.setattr(clipboard, "_osc52", lambda t: True)
    assert clipboard.copy("short") == "OSC52 (terminal)"
