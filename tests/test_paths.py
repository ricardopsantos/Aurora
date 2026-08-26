"""Tests for aurora.paths — AURORA_HOME resolution and the atomic write
helpers. write_text_atomic already has coverage in test_core.py;
write_bytes_atomic (used by the encrypted key store) had none. The
`~/.aurora-path` marker-file resolution branch of aurora_home() was also
untested."""

import os

import pytest

from aurora import paths

# ── write_bytes_atomic ────────────────────────────────────────────────────

def test_write_bytes_atomic_writes_and_reads_back(tmp_path):
    target = tmp_path / "key.bin"
    paths.write_bytes_atomic(target, b"\x00\x01secret\xff")
    assert target.read_bytes() == b"\x00\x01secret\xff"


def test_write_bytes_atomic_overwrites_existing_file(tmp_path):
    target = tmp_path / "key.bin"
    paths.write_bytes_atomic(target, b"first")
    paths.write_bytes_atomic(target, b"second-longer-value")
    assert target.read_bytes() == b"second-longer-value"


def test_write_bytes_atomic_leaves_no_temp_files_behind(tmp_path):
    target = tmp_path / "key.bin"
    paths.write_bytes_atomic(target, b"a")
    paths.write_bytes_atomic(target, b"b")
    assert [p.name for p in tmp_path.iterdir()] == ["key.bin"]


def test_write_bytes_atomic_applies_mode_before_the_file_is_visible(tmp_path):
    target = tmp_path / "key.bin"
    paths.write_bytes_atomic(target, b"secret", mode=0o600)
    assert oct(target.stat().st_mode & 0o777) == oct(0o600)


def test_write_bytes_atomic_mode_none_leaves_default_permissions(tmp_path):
    target = tmp_path / "key.bin"
    paths.write_bytes_atomic(target, b"secret")   # mode=None
    # must not raise and must produce a normal, readable file
    assert target.read_bytes() == b"secret"


def test_write_bytes_atomic_creates_parent_directories(tmp_path):
    target = tmp_path / "nested" / "dir" / "key.bin"
    paths.write_bytes_atomic(target, b"secret")
    assert target.read_bytes() == b"secret"


def test_write_bytes_atomic_rollback_on_crash_leaves_original_untouched(
        tmp_path, monkeypatch):
    """A crash between the temp-file write and the rename must not corrupt
    or truncate the EXISTING file — the write lands whole or not at all,
    same guarantee write_text_atomic already has."""
    target = tmp_path / "key.bin"
    paths.write_bytes_atomic(target, b"original")

    def boom(*a, **k):
        raise OSError("crash mid-write")

    monkeypatch.setattr(paths.os, "replace", boom)
    with pytest.raises(OSError):
        paths.write_bytes_atomic(target, b"corrupted-attempt")
    assert target.read_bytes() == b"original"


def test_write_bytes_atomic_rollback_removes_the_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "key.bin"

    def boom(*a, **k):
        raise OSError("crash mid-write")

    monkeypatch.setattr(paths.os, "replace", boom)
    with pytest.raises(OSError):
        paths.write_bytes_atomic(target, b"data")
    # no dangling .key.bin.*.tmp left over
    assert list(tmp_path.iterdir()) == []


def test_write_bytes_atomic_fsyncs_the_file_contents(tmp_path, monkeypatch):
    synced = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(paths.os, "fsync", spy_fsync)
    paths.write_bytes_atomic(tmp_path / "key.bin", b"secret")
    # R234: two fsyncs — the temp file's contents AND the parent directory
    # entry the rename creates. This asserted `== 1` before, i.e. it pinned
    # the missing-durability bug in place: the key store (the one file whose
    # loss is unrecoverable) was the only atomic write without R171's
    # directory fsync.
    assert len(synced) == 2


def test_write_bytes_atomic_fsyncs_the_parent_directory(tmp_path, monkeypatch):
    """R234: the directory fd must be fsynced AFTER the rename, or a power
    loss can drop the rename and resurrect the previous key store."""
    order = []
    real_replace, real_fsync = paths.os.replace, os.fsync
    target = tmp_path / "key.bin"

    def spy_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    def spy_fsync(fd):
        order.append("fsync-dir" if os.fstat(fd).st_mode & 0o040000 else "fsync-file")
        return real_fsync(fd)

    monkeypatch.setattr(paths.os, "replace", spy_replace)
    monkeypatch.setattr(paths.os, "fsync", spy_fsync)
    paths.write_bytes_atomic(target, b"secret")
    assert order == ["fsync-file", "replace", "fsync-dir"]


# ── aurora_home() resolution order ───────────────────────────────────────

def test_aurora_home_prefers_env_var(tmp_path, monkeypatch):
    target = tmp_path / "envhome"
    monkeypatch.setenv("AURORA_HOME", str(target))
    assert paths.aurora_home() == target
    assert target.is_dir()


def test_aurora_home_uses_marker_file_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("AURORA_HOME", raising=False)
    marker_target = tmp_path / "markerhome"
    fake_marker = tmp_path / ".aurora-path"
    fake_marker.write_text(str(marker_target), encoding="utf-8")
    monkeypatch.setattr(paths, "_MARKER", fake_marker)
    assert paths.aurora_home() == marker_target
    assert marker_target.is_dir()


def test_aurora_home_marker_file_content_is_stripped(tmp_path, monkeypatch):
    """A trailing newline (the common case for a hand-edited or
    echo-appended marker file) must not become part of the path."""
    monkeypatch.delenv("AURORA_HOME", raising=False)
    marker_target = tmp_path / "markerhome"
    fake_marker = tmp_path / ".aurora-path"
    fake_marker.write_text(f"{marker_target}\n\n", encoding="utf-8")
    monkeypatch.setattr(paths, "_MARKER", fake_marker)
    assert paths.aurora_home() == marker_target


def test_aurora_home_env_var_takes_priority_over_marker_file(tmp_path, monkeypatch):
    env_target = tmp_path / "envhome"
    marker_target = tmp_path / "markerhome"
    fake_marker = tmp_path / ".aurora-path"
    fake_marker.write_text(str(marker_target), encoding="utf-8")
    monkeypatch.setattr(paths, "_MARKER", fake_marker)
    monkeypatch.setenv("AURORA_HOME", str(env_target))
    assert paths.aurora_home() == env_target


def test_aurora_home_falls_back_to_dot_aurora_when_nothing_set(tmp_path, monkeypatch):
    monkeypatch.delenv("AURORA_HOME", raising=False)
    monkeypatch.setattr(paths, "_MARKER", tmp_path / "does-not-exist")
    fake_home = tmp_path / "home"
    monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: fake_home))
    assert paths.aurora_home() == fake_home / ".aurora"


def test_aurora_home_env_var_supports_tilde_expansion(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))   # Path.expanduser() reads $HOME
    monkeypatch.setenv("AURORA_HOME", "~/customhome")
    assert paths.aurora_home() == fake_home / "customhome"


# ── AuroraHomeError / _ensure_dir ────────────────────────────────────────

def test_aurora_home_raises_when_target_is_a_file_not_a_directory(tmp_path, monkeypatch):
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file")
    monkeypatch.setenv("AURORA_HOME", str(blocker))
    with pytest.raises(paths.AuroraHomeError, match="not a directory"):
        paths.aurora_home()


def test_aurora_home_raises_a_readable_message_on_permission_denied(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "denied"))

    def boom(self, parents=True, exist_ok=True):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(paths.Path, "mkdir", boom)
    with pytest.raises(paths.AuroraHomeError, match="Permission denied"):
        paths.aurora_home()


def test_ensure_dir_is_idempotent_on_an_existing_directory(tmp_path):
    d = tmp_path / "already-there"
    d.mkdir()
    assert paths._ensure_dir(d, "test dir") == d
    assert paths._ensure_dir(d, "test dir") == d   # calling again is a no-op


# ── sessions_dir() ────────────────────────────────────────────────────────

def test_sessions_dir_is_created_under_aurora_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    sd = paths.sessions_dir()
    assert sd == tmp_path / "home" / "sessions"
    assert sd.is_dir()


def test_sessions_dir_error_names_itself_not_aurora_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    blocker = tmp_path / "home" / "sessions"
    blocker.write_text("i am a file")
    with pytest.raises(paths.AuroraHomeError, match="sessions directory"):
        paths.sessions_dir()


# ── R241: write_text_preserving — atomic writes for the USER's files ─────

def test_write_text_preserving_keeps_the_executable_bit(tmp_path):
    """R241: `os.replace` carries the TEMP file's mode across, and mkstemp
    makes 0600 — so a plain atomic write turns an executable script into a
    private, non-executable file."""
    script = tmp_path / "s.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    paths.write_text_preserving(script, "#!/bin/sh\necho hi\n")
    assert oct(script.stat().st_mode & 0o777) == oct(0o755)
    assert script.read_text() == "#!/bin/sh\necho hi\n"


def test_write_text_preserving_writes_through_a_symlink(tmp_path):
    """R241: `os.replace` would swap the LINK for a regular file, orphaning
    the file the user actually meant to edit."""
    real = tmp_path / "real.txt"
    real.write_text("old")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    paths.write_text_preserving(link, "new")
    assert link.is_symlink()
    assert real.read_text() == "new"


def test_write_text_preserving_creates_parent_directories(tmp_path):
    target = tmp_path / "a" / "b" / "new.txt"
    paths.write_text_preserving(target, "created")
    assert target.read_text() == "created"


def test_write_text_preserving_leaves_the_original_on_crash(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_text("original")

    def boom(*a, **k):
        raise OSError("crash mid-write")

    monkeypatch.setattr(paths.os, "replace", boom)
    with pytest.raises(OSError):
        paths.write_text_preserving(target, "half")
    assert target.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_write_text_preserving_new_file_is_not_locked_to_0600(tmp_path):
    """A brand-new file should follow the umask like a normal create, not
    inherit mkstemp's private 0600."""
    target = tmp_path / "new.txt"
    paths.write_text_preserving(target, "x")
    assert target.stat().st_mode & 0o044      # group/other readable
