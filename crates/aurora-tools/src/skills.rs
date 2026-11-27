//! Skills (Python `aurora/skills.py`, R11, R247): `/name args` runs an
//! executable or `.py` file from `<config dir>/skills/` or
//! `AURORA_HOME/skills/`; a doc skill is a `SKILL.md` under a configured
//! `doc_skill_roots:` directory, whose body is fed to the model as a turn.

use std::fs;
use std::io::{BufRead, BufReader};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use aurora_core::CancellationToken;
use aurora_storage::pathlike::{shlex_join, shlex_split};
use serde_json::Value;

use crate::command::{run_once, squeeze, Ran, COMMAND_RESULT_LIMIT};

pub const SKILL_TIMEOUT: Duration = Duration::from_secs(300);

fn dirs(config_base: Option<&Path>, home: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Some(b) = config_base {
        out.push(b.join("skills"));
    }
    out.push(home.join("skills"));
    out.into_iter().filter(|d| d.is_dir()).collect()
}

fn executable(p: &Path) -> bool {
    fs::metadata(p).map(|m| m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

fn stem(p: &Path) -> String {
    p.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default()
}

/// name → path; earlier directories shadow later ones.
pub fn discover(config_base: Option<&Path>, home: &Path) -> Vec<(String, PathBuf)> {
    let mut found: Vec<(String, PathBuf)> = Vec::new();
    for d in dirs(config_base, home) {
        let Ok(rd) = fs::read_dir(&d) else { continue };
        let mut entries: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
        entries.sort();
        for p in entries {
            if p.is_file() && (executable(&p) || p.extension().is_some_and(|e| e == "py")) {
                let name = stem(&p);
                if !found.iter().any(|(n, _)| *n == name) {
                    found.push((name, p));
                }
            }
        }
    }
    found
}

/// The first line's trailing comment (not a shebang), at most 70 characters.
fn blurb(p: &Path) -> String {
    let Ok(f) = fs::File::open(p) else { return String::new() };
    let mut r = BufReader::new(f);
    for _ in 0..3 {
        let mut buf = Vec::new();
        if r.by_ref().take(512).read_until(b'\n', &mut buf).is_err() {
            break;
        }
        let line = String::from_utf8_lossy(&buf).to_string();
        if let Some((_, after)) = line.split_once('#') {
            if !line.starts_with("#!") {
                return after.trim().chars().take(70).collect();
            }
        }
    }
    String::new()
}

use std::io::Read;

fn doc_roots(roots: &[String]) -> Vec<PathBuf> {
    roots.iter().map(|r| aurora_storage::paths::expand_user(r)).filter(|p| p.is_dir()).collect()
}

fn walk_follow(dir: &Path, out: &mut Vec<PathBuf>, depth: usize) {
    if depth > 40 {
        return;
    }
    let Ok(rd) = fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            walk_follow(&p, out, depth + 1);
        } else if e.file_name() == "SKILL.md" {
            out.push(p);
        }
    }
}

/// name (the SKILL.md's directory) → path, found recursively, symlinks followed.
pub fn discover_docs(roots: &[String]) -> Vec<(String, PathBuf)> {
    let mut found: Vec<(String, PathBuf)> = Vec::new();
    for root in doc_roots(roots) {
        let mut matches = Vec::new();
        walk_follow(&root, &mut matches, 0);
        matches.sort();
        for p in matches {
            let name = p.parent().and_then(|d| d.file_name()).map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
            if !found.iter().any(|(n, _)| *n == name) {
                found.push((name, p));
            }
        }
    }
    found
}

fn lenient_frontmatter(raw: &str) -> serde_json::Map<String, Value> {
    let mut out = serde_json::Map::new();
    for line in aurora_tools_lines(raw) {
        let Some((k, v)) = line.split_once(':') else { continue };
        let ok_key = k.chars().next().is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
            && k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
        if ok_key && !out.contains_key(k) {
            let v = v.strip_prefix([' ', '\t']).unwrap_or(v);
            out.insert(k.to_string(), Value::String(v.trim().to_string()));
        }
    }
    out
}

fn aurora_tools_lines(s: &str) -> Vec<&str> {
    crate::patch::py_splitlines(s)
}

/// Split a leading `---` YAML block from the body; malformed YAML falls back to flat `key: value` lines.
pub fn parse_frontmatter(text: &str) -> (serde_json::Map<String, Value>, String) {
    if !text.starts_with("---") {
        return (Default::default(), text.to_string());
    }
    let parts: Vec<&str> = text.splitn(3, "---").collect();
    if parts.len() < 3 {
        return (Default::default(), text.to_string());
    }
    let meta = match aurora_storage::config::parse_yaml(parts[1]) {
        Ok(Value::Object(m)) => m,
        Ok(_) => Default::default(),
        Err(_) => lenient_frontmatter(parts[1]),
    };
    (meta, parts[2].trim_start_matches('\n').to_string())
}

/// Python `str(value)` for a frontmatter field.
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn doc_blurb(p: &Path) -> String {
    let Ok(bytes) = fs::read(p) else { return String::new() };
    let (meta, _) = parse_frontmatter(&String::from_utf8_lossy(&bytes));
    meta.get("description").map(py_str).unwrap_or_default().chars().take(100).collect()
}

/// The body fed to the model, with a `# Skill:` header; None for an unknown name.
pub fn load_doc(name: &str, roots: &[String]) -> Option<String> {
    let path = discover_docs(roots).into_iter().find(|(n, _)| n == name)?.1;
    let text = String::from_utf8_lossy(&fs::read(&path).ok()?).to_string();
    let (meta, body) = parse_frontmatter(&text);
    let mut header = format!("# Skill: {}\n", meta.get("name").map(py_str).unwrap_or_else(|| name.to_string()));
    if let Some(d) = meta.get("description").filter(|d| truthy(d)) {
        header.push_str(&format!("> {}\n", py_str(d)));
    }
    Some(format!("{header}\n{body}"))
}

pub fn listing(config_base: Option<&Path>, home: &Path, roots: &[String]) -> String {
    let sk = discover(config_base, home);
    let docs = discover_docs(roots);
    if sk.is_empty() && docs.is_empty() {
        return "[no skills installed — drop executables or .py files in skills/, or point doc_skill_roots at a SKILL.md library]".into();
    }
    let mut lines: Vec<String> = sk.iter().map(|(n, p)| format!("/{n}  {}", blurb(p))).collect();
    lines.extend(docs.iter().filter(|(n, _)| !sk.iter().any(|(s, _)| s == n)).map(|(n, p)| format!("/{n}  [doc] {}", doc_blurb(p))));
    lines.join("\n")
}

/// Run an executable skill (not boxed: the user typed it, like bash mode, R475).
/// A non-executable `.py` runs with `python` (Python uses its own interpreter).
pub async fn run(name: &str, args: &str, config_base: Option<&Path>, home: &Path, python: &str, timeout: Duration) -> String {
    let Some((_, path)) = discover(config_base, home).into_iter().find(|(n, _)| n == name) else {
        return format!("[unknown skill: /{name} — try /skills]");
    };
    let mut cmd: Vec<String> = if path.extension().is_some_and(|e| e == "py") && !executable(&path) {
        vec![python.to_string(), path.display().to_string()]
    } else {
        vec![path.display().to_string()]
    };
    if !args.is_empty() {
        match shlex_split(args) {
            Ok(a) => cmd.extend(a),
            Err(()) => return "[skill args error: No closing quotation]".into(),
        }
    }
    match run_once(&shlex_join(&cmd), None, timeout, &CancellationToken::new(), None).await {
        Err(e) => format!("[skill error: {e}]"),
        Ok(Ran::TimedOut(out)) => {
            let msg = format!("[skill timeout after {}s]", timeout.as_secs());
            if out.is_empty() { msg } else { format!("{}\n{msg}", squeeze(&out, COMMAND_RESULT_LIMIT)) }
        }
        Ok(Ran::Cancelled(out)) | Ok(Ran::Exited(out, 0)) => {
            let s = squeeze(out.trim(), COMMAND_RESULT_LIMIT);
            if s.is_empty() { "[no output]".into() } else { s }
        }
        Ok(Ran::Exited(out, code)) => {
            let s = squeeze(out.trim(), COMMAND_RESULT_LIMIT);
            format!("{}\n[exit {code}]", if s.is_empty() { "[no output]".to_string() } else { s })
        }
    }
}
