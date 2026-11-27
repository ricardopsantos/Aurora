"""R484 — the Rust secret scanner finds, redacts and summarises exactly what
`aurora/secrets.py` does, over this repository's own files, hand-written
cases and seeded random tokens. Skipped when cargo is not installed."""

import json
import random
import string
import subprocess
from pathlib import Path

import pytest

from aurora import secrets

ROOT = Path(__file__).resolve().parent.parent
CARGO = Path.home() / ".cargo" / "bin" / "cargo"

HAND = [
    "token ghp_abcdefghijklmnopqrstuvwxyz0123456789 and AKIAABCDEFGHIJKLMNOP",
    "export MY_API_KEY=hunter2\nPWD=/home/me\nDB_PWD=x\nAPI_KEY=abc\nMYAPP_DBPASS=zz\nmy_pass = q\nSECRET : no",
    "password=None\nTOKEN=''\nsecret=Condition(lambda: 1)\nsecret=f'x'\nTOKEN=null\nPASSWORD=\"real one\"",
    "Authorization: Bearer abcdefghijklmnop1234== trailing",
    "sk-or-v1-" + "0123456789abcdef" * 4, "sk-" + "aB3xY7qLmZ2pQ8rT5vW9nK1", "sk_live_" + "a1B2c3D4e5F6g7H8i9",
    "xoxb-1234567890-abcdefghij", "550e8400-e29b-41d4-a716-446655440000", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    "HOME=/root\0SECRET_TOKEN=abc123\0X=1", "99999999aB3xY7qLmZ2pQ8", "20260710_211200_report.txt", "double-Esc-within-2s",
    "4f9c0a1b2c3d4e5f60718293a4b5c6d7e8f90123", "ünïcödé aB3xY7qLmZ2pQ8rT5vW9nK1 ✓ tail", "", "plain english text with no secrets at all",
    "-----BEGIN PRIVATE KEY-----\nxxxxxxxxxxxxxxxxxxxxxxxxxxx550e8400-e29b-41d4-a716-446655440000\n-----END PRIVATE KEY-----aB3xY7qLmZ2pQ8rT5vW9nK1",
]


def _random_texts():
    rng = random.Random(484)
    alpha = string.ascii_letters + string.digits
    out = []
    for _ in range(300):
        n = rng.randint(18, 48)
        tok = "".join(rng.choice(alpha + "_-+") for _ in range(n))
        pre = rng.choice(["", "key: ", "see ", "x=", "\n", "Bearer "])
        out.append(f"{pre}{tok} rest")
    for _ in range(100):
        out.append("".join(rng.choice(alpha + " \n=_-./") for _ in range(rng.randint(5, 200))))
    return out


def _repo_texts():
    out = []
    files = sorted(p for p in ROOT.rglob("*") if p.suffix in {".py", ".md", ".yaml", ".rs", ".toml", ".sh", ".json"}
                   and p.is_file() and not any(x in p.parts for x in (".venv", "target", ".git", "node_modules", "fixtures")))
    for p in files[:400]:
        try:
            out.append(p.read_text(encoding="utf-8")[:40000])
        except (OSError, UnicodeDecodeError):
            pass
    return out


@pytest.fixture(scope="module")
def oracle():
    if not CARGO.is_file():
        pytest.skip("cargo not installed")
    subprocess.run([str(CARGO), "build", "-q", "-p", "aurora-cli"], cwd=ROOT, check=True, timeout=900)
    exe = str(ROOT / "target" / "debug" / "aurora-rs")

    def ask(texts):
        p = subprocess.run([exe, "--secrets-oracle"], input=json.dumps(texts), capture_output=True, text=True, timeout=600)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)
    return ask


def _python(t):
    ms = secrets.scan(t)
    return {"matches": [[m.kind, m.text] for m in ms], "redacted": secrets.redact(t, ms), "preview": secrets.preview(ms)}


@pytest.mark.parametrize("name,texts", [("hand-written", lambda: HAND), ("random", _random_texts), ("repository files", _repo_texts)])
def test_scanner_matches_python(oracle, name, texts):
    texts = texts()
    got = oracle(texts)
    bad = [(i, t) for i, (t, g) in enumerate(zip(texts, got)) if _python(t) != g]
    assert not bad, f"{name}: {len(bad)} of {len(texts)} differ; first: {bad[0][1][:300]!r}\npython: {_python(bad[0][1])}\nrust:   {got[bad[0][0]]}"
    if name != "repository files":
        assert any(g["matches"] for g in got), "the corpus found nothing, so the comparison proves nothing"
