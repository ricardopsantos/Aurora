"""YAML configuration with ${ENV_VAR} expansion (ported from Terminal-Agent V2)
plus write-back support for the handful of settings slash-commands persist
(/max). Writes edit only the raw (unexpanded) file so ${VARS} survive."""

import os
import re
from pathlib import Path

import yaml

from .paths import file_lock, write_text_atomic

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _merge(base, over):
    """R316: dicts merge key by key, anything else (lists included) is replaced
    — so an overlay swaps a whole `base_url` list instead of splicing into it."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base[k], v) if k in base else v
        return out
    return over


def read_raw_config(path: str | Path) -> dict:
    """R316: the config file, with $AURORA_HOME/config.local.yaml merged over it
    when that exists. The committed config.yaml is shared by every machine; the
    overlay holds only what one machine does differently (its own llama-server
    URL, a dropped key), so the repo never has to be edited per machine."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    from .paths import aurora_home
    local = aurora_home() / "config.local.yaml"
    if local.is_file():
        with open(local, encoding="utf-8") as f:
            over = yaml.safe_load(f) or {}
        if isinstance(over, dict) and isinstance(raw, dict):
            raw = _merge(raw, over)
    return raw


def load_config(path: str | Path) -> dict:
    path = Path(path)
    # R199: encoding pinned, never the locale's. Aurora WRITES this file as
    # UTF-8 with `allow_unicode=True` (see persist_model_entry), so under
    # LANG=C it could not read back a config it had written itself — and
    # `/model add` puts OpenRouter's descriptions, which are full of em
    # dashes, straight into it. Same defect R146b fixed in session.py.
    cfg = _expand(read_raw_config(path)) or {}
    # R244: `setdefault` does NOT replace a key that exists with a null value,
    # and a bare `runtime:` line in YAML parses as exactly that. Every consumer
    # then does `cfg["runtime"].get(...)` on a None and Aurora refuses to start
    # — on a file it explicitly invites the user to hand-edit. R150e fixed this
    # for `models` alone, inside Engine; the same hazard is on all four keys, so
    # the coercion belongs here, where the shape is promised in the first place.
    for key, empty in (("providers", {}), ("models", []),
                       ("runtime", {}), ("skills", {})):
        if not isinstance(cfg.get(key), type(empty)):
            cfg[key] = empty
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
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_state_values(**values) -> None:
    with file_lock(_state_path()):        # R530: the terminal and the web page change different keys of one file
        st = load_state()
        st.update(values)
        write_text_atomic(_state_path(), yaml.safe_dump(st, sort_keys=False))


def _raw_with_section(path: Path, key: str, empty):
    """R244: the raw config, with `key` guaranteed to hold a container of the
    right type. The write paths used `raw.setdefault(key, ...)`, which returns
    the existing None for a bare `runtime:` / `models:` line — so the very next
    subscript raised TypeError and no setting could be saved at all until the
    file was hand-edited. `load_config` coerces the same four keys on the READ
    side; this is the write side of one rule."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw.get(key), type(empty)):
        raw[key] = empty
    return raw


def persist_runtime_value(cfg: dict, key: str, value) -> None:
    """Rewrite one runtime.<key> in the config file, preserving ${VARS}
    values (the raw, unexpanded text is re-parsed, mutated, and dumped).
    YAML comments do NOT survive the round-trip — anywhere in the file, not
    just the runtime block; acceptable for v1."""
    path = Path(cfg["_path"])
    raw = _raw_with_section(path, "runtime", {})
    raw["runtime"][key] = value
    write_text_atomic(path, yaml.safe_dump(raw, sort_keys=False,
                                          allow_unicode=True))
    cfg["runtime"][key] = value


def persist_model_entry(cfg: dict, entry: dict) -> None:
    """Append one model entry to config.yaml's models: list (/model add).
    R423: edited as text (modelcfg), so every comment in the file survives.
    Also appends to the live cfg dict so the running engine sees it without a reload."""
    from . import modelcfg
    path = Path(cfg["_path"])
    write_text_atomic(path, modelcfg.add(path.read_text(encoding="utf-8"), dict(entry)))
    cfg.setdefault("models", []).append(entry)


def remove_model_entries(cfg: dict, model_id: str) -> int:
    """Remove every models: entry matching model_id from config.yaml and the
    live cfg list (/model remove). The live list is mutated IN PLACE —
    Engine.models aliases it. Returns how many entries were removed.
    R423: edited as text (modelcfg), so every comment in the file survives."""
    from . import modelcfg
    path = Path(cfg["_path"])
    text = path.read_text(encoding="utf-8")
    hits = [i for i, m in enumerate(modelcfg.entries(text)) if isinstance(m, dict) and m.get("model") == model_id]
    for i in reversed(hits):
        text = modelcfg.delete(text, i)
    if hits:
        write_text_atomic(path, text)
        live = cfg.get("models")
        if live is not None:
            live[:] = [m for m in live if m.get("model") != model_id]
    return len(hits)
