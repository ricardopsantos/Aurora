# Migration baseline: Python Aurora as measured before the Rust work

Recorded 2026-11-25 on the dev machine (Linux x86_64, Python 3.13), at commit
`e7ca176`, the last Python-only commit, which is 674 commits deep. The Rust core
is judged against this snapshot. Python is the oracle: where the two differ,
Rust is wrong unless a requirement says otherwise.

## Requirements

R1 to R477. R1–R293 are in `documents/CHANGELOG_TECHNICAL.md` (the original
spec) and R223 onward in `requirements/*.md`. Some numbers below R223 are only
in commit subjects. `03-FEATURE-PARITY.md` lists them all.

## Test suite

`./.venv/bin/python -m pytest -q`: **2414 passed, 33 skipped, 4 warnings in
354.8 s** of wall time, 33 s of user CPU. Most of the wall time is waiting:
timeouts, sleeps and subprocess round trips, not computation. There are 128
test files (32 113 lines), plus JavaScript tests under `tests/js`.

## Code

56 Python modules, 25 644 lines. The largest, which hold most of the porting
work:

| Module | Lines | What |
|---|---|---|
| `tui.py` | 4369 | full-screen terminal UI (prompt_toolkit) |
| `ui.py` | 2423 | shared UI pieces, rendering, pickers |
| `engine.py` | 1925 | turn orchestration, history, compaction, resume |
| `tools.py` | 1517 | built-in tools and their result strings |
| `web/core.py`, `web/server.py` | 2202 | local web page and its HTTP API |
| `providers/openai_compat.py` | 1033 | the one provider |
| `approve.py` | 885 | allow/deny rules, command and path matching |
| `agent.py` | 885 | the tool loop |
| `rewind.py` | 823 | checkpoints in a shadow git repo |
| `mcp.py` | 702 | MCP client |
| `session.py` | 476 | session JSONL |
| `secrets.py` | 364 | secret scanning and redaction |
| `keystore.py` | 314 | keys: environment, OS keyring, `keys.enc` |
| `sandbox.py` | 267 | R475 command sandbox |

Runtime dependencies: httpx, PyYAML, prompt_toolkit 3, keyring, cryptography,
ddgs, pypdf and Pillow.

## Files in AURORA_HOME that both versions must share

`config.yaml` (when not given elsewhere), `config.local.yaml`, `state.yaml`,
`allowlist.yaml`, `denylist.yaml`, `keys.enc` + `keys.salt`, `sessions/*.jsonl`,
`notes/`, `memory/`, `skills/`, `extensions/`, the checkpoint shadow repos, and
the web server's token and log. `05-DATA-COMPATIBILITY.md` gives the formats.
