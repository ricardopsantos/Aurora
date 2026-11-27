# summary: R394 front-end facade — the only core API that aurora/web (and any future front end) may import.
"""R394: the stable contract between Aurora's core and its front ends.

Front ends (today `aurora/web/`) import the core only through this module, so the
core can be refactored freely as long as these functions keep their shape. The
engine object and the Frontend protocol are passed in by the caller and are part
of the contract too; everything else lives here. A guard test
(tests/test_architecture.py) enforces it.

Every function delegates at call time through the module attribute, so patching
`aurora.session.<x>` in a test still takes effect.
"""
import re

from . import __version__ as _version
from . import config as _config
from . import guard as _guard
from . import htmltext as _htmltext
from . import keystore as _keystore
from . import configedit as _configedit
from . import modelcfg as _modelcfg
from . import paths as _paths
from . import rewind as _rewind
from . import secrets as _secrets
from . import session as _session
from . import tools as _tools
from .providers.base import ProviderError

__all__ = [
    "ProviderError", "version", "aurora_home",
    "session_ids", "session_exists", "list_sessions", "search_sessions", "session_mtime",
    "count_prompts", "export_markdown", "usage_by_model", "new_session",
    "load_state", "save_state", "set_secret_prompter",
    "checkpoints", "diff_since", "undo_preview", "undo_diff", "undo",
    "model_price", "refresh_prices", "refresh_prices_in_background",
    "session_records", "format_tool_call",
    "ModelEditError", "model_entries", "model_add", "model_update", "model_delete",
    "load_models", "write_text_atomic",
    "ConfigError", "config_redact", "config_restore", "config_parse", "config_check", "config_digest",
    "config_backup_write", "config_merge", "load_config",
]


# ── identity / paths ────────────────────────────────────────────────────
def version() -> str:
    return str(_version)


def core_binary() -> str | None:
    """R509: the aurora-rs a turn would start (rustcore.binary), or None."""
    from . import rustcore
    return rustcore.binary()


def aurora_home():
    """The per-machine data dir (a Path)."""
    return _paths.aurora_home()


# ── sessions ────────────────────────────────────────────────────────────
def starts_with_path(line: str) -> bool:
    """R453: a line starting with `/` that is a message about a path, not a command."""
    return _paths.starts_with_path(line)


def session_ids() -> list[str]:
    """Every exact session id on disk."""
    return _session._base_session_ids()


_PLAIN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def session_exists(sid: str) -> bool:
    """True only for an exact session id, so a crafted id never reaches the filesystem.
    R395: a plain id (letters, digits, _ and -; every id Aurora makes) is one stat of its base log,
    not a scan of the whole directory; anything else falls back to the full list."""
    if not isinstance(sid, str) or not sid:
        return False
    if _PLAIN_ID.fullmatch(sid):
        if not (_paths.sessions_dir() / f"{sid}.jsonl").is_file():
            return False
        # a case-insensitive filesystem (macOS) would also match "ABC" for "abc": re-check exact case
        return sid == sid.lower() or sid in _session._base_session_ids()
    return sid in _session._base_session_ids()


def list_sessions(limit: int = 20) -> list[tuple[str, str, str]]:
    """(id, when, first message), newest first."""
    return _session.list_sessions(limit)


def search_sessions(query: str, limit: int = 20, counts: bool = False) -> list[tuple]:
    """(id, when, event, snippet) for sessions whose log contains `query`; counts=True adds (hits, capped)."""
    return _session.search_sessions(query, limit, counts)


def session_mtime(sid: str) -> float:
    return _session._latest_mtime(sid)


def count_prompts(sid: str) -> int:
    return _session.count_prompts(sid)


def export_markdown(sid: str, max_chars: int | None = None) -> str:
    return _session.export_markdown(sid, max_chars)


def session_records(sid: str):
    """R421: every record of one session, oldest first (the caller has checked session_exists)."""
    return _session.Session(sid).iter_records()


def format_tool_call(name, args) -> str:
    return _session.format_tool_call(name, args)


def usage_by_model(sid: str) -> dict[str, dict]:
    return _session.usage_by_model(sid)


def new_session():
    """A fresh Session (its own log) to hand to the engine."""
    return _session.Session()


# ── remembered state (state.yaml) ───────────────────────────────────────
def load_state() -> dict:
    return _config.load_state()


def save_state(**values) -> None:
    _config.save_state_values(**values)


# ── key store ───────────────────────────────────────────────────────────
def set_secret_prompter(fn) -> None:
    """Route the key store's "enter the key" prompt to this front end."""
    _keystore.set_prompter(fn)


# ── checkpoints / undo (rewind) ─────────────────────────────────────────
def checkpoints() -> list[dict]:
    return _rewind.entries()


def diff_since(ref) -> str:
    return _rewind.diff_since(ref)


def undo_preview() -> tuple[str, list[str]]:
    return _rewind.undo_preview()


def undo_diff() -> str:
    return _rewind.undo_diff()


def undo() -> str:
    return _rewind.undo()


# ── OpenRouter prices (R397) ────────────────────────────────────────────
def estimate_tokens(text: str) -> int:
    """R401: rough token count of a text (the same estimate the context meter uses)."""
    from . import tokens as _tokens
    return _tokens.estimate_tokens(text)


def model_price(model_id: str) -> dict | None:
    """The cached catalog price of one OpenRouter model: {in, out, cache_read} in $ per million tokens
    (None where the catalog lists none) and `at` (epoch seconds of the last catalog refresh, or None).
    None when the model has no cached entry at all."""
    from .providers import openai_compat as oc
    e = oc.REMOTE_CONTEXT_LIMITS.get(model_id)
    if not e:
        return None
    at = e.get("refreshed_at")
    try:
        at = float(at) if at is not None else None
    except (TypeError, ValueError):
        at = None
    return {"in": e.get("price_in_per_mtok"), "out": e.get("price_out_per_mtok"),
            "cache_read": e.get("price_cache_read_per_mtok"), "at": at}


def refresh_prices(model_ids: list[str]) -> tuple[dict[str, dict], bool]:
    """Fetch OpenRouter's catalog now and store the prices of `model_ids` (one fetch, one write).
    Returns ({id: info} for ids found, catalog_ok); blocks up to the catalog timeout."""
    from .providers import openai_compat as oc
    ids = list(dict.fromkeys(m for m in model_ids if m))
    if not ids:
        return {}, True
    info, ok = oc.refresh_prices_for(ids)
    if ok and info:
        oc.save_remote_model_infos(info)
    return info, ok


def refresh_prices_in_background(model_ids: list[str], on_done=None) -> bool:
    """The terminal /model's refresh: only when a price is older than 24 h, one at a time, silent on failure."""
    from .providers import openai_compat as oc
    return oc.refresh_prices_in_background(model_ids, on_done)



# ── models: list in config.yaml, edited as text (R423) ──────────────────
ModelEditError = _modelcfg.EditError


def model_entries(text: str) -> list:
    return _modelcfg.entries(text)


def model_add(text: str, entry: dict) -> str:
    return _modelcfg.add(text, entry)


def model_update(text: str, n: int, entry: dict) -> str:
    return _modelcfg.update(text, n, entry)


def model_delete(text: str, n: int) -> str:
    return _modelcfg.delete(text, n)


def load_models(path) -> list[dict]:
    """The models: list as the engine sees it (overlay merged, ${VARS} expanded)."""
    return _config.load_config(path)["models"]


def write_text_atomic(path, text: str) -> None:
    _paths.write_text_atomic(path, text)


# ── raw config editor (R427) ────────────────────────────────────────────
ConfigError = _configedit.ConfigError


def config_redact(text: str) -> str:
    return _configedit.redact(text)


def config_restore(new_text: str, original: str) -> str:
    return _configedit.restore(new_text, original)


def config_parse(text: str, name: str) -> dict:
    return _configedit.parse(text, name)


def config_check(merged: dict, current) -> list:
    return _configedit.check_shape(merged, current)


def config_digest(text: str) -> str:
    return _configedit.digest(text)


def config_backup_write(path, text: str):
    return _configedit.backup_and_write(path, text, _paths.write_text_atomic)


def config_merge(base, over):
    return _config._merge(base, over)


def load_config(path) -> dict:
    return _config.load_config(path)


def has_secret(text: str) -> bool:
    """R463: does this text contain something secrets.scan would redact?"""
    return bool(_secrets.scan(text))


# ── R450: what the web page needs from the tool layer ────────────────────────
def set_web_guard(on: bool = True) -> None:
    """Web mode: file tools refuse secrets, and Aurora's own policy/code files (see guard.py)."""
    _guard.WEB = bool(on)


def url_needs_approval(url: str) -> bool:
    """R283: could fetching this URL reach a private service or carry data out?"""
    return _tools.url_needs_approval(url)


def html_to_plain(html: str) -> str:
    """Tags and comments removed in one forward pass (linear time), `<script>`/`<style>` dropped."""
    _h = _htmltext
    return _h._flatten(_h._strip_blocks(_h._strip_comments(html), ("script", "style")))


def add_tools(specs: list[dict], runners: dict) -> list[str]:
    """R462: a web-only tool (e.g. `remember`) joins the extension tools the engine installed."""
    return _tools.set_extensions(list(_tools._EXTENSION_SPECS) + list(specs), {**_tools._EXTENSION_RUNNERS, **runners})


# ── R475: the command sandbox ────────────────────────────────────────────────
def web_sandbox(runtime: dict) -> str:
    """Box the web page's commands as `runtime: sandbox:` says (web, all: on; off). Returns "" or why it could not."""
    from . import sandbox as _sandbox
    if _sandbox.mode_from(runtime or {}) == "off":
        _tools.disable_sandbox()
        return ""
    return _tools.enable_sandbox(_sandbox.policy_from(runtime or {}, str(aurora_home())))


def sandbox_status() -> dict:
    """{"wanted", "active", "problem"}: whether commands run in the box, and why not when one was asked for."""
    return {"wanted": _tools.SANDBOX_WANTED, "active": _tools.sandbox_active(), "problem": _tools.SANDBOX_PROBLEM}
