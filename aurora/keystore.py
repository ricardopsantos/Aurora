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
from collections.abc import Callable

from .paths import aurora_home, write_bytes_atomic

_SERVICE = "aurora-agent"
_ENC_FILE = "keys.enc"
_passphrase_cache: dict[str, bytes] = {}


class KeystoreError(Exception):
    """R201: raised instead of silently replacing a key store that could not
    be read back. Named so callers can report it as a normal refusal rather
    than a traceback — the user mistyped a passphrase, which is not a bug."""

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
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt_path = aurora_home() / "keys.salt"
    if not salt_path.exists():
        # R201: same atomic+mode path as the store itself. A salt is not
        # secret, but a half-written one is worse than none: it would derive
        # a different key and make an existing store undecryptable.
        write_bytes_atomic(salt_path, os.urandom(16), mode=0o600)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=salt_path.read_bytes(), iterations=600_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode())))


def _encfile_load(passphrase: str) -> dict:
    p = aurora_home() / _ENC_FILE
    if not p.exists():
        return {}
    return json.loads(_fernet(passphrase).decrypt(p.read_bytes()))


def _encfile_save(passphrase: str, data: dict) -> None:
    # R201: atomic, and 0600 before the file is visible — a half-written key
    # store is an unrecoverable one, since nothing else holds the plaintext.
    write_bytes_atomic(aurora_home() / _ENC_FILE,
                       _fernet(passphrase).encrypt(json.dumps(data).encode()),
                       mode=0o600)


def _encfile_get(name: str, interactive: bool = True) -> str | None:
    if not (aurora_home() / _ENC_FILE).exists():
        return None
    pw = _passphrase_cache.get("pw")
    if pw is None:
        if not interactive:
            # a non-interactive lookup (footer renders, the /model picker's
            # has_key checks) must NEVER block on a passphrase prompt — with
            # an encrypted store present and nothing cached, the honest
            # answer is "can't tell right now", not a hidden getpass that
            # freezes the UI on a prompt nobody sees
            return None
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


def forget_passphrase() -> bool:
    """R170i: drop the cached encrypted-file passphrase, if any. Until this
    existed, `_passphrase_cache["pw"]` had NO clearing path at all once
    entered — it lived for the rest of the process, however long the
    session ran, with no timeout. `clear_key()` (below) clears a STORED
    KEY, not the cache — a different operation, despite the similar name;
    it in fact populates the cache on success (line ~192) to avoid asking
    twice in a row. This is the actual "forget the passphrase" primitive,
    for a caller (a `/key forget` command, an inactivity timeout) that
    wants to shrink the exposure window instead of "eventually, when the
    process exits." Best effort: `bytes` are immutable in Python, so this
    drops the dict's only reference rather than overwriting the memory in
    place — it removes the easy `_passphrase_cache` access path, not a
    guarantee against every form of memory inspection."""
    return _passphrase_cache.pop("pw", None) is not None


def get_key(env_var: str, interactive: bool = True) -> str | None:
    """env → keyring → encrypted file → prompt (offering to store)."""
    val = os.environ.get(env_var)
    if val:
        return val
    val = _keyring_get(env_var)
    if val:
        return val
    val = _encfile_get(env_var, interactive=interactive)
    if val:
        return val
    if not interactive:
        return None
    val = _prompter(f"Enter {env_var} (input hidden, empty to skip): ")
    if not val:
        return None
    try:
        store_key(env_var, val)
    except KeystoreError:
        pass   # R304: not stored, but still usable for this session
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
                # decrypted fine, key just isn't in there — a real "not
                # set" answer, distinct from the exception case below
                return "not set"
            except Exception:
                # R228: a cached passphrase that fails to decrypt the
                # CURRENT store (stale from an external re-encryption, a
                # different session sharing AURORA_HOME) used to fall
                # through silently to a flat "not set" — indistinguishable
                # from "no key store exists at all" and confidently wrong:
                # the store is right there, it just couldn't be checked
                # with this passphrase. Report the same "can't confirm"
                # answer as the no-cached-passphrase case, not a false
                # negative.
                return ("possibly set (encrypted file — enter passphrase "
                       "to confirm)")
        return "possibly set (encrypted file — enter passphrase to confirm)"
    return "not set"


def store_key(env_var: str, value: str) -> str:
    """Store via keyring when available, else the encrypted file. Returns a
    human description of where it went."""
    if _keyring_set(env_var, value):
        return "OS keyring"
    pw = _passphrase_cache.get("pw")
    if pw is None:
        # R201: "Choose" is only true for a NEW store. Asking someone to
        # "choose" a passphrase when an encrypted store already exists invites
        # exactly the typo that used to wipe it — they answer as if setting
        # one, not recalling one.
        label = ("Choose a key-store passphrase: "
                 if not (aurora_home() / _ENC_FILE).exists()
                 else "Aurora key-store passphrase: ")
        pw = _prompter(label).encode()
        # R304: an empty passphrase used to be accepted when CREATING the
        # store, but reading treats an empty entry as "skip" — so a store made
        # with Enter could never be opened again. Refused up front instead.
        if not pw:
            raise KeystoreError("an empty passphrase can't protect the key store")
        # R304: a NEW store's passphrase is typed twice — a typo there made
        # every key stored under it unrecoverable.
        if label.startswith("Choose") and \
                _prompter("Repeat the passphrase: ").encode() != pw:
            raise KeystoreError("passphrases don't match — key not stored")
        _passphrase_cache["pw"] = pw
    # R201: a decrypt failure here used to be swallowed, leaving `data` as
    # `{}` — and the save below then replaced the WHOLE store with just this
    # one key. The commonest way to reach it is not corruption but a MISTYPED
    # passphrase: with an existing store holding OPENROUTER_API_KEY and
    # ANTHROPIC_API_KEY, one typo while adding a third key destroyed both,
    # re-encrypted the file under the typo, and reported success. Verified
    # end-to-end before the fix — the correct passphrase then raised
    # InvalidToken against a store containing only the new key.
    #
    # "No file yet" is the one case where an empty dict is genuinely right,
    # and it is distinguishable without decrypting anything, so the two are
    # split apart rather than both landing in one `except`.
    if not (aurora_home() / _ENC_FILE).exists():
        data = {}
    else:
        try:
            data = _encfile_load(pw.decode())
        except Exception as e:
            # Drop the bad passphrase so the next attempt re-prompts rather
            # than failing again against the cached typo.
            _passphrase_cache.pop("pw", None)
            raise KeystoreError(
                "the existing key store could not be decrypted — wrong "
                "passphrase, or the file is damaged. Nothing was written: "
                "saving now would replace every key already in it."
            ) from e
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
