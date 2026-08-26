"""Tests for aurora.keystore.key_status() — previously had ZERO direct
tests despite being the function with a real reporting bug (R228). Also
covers get_key()'s full resolution order and _encfile_get()'s interactive/
non-interactive split, both thin in existing coverage (test_core.py covers
store_key/clear_key/forget_passphrase well already)."""

from aurora import keystore


def _no_keyring(monkeypatch):
    """Force every keystore call past the keyring step, onto the encrypted
    file — same pattern test_core.py's keystore tests already use."""
    monkeypatch.setattr(keystore, "_keyring_get", lambda n: None)
    monkeypatch.setattr(keystore, "_keyring_set", lambda n, v: False)


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    keystore._passphrase_cache.clear()
    monkeypatch.delenv("PROBE_VAR", raising=False)


# ── key_status(): env var / keyring / no store at all ───────────────────

def test_key_status_env_var(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("PROBE_VAR", "value")
    assert keystore.key_status("PROBE_VAR") == "set (env var)"


def test_key_status_keyring(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(keystore, "_keyring_get",
                        lambda n: "value" if n == "PROBE_VAR" else None)
    assert keystore.key_status("PROBE_VAR") == "set (OS keyring)"


def test_key_status_env_var_beats_keyring(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("PROBE_VAR", "from-env")
    monkeypatch.setattr(keystore, "_keyring_get", lambda n: "from-keyring")
    assert keystore.key_status("PROBE_VAR") == "set (env var)"


def test_key_status_nothing_stored_anywhere(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    assert keystore.key_status("PROBE_VAR") == "not set"


# ── key_status(): encrypted file, all four cache/content combinations ──

def test_key_status_encrypted_file_present_key_found_with_cached_pw(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "the-passphrase")
    keystore.store_key("PROBE_VAR", "secret")
    assert keystore.key_status("PROBE_VAR") == "set (encrypted file)"


def test_key_status_encrypted_file_present_key_absent_with_cached_pw(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "the-passphrase")
    keystore.store_key("SOME_OTHER_VAR", "secret")   # creates the file
    assert keystore.key_status("PROBE_VAR") == "not set"


def test_key_status_encrypted_file_present_no_cached_passphrase(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "the-passphrase")
    keystore.store_key("PROBE_VAR", "secret")
    keystore.forget_passphrase()
    assert keystore.key_status("PROBE_VAR") == \
        "possibly set (encrypted file — enter passphrase to confirm)"


def test_key_status_encrypted_file_present_wrong_cached_passphrase(
        tmp_path, monkeypatch):
    """R228: a stale/wrong cached passphrase used to make key_status()
    silently swallow the decrypt failure and report a flat "not set" —
    indistinguishable from no store existing at all, and confidently wrong
    since the file is right there. Must report the same "can't confirm"
    answer the no-cached-passphrase case gets."""
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "the-passphrase")
    keystore.store_key("PROBE_VAR", "secret")
    keystore._passphrase_cache["pw"] = b"definitely-the-wrong-passphrase"
    assert keystore.key_status("PROBE_VAR") == \
        "possibly set (encrypted file — enter passphrase to confirm)"


def test_key_status_never_prompts(tmp_path, monkeypatch):
    """key_status()'s own docstring: safe to call without prompting for
    anything, even with an encrypted file present and no cached passphrase."""
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "the-passphrase")
    keystore.store_key("PROBE_VAR", "secret")
    keystore.forget_passphrase()
    prompts = []
    monkeypatch.setattr(keystore, "_prompter",
                        lambda label: prompts.append(label) or "should-not-run")
    keystore.key_status("PROBE_VAR")
    assert prompts == []


def test_key_status_does_not_mutate_the_passphrase_cache(tmp_path, monkeypatch):
    """A successful status check with a correct cached passphrase must
    leave the cache exactly as it found it — it's a read-only probe."""
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "the-passphrase")
    keystore.store_key("PROBE_VAR", "secret")
    before = dict(keystore._passphrase_cache)
    keystore.key_status("PROBE_VAR")
    assert keystore._passphrase_cache == before


# ── get_key(): full resolution order ─────────────────────────────────────

def test_get_key_env_beats_everything(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("PROBE_VAR", "from-env")
    monkeypatch.setattr(keystore, "_keyring_get", lambda n: "from-keyring")
    assert keystore.get_key("PROBE_VAR") == "from-env"


def test_get_key_keyring_beats_encrypted_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(keystore, "_keyring_get",
                        lambda n: "from-keyring" if n == "PROBE_VAR" else None)
    monkeypatch.setattr(keystore, "_encfile_get",
                        lambda n, interactive=True: "from-encfile")
    assert keystore.get_key("PROBE_VAR") == "from-keyring"


def test_get_key_falls_back_to_encrypted_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "the-passphrase")
    keystore.store_key("PROBE_VAR", "secret-from-file")
    keystore.forget_passphrase()
    monkeypatch.setattr(keystore, "_prompter", lambda label: "the-passphrase")
    assert keystore.get_key("PROBE_VAR") == "secret-from-file"


def test_get_key_prompts_and_stores_when_nothing_found(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "typed-value")
    val = keystore.get_key("PROBE_VAR")
    assert val == "typed-value"
    # must actually have been stored — a second lookup finds it without
    # prompting again
    monkeypatch.setattr(keystore, "_prompter",
                        lambda label: (_ for _ in ()).throw(
                            AssertionError("should not re-prompt")))
    assert keystore.get_key("PROBE_VAR") == "typed-value"


def test_get_key_noninteractive_returns_none_when_nothing_found(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    assert keystore.get_key("PROBE_VAR", interactive=False) is None


def test_get_key_empty_prompt_response_returns_none_without_storing(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "")
    assert keystore.get_key("PROBE_VAR") is None
    assert not (tmp_path / "keys.enc").exists()


# ── _encfile_get(): passphrase caching across calls ─────────────────────

def test_encfile_get_caches_the_passphrase_after_first_prompt(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _no_keyring(monkeypatch)
    monkeypatch.setattr(keystore, "_prompter", lambda label: "the-passphrase")
    keystore.store_key("PROBE_VAR", "secret")
    keystore.forget_passphrase()

    prompt_count = {"n": 0}

    def counting_prompter(label):
        prompt_count["n"] += 1
        return "the-passphrase"

    monkeypatch.setattr(keystore, "_prompter", counting_prompter)
    keystore.get_key("PROBE_VAR")
    keystore.get_key("PROBE_VAR")
    assert prompt_count["n"] == 1   # second call reused the cached passphrase


def test_encfile_get_returns_none_when_file_does_not_exist(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert keystore._encfile_get("PROBE_VAR", interactive=False) is None
