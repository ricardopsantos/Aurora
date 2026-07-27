"""Tests for R136: the bundled refresh_model_prices extension
(aurora/extensions_bundled/price_refresh_extension.py)."""

from aurora.extensions_bundled import price_refresh_extension as ext


class _FakeEngine:
    def __init__(self, models):
        self.models = models


def test_no_models_configured():
    specs, runners = ext.register(_FakeEngine([]))
    assert runners["refresh_model_prices"]() == \
        "no OpenRouter models configured in config.yaml"


def test_skips_the_local_model():
    """The local provider has no OpenRouter listing/price — it must never be
    sent to refresh_prices_for, or the batch call would be asking OpenRouter
    about a model id it can't possibly know."""
    engine = _FakeEngine([{"provider": "local", "model": "local"}])
    _, runners = ext.register(engine)
    assert runners["refresh_model_prices"]() == \
        "no OpenRouter models configured in config.yaml"


def test_refreshes_and_saves_each_configured_openrouter_model(monkeypatch):
    engine = _FakeEngine([
        {"provider": "openrouter", "model": "vendor/a"},
        {"provider": "openrouter", "model": "vendor/b"},
        {"provider": "local", "model": "local"},
    ])
    seen_ids = []
    saved = {}

    def _fake_refresh(model_ids):
        seen_ids.extend(model_ids)
        return ({"vendor/a": {"context_size": 128000,
                              "price_in_per_mtok": 1.5,
                              "price_out_per_mtok": 6.0,
                              "description": ""}}, True)

    def _fake_save(infos):
        saved.update(infos)

    monkeypatch.setattr(
        "aurora.providers.openai_compat.refresh_prices_for", _fake_refresh)
    monkeypatch.setattr(
        "aurora.providers.openai_compat.save_remote_model_infos", _fake_save)

    _, runners = ext.register(engine)
    out = runners["refresh_model_prices"]()

    assert seen_ids == ["vendor/a", "vendor/b"]   # local never sent
    assert saved == {"vendor/a": {"context_size": 128000,
                                  "price_in_per_mtok": 1.5,
                                  "price_out_per_mtok": 6.0,
                                  "description": ""}}
    assert "vendor/a: ctx 128k, $1.5/$6 per M (listed price)" in out
    assert "vendor/b: not found on OpenRouter (skipped)" in out


def test_saves_in_one_batch_call_not_one_per_model(monkeypatch):
    """R136 review: refreshing N models used to call save_remote_model_info
    (a full read-modify-write of remote_context_limits.json) once per model
    — N configured models meant N full-file rewrites, and a crash between
    any two left the file half-written for every OTHER model too, not just
    the one in flight. One batched call now covers all of them."""
    engine = _FakeEngine([
        {"provider": "openrouter", "model": "vendor/a"},
        {"provider": "openrouter", "model": "vendor/b"},
    ])
    calls = []
    monkeypatch.setattr(
        "aurora.providers.openai_compat.refresh_prices_for",
        lambda model_ids: ({
            "vendor/a": {"context_size": 1000, "price_in_per_mtok": 1.0,
                        "price_out_per_mtok": 2.0, "description": ""},
            "vendor/b": {"context_size": 2000, "price_in_per_mtok": 3.0,
                        "price_out_per_mtok": 4.0, "description": ""},
        }, True))
    monkeypatch.setattr(
        "aurora.providers.openai_compat.save_remote_model_infos",
        lambda infos: calls.append(infos))
    _, runners = ext.register(engine)
    runners["refresh_model_prices"]()
    assert len(calls) == 1                     # one call, not two
    assert set(calls[0]) == {"vendor/a", "vendor/b"}


def test_a_genuinely_free_model_reports_and_would_persist_zero_price(
        monkeypatch):
    """R136 review: `save_remote_model_info`'s old truthiness check
    (`if info.get(k)`) treated a real $0 price the same as "the catalog said
    nothing" and silently dropped it — a free model refreshed after having
    once had a stale non-zero price would keep the stale one forever. This
    test pins the DISPLAY side (the extension must not treat 0 as missing
    either); the storage-side fix is covered directly in
    test_core.py-adjacent provider tests via `_merge_model_entry`."""
    engine = _FakeEngine([{"provider": "openrouter", "model": "vendor/free"}])
    monkeypatch.setattr(
        "aurora.providers.openai_compat.refresh_prices_for",
        lambda model_ids: ({"vendor/free": {
            "context_size": 32000, "price_in_per_mtok": 0.0,
            "price_out_per_mtok": 0.0, "description": ""}}, True))
    monkeypatch.setattr(
        "aurora.providers.openai_compat.save_remote_model_infos",
        lambda infos: None)
    _, runners = ext.register(engine)
    out = runners["refresh_model_prices"]()
    assert "vendor/free: ctx 32k, $0/$0 per M (listed price)" in out


def test_malformed_context_size_does_not_abort_the_whole_refresh(monkeypatch):
    """R136 review: a non-numeric context_size from the catalog used to raise
    inside int(), which would escape the runner and abort refreshing every
    OTHER model in the same call too — not just the one with bad data."""
    engine = _FakeEngine([
        {"provider": "openrouter", "model": "vendor/bad"},
        {"provider": "openrouter", "model": "vendor/good"},
    ])
    monkeypatch.setattr(
        "aurora.providers.openai_compat.refresh_prices_for",
        lambda model_ids: ({
            "vendor/bad": {"context_size": "not-a-number",
                          "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0,
                          "description": ""},
            "vendor/good": {"context_size": 4000, "price_in_per_mtok": 1.0,
                           "price_out_per_mtok": 2.0, "description": ""},
        }, True))
    monkeypatch.setattr(
        "aurora.providers.openai_compat.save_remote_model_infos",
        lambda infos: None)
    _, runners = ext.register(engine)
    out = runners["refresh_model_prices"]()   # must not raise
    assert "vendor/bad: $1/$2 per M (listed price)" in out   # ctx bit dropped
    assert "vendor/good: ctx 4k, $1/$2 per M (listed price)" in out


def test_duplicate_config_entries_are_not_double_processed(monkeypatch):
    """R136 review: two config.yaml entries for the same model id (e.g. two
    aliases pointing at it) used to be reported and saved twice."""
    engine = _FakeEngine([
        {"provider": "openrouter", "model": "vendor/a"},
        {"provider": "openrouter", "model": "vendor/a"},
    ])
    seen_ids = []
    monkeypatch.setattr(
        "aurora.providers.openai_compat.refresh_prices_for",
        lambda model_ids: (seen_ids.extend(model_ids) or
                          ({"vendor/a": {"context_size": 1000,
                                        "price_in_per_mtok": 1.0,
                                        "price_out_per_mtok": 2.0,
                                        "description": ""}}, True)))
    monkeypatch.setattr(
        "aurora.providers.openai_compat.save_remote_model_infos",
        lambda infos: None)
    _, runners = ext.register(engine)
    out = runners["refresh_model_prices"]()
    assert seen_ids == ["vendor/a"]
    assert out.count("vendor/a:") == 1


def test_catalog_unreachable_reports_that_not_a_per_model_failure(monkeypatch):
    engine = _FakeEngine([{"provider": "openrouter", "model": "vendor/a"}])
    monkeypatch.setattr(
        "aurora.providers.openai_compat.refresh_prices_for",
        lambda model_ids: ({}, False))
    _, runners = ext.register(engine)
    out = runners["refresh_model_prices"]()
    assert "couldn't reach the OpenRouter catalog" in out


# ── bundled-extension wiring: the tool is really discoverable ──────────────
def test_refresh_model_prices_is_registered_as_a_bundled_extension(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  local: {type: openai, base_url: x}\n"
                  "models:\n  - {provider: local, model: m}\n")
    from aurora import tools
    from aurora.engine import Engine
    Engine(str(cfg))
    try:
        names = [s["name"] for s in tools.specs()]
        assert "refresh_model_prices" in names
    finally:
        tools.set_extensions([], {})   # don't leak into later tests


# ── openai_compat.refresh_prices_for: one HTTP call for N models ──────────
def test_refresh_prices_for_makes_a_single_catalog_fetch(monkeypatch):
    from aurora.providers import openai_compat

    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [
                {"id": "vendor/a", "context_length": 64000,
                 "pricing": {"prompt": "0.000001", "completion": "0.000004"}},
                {"id": "vendor/c", "context_length": 32000,
                 "pricing": {"prompt": "0.0000005", "completion": "0.000002"}},
            ]}

    def _fake_get(url, timeout=10):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(openai_compat.httpx, "get", _fake_get)
    info, ok = openai_compat.refresh_prices_for(
        ["vendor/a", "vendor/b", "vendor/c"])
    assert ok is True
    assert len(calls) == 1                 # not one GET per model id
    assert set(info) == {"vendor/a", "vendor/c"}   # "vendor/b" not in catalog
    assert info["vendor/a"]["price_in_per_mtok"] == 1.0
    assert info["vendor/a"]["price_out_per_mtok"] == 4.0
