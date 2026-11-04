"""R427: the page reads the raw config (keys hidden), validates and saves it; models and providers reload."""
from tests.test_web import app, call  # noqa: F401  (fixture)


def _files(app):
    return {f["name"]: f for f in call(app, "/api/config/files")[1]["files"]}


def test_r427_read_validate_save_reload(app):
    p = app.tmp / "config.yaml"
    p.write_text(p.read_text().replace('base_url: "http://x"}', 'base_url: "http://x", api_key: SECRETKEY}') + "# end\n")
    f = _files(app)["config.yaml"]
    assert "SECRETKEY" not in f["text"] and "«hidden»" in f["text"]
    new = f["text"].replace("  - {model: local, provider: local, tools: true}\n",
                            "  - {model: local, provider: local, tools: true}\n  - {model: added, provider: local}\n")
    assert call(app, "/api/config/validate", {"name": "config.yaml", "text": new, "base": f["base"]})[0] == 200
    st, out = call(app, "/api/config/save", {"name": "config.yaml", "text": new, "base": f["base"]})
    assert st == 200 and out["backup"], out
    assert [m["model"] for m in app.e.models] == ["other", "local", "added"] and app.e.current["model"] == "local"
    assert "SECRETKEY" in p.read_text() and "# end" in p.read_text()
    assert call(app, "/api/config/save", {"name": "config.yaml", "text": new, "base": f["base"]})[0] == 409   # stale base


def test_r427_invalid_is_refused_with_a_line(app):
    f = _files(app)["config.yaml"]
    before = (app.tmp / "config.yaml").read_text()
    st, out = call(app, "/api/config/save", {"name": "config.yaml", "text": "models: [\n", "base": f["base"]})
    assert st == 400 and out["line"]
    bad = f["text"].replace("provider: local, tools: true", "provider: ghost, tools: true")
    st, out = call(app, "/api/config/validate", {"name": "config.yaml", "text": bad, "base": f["base"]})
    assert st == 400 and any("ghost" in e for e in out["errors"])
    assert (app.tmp / "config.yaml").read_text() == before
