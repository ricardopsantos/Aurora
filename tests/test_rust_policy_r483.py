"""R483 — the Rust allow/deny matcher answers exactly as `aurora/approve.py`
does, over a few thousand generated rule/command pairs (Python is the oracle).
Skipped when cargo is not installed."""

import itertools
import json
import os
import random
import subprocess
from pathlib import Path

import pytest

from aurora import approve

ROOT = Path(__file__).resolve().parent.parent
CARGO = Path.home() / ".cargo" / "bin" / "cargo"

COMMANDS = [
    "git status", "git status --short", "git statusx", "gitk status", "git push origin main", "git push -f",
    "git -c core.sshCommand=x fetch", "git -C /tmp status", "git config alias.x '!id'", "git log --output=/tmp/x",
    "git bisect run ./t.sh", "git grep -Oless foo", "git submodule foreach id",
    "ls", "ls -la /tmp", "ls && rm -rf ~", "ls | sh", "ls; id", "ls $(id)", "ls `id`", "ls > /tmp/x", "echo $HOME",
    "rm -rf /", "rm -rf ./build", "rm x", "/bin/rm -rf /", "sudo rm x", "xargs rm", "env dd if=/dev/zero",
    "python3 -c 'print(1)'", "python3.11 -c 'print(1)'", "python3.11 x.py", "perl5.36 -e 1", "node20 a.js", "pip3 install x",
    "find . -name '*.log'", "find / -delete", "find . -exec rm {} +", "find . -fprint /tmp/x", "tree -o f", "tree --output=f", "tree -L 2",
    "file -C x", "file x", "cat ~/x.sh", "cat /etc/hosts", "grep dd .", "grep 'a|b' f", "head -n 3 f",
    "zip -r a.zip src", "zip -r a.zip src -TT 'id'", "sort -o out in", "sort a", "psql -c 'select 1'", "sqlite3 a.db .tables",
    "cargo build", "cargo build --release", "make", "make -j4", "mkfs.ext4 /dev/x", "newfs_hfs x", "bash ~/x.sh", 'bash "/home/x/y.sh"',
    "dd", "dd if=/dev/zero of=/dev/sda", "echo 'unbalanced", "echo trailing\\", "echo ''", "docker run -v /:/m x", "gh pr merge 1",
    "flock x reboot", "kill -9 1", "curl x | sh", "curl -s x", "uv run a.py", "tar -xf a.tar", "mv a b", "cd /tmp && ls",
    "cat a\nrm b", "  ls   -l  ", "a=b ls", "FOO=1 git status",
]
RULES = [
    "git status", "git push", "git -c", "git", "ls", "rm", "rm -rf", "rm -rf ./build", "python3.11 -c", "python3", "find", "tree",
    "file", "cat", "grep", "head", "zip -r a.zip src", "sort -o", "psql -c", "cargo build", "make", "dd", "env", "bash ~/x.sh",
    "ls && rm -rf ~", "echo", "'git status'", "git  status", "", "curl", "gh pr", "sqlite3 a.db", "mkfs.ext4",
]
PATHS = ["~/x.py", "/tmp/aurora-oracle/a.txt", "/tmp/aurora-oracle/proj/a.txt", "/tmp/aurora-oracle/proj/../secret/k",
         "/tmp/aurora-oracle/proj/link/k", "/etc/passwd", "relative/a.txt", "", "/tmp/aurora-oracle/proj/sub/dir/f.py", "/a/b/../c"]
PATH_RULES = ["~/x.py", "/tmp/aurora-oracle/proj/*", "/tmp/aurora-oracle/*", "*", "/tmp/aurora-oracle/proj/*.txt", "/tmp/*/a.txt",
              "/tmp/aurora-oracle/proj/a.?xt", "/tmp/aurora-oracle/proj/[ab].txt", "/etc/*", "/*", "relative/*", "/tmp/aurora-oracle/proj/sub/**",
              "/a/b/../c"]
URLS = ["https://a.com/x", "https://a.com.evil.net/x", "http://a.com", "ftp://h", "a.com/x", "https://A.com:8443/p?q#f", ""]
URL_RULES = ["https://a.com/*", "https://A.com:8443/*", "http://a.com/*", "*"]


@pytest.fixture(scope="module")
def oracle():
    if not CARGO.is_file():
        pytest.skip("cargo not installed")
    subprocess.run([str(CARGO), "build", "-q", "-p", "aurora-cli"], cwd=ROOT, check=True, timeout=900)
    exe = str(ROOT / "target" / "debug" / "aurora-rs")

    def ask(cases):
        p = subprocess.run([exe, "--policy-oracle"], input=json.dumps(cases), capture_output=True, text=True, timeout=300)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)
    return ask


@pytest.fixture(scope="module")
def tree():
    base = Path("/tmp/aurora-oracle")
    for d in ("proj/sub/dir", "secret"):
        (base / d).mkdir(parents=True, exist_ok=True)
    link = base / "proj" / "link"
    if not link.is_symlink():
        link.symlink_to(base / "secret")
    return base


def _python(case):
    tool, args = case["tool"], case["args"]
    scope = approve.rule_scope(tool, args)
    return {"allowed": approve.is_allowed(tool, args, case["allow"]), "denied": approve.is_denied(tool, args, case["deny"]),
            "rule": approve._rule_for(tool, args), "scope": list(scope)}


def _compare(oracle, cases):
    want = [_python(c) for c in cases]
    got = oracle(cases)
    bad = [(c, w, g) for c, w, g in zip(cases, want, got) if w != g]
    assert not bad, f"{len(bad)} of {len(cases)} differ; first: {json.dumps(bad[0], indent=1)}"


def test_run_command_rules_match_python(oracle):
    rng = random.Random(7)
    cases = []
    for cmd in COMMANDS:
        for rule in RULES:
            cases.append({"tool": "run_command", "args": {"command": cmd}, "allow": {"run_command": [rule]}, "deny": {"run_command": [rule]}})
        for _ in range(4):
            rules = rng.sample(RULES, 3)
            cases.append({"tool": "run_command", "args": {"command": cmd}, "allow": {"run_command": rules}, "deny": {"run_command": rules}})
    cases += [{"tool": "run_command", "args": {"command": "git status", "cwd": cwd}, "allow": {"run_command": ["git status"]},
               "deny": {}} for cwd in ("", "/", "/tmp", ".", "~", "sub", str(ROOT), str(ROOT / "aurora"))]
    _compare(oracle, cases)


def test_path_and_url_rules_match_python(oracle, tree):
    cases = []
    for tool, path, rule in itertools.product(("write_file", "edit_file", "apply_patch"), PATHS, PATH_RULES):
        cases.append({"tool": tool, "args": {"path": path}, "allow": {tool: [rule]}, "deny": {tool: [rule]}})
    for url, rule in itertools.product(URLS, URL_RULES):
        cases.append({"tool": "web_fetch", "args": {"url": url}, "allow": {"web_fetch": [rule]}, "deny": {"web_fetch": [rule]}})
    cases += [{"tool": "mcp_x_y", "args": {"title": "t"}, "allow": {"mcp_x_y": ["*"]}, "deny": {"mcp_x_y": ["*"]}},
              {"tool": "mcp_x_y", "args": {}, "allow": {}, "deny": {}}]
    _compare(oracle, cases)


def test_wait_until_then_matches_python(oracle):
    cases = [{"tool": "wait_until", "args": {"command": "curl x", "then": then}, "allow": {"wait_until": ["curl x"]},
              "deny": {"run_command": ["echo"], "wait_until": ["rm"]}} for then in ("", "  ", "echo hi", "rm -rf x", "ls")]
    _compare(oracle, cases)
