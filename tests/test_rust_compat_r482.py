"""R480/R482 (also R311, R337, R478, R479, R481, R485, R486, R293) — the Rust core (`aurora-rs`, crates/) against Python Aurora as
the oracle: same config, same scripted model, same session records, and
Python's readers open what Rust wrote. Skipped when cargo is not installed."""

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CARGO = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo")


@pytest.fixture(scope="module")
def aurora_rs():
    if not Path(CARGO).is_file() or not (ROOT / "Cargo.toml").is_file():
        pytest.skip("cargo not installed")
    subprocess.run([CARGO, "build", "-q", "-p", "aurora-cli"], cwd=ROOT, check=True, timeout=900)
    return str(ROOT / "target" / "debug" / "aurora-rs")


def _sse(*deltas, usage=(30, 5)):
    events = [{"choices": [{"delta": d}]} for d in deltas]
    events.append({"choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def _write_call(path, content):
    args = json.dumps({"path": path, "content": content})
    return _sse({"tool_calls": [{"index": 0, "id": "call_w", "type": "function",
                                 "function": {"name": "write_file", "arguments": args[:9]}}]},
                {"tool_calls": [{"index": 0, "function": {"arguments": args[9:]}}]})


class _Model:
    """A scripted OpenAI-compatible server: each chat request gets the next reply."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        model = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                model.requests.append(json.loads(self.rfile.read(n)))
                body = (model.replies.pop(0) if model.replies else _sse({"content": "(no more)"})).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def _setup(tmp_path, url, redact=False):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir(parents=True)
    work.mkdir(parents=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "providers:\n"
        "  local:\n    type: openai\n    base_url: " + url + "\n"
        "models:\n  - provider: local\n    model: local\n    tools: true\n"
        "runtime:\n  max_iterations: 5\n  redact_secrets: " + ("true" if redact else "false") + "\n  sandbox: off\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AURORA_")}
    env.update(AURORA_HOME=str(home), AURORA_CONFIG=str(cfg), AURORA_KEYRING_TIMEOUT="1",
               AURORA_PYTHON=sys.executable, PYTHONPATH=str(ROOT))
    return home, work, cfg, env


def _records(home):
    files = sorted((home / "sessions").glob("*.jsonl"))
    assert len(files) == 1, files
    sid = files[0].stem
    lines = files[0].read_text(encoding="utf-8").splitlines()
    return sid, [json.loads(line) for line in lines], lines


def _run(argv, env, cwd, timeout=120):
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)


SCRIPT = lambda: [_write_call("note.txt", "olá\n"), _sse({"content": "Wrote "}, {"content": "it."})]  # noqa: E731
CORE = ("user", "approval", "tool", "assistant")


def test_same_task_same_records_as_python(aurora_rs, tmp_path):
    runs = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        model = _Model(SCRIPT())
        try:
            home, work, _cfg, env = _setup(tmp_path / name, model.url)
            env["PYTHONPATH"] = str(ROOT)
            p = _run(argv + ["-t", "write the note", "--yes"], env, work)
        finally:
            model.close()
        assert p.returncode == 0, (name, p.stdout, p.stderr)
        assert p.stdout == "Wrote it.\n", (name, p.stdout)
        assert (work / "note.txt").read_text(encoding="utf-8") == "olá\n"
        _sid, recs, _lines = _records(home)
        runs[name] = ([r for r in recs if r["event"] in CORE], model.requests)
    py, rs = runs["python"][0], runs["rust"][0]
    assert [r["event"] for r in rs] == [r["event"] for r in py] == list(CORE)
    for a, b in zip(py, rs):
        missing = set(a) - set(b) - {"latency_s"}
        assert not missing, (a["event"], missing)
        for k in ("text", "model", "tool", "decision", "name", "output", "args", "chars", "status",
                  "input_tokens", "output_tokens", "billed_input", "cached_input"):
            if k in a:
                assert b[k] == a[k], (a["event"], k, a[k], b[k])
    # both send the model the same conversation on the second request
    py_req, rs_req = runs["python"][1][1], runs["rust"][1][1]
    strip = lambda msgs: [{k: v for k, v in m.items() if k != "content" or m["role"] != "system"}  # noqa: E731
                          for m in msgs]
    assert strip(rs_req["messages"]) == strip(py_req["messages"])
    # R495: extension tools included, through the aurora-ext/1 host
    assert rs_req["tools"] == py_req["tools"]


def test_python_reads_what_rust_wrote(aurora_rs, tmp_path, monkeypatch):
    model = _Model(SCRIPT())
    try:
        home, work, _cfg, env = _setup(tmp_path, model.url)
        p = _run([aurora_rs, "-t", "write the note", "--yes", "--json"], env, work)
    finally:
        model.close()
    assert p.returncode == 0, p.stderr
    report = json.loads(p.stdout)
    assert report["ok"] is True and report["reply"] == "Wrote it."
    assert report["tool_calls"][0]["name"] == "write_file"
    assert set(report) >= {"ok", "exit_code", "reply", "reasons", "model", "session", "tool_calls",
                           "cost_usd", "context_tokens"}
    sid, _recs, lines = _records(home)
    assert report["session"] == sid
    # Python's prefilter matches the literal '"event": "<name>"' (R96e)
    assert all('"event": "' in line for line in lines)
    monkeypatch.setenv("AURORA_HOME", str(home))
    from aurora import session
    s = session.Session(sid)
    assert [r["event"] for r in s.iter_records(events={"assistant"})] == ["assistant"]
    assert session.usage_by_model(sid) == {"local": {"turns": 1, "input": 30, "billed": 60, "output": 10, "cached": 0}}
    md = session.export_markdown(sid)
    assert "write the note" in md and "Wrote it." in md and "write_file" in md
    assert session.latest_session_id() == sid


def test_headless_without_yes_denies_and_exits_1(aurora_rs, tmp_path):
    model = _Model(SCRIPT())
    try:
        home, work, _cfg, env = _setup(tmp_path, model.url)
        p = _run([aurora_rs, "-t", "write the note"], env, work)
    finally:
        model.close()
    assert p.returncode == 1
    assert not (work / "note.txt").exists()
    assert "write_file denied (not on the allowlist)" in p.stderr
    _sid, recs, _ = _records(home)
    appr = [r for r in recs if r["event"] == "approval"]
    assert appr[0]["decision"] == "denied"
    assert [r["output"] for r in recs if r["event"] == "tool"][0].startswith("[denied by user: headless")


def test_denylist_rules_turn_yes_into_refusal(aurora_rs, tmp_path):
    model = _Model(SCRIPT())
    try:
        home, work, _cfg, env = _setup(tmp_path, model.url)
        (home / "denylist.yaml").write_text("write_file:\n  - '*'\n", encoding="utf-8")
        p = _run([aurora_rs, "-t", "write the note", "--yes"], env, work)
    finally:
        model.close()
    assert p.returncode == 1
    assert not (work / "note.txt").exists()


def test_config_parses_like_python(aurora_rs, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.local.yaml").write_text(
        "providers:\n  local:\n    base_url: [http://a.example/v1, http://b.example/v1]\n"
        "runtime:\n  timeout: 60\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text((ROOT / "config.yaml.example").read_text(encoding="utf-8")
                   + "\nskills:\n  dir: ${AURORA_T_DIR}/skills\n  other: x${AURORA_T_UNSET}y\n"
                   # R498: YAML 1.1 booleans, as PyYAML reads them
                   "yaml11:\n  a: off\n  b: Yes\n  c: 'no'\n  d: [on, \"OFF\"]\n  e: offline\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AURORA_")}
    env.update(AURORA_HOME=str(home), AURORA_T_DIR="/opt/t")
    p = _run([aurora_rs, "--config", str(cfg), "--dump-config"], env, tmp_path)
    assert p.returncode == 0, p.stderr
    monkeypatch.setenv("AURORA_HOME", str(home))
    monkeypatch.setenv("AURORA_T_DIR", "/opt/t")
    monkeypatch.delenv("AURORA_T_UNSET", raising=False)
    from aurora.config import load_config
    assert json.loads(p.stdout) == load_config(cfg)


def test_allowlist_skips_the_question_like_python(aurora_rs, tmp_path):
    """R483: a standing rule in allowlist.yaml approves without --yes, and both log `allowlisted`."""
    seen = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        model = _Model(SCRIPT())
        try:
            home, work, _cfg, env = _setup(tmp_path / name, model.url)
            env["PYTHONPATH"] = str(ROOT)
            (home / "allowlist.yaml").write_text(f"write_file:\n- note.txt\n", encoding="utf-8")
            p = _run(argv + ["-t", "write the note"], env, work)
        finally:
            model.close()
        assert p.returncode == 0, (name, p.stdout, p.stderr)
        assert (work / "note.txt").read_text(encoding="utf-8") == "olá\n"
        seen[name] = [r["decision"] for r in _records(home)[1] if r["event"] == "approval"]
    assert seen["python"] == seen["rust"] == ["allowlisted"]


KEY = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def _read_call(path):
    args = json.dumps({"path": path})
    return _sse({"tool_calls": [{"index": 0, "id": "call_r", "type": "function",
                                 "function": {"name": "read_file", "arguments": args}}]})


@pytest.mark.parametrize("where", ["prompt", "write", "tool-output"])
def test_a_secret_stops_a_headless_run_like_python(aurora_rs, tmp_path, where):
    """R484: Python's headless mode ends the run on a likely secret; so does the Rust one, and the secret
    never reaches the session log, the model, or the disk."""
    out = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        script = {"prompt": lambda: [_sse({"content": "never asked"})],
                  "write": lambda: [_write_call("k.txt", KEY)],
                  "tool-output": lambda: [_read_call("leak.txt"), _sse({"content": "never"})]}[where]()
        model = _Model(script)
        try:
            home, work, _cfg, env = _setup(tmp_path / name, model.url, redact=True)
            env["PYTHONPATH"] = str(ROOT)
            (work / "leak.txt").write_text(f"token: {KEY}\n", encoding="utf-8")
            task = f"does {KEY} still work?" if where == "prompt" else "do it"
            p = _run(argv + ["-t", task, "--yes"], env, work)
        finally:
            model.close()
        log = "".join(f.read_text(encoding="utf-8") for f in (home / "sessions").glob("*.jsonl")) if (home / "sessions").exists() else ""
        sent = json.dumps(model.requests)
        out[name] = (p.returncode, f"likely secret in {where if where != 'write' else 'write:write_file'}" in p.stderr
                     or where == "tool-output" and "likely secret in tool:read_file" in p.stderr,
                     (work / "k.txt").exists(), KEY in log, KEY in sent if where != "prompt" else KEY in sent)
    assert out["python"][0] == out["rust"][0] == 1, out
    assert out["rust"][1], out
    assert out["python"][2:] == out["rust"][2:], out
    assert out["rust"][2] is False and out["rust"][3] is False, out


def _cmd_call(command):
    args = json.dumps({"command": command})
    return _sse({"tool_calls": [{"index": 0, "id": "call_c", "type": "function",
                                 "function": {"name": "run_command", "arguments": args}}]})


def test_sandbox_all_boxes_commands_the_same_way_as_python(aurora_rs, tmp_path, box_home_root):
    """R475: with `sandbox: all` a command can write the workspace but neither the home folder nor a hidden secret, in both."""
    if not (shutil.which("bwrap") or (sys.platform == "darwin" and shutil.which("sandbox-exec"))):
        pytest.skip("no command box on this machine")
    seen = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        model = _Model([_cmd_call("echo ok > in_ws.txt; echo x > ~/escape.txt; cat ~/.ssh/id_x"), _sse({"content": "done"})])
        try:
            home, work, cfg, env = _setup(tmp_path / name, model.url)
            env["PYTHONPATH"] = str(ROOT)
            fake_home = box_home_root / name / "uhome"
            (fake_home / ".ssh").mkdir(parents=True)
            (fake_home / ".ssh" / "id_x").write_text("PRIVATE")
            env["HOME"] = str(fake_home)
            cfg.write_text(cfg.read_text().replace("sandbox: off", "sandbox: all"), encoding="utf-8")
            p = _run(argv + ["-t", "go", "--yes"], env, work)
        finally:
            model.close()
        if "sandbox unavailable" in p.stderr or "sandbox" in p.stderr and "cannot" in p.stderr:
            pytest.skip("the box cannot run here")
        result = json.dumps(model.requests[-1]["messages"][-1])
        seen[name] = ((work / "in_ws.txt").exists(), (fake_home / "escape.txt").exists(), "PRIVATE" in result, p.returncode)
    assert seen["python"] == seen["rust"] == (True, False, False, 0), seen


def _tool_call(name, args, cid="call_x"):
    a = json.dumps(args)
    return _sse({"tool_calls": [{"index": 0, "id": cid, "type": "function", "function": {"name": name, "arguments": a}}]})


@pytest.mark.parametrize("ctl", ["python", "rust"])
def test_background_jobs_report_alike_and_never_outlive_the_run(aurora_rs, tmp_path, ctl):
    """R245/R246: start, poll, and leave a job running; both Auroras answer alike and both kill it at exit."""
    marker = f"31{os.getpid() % 1000:03d}.{int(ctl == 'rust')}7"
    argv = [sys.executable, "-m", "aurora"] if ctl == "python" else [aurora_rs]
    model = _Model([_tool_call("run_command", {"command": f"sleep {marker}", "background": True}),
                    _tool_call("check_command", {"job_id": "bg1"}), _tool_call("check_command", {}),
                    _tool_call("cancel_command", {"job_id": "bg9"}), _sse({"content": "done"})])
    try:
        home, work, cfg, env = _setup(tmp_path, model.url)
        env["PYTHONPATH"] = str(ROOT)
        p = _run(argv + ["-t", "go", "--yes"], env, work)
    finally:
        model.close()
    tool_msgs = [m["content"] for m in model.requests[-1]["messages"] if m.get("role") == "tool"]
    assert p.returncode == 0, p.stderr
    assert tool_msgs[0] == '[background job bg1 started]\ncheck on it with check_command(job_id="bg1")'
    assert tool_msgs[1] == "[still running]\n[no output yet]"
    assert tool_msgs[2].startswith("bg1: running ") and tool_msgs[2].endswith(f"s — sleep {marker}")
    assert tool_msgs[3] == "[error: no such background job: bg9]"
    left = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True).stdout.split()
    assert left == [], f"the job outlived the run: {left}"
