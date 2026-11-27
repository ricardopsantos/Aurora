"""R442 - the /model picker names the provider when a model name repeats across providers."""
import re

from aurora import ui
from tests.test_core import _mk_engine

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _labels(tmp_path, monkeypatch, models):
    e = _mk_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(e, "list_models", lambda: models)
    seen = []
    monkeypatch.setattr(ui, "select", lambda prompt, options, **k: (
        seen.append([ANSI.sub("", o[1]) for o in options]), None)[1])
    ui._pick_model(e, ui.TerminalFrontend())
    return seen[0]


def test_r442_several_local_providers_show_provider_slash_local(tmp_path, monkeypatch):
    models = [{"provider": "srv-a", "model": "local"}, {"provider": "srv-b", "model": "local"},
              {"provider": "srv-c", "model": "local"}, {"provider": "openrouter", "model": "acme/x"}]
    labels = _labels(tmp_path, monkeypatch, models)
    for p in ("srv-a", "srv-b", "srv-c"):
        assert any(l.startswith(f"{p}/local") for l in labels), (p, labels)
    assert any(l.startswith("acme/x") for l in labels) and not any(l.startswith("openrouter/") for l in labels)


def test_r442_a_single_local_provider_keeps_the_plain_name(tmp_path, monkeypatch):
    labels = _labels(tmp_path, monkeypatch, [{"provider": "srv-a", "model": "local"}, {"provider": "openrouter", "model": "acme/x"}])
    assert any(l.startswith("local") for l in labels) and not any(l.startswith("srv-a/") for l in labels)
