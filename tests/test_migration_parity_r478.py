"""R478 — the Aurora 2.0 parity table lists every requirement and is current."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "documents" / "migration" / "build_parity.py"
TABLE = ROOT / "documents" / "migration" / "03-FEATURE-PARITY.md"


def test_table_is_current():
    p = subprocess.run([sys.executable, str(TOOL), "--check"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr


def test_every_number_from_r1_to_the_highest_has_one_row():
    rows = [int(n) for n in re.findall(r"^\| R(\d+) \|", TABLE.read_text(encoding="utf-8"), re.M)]
    index = (ROOT / "REQUIREMENTS.md").read_text(encoding="utf-8")
    top = max(int(n) for n in re.findall(r"\bR(\d+)\b", index))
    assert rows == list(range(1, max(rows) + 1))
    assert max(rows) >= top


def test_statuses_are_known():
    for line in (ROOT / "documents" / "migration" / "parity-status.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("R"):
            assert re.match(r'^R\d+: "(PORTED|PARTIAL|NOT_APPLICABLE): .+"$', line), line
