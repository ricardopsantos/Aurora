"""R493 — the Rust core discovers, lists, loads and runs skills as
`aurora/skills.py` does (R11, R247): executable and .py skills from the config
dir and AURORA_HOME (earlier shadows later), SKILL.md doc skills under
doc_skill_roots (symlinks followed, lenient frontmatter)."""

import json
import os
import subprocess

from aurora import skills
from tests.test_rust_compat_r482 import aurora_rs  # noqa: F401


def _mk(p, text, mode=0o644):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    p.chmod(mode)


def test_skills_match_python(aurora_rs, tmp_path, monkeypatch):
    home, base, lib, shared = tmp_path / "home", tmp_path / "cfg", tmp_path / "lib", tmp_path / "shared"
    home.mkdir()
    _mk(base / "skills" / "hello", "#!/bin/sh\n# says hello to $1\necho \"hello $1\"\n", 0o755)
    _mk(base / "skills" / "fail", "#!/bin/sh\necho bad >&2\nexit 3\n", 0o755)
    _mk(base / "skills" / "notes.txt", "not a skill\n")
    _mk(base / "skills" / "quiet", "#!/bin/sh\ntrue\n", 0o755)
    _mk(home / "skills" / "hello", "#!/bin/sh\necho shadowed\n", 0o755)
    _mk(home / "skills" / "count.py", "import sys  # counts its arguments\nprint(len(sys.argv) - 1)\n")
    _mk(lib / "meta" / "plan" / "SKILL.md", "---\nname: plan\ndescription: Plan the work\n---\n\nStep one.\n")
    _mk(lib / "bad" / "SKILL.md", "---\nname: bad\ndescription: a topic: with a colon inside\n---\nBody\n")
    _mk(lib / "plain" / "SKILL.md", "No frontmatter here.\n")
    _mk(lib / "hello" / "SKILL.md", "---\ndescription: shadowed by the executable\n---\nx\n")
    _mk(shared / "linked" / "SKILL.md", "---\nname: Linked One\ndescription: via a symlink\n---\nL\n")
    (lib / "cat").symlink_to(shared)
    roots = [str(lib), str(tmp_path / "missing")]
    monkeypatch.setenv("AURORA_HOME", str(home))
    ops = [["listing"], ["load_doc", "plan"], ["load_doc", "bad"], ["load_doc", "plain"], ["load_doc", "linked"],
           ["load_doc", "nope"], ["run", "hello", "world"], ["run", "hello", "'a b'"], ["run", "fail", ""],
           ["run", "quiet", ""], ["run", "count", "x 'y z'"], ["run", "nope", ""], ["run", "hello", "'unclosed"]]
    py = []
    for op in ops:
        if op[0] == "listing":
            py.append(skills.listing(str(base), roots))
        elif op[0] == "load_doc":
            py.append(skills.load_doc(op[1], roots))
        else:
            py.append(skills.run(op[1], op[2], str(base)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("AURORA_")}
    env["AURORA_HOME"] = str(home)
    p = subprocess.run([aurora_rs, "--skills-oracle"], input=json.dumps({"config_base": str(base), "roots": roots, "ops": ops}),
                       env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    rs = json.loads(p.stdout)
    for op, a, b in zip(ops, py, rs):
        assert b == a, (op, a, b)
    # an empty setup says how to add skills, on both sides
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("AURORA_HOME", str(empty))
    env["AURORA_HOME"] = str(empty)
    p = subprocess.run([aurora_rs, "--skills-oracle"], input=json.dumps({"ops": [["listing"]]}),
                       env=env, capture_output=True, text=True, timeout=60)
    assert json.loads(p.stdout) == [skills.listing(None, None)]
