"""R237: bootstrap.py — capped fetch and atomic persistence."""
import httpx

from aurora import bootstrap


def test_fetch_url_caps_an_oversized_body(monkeypatch):
    """R237: a plain c.get(url) + r.text pulls an unbounded body into memory,
    and this content becomes the first TOOL-ENABLED turn of the session."""
    huge = b"x" * (bootstrap._FETCH_CAP * 3)

    def handler(request):
        return httpx.Response(200, content=huge)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*a, **k):
        k["transport"] = transport
        return real_client(*a, **k)

    monkeypatch.setattr(bootstrap.httpx, "Client", fake_client)
    text = bootstrap.fetch_url("https://example.invalid/b.md")
    assert len(text) == bootstrap._FETCH_CAP


def test_fetch_url_returns_a_small_body_whole(monkeypatch):
    body = "# Session bootstrap\nread the rules\n"

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=body.encode()))
    real_client = httpx.Client

    def fake_client(*a, **k):
        k["transport"] = transport
        return real_client(*a, **k)

    monkeypatch.setattr(bootstrap.httpx, "Client", fake_client)
    assert bootstrap.fetch_url("https://example.invalid/b.md") == body


def test_save_writes_atomically(tmp_path, monkeypatch):
    """R237: a persisted file under AURORA_HOME gets R146a's treatment —
    a crash mid-write must not leave a truncated bootstrap prompt."""
    calls = []
    real = bootstrap.write_text_atomic

    def spy(path, text):
        calls.append(str(path))
        return real(path, text)

    monkeypatch.setattr(bootstrap, "write_text_atomic", spy)
    monkeypatch.setattr(bootstrap, "_global_path",
                        lambda: tmp_path / "bootstrap.md")
    bootstrap.save("do the thing", source_url="https://example.invalid/b.md")
    assert any(c.endswith("bootstrap.md") for c in calls)
    assert any(c.endswith("bootstrap.md.source") for c in calls)
    assert (tmp_path / "bootstrap.md").read_text() == "do the thing\n"


def test_save_leaves_no_temp_files_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "_global_path",
                        lambda: tmp_path / "bootstrap.md")
    bootstrap.save("do the thing")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bootstrap.md"]
