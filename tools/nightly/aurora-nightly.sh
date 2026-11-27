#!/bin/sh
# N5: the nightly Python-vs-Rust run (aurora-nightly.timer). Pulls aurora-2, builds, runs clippy, the Rust
# tests and the full Python suite (the differential tests compare both cores on the same fixtures), then the live
# local-model tests (R502) when ~/.config/aurora-nightly.env sets AURORA_LIVE=1 and AURORA_LIVE_URL. One log per night
# in ~/.cache/aurora-nightly; the unit fails when any step fails, so `systemctl --user status` shows it.
set -u
repo=${AURORA_REPO:-$HOME/repositories/Aurora}
logs=$HOME/.cache/aurora-nightly
mkdir -p "$logs" "$HOME/.cache/aurora-tests"
log=$logs/$(date +%F).log
export PATH=$HOME/.cargo/bin:$PATH TMPDIR=$HOME/.cache/aurora-tests
cd "$repo" || exit 1
fail=0
step() { echo "== $*" >>"$log"; "$@" >>"$log" 2>&1 || { echo "!! FAILED: $*" >>"$log"; fail=1; }; }
echo "aurora nightly $(date -Is)" >"$log"
if [ -n "$(git status --porcelain)" ]; then
  echo "== working tree has changes: testing it as it is, no pull" >>"$log"
else
  step git pull -q --ff-only origin aurora-2
fi
echo "commit $(git rev-parse --short HEAD)" >>"$log"
step cargo build -q -p aurora-cli
step cargo clippy -q --workspace --all-targets -- -D warnings
step cargo test -q --workspace
step ./.venv/bin/python -m pytest -q -p no:cacheprovider
# R505/R508: the same suite again on the other loop: Rust is the default, so this run forces Python's
step env AURORA_CORE=python ./.venv/bin/python -m pytest -q -p no:cacheprovider
if [ "${AURORA_LIVE:-}" = 1 ]; then
  step ./.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_live_r502.py
fi
ls -1t "$logs"/*.log | tail -n +31 | xargs -r rm -f       # keep a month
echo "result: $([ $fail = 0 ] && echo PASS || echo FAIL)" >>"$log"
exit $fail
