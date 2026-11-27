"""R441, R582 - the live version is 2.0.<commits since the last published release>."""
import json
from pathlib import Path

import aurora

ROOT = Path(__file__).resolve().parent.parent


def test_r441_major_minor_is_2_0_and_x_counts_from_the_baseline():
    base = json.loads((ROOT / "VERSION_BASELINE.json").read_text())
    assert base["major_minor"] == "2.0"
    assert aurora._live_version() == f"2.0.{max(0, aurora._commit_count() - int(base['baseline_commit_count']))}"
