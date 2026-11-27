"""R488 — the Rust core reads and trusts bootstrap prompts as `aurora/bootstrap.py`
does (R286): project over global, empty files skipped, the `.source` URL, and
`trusted_bootstraps.json` written by either side is honoured by the other."""

import json
import os
import subprocess

from aurora import bootstrap
from tests.test_rust_compat_r482 import aurora_rs  # noqa: F401


def _py_state():
    prompt, source = bootstrap.load(".")
    return {"prompt": prompt, "source": source, "url": bootstrap.source_url("."),
            "needs_trust": bootstrap.needs_trust(".")}


def _rs(aurora_rs, ops, home, work):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AURORA_")}
    env["AURORA_HOME"] = str(home)
    p = subprocess.run([aurora_rs, "--bootstrap-oracle"], input=json.dumps(ops), env=env, cwd=work,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_load_and_trust_agree(aurora_rs, tmp_path, monkeypatch):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("AURORA_HOME", str(home))
    monkeypatch.chdir(work)
    assert _rs(aurora_rs, ["state"], home, work) == [_py_state()]
    bootstrap.save("global prompt", project=False, cwd=".", source_url="https://example.org/b.md")
    assert _rs(aurora_rs, ["state"], home, work) == [_py_state()]
    (work / ".aurora").mkdir()
    (work / ".aurora" / "bootstrap.md").write_text("   \n", encoding="utf-8")   # empty: skipped
    assert _rs(aurora_rs, ["state"], home, work) == [_py_state()]
    bootstrap.save("ação do projeto", project=True, cwd=".")
    rs = _rs(aurora_rs, ["state"], home, work)
    assert rs == [_py_state()] and rs[0]["needs_trust"] is True
    # Python trusts → Rust agrees
    bootstrap.trust(".")
    assert _rs(aurora_rs, ["state"], home, work)[0]["needs_trust"] is False
    py_file = (home / "trusted_bootstraps.json").read_bytes()
    # Rust trusts in a fresh home → the same bytes, and Python agrees
    (home / "trusted_bootstraps.json").unlink()
    assert bootstrap.needs_trust(".")
    assert _rs(aurora_rs, ["trust"], home, work)[0]["needs_trust"] is False
    assert (home / "trusted_bootstraps.json").read_bytes() == py_file
    assert not bootstrap.needs_trust(".")
    # changed content needs trust again on both sides
    bootstrap.save("changed", project=True, cwd=".")
    assert _rs(aurora_rs, ["state"], home, work) == [_py_state()] and _py_state()["needs_trust"]
