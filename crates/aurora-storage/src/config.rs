//! config.yaml (Python `aurora/config.py`): `${ENV_VAR}` expansion,
//! `$AURORA_HOME/config.local.yaml` merged over it (R316), the four sections
//! coerced to their container type, `_path` and `_base_dir` added.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Number, Value};

use crate::paths::aurora_home;
use crate::{Result, StorageError};

fn yaml_to_json(v: serde_yaml_ng::Value) -> Value {
    yaml_to_json_in(v, &std::collections::HashSet::new())
}

/// YAML 1.1 booleans PyYAML resolves and YAML 1.2 (serde_yaml_ng) leaves as strings.
const YAML11_BOOLS: [(&str, bool); 12] = [
    ("yes", true), ("Yes", true), ("YES", true), ("on", true), ("On", true), ("ON", true),
    ("no", false), ("No", false), ("NO", false), ("off", false), ("Off", false), ("OFF", false),
];

/// The 1.1 words that appear quoted somewhere in `text`: those stay strings (the parsed value does not keep the style).
fn quoted_words(text: &str) -> std::collections::HashSet<String> {
    YAML11_BOOLS
        .iter()
        .filter(|(w, _)| text.contains(&format!("\"{w}\"")) || text.contains(&format!("'{w}'")))
        .map(|(w, _)| w.to_string())
        .collect()
}

fn yaml_to_json_in(v: serde_yaml_ng::Value, quoted: &std::collections::HashSet<String>) -> Value {
    use serde_yaml_ng::Value as Y;
    match v {
        Y::Null => Value::Null,
        Y::Bool(b) => Value::Bool(b),
        Y::Number(n) => {
            if let Some(i) = n.as_i64() {
                Value::Number(i.into())
            } else if let Some(u) = n.as_u64() {
                Value::Number(u.into())
            } else {
                n.as_f64().and_then(Number::from_f64).map(Value::Number).unwrap_or(Value::Null)
            }
        }
        // R498: an unquoted yes/no/on/off is a boolean, as in PyYAML
        Y::String(s) => match YAML11_BOOLS.iter().find(|(w, _)| *w == s) {
            Some((_, b)) if !quoted.contains(&s) => Value::Bool(*b),
            _ => Value::String(s),
        },
        Y::Sequence(items) => Value::Array(items.into_iter().map(|i| yaml_to_json_in(i, quoted)).collect()),
        Y::Mapping(m) => Value::Object(
            m.into_iter()
                .map(|(k, v)| {
                    let key = match k {
                        Y::String(s) => s,
                        k => match yaml_to_json(k) {
                            Value::String(s) => s,
                            other => other.to_string(),
                        },
                    };
                    (key, yaml_to_json_in(v, quoted))
                })
                .collect(),
        ),
        Y::Tagged(t) => yaml_to_json_in(t.value, quoted),
    }
}

/// Parse one YAML file the way `yaml.safe_load(f) or {}` does.
/// `yaml.safe_load(text)` as JSON; a parse error is an error.
pub fn parse_yaml(text: &str) -> Result<Value> {
    let y: serde_yaml_ng::Value = serde_yaml_ng::from_str(text).map_err(|e| StorageError::Format(e.to_string()))?;
    Ok(yaml_to_json_in(y, &quoted_words(text)))
}

pub fn read_yaml(path: &Path) -> Result<Value> {
    let text = fs::read_to_string(path).map_err(|e| StorageError::Io(format!("{}: {e}", path.display())))?;
    if text.trim().is_empty() {
        return Ok(Value::Object(Map::new()));
    }
    let y: serde_yaml_ng::Value =
        serde_yaml_ng::from_str(&text).map_err(|e| StorageError::Format(format!("{}: {e}", path.display())))?;
    Ok(match yaml_to_json_in(y, &quoted_words(&text)) {
        Value::Null => Value::Object(Map::new()),
        v => v,
    })
}

/// R316: dicts merge key by key; anything else (lists included) is replaced.
pub fn merge(base: Value, over: Value) -> Value {
    match (base, over) {
        (Value::Object(mut b), Value::Object(o)) => {
            for (k, v) in o {
                let merged = match b.remove(&k) {
                    Some(old) => merge(old, v),
                    None => v,
                };
                b.insert(k, merged);
            }
            Value::Object(b)
        }
        (_, over) => over,
    }
}

/// `${NAME}` → the environment value, or "" when unset. Only that exact form;
/// `$NAME` and `${1X}` are left alone, as Python's regex does.
pub fn expand_env(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find("${") {
        out.push_str(&rest[..i]);
        let after = &rest[i + 2..];
        let name_len = after.find('}').filter(|&n| {
            let name = &after[..n];
            name.chars().next().is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
                && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
        });
        match name_len {
            Some(n) => {
                out.push_str(&std::env::var(&after[..n]).unwrap_or_default());
                rest = &after[n + 1..];
            }
            None => {
                out.push_str("${");
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

fn expand(v: Value) -> Value {
    match v {
        Value::String(s) => Value::String(expand_env(&s)),
        Value::Array(a) => Value::Array(a.into_iter().map(expand).collect()),
        Value::Object(m) => Value::Object(m.into_iter().map(|(k, v)| (k, expand(v))).collect()),
        other => other,
    }
}

/// The file with the per-machine overlay merged over it, unexpanded.
pub fn read_raw_config(path: &Path) -> Result<Value> {
    let raw = read_yaml(path)?;
    let local = aurora_home()?.join("config.local.yaml");
    // AURORA_NO_LOCAL_CONFIG: a generated config (the test suite's Rust-loop shim, R507) is used as written
    if local.is_file() && std::env::var_os("AURORA_NO_LOCAL_CONFIG").is_none() {
        let over = read_yaml(&local)?;
        if raw.is_object() && over.is_object() {
            return Ok(merge(raw, over));
        }
    }
    Ok(raw)
}

pub fn load_config(path: &Path) -> Result<Value> {
    let mut cfg = match expand(read_raw_config(path)?) {
        Value::Object(m) => m,
        _ => Map::new(),
    };
    for (key, empty) in [("providers", Value::Object(Map::new())), ("models", Value::Array(vec![])),
                         ("runtime", Value::Object(Map::new())), ("skills", Value::Object(Map::new()))] {
        let ok = matches!((cfg.get(key), &empty), (Some(Value::Object(_)), Value::Object(_)) | (Some(Value::Array(_)), Value::Array(_)));
        if !ok {
            cfg.insert(key.into(), empty);
        }
    }
    let abs = fs::canonicalize(path)?;
    cfg.insert("_path".into(), Value::String(abs.to_string_lossy().into()));
    cfg.insert("_base_dir".into(), Value::String(abs.parent().unwrap_or(Path::new("/")).to_string_lossy().into()));
    Ok(Value::Object(cfg))
}

/// Which config.yaml to use: an explicit path, else $AURORA_CONFIG, else
/// $AURORA_HOME/config.yaml. (Python also looks next to its package; a
/// compiled binary has no package directory, so name the file instead.)
pub fn find_config(explicit: Option<&Path>) -> Result<PathBuf> {
    if let Some(p) = explicit {
        return if p.is_file() { Ok(p.into()) } else { Err(StorageError::Io(format!("not a file: {}", p.display()))) };
    }
    if let Ok(env) = std::env::var("AURORA_CONFIG") {
        if !env.is_empty() {
            let p = PathBuf::from(&env);
            return if p.is_file() { Ok(p) } else { Err(StorageError::Io(format!("AURORA_CONFIG is not a file: {env}"))) };
        }
    }
    let home = aurora_home()?.join("config.yaml");
    if home.is_file() {
        return Ok(home);
    }
    Err(StorageError::Io("no config.yaml found: pass --config FILE, set AURORA_CONFIG, or put one in AURORA_HOME".into()))
}

/// Per-machine state (`$AURORA_HOME/state.yaml`); {} when missing or unreadable.
pub fn load_state() -> Map<String, Value> {
    aurora_home()
        .ok()
        .map(|h| h.join("state.yaml"))
        .filter(|p| p.exists())
        .and_then(|p| read_yaml(&p).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn env_expansion_matches_the_python_regex() {
        unsafe { std::env::set_var("AURORA_T_X", "v") };
        assert_eq!(expand_env("a${AURORA_T_X}b"), "avb");
        assert_eq!(expand_env("${AURORA_T_UNSET_9}"), "");
        assert_eq!(expand_env("$AURORA_T_X ${1X} ${"), "$AURORA_T_X ${1X} ${");
    }

    #[test]
    fn overlay_replaces_lists_and_merges_maps() {
        let base = json!({"providers": {"local": {"base_url": ["a", "b"], "type": "openai"}}, "models": [1]});
        let over = json!({"providers": {"local": {"base_url": ["c"]}}});
        assert_eq!(merge(base, over), json!({"providers": {"local": {"base_url": ["c"], "type": "openai"}}, "models": [1]}));
    }

    #[test]
    fn bare_sections_become_empty_containers() {
        let dir = tempfile::tempdir().unwrap();
        unsafe { std::env::set_var("AURORA_HOME", dir.path().join("home")) };
        let p = dir.path().join("config.yaml");
        fs::write(&p, "runtime:\nmodels:\n").unwrap();
        let cfg = load_config(&p).unwrap();
        assert_eq!(cfg["runtime"], json!({}));
        assert_eq!(cfg["models"], json!([]));
        assert_eq!(cfg["providers"], json!({}));
        assert!(cfg["_path"].as_str().unwrap().ends_with("config.yaml"));
    }
}

#[cfg(test)]
mod yaml11_tests {
    use super::*;

    #[test]
    fn yaml11_words_are_booleans_unless_quoted() {
        let v = parse_yaml("a: off\nb: Yes\nc: 'no'\nd: [on, ON]\non: 1\ne: offline\n").unwrap();
        assert_eq!(v["a"], Value::Bool(false));
        assert_eq!(v["b"], Value::Bool(true));
        assert_eq!(v["c"], Value::String("no".into()));
        assert_eq!(v["d"], serde_json::json!([true, true]));
        assert_eq!(v["on"], serde_json::json!(1));
        assert_eq!(v["e"], Value::String("offline".into()));
    }
}
