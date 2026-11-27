# Handoff: check Aurora 2.0 on macOS now that the Rust loop is the default (R508)

**Card:** claude-code `20261124-224007-create-aurora-2-0-convert-aurora-project`, the last check before review.
**Written:** 2026-11-26 on the build host. **Branch:** `aurora-2`, at `804b00a` or later.
Python is the oracle: where the two loops differ, Rust is wrong.

## Why this run
Your last run (`f824127`) was before R505–R508. Since then:
- **R505:** Python's front ends call their own provider objects through the Rust core (`--provider-bridge`).
- **R506:** the web page's notes reach the model.
- **R507:** the test suite's direct `agent.run_turn` calls also run on the Rust loop. That found real gaps, now fixed: the repeat nudge and stop, the auto-continue backstop, the mid-turn spend limit, the allow/deny lists re-read each turn, the shared secret and session-rule sets, generation speed.
- **R508:** the Rust loop is the **default**. `runtime.core: python` or `AURORA_CORE=python` keeps Python's. `install.sh` now builds `aurora-rs` when cargo is installed.

So on the Mac the whole suite now runs on the Rust loop and its macOS box (`sandbox-exec`) by default. That combination has never run on macOS.

## 0. Update the checkout: pull with rebase first
In your Aurora checkout:
```sh
git status                      # commit or stash anything local first
git fetch origin
git pull --rebase origin aurora-2
git log --oneline -1            # 804b00a or later
./.venv/bin/pip install -e '.[dev]' -q
cargo build -q -p aurora-cli    # the tests use target/debug/aurora-rs: rebuild after every pull
```
If the rebase conflicts in `documents/migration/parity-status.yaml` or `03-FEATURE-PARITY.md`, keep the build host's side: `git checkout --ours <file>` (during a rebase, "ours" is the branch you rebase onto), then `git add <file>` and `git rebase --continue`. The build host owns those files.

## 1. Run the checks, in this order
```sh
cargo clippy -q --workspace --all-targets -- -D warnings
cargo test -q --workspace                                     # 106 on the build host
./.venv/bin/python -m pytest -q -p no:cacheprovider           # the default: the Rust loop. 2490 passed on the build host
AURORA_CORE=python ./.venv/bin/python -m pytest -q -p no:cacheprovider   # Python's loop. 2490 passed on the build host
```
Set `TMPDIR` to a folder with space if `/tmp` is small. The suite now **fails** if any test changes your real `~/.aurora/allowlist.yaml` or `denylist.yaml`. If you see `tests changed the real ~/.aurora`, report the test: don't just re-run.

Also check the install path once, on a scratch clone if you prefer: `./install.sh` should print `→ building the Rust core` and then `→ Rust core: …/target/release/aurora-rs`.

## 2. If something fails
- **macOS-only bug in the Rust code:** fix it so the Python oracle and Rust agree. Add or extend a `tests/test_rust_*.py` that names the R number. Commit with a plain message (no AI attribution lines) and push to `aurora-2`.
- **Test that assumes Linux:** fix the test, not the product.
- **Flaky timing test under load:** re-run it alone. Report it only if it fails alone.
- Do **not** edit `documents/migration/parity-status.yaml` or the parity table.
- Don't put machine names, hostnames or personal paths in tracked files: `tests/test_no_personal_references.py` checks this.

## 3. Report back (the owner relays it to the build host's session)
- the commit you tested and any commits you pushed;
- the four results (clippy, cargo tests, suite on the default loop, suite with `AURORA_CORE=python`) with counts;
- whether `install.sh` built the Rust core;
- anything you saw but didn't fix.
