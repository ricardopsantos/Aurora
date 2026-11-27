"""R501 — the macOS box: Rust writes the same `sandbox-exec` profile as
`aurora/sandbox.py`'s `macos_profile` for the same policy (checked here on any
OS, as text). Running it for real needs a Mac: see
documents/migration/HANDOFF-MACOS.md."""

import json
import os
import subprocess
import sys

import pytest

from tests.test_rust_compat_r482 import aurora_rs  # noqa: F401


@pytest.mark.parametrize("network", [True, False])
def test_profile_text_matches_python(aurora_rs, tmp_path, monkeypatch, network):
    home, ah, ws, dt = tmp_path / "home", tmp_path / "ah", tmp_path / "proj", tmp_path / "dtmp"
    for d in (home / ".ssh", home / ".cache", home / ".config" / "gh", ah / "skills", ah / "workspace", ws / ".git" / "hooks",
              dt, tmp_path / 'od"d' / "x", tmp_path / "extra"):
        d.mkdir(parents=True, exist_ok=True)
    (ws / ".git" / "config").write_text("")
    (home / ".netrc").write_text("")
    pol = {"home": str(home), "aurora_home": str(ah), "network": network,
           "writable": [str(tmp_path / "extra"), "/", str(home)], "hide": [str(tmp_path / 'od"d' / "x")],
           "workspace": str(ws), "darwin_tmp": str(dt)}
    p = subprocess.run([aurora_rs, "--macos-profile-oracle"], input=json.dumps(pol), capture_output=True, text=True,
                       timeout=60, env={k: v for k, v in os.environ.items() if not k.startswith("AURORA_")})
    assert p.returncode == 0, p.stderr
    import tempfile
    from aurora import sandbox
    monkeypatch.setattr(tempfile, "tempdir", str(dt))
    want = sandbox.macos_profile(sandbox.Policy(home=pol["home"], aurora_home=pol["aurora_home"], network=network,
                                                writable=pol["writable"], hide=pol["hide"]), str(ws))
    assert p.stdout == want
    assert "(subpath " + json.dumps(str(home / ".ssh")) + ")" in want
    if sys.platform == "darwin":            # on the Mac, the box must also really run
        assert subprocess.run(["sandbox-exec", "-p", want, "/bin/sh", "-c", "true"], timeout=20).returncode == 0
