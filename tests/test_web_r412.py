"""R412: a sleeping model does not block pictures; a model that cannot see gets the saved file instead of a refusal."""
from tests.test_web import app, call, types, wait_for  # noqa: F401  (fixture)
from tests.test_web_r401 import _seen
from tests.test_web_r406 import _jpeg_with_exif, _upload


def test_r412_asleep_uses_the_remembered_answer_and_defaults_to_yes(app):
    from aurora.web import core
    prov = app.state["prov"]
    prov.serves_vision = lambda: None                               # asleep, never asked before
    assert app.s.vision(probe=True) is True
    prov.serves_vision = lambda: False                              # awake: a real no is remembered
    app.s._vision_cache.clear()
    assert app.s.vision(probe=True) is False
    prov.serves_vision = lambda: None
    app.s._vision_cache.clear()
    assert app.s.vision(probe=True) is False                        # asleep again: the remembered no
    assert list(core._vision_memory().values()) == [False]


def test_r412_no_vision_model_still_takes_the_picture_as_a_file(app):
    app.state["prov"].serves_vision = lambda: False
    d = _upload(app, "p.jpg", _jpeg_with_exif())
    assert d["kind"] == "image" and d["vision"] is False and d["path"]
    seen = _seen(app)
    assert call(app, "/api/send", {"text": "make it grey", "images": [d["data_url"]], "image_paths": [d["path"]]})[0] == 200
    assert wait_for(lambda: "done" in types(app))
    assert f"[Attached picture saved at: {d['path']}]" in seen[0] and "cannot see pictures" in seen[0]


def test_r412_a_picture_without_a_saved_file_is_still_refused_on_a_blind_model(app):
    app.state["prov"].serves_vision = lambda: False
    assert call(app, "/api/send", {"text": "x", "images": ["data:image/png;base64,AAAA"]})[0] == 400
