# Handoff: the macOS R508 check is done, back to the build host

**Card:** claude-code `20261124-224007-create-aurora-2-0-convert-aurora-project`.
**Written:** 2026-11-26 on the Mac, after running `HANDOFF-MACOS.md`.
**Tested:** `547e422`. Since then only `4a065dd` (web page JS, the no-token banner) has landed; it touches no Rust or loop code.

## Results on macOS
| Check | Result |
|---|---|
| `git pull --rebase origin aurora-2` | clean, no conflicts |
| `cargo clippy --workspace --all-targets -- -D warnings` | clean |
| `cargo test --workspace` | 106 passed (same as the build host) |
| suite, default (Rust loop, `sandbox-exec`) | **2489 passed, 36 skipped, 0 failed** (13m49s) |
| suite, `AURORA_CORE=python` | **2489 passed, 36 skipped, 0 failed** (7m59s) |
| `./install.sh` (scratch clone, scratch `HOME`) | printed `→ building the Rust core` then `→ Rust core: …/target/release/aurora-rs`; the binary runs (`aurora-rs 0.1.0`) |

- The suite did not report `tests changed the real ~/.aurora`.
- Nothing failed, so nothing was fixed or pushed. The parity files are untouched.
- Both loops agree on macOS: same counts, same skips.

## Skips on macOS (36, the same on both loops)
- 24: `playwright` not installed (`tests/test_web_*_ui.py`, mobile, offline, prices, speech). `playwright` is not in the `dev` extras.
- 8 macOS-only:
  - `test_closed_pool_recovery.py:54`: closing a socket under a blocked read does not wake it on macOS
  - `test_deepdive_20261022.py:151`: needs /proc
  - `test_sandbox_r475.py:61, 139, 199`: bubblewrap only (no private /tmp, no PID namespace)
  - `test_sandbox_r475.py:92`: no /etc/environment
  - `test_sandbox_r475.py:124`: no reachable docker socket
  - `test_sandbox_r475.py:132`: no user runtime dir
- 2: `test_live_r502.py` (needs `AURORA_LIVE=1`)
- 2: `test_secrets.py:347` (GUID/UUID and env credential have no guard)

## Seen, not fixed
1. **One pass short of 2490.** If the 8 macOS-only skips run on the build host, the build host should report more than 2490, so the numbers don't add up yet. Compare the skip lists: run `./.venv/bin/python -m pytest -q -p no:cacheprovider -rs | grep -E '^SKIPPED|passed'` on the build host and diff it against the list above.
2. **A split venv on the Mac (local, now fixed).** `.venv/pyvenv.cfg` and `pip` were Python 3.15, but `.venv/bin/python` still linked to 3.14. `pip install -e '.[dev]'` went into 3.15 while the tests ran on 3.14's stale packages, so `pyte` was "missing" (first runs: 2481 passed, 37 skipped). After the venv was rebuilt on 3.15, both suites gave 2489. Nothing in the repo or in `install.sh` causes this, so no fix is needed. If a user's install looks odd, `.venv/bin/python -V` is worth checking.
3. **Setuptools deprecation warnings during `pip install -e .`:** `project.license` as a TOML table and the license classifiers. Setuptools says this stops building after 2027-02-18. Cheap to fix in `pyproject.toml`, and it falls in the documentation and packaging pass.

## For the build host to do next
1. Post the results above as a comment on the card, as the macOS run of item 1.
2. Settle the 2489 against 2490 gap (item 1 of "Seen, not fixed").
3. Do the documentation update (item 2 of the card's remaining list).
4. Wrap up: post the results, move the card to humanreview and remove the "## " from its title.
