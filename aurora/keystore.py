"""API-key storage. Resolution order, first hit wins (R22):
  1. environment variable            (the common self-hosted-box pattern —
                                        e.g. set in /etc/environment)
  2. OS keyring                      (macOS Keychain / SecretService)
  3. Fernet-encrypted file, opt-in   (passphrase at launch, held in memory)
  4. interactive prompt, offer to store via 2 or 3
Never plaintext on disk."""

import base64
import json
import os
from pathlib import Path
from typing import Callable

from .paths import aurora_home

_SERVICE = "aurora-agent"
_ENC_FILE = "keys.enc"
_passphrase_cache: dict[str, bytes] = {}

# Secret prompter — injected by the caller (the UI) so the engine never owns
# terminal I/O. Signature: (label) -> entered string ('' = skip/cancel).
# A default is set at import for headless/CLI use, but any front end can
# override it via set_prompter() so key entry works in an HTML UI too.
def _default_prompter(label: str) -> str:
    import getpass
    return getpass.getpass(label).strip()


_prompter: Callable[[str], str] = _default_prompter


def set_prompter(fn: Callable[[str], str]) -> None:
    global _prompter
    _prompter = fn


def _keyring_get(name: str) -> str | None:
    try:
        import keyring
        return keyring.get_password(_SERVICE, name)
    except Exception:
        return None


def _keyring_set(name: str, value: str) -> bool:
    try:
        import keyring
        keyring.set_password(_SERVICE, name, value)
        return True
    except Exception:
        return False


def _fernet(passphrase: str):
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.fernet import Fernet
    salt_path = aurora_home() / "keys.salt"
    if not salt_path.exists():
        salt_path.write_bytes(os.urandom(16))
        salt_path.chmod(0o600)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=salt_path.read_bytes(), iterations=600_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode())))


def _encfile_load(passphrase: str) -> dict:
    p = aurora_home() / _ENC_FILE
    if not p.exists():
        return {}
    return json.loads(_fernet(passphrase).decrypt(p.read_bytes()))


def _encfile_save(passphrase: str, data: dict) -> None:
    p = aurora_home() / _ENC_FILE
    p.write_bytes(_fernet(passphrase).encrypt(json.dumps(data).encode()))
    p.chmod(0o600)


def _encfile_get(name: str) -> str | None:
    if not (aurora_home() / _ENC_FILE).exists():
        return None
    pw = _passphrase_cache.get("pw")
    if pw is None:
        entered = _prompter("Aurora key-store passphrase: ")
        if not entered:
            return None
        pw = entered.encode()
        _passphrase_cache["pw"] = pw
    try:
        return _encfile_load(pw.decode()).get(name)
    except Exception:
        _passphrase_cache.pop("pw", None)
        return None


def get_key(env_var: str, interactive: bool = True) -> str | None:
    """env → keyring → encrypted file → prompt (offering to store)."""
    val = os.environ.get(env_var)
    if val:
        return val
    val = _keyring_get(env_var)
    if val:
        return val
    val = _encfile_get(env_var)
    if val:
        return val
    if not interactive:
        return None
    val = _prompter(f"Enter {env_var} (input hidden, empty to skip): ")
    if not val:
        return None
    store_key(env_var, val)
    return val


def key_status(env_var: str) -> str:
    """Where env_var would resolve from, without prompting for anything (so
    it's safe to call just to report status). Doesn't decrypt keys.enc —
    that needs a passphrase — but says whether one is stored there."""
    if os.environ.get(env_var):
        return "set (env var)"
    if _keyring_get(env_var):
        return "set (OS keyring)"
    p = aurora_home() / _ENC_FILE
    if p.exists():
        pw = _passphrase_cache.get("pw")
        if pw is not None:
            try:
                if env_var in _encfile_load(pw.decode()):
                    return "set (encrypted file)"
            except Exception:
                pass
        else:
            return "possibly set (encrypted file — enter passphrase to confirm)"
    return "not set"


def store_key(env_var: str, value: str) -> str:
    """Store via keyring when available, else the encrypted file. Returns a
    human description of where it went."""
    if _keyring_set(env_var, value):
        return "OS keyring"
    pw = _passphrase_cache.get("pw")
    if pw is None:
        pw = _prompter("Choose a key-store passphrase: ").encode()
        _passphrase_cache["pw"] = pw
    data = {}
    try:
        data = _encfile_load(pw.decode())
    except Exception:
        pass
    data[env_var] = value
    _encfile_save(pw.decode(), data)
    return "encrypted file"


def clear_key(env_var: str) -> list[str]:
    """Remove a stored key from every backend that can actually be cleared
    (keyring, encrypted file). An env var can't be unset from here — the
    caller must tell the user to do that themselves. Returns which backends
    it was found and removed from (empty list = wasn't stored anywhere we
    can reach)."""
    removed = []
    try:
        import keyring
        if keyring.get_password(_SERVICE, env_var) is not None:
            keyring.delete_password(_SERVICE, env_var)
            removed.append("OS keyring")
    except Exception:
        pass
    p = aurora_home() / _ENC_FILE
    if p.exists():
        pw = _passphrase_cache.get("pw")
        if pw is None:
            entered = _prompter("Aurora key-store passphrase: ")
            pw = entered.encode() if entered else None
        if pw is not None:
            try:
                data = _encfile_load(pw.decode())
                if env_var in data:
                    del data[env_var]
                    _encfile_save(pw.decode(), data)
                    removed.append("encrypted file")
                _passphrase_cache["pw"] = pw   # only cache once it decrypted OK
            except Exception:
                pass
    return removed
