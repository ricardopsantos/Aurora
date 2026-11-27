"""R583 - the one-line install asks on the terminal, and takes the defaults without one."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _prompt_part(tmp_path):
    """install.sh up to (not including) the venv step: the tty switch, the data-dir question and its use."""
    src = (ROOT / "install.sh").read_text()
    head = src.split('PY="$(command -v python3)"')[0].replace('cd "$(dirname "$0")"', "")
    script = tmp_path / "prompt.sh"
    script.write_text(head + 'echo "DATA=$DATA_DIR"\n')
    return script


def test_r583_reads_from_the_terminal_when_stdin_is_a_pipe():
    src = (ROOT / "install.sh").read_text()
    assert 'if [ ! -t 0 ] && { : </dev/tty; } 2>/dev/null; then exec </dev/tty; fi' in src
    assert src.index("exec </dev/tty") < src.index('read -rp "Aurora data dir')


def test_r583_every_read_has_a_default():
    for line in (ROOT / "install.sh").read_text().splitlines():
        if line.strip().startswith("read "):
            assert "||" in line, line


def test_r583_no_terminal_takes_the_default_instead_of_stopping(tmp_path):
    script = _prompt_part(tmp_path)
    env = dict(os.environ, HOME=str(tmp_path))
    p = subprocess.run(["bash", str(script)], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       env=env, cwd=tmp_path, start_new_session=True)  # new session: no controlling tty
    assert p.returncode == 0, p.stderr
    assert f"DATA={tmp_path}/.aurora" in p.stdout
    assert (tmp_path / ".aurora-path").read_text().strip() == f"{tmp_path}/.aurora"
