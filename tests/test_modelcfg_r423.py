"""R423: config.yaml's models: list is edited as text; every comment and every other line survives."""
import pytest
import yaml

from aurora import modelcfg as M

T = """# top comment
providers:
  local: {base_url: http://x}   # keep me
  openrouter: {base_url: https://o}
models:
# the list
- provider: local   # small
  model: qwen3:1.7b
  tools: true
- provider: openrouter
  model: anthropic/claude-sonnet-4.6
  tools: true
  vision: true   # R381 note
- provider: local
  model: local
  extra_body:
    chat_template_kwargs:
      enable_thinking: false   # nested comment
doc_skill_roots:
  - /a   # after
runtime: {}
"""
KEEP = ["# top comment", "# keep me", "# the list", "# small", "# R381 note", "# nested comment", "  - /a   # after"]


def _kept(t, but=()):
    for c in KEEP:
        if c not in but:
            assert c in t, c


def test_change_a_scalar_keeps_its_inline_comment_and_every_other_line():
    t = M.update(T, 1, {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.7", "tools": False, "vision": True})
    _kept(t)
    assert "  model: anthropic/claude-sonnet-4.7\n  tools: false\n  vision: true   # R381 note\n" in t
    assert yaml.safe_load(t)["models"][1]["model"] == "anthropic/claude-sonnet-4.7"


def test_add_delete_and_nested_change():
    t = M.add(T, {"provider": "openrouter", "model": "x/y", "tools": True})
    _kept(t)
    assert t.index("model: x/y") < t.index("doc_skill_roots:")
    t = M.update(t, 2, {"provider": "local", "model": "local", "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}, "cache": False})
    _kept(t, but=("# nested comment",))                     # only the changed block's own comment can go
    t = M.delete(t, 1)
    _kept(t, but=("# nested comment", "# R381 note"))
    assert [m["model"] for m in yaml.safe_load(t)["models"]] == ["qwen3:1.7b", "local", "x/y"]


def test_first_key_removed_keeps_the_dash():
    t = M.update(T, 0, {"model": "qwen3:4b", "tools": True})
    assert yaml.safe_load(t)["models"][0] == {"model": "qwen3:4b", "tools": True}


def test_flow_style_entries_and_missing_or_empty_list():
    f = "providers: {local: {}}\nmodels:\n  - {model: other, provider: local}   # note\n  - {model: b, provider: local}\n"
    t = M.update(f, 0, {"model": "other2", "provider": "local", "tools": True})
    assert "# note" in t and yaml.safe_load(t)["models"][0]["model"] == "other2"
    assert yaml.safe_load(M.add("providers: {}\n", {"provider": "a", "model": "b"}))["models"] == [{"provider": "a", "model": "b"}]
    assert yaml.safe_load(M.add("models: []\nruntime: {}\n", {"provider": "a", "model": "b"}))["runtime"] == {}


def test_a_layout_it_cannot_edit_is_refused_not_mangled():
    with pytest.raises(M.EditError):
        M.add("models: [{model: a, provider: b}]\n", {"provider": "c", "model": "d"})
    with pytest.raises(M.EditError):
        M.delete(T, 9)
