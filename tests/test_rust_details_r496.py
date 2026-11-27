"""R496 — N4 coverage for behaviours the Rust core already had without their
own differential test: non-object tool arguments (R268), head-and-tail of long
command output (R310), secret variables kept from commands (R448) and the
private AURORA_HOME (R450). Each runs the same scripted task through both."""

import json
import stat
import sys

from tests.test_rust_compat_r482 import _Model, _records, _run, _setup, _sse, aurora_rs  # noqa: F401


def _raw_call(name, arguments, cid="call_r"):
    return _sse({"tool_calls": [{"index": 0, "id": cid, "type": "function",
                                 "function": {"name": name, "arguments": arguments}}]})


def _both(aurora_rs, tmp_path, replies, extra_env=None):
    out = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        model = _Model(list(replies))
        try:
            home, work, _cfg, env = _setup(tmp_path / name, model.url)
            env.update(extra_env or {})
            p = _run(argv + ["-t", "go", "--yes"], env, work)
        finally:
            model.close()
        _sid, recs, _ = _records(home)
        tool_msgs = [m["content"] for m in model.requests[-1]["messages"] if m.get("role") == "tool"]
        out[name] = (p.returncode, [r.get("output") for r in recs if r["event"] == "tool"], tool_msgs, home)
    return out


def test_non_object_arguments_are_a_malformed_call(aurora_rs, tmp_path):
    got = _both(aurora_rs, tmp_path, [_raw_call("read_file", "[1, 2]"), _sse({"content": "ok"})])
    assert got["rust"][:3] == got["python"][:3]


def test_long_output_keeps_head_and_tail(aurora_rs, tmp_path):
    cmd = json.dumps({"command": "seq 1 40000"})
    got = _both(aurora_rs, tmp_path, [_raw_call("run_command", cmd), _sse({"content": "ok"})])
    assert got["rust"][:3] == got["python"][:3]
    out = got["rust"][2][0]
    assert "\n1\n" in "\n" + out and "40000" in out


def test_commands_never_see_secret_variables(aurora_rs, tmp_path):
    cmd = json.dumps({"command": "env | grep -c -E '^(GITHUB_TOKEN|AWS_SECRET_ACCESS_KEY|SSH_AUTH_SOCK|MY_PAT|PLAIN_VAR)=' ; echo ${PLAIN_VAR}"})
    env = {"GITHUB_TOKEN": "x1", "AWS_SECRET_ACCESS_KEY": "x2", "SSH_AUTH_SOCK": "/x", "MY_PAT": "x3", "PLAIN_VAR": "kept"}
    got = _both(aurora_rs, tmp_path, [_raw_call("run_command", cmd), _sse({"content": "ok"})], env)
    assert got["rust"][:3] == got["python"][:3]
    assert "kept" in got["rust"][2][0]


def test_aurora_home_is_private(aurora_rs, tmp_path):
    got = _both(aurora_rs, tmp_path, [_sse({"content": "ok"})])
    modes = {k: stat.S_IMODE(v[3].stat().st_mode) for k, v in got.items()}
    assert modes["rust"] == modes["python"] == 0o700
