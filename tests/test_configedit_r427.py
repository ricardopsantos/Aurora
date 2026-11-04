"""R427: secrets hidden for display and put back on save; YAML/shape errors with line numbers; backups kept."""
import pytest

from aurora import configedit as C

T = """providers:
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-SECRET1   # mine
    api_key_env: OPENROUTER_API_KEY
  local:
    base_url: http://x
    token: "tok SECRET2"
models:
- provider: local
  model: local
"""


def test_redact_hides_secrets_but_not_env_names():
    r = C.redact(T)
    assert "SECRET" not in r and r.count(C.HIDDEN) == 2
    assert "api_key: «hidden»   # mine" in r and "api_key_env: OPENROUTER_API_KEY" in r


def test_restore_puts_values_back_and_accepts_a_new_one():
    r = C.redact(T).replace("model: local\n", "model: local\n- provider: openrouter\n  model: a/b\n")
    out = C.restore(r, T)
    assert "api_key: sk-or-SECRET1   # mine" in out and 'token: "tok SECRET2"' in out and "model: a/b" in out
    out2 = C.restore(C.redact(T).replace('token: «hidden»', 'token: new'), T)
    assert "token: new" in out2 and "SECRET2" not in out2


def test_restore_refuses_a_hidden_value_that_moved():
    moved = C.redact(T).replace("  local:\n    base_url: http://x\n    token:", "  local2:\n    base_url: http://x\n    token:")
    with pytest.raises(C.ConfigError) as e:
        C.restore(moved, T)
    assert e.value.line == 8


def test_parse_reports_line_and_column():
    with pytest.raises(C.ConfigError) as e:
        C.parse("a: 1\nb: [1, 2\nc: 3\n", "config.yaml")
    assert e.value.line and "not valid YAML" in str(e.value)


def test_shape_checks():
    ok = {"providers": {"local": {}}, "models": [{"provider": "local", "model": "m"}]}
    assert C.check_shape(ok, ("local", "m")) == []
    errs = C.check_shape({"providers": {"local": {}}, "models": [{"provider": "nope", "model": "x"}, "bad"]}, ("local", "m"))
    assert any("nope" in e for e in errs) and any("entry 2" in e for e in errs) and any("in use" in e for e in errs)
    assert C.check_shape({"models": {}}, None) == ["models: must be a list (each entry starts with '- ')"]


def test_backups_keep_five_and_never_touch_hand_made_ones(tmp_path):
    p = tmp_path / "config.yaml"; p.write_text("a: 0\n")
    hand = tmp_path / "config.yaml.bak-20261102"; hand.write_text("mine")
    write = lambda path, text: path.write_text(text)
    for i in range(8):
        C.backup_and_write(p, f"a: {i + 1}\n", write)
    assert p.read_text() == "a: 8\n" and hand.read_text() == "mine"
    assert len(list(tmp_path.glob("config.yaml" + C.BAK_TAG + "*"))) == 5


def test_inline_mappings_are_hidden_and_restored():
    t = 'providers:\n  local: {type: openai, base_url: "http://x", api_key: SECRETKEY}   # c\n  b: {api_key: "two, three"}\n'
    r = C.redact(t)
    assert "SECRETKEY" not in r and "two, three" not in r and r.count(C.HIDDEN) == 2 and 'base_url: "http://x"' in r
    assert C.restore(r, t) == t
