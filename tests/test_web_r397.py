"""R397: Aurora Web shows OpenRouter prices and refreshes them on demand (through aurora.service)."""
import time

import pytest

from aurora import service
from aurora.providers import openai_compat as oc
from aurora.web import core
from tests.test_web import app, call, types, wait_for  # noqa: F401  (fixture)

A, B = "vendor/model-a", "vendor/model-b"


@pytest.fixture
def prices(app, monkeypatch):
    """Two OpenRouter models in the config, a fake catalog, and saves kept in memory (the real file is in the repo)."""
    app.e.models.extend([{"model": A, "provider": "openrouter"}, {"model": B, "provider": "openrouter"},
                         {"model": A, "provider": "openrouter"}])
    table = {A: {"model": A, "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0, "refreshed_at": time.time() - 3 * 86400}}
    monkeypatch.setattr(oc, "REMOTE_CONTEXT_LIMITS", table)
    catalog = {"ok": True, "info": {A: {"price_in_per_mtok": 1.5, "price_out_per_mtok": 2.0}}, "calls": []}

    def fake_refresh(ids):
        catalog["calls"].append(list(ids))
        if not catalog["ok"]:
            return {}, False
        return {k: v for k, v in catalog["info"].items() if k in ids}, True

    def fake_save(infos):
        for mid, info in infos.items():
            e = dict(table.get(mid) or {"model": mid})
            e.update({k: v for k, v in info.items() if v is not None}); e["refreshed_at"] = time.time()
            table[mid] = e
    monkeypatch.setattr(oc, "refresh_prices_for", fake_refresh)
    monkeypatch.setattr(oc, "save_remote_model_infos", fake_save)
    monkeypatch.setattr(oc, "refresh_prices_in_background", lambda ids, on_done=None: catalog.setdefault("bg", []).append(ids) or False)
    monkeypatch.setattr(core, "PRICE_REFRESH_GAP_S", 0)
    return catalog


def test_model_list_carries_the_cached_price_and_its_age(app, prices):
    rows = {r["model"]: r for r in call(app, "/api/models")[1]["models"] if r["provider"] == "openrouter"}
    assert rows[A]["price"]["in"] == 1.0 and rows[A]["price"]["out"] == 2.0 and rows[A]["price"]["at"] > 0
    assert rows[B]["price"] is None                                # nothing cached yet
    assert "price" not in next(r for r in call(app, "/api/models")[1]["models"] if r["provider"] == "local")
    assert prices["bg"][-1] == [A, B]                              # the 24 h background refresh, de-duplicated


def test_refresh_updates_prices_and_says_what_changed(app, prices):
    code, d = call(app, "/api/prices/refresh", {})
    assert code == 200 and d["refreshed"] == 1 and d["missing"] == [B]
    assert d["changed"] == [{"model": A, "before": {"in": 1.0, "out": 2.0}, "after": {"in": 1.5, "out": 2.0}}]
    assert prices["calls"] == [[A, B]]                             # one catalog fetch for all models
    p = service.model_price(A)
    assert p["in"] == 1.5 and time.time() - p["at"] < 5
    assert wait_for(lambda: "prices" in types(app))
    ev = [e for e in app.s.events if e["type"] == "notify" and "prices refreshed" in e.get("text", "")]
    assert ev and "1 changed" in ev[-1]["text"] and B in ev[-1]["text"]


def test_refresh_with_no_changes_says_so(app, prices):
    prices["info"] = {A: {"price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0}}
    code, d = call(app, "/api/prices/refresh", {})
    assert code == 200 and d["changed"] == []


def test_catalog_unreachable_is_a_502_and_keeps_the_old_price(app, prices):
    prices["ok"] = False
    code, d = call(app, "/api/prices/refresh", {})
    assert code == 502 and "openrouter" in d["error"].lower()
    assert service.model_price(A)["in"] == 1.0


def test_an_exception_in_the_fetch_is_reported_not_raised(app, prices, monkeypatch):
    monkeypatch.setattr(oc, "refresh_prices_for", lambda ids: (_ for _ in ()).throw(OSError("dns")))
    code, d = call(app, "/api/prices/refresh", {})
    assert code == 502 and "dns" in d["error"]
    assert call(app, "/api/prices/refresh", {})[0] == 502            # the in-flight flag was released


def test_refresh_is_rate_limited(app, prices, monkeypatch):
    monkeypatch.setattr(core, "PRICE_REFRESH_GAP_S", 60)
    assert call(app, "/api/prices/refresh", {})[0] == 200
    code, d = call(app, "/api/prices/refresh", {})
    assert code == 429 and "ago" in d["error"]
    assert len(prices["calls"]) == 1


def test_only_one_refresh_at_a_time(app, prices):
    app.s._price_refreshing = True
    code, d = call(app, "/api/prices/refresh", {})
    assert code == 429 and "already running" in d["error"] and prices["calls"] == []


def test_no_openrouter_models_is_a_400(app, monkeypatch):
    called = []
    monkeypatch.setattr(oc, "refresh_prices_for", lambda ids: called.append(ids))
    code, d = call(app, "/api/prices/refresh", {})
    assert code == 400 and "no OpenRouter models" in d["error"] and not called


def test_refresh_needs_the_token(app, prices):
    assert call(app, "/api/prices/refresh", {}, token="")[0] == 401
    assert prices["calls"] == []


def test_usage_cost_follows_the_refreshed_price(app, prices):
    assert oc.cost_for(A, 1_000_000, 0) == pytest.approx(1.0)
    call(app, "/api/prices/refresh", {})
    assert oc.cost_for(A, 1_000_000, 0) == pytest.approx(1.5)


def test_service_model_price_handles_bad_stamps(monkeypatch):
    monkeypatch.setattr(oc, "REMOTE_CONTEXT_LIMITS", {"m": {"price_in_per_mtok": 0.0, "price_out_per_mtok": 0.0, "refreshed_at": "junk"}})
    assert service.model_price("m") == {"in": 0.0, "out": 0.0, "cache_read": None, "at": None}
    assert service.model_price("absent") is None
    assert service.refresh_prices([]) == ({}, True)
