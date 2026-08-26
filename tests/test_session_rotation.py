"""Tests for aurora.session's size-based log rotation — SESSION_LOG_MAX_BYTES,
`_write_target`, `_parts_for`, and every reader that must walk a session's
full sequence of rotated part files. This subsystem previously had zero
direct test coverage (every existing session test uses a single small log
that never rotates)."""

from aurora import session as sessionmod
from aurora.session import Session


def test_write_target_is_base_file_for_a_brand_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    s = Session("rot0000001")
    assert s._write_target() == s.log_path


def test_write_target_stays_on_base_file_below_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    s = Session("rot0000002")
    s.log("user", text="hello")
    assert s._write_target() == s.log_path


def test_rotates_to_part_2_once_base_file_exceeds_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 100)
    s = Session("rot0000003")
    s.log("user", text="x" * 200)   # pushes part 1 well past the 100-byte cap
    assert s._write_target() == sessionmod.sessions_dir() / "rot0000003.2.jsonl"


def test_rotation_only_advances_by_one_part_per_check(tmp_path, monkeypatch):
    """The cap is approximate — a single oversized record can push a part
    past the cap, but the NEXT write only advances to part+1, never skips
    ahead to part+2 even if that write would also be oversized."""
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 50)
    s = Session("rot0000004")
    s.log("user", text="x" * 100)
    s.log("user", text="y" * 100)
    parts = sessionmod._parts_for("rot0000004")
    assert len(parts) == 2
    assert parts[1].name == "rot0000004.2.jsonl"


def test_many_records_produce_several_rotated_parts_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 60)
    s = Session("rot0000005")
    for i in range(20):
        s.log("user", text=f"message number {i} padding padding padding")
    parts = sessionmod._parts_for("rot0000005")
    assert len(parts) >= 3
    names = [p.name for p in parts]
    assert names[0] == "rot0000005.jsonl"
    # _parts_for probes n=2,3,4,... in numeric order, so the list itself is
    # already the correct oldest-first sequence — verify against that
    # numeric order rather than a lexical sort (which would misplace "10"
    # before "2").
    def _n(name):
        return 1 if name == "rot0000005.jsonl" else int(name.split(".")[1])
    assert [_n(n) for n in names] == sorted(_n(n) for n in names)


def test_iter_records_reads_across_all_rotated_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 60)
    s = Session("rot0000006")
    for i in range(15):
        s.log("user", text=f"msg{i} " + "pad" * 10)
    all_recs = list(s.iter_records())
    assert len(all_recs) == 15
    assert [r["text"].split()[0] for r in all_recs] == [f"msg{i}" for i in range(15)]


def test_iter_records_event_filter_works_across_rotated_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 60)
    s = Session("rot0000007")
    for i in range(10):
        s.log("user", text=f"u{i}" + "pad" * 10)
        s.log("assistant", text=f"a{i}" + "pad" * 10, model="m")
    got = list(s.iter_records(events={"assistant"}))
    assert len(got) == 10
    assert all(r["event"] == "assistant" for r in got)


def test_base_session_ids_counts_a_rotated_session_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 60)
    s = Session("rot0000008")
    for i in range(10):
        s.log("user", text=f"m{i}" + "pad" * 10)
    ids = sessionmod._base_session_ids()
    assert ids.count("rot0000008") == 1


def test_latest_mtime_follows_the_currently_written_part(tmp_path, monkeypatch):
    """A rotated session's original base file stops changing the moment
    rotation happens — sort/display order must follow whichever part is
    CURRENTLY being written, not the original file's now-stale mtime."""
    import os
    import time as _time

    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 60)
    s = Session("rot0000009")
    s.log("user", text="x" * 100)   # base file now over the cap
    s.log("user", text="y" * 10)    # this write actually lands in part 2
    parts = sessionmod._parts_for("rot0000009")
    assert len(parts) == 2
    old_time = _time.time() - 1000
    os.utime(parts[0], (old_time, old_time))
    latest = sessionmod._latest_mtime("rot0000009")
    assert latest == parts[1].stat().st_mtime


def test_usage_by_model_sums_across_rotated_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 60)
    s = Session("rot0000010")
    for i in range(8):
        s.log("assistant", model="gpt", input_tokens=10, output_tokens=5,
             text="pad" * 10)
    usage = sessionmod.usage_by_model("rot0000010")
    assert usage["gpt"]["turns"] == 8
    assert usage["gpt"]["input"] == 80
    assert usage["gpt"]["output"] == 40


def test_search_sessions_finds_a_hit_in_a_later_rotated_part(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 60)
    s = Session("rot0000011")
    for i in range(10):
        s.log("user", text=f"filler {i} " + "pad" * 10)
    s.log("user", text="the needle is here " + "pad" * 10)
    hits = sessionmod.search_sessions("needle")
    assert any(h[0] == "rot0000011" for h in hits)


def test_corrupt_line_in_a_rotated_part_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 60)
    s = Session("rot0000012")
    for i in range(8):
        s.log("user", text=f"m{i}" + "pad" * 10)
    parts = sessionmod._parts_for("rot0000012")
    assert len(parts) >= 2
    with open(parts[-1], "a", encoding="utf-8") as f:
        f.write("{not valid json\n")
    recs = list(s.iter_records())
    assert all("text" in r for r in recs)


def test_export_markdown_includes_records_from_every_rotated_part(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path))
    monkeypatch.setattr(sessionmod, "SESSION_LOG_MAX_BYTES", 60)
    s = Session("rot0000013")
    for i in range(10):
        s.log("user", text=f"question {i} " + "pad" * 10)
    md = sessionmod.export_markdown("rot0000013")
    for i in range(10):
        assert f"question {i}" in md


# ── R232: the record must reach the fd inside the flock ──────────────────

def test_log_flushes_before_releasing_the_lock(tmp_path, monkeypatch):
    """R232: the unlock used to happen while the record was still sitting in
    Python's write buffer, so R170f's lock protected nothing that touched
    the file. A record larger than the 8KB buffer flushed part of itself
    under the lock and the rest after it — the interleaving window the lock
    exists to close."""
    import fcntl as real_fcntl
    monkeypatch.setattr(sessionmod, "sessions_dir", lambda: tmp_path)
    order = []
    real_flock = real_fcntl.flock

    def spy_flock(f, op):
        order.append("lock" if op == real_fcntl.LOCK_EX else "unlock")
        return real_flock(f, op)

    class _Spy:
        def __init__(self, f):
            self._f = f

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._f.__exit__(*exc)

        def flush(self):
            order.append("flush")
            return self._f.flush()

    monkeypatch.setattr(sessionmod.fcntl, "flock", spy_flock)
    s = sessionmod.Session("r232")
    real_open = open

    def spy_open(*a, **k):
        return _Spy(real_open(*a, **k))

    monkeypatch.setattr("builtins.open", spy_open)
    s.log("user", text="x" * 50_000)     # far larger than the 8KB buffer
    monkeypatch.undo()
    assert order[0] == "lock"
    assert order[-1] == "unlock"
    assert "flush" in order
    assert order.index("flush") < order.index("unlock")
