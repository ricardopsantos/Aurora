"""R423: the page reads and edits config.yaml's models: list; comments survive; the live model list follows."""
from tests.test_web import app, call  # noqa: F401  (fixture)


def _cfg(app):
    return (app.tmp / "config.yaml").read_text()


def test_r423_list_add_save_delete(app):
    p = app.tmp / "config.yaml"
    p.write_text("# mine\n" + p.read_text().replace("provider: local, tools: true}", "provider: local, tools: true}   # in use"))
    d = call(app, "/api/models/config")[1]
    assert d["editable"] and d["providers"] == ["local"]
    assert [(m["model"], m["current"]) for m in d["models"]] == [("other", False), ("local", True)]
    st, out = call(app, "/api/models/config/add", {"provider": "local", "model": "new-one", "tools": False, "extra": "cache: false"})
    assert st == 200, out
    assert "# mine" in _cfg(app) and "# in use" in _cfg(app)
    assert [m["model"] for m in app.e.models] == ["other", "local", "new-one"] and app.e.models[2]["cache"] is False
    st, out = call(app, "/api/models/config/save", {"i": 0, "expect": {"provider": "local", "model": "other"},
                                                    "provider": "local", "model": "renamed", "tools": True})
    assert st == 200, out
    assert app.e.current["model"] == "local" and app.e.current is app.e.models[1]
    assert call(app, "/api/models/config/delete", {"i": 1, "expect": {"provider": "local", "model": "local"}})[0] == 409   # in use
    assert call(app, "/api/models/config/delete", {"i": 0, "expect": {"provider": "local", "model": "other"}})[0] == 409   # stale
    assert call(app, "/api/models/config/delete", {"i": 0, "expect": {"provider": "local", "model": "renamed"}})[0] == 200
    assert app.e.current is app.e.models[0] and app.e.current["model"] == "local"
    assert "# mine" in _cfg(app) and "# in use" in _cfg(app) and "renamed" not in _cfg(app)


def test_r423_bad_input_is_refused(app):
    before = _cfg(app)
    assert call(app, "/api/models/config/add", {"provider": "nope", "model": "x"})[0] == 400
    assert call(app, "/api/models/config/add", {"provider": "local", "model": ""})[0] == 400
    assert call(app, "/api/models/config/add", {"provider": "local", "model": "x", "extra": "- a list"})[0] == 400
    assert call(app, "/api/models/config/add", {"provider": "local", "model": "x", "extra": "model: y"})[0] == 400
    assert _cfg(app) == before


def test_r423_overlay_models_make_it_read_only(app):
    import os
    from pathlib import Path
    home = Path(os.environ["AURORA_HOME"]); home.mkdir(parents=True, exist_ok=True)
    (home / "config.local.yaml").write_text("models:\n  - {model: z, provider: local}\n")
    d = call(app, "/api/models/config")[1]
    assert not d["editable"] and "config.local.yaml" in d["why"]
    assert call(app, "/api/models/config/add", {"provider": "local", "model": "x"})[0] == 409


def test_r423_renaming_the_model_in_use_moves_the_remembered_pick(app):
    from aurora import service
    st, out = call(app, "/api/models/config/save", {"i": 1, "expect": {"provider": "local", "model": "local"},
                                                    "provider": "local", "model": "local2", "tools": True})
    assert st == 200, out
    assert app.e.current["model"] == "local2" and service.load_state().get("web_last_model") == "local2"
