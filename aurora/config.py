"""YAML configuration with ${ENV_VAR} expansion (ported from Terminal-Agent V2)
plus write-back support for the handful of settings slash-commands persist
(/max). Writes edit only the raw (unexpanded) file so ${VARS} survive."""

import os
import re
from pathlib import Path

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: str | Path) -> dict:
    path = Path(path)
    with open(path) as f:
        cfg = _expand(yaml.safe_load(f)) or {}
    cfg.setdefault("providers", {})
    cfg.setdefault("models", [])
    cfg.setdefault("runtime", {})
    cfg.setdefault("skills", {})
    cfg["_path"] = str(path.resolve())
    cfg["_base_dir"] = str(path.resolve().parent)
    return cfg


def _state_path() -> Path:
    from .paths import aurora_home
    return aurora_home() / "state.yaml"


def load_state() -> dict:
    """Per-machine mutable state (last model used, …) — lives in AURORA_HOME,
    never in config.yaml, which is committed and synced between machines."""
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {}


def save_state_values(**values) -> None:
    st = load_state()
    st.update(values)
    _state_path().write_text(yaml.safe_dump(st, sort_keys=False))


def persist_runtime_value(cfg: dict, key: str, value) -> None:
    """Rewrite one runtime.<key> in the config file, preserving ${VARS}
    values (the raw, unexpanded text is re-parsed, mutated, and dumped).
    YAML comments do NOT survive the round-trip — anywhere in the file, not
    just the runtime block; acceptable for v1."""
    path = Path(cfg["_path"])
    raw = yaml.safe_load(path.read_text()) or {}
    raw.setdefault("runtime", {})[key] = value
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    cfg["runtime"][key] = value


def persist_model_entry(cfg: dict, entry: dict) -> None:
    """Append one model entry to config.yaml's models: list (/model add).
    Same raw-text round-trip as persist_runtime_value — ${VARS} elsewhere
    survive, YAML comments do not. Also appends to the live cfg dict so the
    running engine sees it without a reload."""
    path = Path(cfg["_path"])
    raw = yaml.safe_load(path.read_text()) or {}
    raw.setdefault("models", []).append(dict(entry))
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    cfg.setdefault("models", []).append(entry)


def remove_model_entries(cfg: dict, model_id: str) -> int:
    """Remove every models: entry matching model_id from config.yaml and the
    live cfg list (/model remove). The live list is mutated IN PLACE —
    Engine.models aliases it. Returns how many entries were removed."""
    path = Path(cfg["_path"])
    raw = yaml.safe_load(path.read_text()) or {}
    models = raw.get("models") or []
    kept = [m for m in models if m.get("model") != model_id]
    removed = len(models) - len(kept)
    if removed:
        raw["models"] = kept
        path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
        live = cfg.get("models")
        if live is not None:
            live[:] = [m for m in live if m.get("model") != model_id]
    return removed
