"""R309 (edit-failure hints) and R310 (command output keeps head AND tail)."""

from aurora import tools


def _edit(tmp_path, content, old):
    f = tmp_path / "f.txt"
    f.write_bytes(content)
    out = tools.edit_file(str(f), old, "X")
    assert f.read_bytes() == content
    return out


def test_hint_whitespace(tmp_path):
    assert "leading/trailing whitespace" in _edit(tmp_path, b"a = 1\n", "  a = 1\n\n")


def test_hint_indentation(tmp_path):
    assert "tabs vs spaces" in _edit(tmp_path, b"if x:\n\treturn 1\n", "if x:\n    return 1")


def test_hint_first_line(tmp_path):
    assert "first line exists" in _edit(tmp_path, b"def f():\n    return 1\n", "def f():\n    return 2")


def test_no_hint(tmp_path):
    assert _edit(tmp_path, b"abc\n", "zzz") == "[error: `old` text not found — it must match exactly]"


def test_crlf_file_no_false_hint(tmp_path):
    assert _edit(tmp_path, b"one\r\ntwo\r\n", "one\ntwo\nthree") .endswith("]")
    assert "whitespace" not in _edit(tmp_path, b"one\r\ntwo\r\n", "one\ntwo\nthree")


def test_command_output_keeps_head_and_tail():
    cap = tools._HeadTail(cap=4096)
    cap.add(b"START\n")
    for _ in range(2000):
        cap.add(b"x" * 100 + b"\n")
    cap.add(b"FINAL: 3 failed\n")
    t = cap.text()
    assert t.startswith("START") and t.endswith("FINAL: 3 failed\n")
    assert "bytes omitted" in t and len(t) < 8000


def test_run_command_end_marker_survives_and_exit_reported():
    out = tools.run_command(
        "echo START; seq 1 200000; echo 'FINAL: 3 failed'; exit 3")
    assert out.startswith("START")
    assert "FINAL: 3 failed" in out and "omitted from the middle" in out
    assert out.endswith("[exit 3]")
    assert len(out) < tools.COMMAND_RESULT_LIMIT + 300


def test_under_cap_unchanged():
    assert tools.run_command("echo hi") == "hi"


def _patch(tmp_path, content, diff):
    f = tmp_path / "p.txt"
    f.write_bytes(content)
    out = tools.apply_patch(str(f), diff)
    assert f.read_bytes() == content
    return out


def test_apply_patch_miss_gets_the_same_hints(tmp_path):
    base = b"if x:\n\treturn 1\n"
    out = _patch(tmp_path, base, "@@ -1,2 +1,2 @@\n if x:\n-    return 1\n+    return 2\n")
    assert "tabs vs spaces" in out
    out = _patch(tmp_path, b"def f():\n    return 1\n",
                 "@@ -1,2 +1,2 @@\n def f():\n-    return 9\n+    return 2\n")
    assert "first line exists" in out
    out = _patch(tmp_path, b"total_count = 5\n", "@@ -1,2 +1,2 @@\n-count = 5\n+count = 6\n")
    assert "inside a longer line" in out


def test_first_line_hint_needs_one_whole_line_match(tmp_path):
    # `}` is on many lines: pointing at "the first line" would point at nothing
    assert "first line" not in _edit(tmp_path, b"a {\n}\nb {\n}\n", "}\nzzz")
    # a substring of a longer line is not "the first line exists"
    assert "first line" not in _edit(tmp_path, b"total = f(x)\n", "f(x)\nzzz")


def test_timed_out_command_output_is_limited():
    old = tools.COMMAND_TIMEOUT
    tools.set_command_timeout(1)
    try:
        out = tools.run_command("echo START; yes ABCDEFGH")
    finally:
        tools.set_command_timeout(old)
    assert out.startswith("START") and "[timeout after" in out
    assert len(out) < tools.COMMAND_RESULT_LIMIT + 300


def test_cancelled_command_output_is_limited():
    out = tools._cancelled_text(tools.CommandCancelled("y\n" * 200_000))
    assert "omitted from the middle" in out and "[cancelled by user" in out
    assert len(out) < tools.COMMAND_RESULT_LIMIT + 300


def test_skill_output_is_limited(tmp_path):
    from aurora import skills
    sk = tmp_path / "skills"
    sk.mkdir()
    (sk / "noisy.py").write_text(
        "# noisy\nprint('START'); print('x\\n' * 100000); print('END')\n")
    out = skills.run("noisy", "", str(tmp_path))
    assert out.startswith("START") and out.endswith("END")
    assert len(out) < tools.COMMAND_RESULT_LIMIT + 300
