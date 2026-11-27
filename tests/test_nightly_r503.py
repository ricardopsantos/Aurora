"""R503 — the nightly script: every step runs, a failing step is recorded, the run fails, and the timer is daily."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N = ROOT / "tools" / "nightly"


def test_a_failing_step_fails_the_night_and_the_rest_still_run(tmp_path):
    repo, home, bin_ = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    (repo / ".venv" / "bin").mkdir(parents=True)
    bin_.mkdir()
    calls = tmp_path / "calls"
    for name, code in (("cargo", "case \"$*\" in *clippy*) exit 3;; esac"), ("git", "case \"$1\" in status) ;; rev-parse) echo abc123;; esac")):
        f = bin_ / name
        f.write_text(f"#!/bin/sh\necho {name} \"$@\" >> {calls}\n{code}\nexit 0\n")
        f.chmod(0o755)
    py = repo / ".venv" / "bin" / "python"
    py.write_text(f"#!/bin/sh\necho python \"$@\" >> {calls}\n")
    py.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin", "AURORA_REPO": str(repo), "AURORA_LIVE": "1"}
    p = subprocess.run([str(N / "aurora-nightly.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 1
    log = next((home / ".cache" / "aurora-nightly").glob("*.log")).read_text()
    assert "!! FAILED: cargo clippy" in log and log.rstrip().endswith("result: FAIL") and "commit abc123" in log
    c = calls.read_text()
    assert "AURORA_CORE=python ./.venv/bin/python -m pytest" in log
    for want in ("git pull", "cargo build", "cargo test", "python -m pytest -q -p no:cacheprovider\n", "test_live_r502.py"):
        assert want in c, want
    env.pop("AURORA_LIVE")
    (home / ".cache" / "aurora-nightly").joinpath("x").touch()
    calls.unlink()
    subprocess.run([str(N / "aurora-nightly.sh")], env=env, timeout=60)
    assert "test_live_r502" not in calls.read_text()


def test_the_timer_is_nightly_and_catches_up():
    t = (N / "aurora-nightly.timer").read_text()
    assert "OnCalendar=*-*-* 03:30" in t and "Persistent=true" in t
    s = (N / "aurora-nightly.service").read_text()
    assert "EnvironmentFile=-%h/.config/aurora-nightly.env" in s and "aurora-nightly.sh" in s
    assert os.access(N / "aurora-nightly.sh", os.X_OK)
