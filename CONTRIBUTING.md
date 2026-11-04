# Contributing

- **Requirements first.** Every behaviour change gets a numbered entry in `requirements/*.md` (indexed from `REQUIREMENTS.md`): a testable rule, not a changelog line, plus a test where anything is testable.
- **Tests.** `pip install -e . pytest` then `pytest -q`. The web layout tests (`tests/test_web_mobile.py`) need `pip install playwright && playwright install chromium`; without them they skip.
- **Generic and machine-independent (R346, R393).** No tracked file may name a person's machines, hostnames, addresses, accounts or home paths, and no default may assume one: examples use `example.com` and placeholders such as `YOUR_USER`. `tests/test_no_personal_references.py` enforces it. Site-specific units and config belong outside this repository.
- **Front ends.** The engine never does terminal I/O (`tests/test_architecture.py`). A front end (the TUI, headless `-t`, the web page) implements the Frontend callbacks (`on_text`, `on_think`, `on_tool_start`, `on_tool_result`, `approve`, `notify`, `cancelled`, …); see `aurora/web/core.py` for a small complete one.
- **Commits.** One topic per commit, the requirement number in the subject (`R393: …`).
