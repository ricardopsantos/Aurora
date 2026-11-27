"""R502 — live tests against a real local model (review note N2), opt-in.

Run with AURORA_LIVE=1, AURORA_LIVE_URL set to a local OpenAI-compatible server
(llama.cpp or Ollama, ending in /v1) and, if it wants one, LLAMA_API_KEY. Only
local servers: no paid keys. Both cores do the same real task: stream a reply, make a real tool call,
and log it the same way. A small model's wording differs between runs, so the
checks are on the shape, not the text."""

import os
import sys

import pytest

from tests.test_rust_compat_r482 import ROOT, _records, _run, aurora_rs  # noqa: F401

LIVE = os.environ.get("AURORA_LIVE") == "1"
URL = os.environ.get("AURORA_LIVE_URL", "")
pytestmark = pytest.mark.skipif(not (LIVE and URL), reason="live tests: set AURORA_LIVE=1 and AURORA_LIVE_URL (local model only)")


def _setup(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir(parents=True)
    work.mkdir(parents=True)
    (work / "secret-word.txt").write_text("periwinkle\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  local:\n    type: openai\n    base_url: " + URL + "\n    api_key_env: LLAMA_API_KEY\n"
                   "models:\n  - provider: local\n    model: local\n    tools: true\n"
                   "runtime:\n  max_iterations: 6\n  sandbox: off\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AURORA_")}
    env.update(AURORA_HOME=str(home), AURORA_CONFIG=str(cfg), AURORA_KEYRING_TIMEOUT="1",
               AURORA_PYTHON=sys.executable, PYTHONPATH=str(ROOT))
    return home, work, env


@pytest.mark.parametrize("core", ["python", "rust"])
def test_a_real_model_reads_a_file_and_answers(aurora_rs, tmp_path, core):
    home, work, env = _setup(tmp_path)
    argv = [sys.executable, "-m", "aurora"] if core == "python" else [aurora_rs]
    p = _run(argv + ["-t", "Use the read_file tool to read secret-word.txt, then reply with the word it holds.", "--yes"],
             env, work, timeout=600)
    assert p.returncode == 0, p.stderr[-2000:]
    _sid, recs, _ = _records(home)
    tools = [r for r in recs if r.get("type") == "tool"]
    assert any(r.get("name") == "read_file" and "periwinkle" in str(r.get("result", r.get("output", ""))) for r in tools), tools
    answers = [r for r in recs if r.get("type") == "assistant"]
    assert answers and "periwinkle" in str(answers[-1]).lower()
