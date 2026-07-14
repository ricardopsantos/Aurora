#!/usr/bin/env bash
# One-line Aurora install: curl -fsSL <raw-url>/bootstrap-install.sh | bash
# Clones the repo (or updates it, if already present) then runs install.sh,
# which sets up the venv + PATH symlink. Set AURORA_DIR to change where the
# repo is cloned (default: ~/Aurora).
set -euo pipefail

REPO_URL="https://github.com/ricardopsantos/Aurora.git"
DIR="${AURORA_DIR:-$HOME/Aurora}"

if ! command -v git >/dev/null 2>&1; then
    echo "error: git is required" >&2
    exit 1
fi

if [ -d "$DIR/.git" ]; then
    echo "→ $DIR already exists, updating instead of cloning"
    git -C "$DIR" pull --ff-only
else
    echo "→ cloning into $DIR"
    git clone "$REPO_URL" "$DIR"
fi

cd "$DIR"
exec ./install.sh
