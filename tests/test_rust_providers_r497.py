"""R497 — what each provider kind puts on the wire, Rust against Python:
the request body for llama.cpp (R2), Ollama (R223) and a remote OpenAI-style
API with prompt caching (R91), on the same scripted server."""

import sys

import pytest

from tests.test_rust_compat_r482 import _Model, _run, _setup, _sse, aurora_rs  # noqa: F401

KINDS = {
    "llama": ("local", "local", ""),
    "ollama": ("ollama", "qwen3:8b", ""),
    "remote": ("openrouter", "anthropic/claude-x", "    cache: true\n"),
}


def _body(req):
    msgs = [{k: v for k, v in m.items() if not (k == "content" and m["role"] == "system" and isinstance(v, str))}
            for m in req["messages"]]
    return {**req, "messages": msgs}


@pytest.mark.parametrize("kind", KINDS)
def test_request_body_matches_python(aurora_rs, tmp_path, kind):
    pkey, model_id, extra = KINDS[kind]
    got = {}
    for name, argv in (("python", [sys.executable, "-m", "aurora"]), ("rust", [aurora_rs])):
        model = _Model([_sse({"content": "hi"})])
        try:
            home, work, cfg, env = _setup(tmp_path / name, model.url)
            cfg.write_text(
                f"providers:\n  {pkey}:\n    type: openai\n    base_url: {model.url}\n"
                f"models:\n  - provider: {pkey}\n    model: {model_id}\n    tools: true\n{extra}"
                "runtime:\n  max_iterations: 3\n  redact_secrets: false\n  sandbox: off\n", encoding="utf-8")
            env["OPENROUTER_API_KEY"] = "test-key"
            p = _run(argv + ["-t", "hello", "--yes"], env, work)
        finally:
            model.close()
        assert p.returncode == 0, (name, p.stderr)
        got[name] = _body(model.requests[0])
    assert got["rust"] == got["python"]
