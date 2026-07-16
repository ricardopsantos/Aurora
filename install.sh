#!/usr/bin/env bash
# Aurora one-command install (R21): venv + editable install + PATH symlink.
# Mostly pure-python deps (Pillow is used for the startup logo), so macOS and Linux behave identically.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f config.yaml ] && [ -f config.yaml.example ]; then
    cp config.yaml.example config.yaml
    echo "→ created config.yaml from config.yaml.example — edit it for your own providers/models"
fi

read -rp "Aurora data dir [~/.aurora]: " DATA_DIR
DATA_DIR="${DATA_DIR:-$HOME/.aurora}"
DATA_DIR="${DATA_DIR/#\~/$HOME}"
mkdir -p "$DATA_DIR"
printf '%s\n' "$DATA_DIR" > "$HOME/.aurora-path"
echo "→ AURORA_HOME = $DATA_DIR (recorded in ~/.aurora-path)"

PY="$(command -v python3)"
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -e .

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
ln -sf "$(pwd)/.venv/bin/aurora" "$BIN_DIR/aurora"
echo "→ symlinked $BIN_DIR/aurora"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "⚠ $BIN_DIR is not on your PATH — add it to your shell profile" ;;
esac

echo
echo "Done. Next:"
echo "  aurora key set            # store your OpenRouter/local key (keyring/encrypted)"
echo "  aurora                    # run (auto-detects .agentic_context/)"
