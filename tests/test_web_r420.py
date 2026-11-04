"""R420: music and video uploads are stored byte for byte, never read; the model is told the path."""
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.test_web import TOKEN, app  # noqa: F401  (fixture)
from tests.test_web_r406 import _upload


def test_r420_mp3_and_mp4_are_saved_unchanged_and_the_path_is_the_text(app):
    raw = b"ID3\x04\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 40     # binary, with NULs
    d = _upload(app, "03 The Kill.mp3", raw)
    assert d["kind"] == "media" and d["media"] == "audio"
    assert Path(d["path"]).read_bytes() == raw
    assert d["path"] in d["text"] and "The Kill.mp3" in d["text"] and "binary" not in d["text"]
    v = _upload(app, "clip.MP4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 500)
    assert v["media"] == "video" and Path(v["path"]).is_file()


def test_r420_media_limit_is_larger_than_documents():
    from aurora.web import attach
    assert attach.max_attach("a.mp3") == attach.MAX_MEDIA > attach.MAX_ATTACH == attach.max_attach("a.pdf")
    big = b"\x00" * (attach.MAX_ATTACH + 1)
    assert attach.process("song.flac", big)["kind"] == "media"
    with pytest.raises(attach.Unsupported):
        attach.process("doc.txt", big)


def test_r420_other_binary_is_still_refused(app):
    with pytest.raises(urllib.error.HTTPError) as e:
        _upload(app, "thing.bin", b"\x00\x01\x02" * 10)
    assert e.value.code == 415
