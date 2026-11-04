# summary: R427 — the raw config.yaml / config.local.yaml editor's core: hide secret values for display, put them back on save (refusing when one moved), validate YAML and the config's shape with line numbers, back up and write.
"""R427: edit config.yaml (and config.local.yaml) as raw text from the web page.

The page never sees a key: `redact` swaps every secret value for HIDDEN, `restore` puts the
real value back for each HIDDEN still at the same place (same key path, same occurrence) and
refuses the save when one has no original — so a key is never written as the text «hidden»
and never silently lost. `validate` checks YAML syntax (line and column) and the shape Aurora
needs. `save` keeps a backup (the last BACKUPS) and writes atomically."""
import hashlib
import re
import time
from pathlib import Path

import yaml

HIDDEN = "«hidden»"
BACKUPS = 5
BAK_TAG = ".aurora-bak-"          # not ".bak-": hand-made backups like config.yaml.bak-20261102 are never pruned
_SECRET = re.compile(r"(^|_)(api_?key|key|token|secret|password|passwd|pass)$", re.I)
_NOT_SECRET = re.compile(r"(_env|_fetch|_file|_path|_url)$", re.I)
_LINE = re.compile(r"^(?P<lead>\s*(?:-\s+)*)(?P<key>[A-Za-z_][\w-]*)\s*:(?P<sp>\s+)(?P<val>[^#\n]*?)(?P<tail>\s+#.*)?\n?$")


class ConfigError(Exception):
    def __init__(self, msg: str, line: int | None = None, column: int | None = None):
        super().__init__(msg)
        self.line, self.column = line, column


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _secret_key(k: str) -> bool:
    return bool(_SECRET.search(k)) and not _NOT_SECRET.search(k)


_FLOW = re.compile(r"""(?P<k>[A-Za-z_][\w-]*)\s*:\s*(?P<v>"[^"]*"|'[^']*'|[^,}\s][^,}]*?)\s*(?=[,}])""")


def _walk(text: str):
    """(line index, (path, occurrence), start, end) for each secret value: `key: value` lines and keys inside
    a one-line `{a: 1, api_key: x}` mapping. path = the ancestor keys by indentation plus the key; occurrence =
    how many times that path was seen before (list entries repeat paths)."""
    stack, seen = [], {}

    def hit(path):
        n = seen.get(path, 0)
        seen[path] = n + 1
        return (path, n)

    for i, ln in enumerate(text.splitlines(keepends=True)):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^(\s*(?:-\s+)*)([A-Za-z_][\w-]*)\s*:", ln)
        if not m:
            continue
        ind = len(m.group(1))
        while stack and stack[-1][0] >= ind:
            stack.pop()
        stack.append((ind, m.group(2)))
        path = tuple(k for _, k in stack)
        lm = _LINE.match(ln)
        if not lm:
            continue
        val = lm.group("val")
        if val.lstrip().startswith("{"):
            base = lm.start("val")
            for fm in _FLOW.finditer(val):
                if _secret_key(fm.group("k")):
                    yield i, hit(path + (fm.group("k"),)), base + fm.start("v"), base + fm.end("v")
        elif _secret_key(lm.group("key")) and val.strip():
            yield i, hit(path), lm.start("val"), lm.end("val")


def _replace(text: str, subs) -> str:
    """subs: (line, start, end, new) — applied right to left within each line."""
    lines = text.splitlines(keepends=True)
    for i, st, en, new in sorted(subs, key=lambda x: (x[0], -x[1])):
        lines[i] = lines[i][:st] + new + lines[i][en:]
    return "".join(lines)


def redact(text: str) -> str:
    lines = text.splitlines(keepends=True)
    return _replace(text, [(i, st, en, HIDDEN) for i, _p, st, en in _walk(text) if lines[i][st:en].strip() != HIDDEN])


def restore(new_text: str, original: str) -> str:
    """Every HIDDEN in `new_text` gets the original value of the same key path back."""
    olines = original.splitlines(keepends=True)
    orig = {p: olines[i][st:en] for i, p, st, en in _walk(original)}
    lines = new_text.splitlines(keepends=True)
    subs = []
    for i, p, st, en in _walk(new_text):
        if lines[i][st:en].strip() != HIDDEN:
            continue
        if p not in orig:
            raise ConfigError(f"line {i + 1}: {p[0][-1]} shows {HIDDEN} but has no saved value at this place "
                              "(was it moved or renamed?); type the value or put it back where it was", i + 1, st + 1)
        subs.append((i, st, en, orig[p]))
    out = _replace(new_text, subs)
    if HIDDEN in out:
        n = out[:out.index(HIDDEN)].count("\n") + 1
        raise ConfigError(f"line {n}: {HIDDEN} is not a value Aurora can put back here", n, 1)
    return out


def parse(text: str, name: str) -> dict:
    try:
        d = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line, col = (mark.line + 1, mark.column + 1) if mark else (None, None)
        what = getattr(e, "problem", None) or str(e).splitlines()[0]
        raise ConfigError(f"{name}: not valid YAML" + (f" at line {line}, column {col}" if line else "") + f": {what}", line, col)
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise ConfigError(f"{name}: the top level must be keys and values (a mapping)", 1, 1)
    return d


def check_shape(merged: dict, current: tuple | None) -> list[str]:
    """What Aurora needs from the (merged) config; [] when fine."""
    errs = []
    provs = merged.get("providers")
    if provs is not None and not isinstance(provs, dict):
        errs.append("providers: must be keys (one per provider), not a list or a value")
        provs = {}
    provs = provs or {}
    for name, p in provs.items():
        if p is not None and not isinstance(p, dict):
            errs.append(f"providers.{name}: must be keys and values (base_url, api_key_env, …)")
    models = merged.get("models")
    if models is not None and not isinstance(models, list):
        errs.append("models: must be a list (each entry starts with '- ')")
        models = []
    for i, m in enumerate(models or [], 1):
        if not isinstance(m, dict):
            errs.append(f"models entry {i}: must be keys and values (provider, model, …)")
            continue
        if not m.get("model") or not isinstance(m.get("model"), str):
            errs.append(f"models entry {i}: needs a model name")
        if not m.get("provider"):
            errs.append(f"models entry {i} ({m.get('model')}): needs a provider")
        elif m.get("provider") not in provs:
            errs.append(f"models entry {i} ({m.get('model')}): provider {m.get('provider')!r} is not under providers:")
    for key in ("runtime", "skills"):
        if merged.get(key) is not None and not isinstance(merged.get(key), dict):
            errs.append(f"{key}: must be keys and values")
    if current and isinstance(models, list) and not any(
            isinstance(m, dict) and (m.get("provider"), m.get("model")) == current for m in models):
        errs.append(f"the model in use ({current[1]}, {current[0]}) is no longer in models:; switch to another model first")
    return errs


def backup_and_write(path: Path, text: str, write_atomic) -> str | None:
    """Keep a copy of the current file (the newest BACKUPS are kept), then write. Returns the backup's name."""
    bak = None
    if path.is_file():
        bak = path.with_name(path.name + BAK_TAG + time.strftime("%Y%m%d-%H%M%S"))
        n = 1
        while bak.exists():
            n += 1
            bak = path.with_name(path.name + BAK_TAG + time.strftime("%Y%m%d-%H%M%S") + f"-{n}")
        bak.write_bytes(path.read_bytes())
        olds = sorted(path.parent.glob(path.name + BAK_TAG + "*"))
        for old in olds[:-BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
    write_atomic(path, text)
    return bak.name if bak else None
