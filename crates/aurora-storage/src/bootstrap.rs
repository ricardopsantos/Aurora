//! The saved bootstrap prompt (Python `aurora/bootstrap.py`): a free-text
//! prompt sent as a session's first, tool-enabled turn. A project's
//! `.aurora/bootstrap.md` wins over `AURORA_HOME/bootstrap.md`; an empty file
//! is skipped. A URL-sourced prompt keeps its URL in a `.source` sidecar.
//! R286: a project prompt runs only once its exact content is trusted on this
//! machine (`trusted_bootstraps.json`: resolved path → SHA-256).
//! Fetching a URL (`/bootstrap set <url>`, refresh) belongs to the front ends
//! and is not here yet.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::paths::write_atomic;
use crate::Result;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    Project,
    Global,
}

impl Scope {
    pub fn label(self) -> &'static str {
        match self {
            Scope::Project => "project",
            Scope::Global => "global",
        }
    }
}

pub struct Bootstrap {
    home: PathBuf,
    cwd: PathBuf,
}

fn read_nonempty(p: &Path) -> Option<String> {
    if !p.is_file() {
        return None;
    }
    let text = fs::read_to_string(p).ok()?;
    (!text.trim().is_empty()).then_some(text)
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect()
}

fn sidecar(p: &Path) -> PathBuf {
    let name = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    p.with_file_name(format!("{name}.source"))
}

/// `json.dumps(data, indent=1)`: one space of indent, ASCII-escaped.
fn dump_indent1(map: &Map<String, Value>) -> String {
    if map.is_empty() {
        return "{}".into();
    }
    let lines: Vec<String> = map
        .iter()
        .map(|(k, v)| format!(" {}: {}", aurora_core::pyjson::dumps_ascii(k), aurora_core::pyjson::dumps_ascii(v)))
        .collect();
    format!("{{\n{}\n}}", lines.join(",\n"))
}

impl Bootstrap {
    /// `cwd` is resolved like Python's `Path(cwd).resolve()`.
    pub fn new(home: PathBuf, cwd: &Path) -> Self {
        let cwd = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
        Self { home, cwd }
    }

    pub fn path(&self, scope: Scope) -> PathBuf {
        match scope {
            Scope::Project => self.cwd.join(".aurora").join("bootstrap.md"),
            Scope::Global => self.home.join("bootstrap.md"),
        }
    }

    fn active(&self) -> Option<(PathBuf, Scope, String)> {
        [Scope::Project, Scope::Global].into_iter().find_map(|s| {
            let p = self.path(s);
            read_nonempty(&p).map(|t| (p, s, t))
        })
    }

    /// `load()`: the prompt (stripped) and its label, e.g. `project (/x/.aurora/bootstrap.md)`.
    pub fn load(&self) -> Option<(String, String)> {
        let (p, s, text) = self.active()?;
        Some((text.trim().to_string(), format!("{} ({})", s.label(), p.display())))
    }

    /// The URL the active prompt was downloaded from, if any.
    pub fn source_url(&self) -> Option<String> {
        let (p, _, _) = self.active()?;
        let url = fs::read_to_string(sidecar(&p)).ok()?;
        let url = url.trim();
        (!url.is_empty()).then(|| url.to_string())
    }

    pub fn save(&self, text: &str, scope: Scope, source_url: Option<&str>) -> Result<PathBuf> {
        let p = self.path(scope);
        write_atomic(&p, format!("{}\n", text.trim_end()).as_bytes(), None)?;
        let sp = sidecar(&p);
        match source_url {
            Some(u) if !u.is_empty() => write_atomic(&sp, format!("{}\n", u.trim()).as_bytes(), None)?,
            _ if sp.is_file() => fs::remove_file(&sp)?,
            _ => {}
        }
        Ok(p)
    }

    pub fn clear(&self, scope: Scope) -> Result<Option<PathBuf>> {
        let p = self.path(scope);
        let sp = sidecar(&p);
        if sp.is_file() {
            fs::remove_file(&sp)?;
        }
        if p.is_file() {
            fs::remove_file(&p)?;
            return Ok(Some(p));
        }
        Ok(None)
    }

    fn trust_file(&self) -> PathBuf {
        self.home.join("trusted_bootstraps.json")
    }

    fn load_trust(&self) -> Map<String, Value> {
        fs::read_to_string(self.trust_file())
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .and_then(|v| v.as_object().cloned())
            .unwrap_or_default()
    }

    /// R286: the active prompt is a project one whose content was never approved here.
    pub fn needs_trust(&self) -> bool {
        let Some((p, Scope::Project, _)) = self.active() else { return false };
        let Ok(bytes) = fs::read(&p) else { return true };
        self.load_trust().get(&p.display().to_string()).and_then(Value::as_str) != Some(sha256_hex(&bytes).as_str())
    }

    /// Record the active project prompt's current content as approved.
    pub fn trust(&self) -> Result<()> {
        let Some((p, Scope::Project, _)) = self.active() else { return Ok(()) };
        let mut data = self.load_trust();
        data.insert(p.display().to_string(), Value::String(sha256_hex(&fs::read(&p)?)));
        write_atomic(&self.trust_file(), format!("{}\n", dump_indent1(&data)).as_bytes(), None)
    }
}

/// `from_input`: one line naming an existing `.md`/`.txt` file → its contents and path.
pub fn from_input(text: &str) -> (String, Option<PathBuf>) {
    let c = text.trim();
    let lower = c.to_lowercase();
    if !c.is_empty() && !c.contains('\n') && (lower.ends_with(".md") || lower.ends_with(".txt")) {
        let p = crate::paths::expand_user(c);
        if let Ok(t) = fs::read_to_string(&p) {
            if p.is_file() {
                return (t, Some(p));
            }
        }
    }
    (text.to_string(), None)
}

/// `is_url`: a single line starting with http:// or https://.
pub fn is_url(text: &str) -> bool {
    let c = text.trim().to_lowercase();
    !c.contains('\n') && (c.starts_with("http://") || c.starts_with("https://"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn setup() -> (tempfile::TempDir, Bootstrap) {
        let d = tempfile::tempdir().unwrap();
        fs::create_dir_all(d.path().join("home")).unwrap();
        fs::create_dir_all(d.path().join("work")).unwrap();
        let b = Bootstrap::new(d.path().join("home"), &d.path().join("work"));
        (d, b)
    }

    #[test]
    fn project_wins_and_empty_files_are_skipped() {
        let (_d, b) = setup();
        assert!(b.load().is_none());
        b.save("global one  ", Scope::Global, None).unwrap();
        assert_eq!(b.load().unwrap().0, "global one");
        fs::create_dir_all(b.path(Scope::Project).parent().unwrap()).unwrap();
        fs::write(b.path(Scope::Project), "  \n").unwrap();
        assert!(b.load().unwrap().1.starts_with("global ("));
        assert!(!b.needs_trust());
        b.save("proj", Scope::Project, Some("https://x/y.md")).unwrap();
        assert!(b.load().unwrap().1.starts_with("project ("));
        assert_eq!(b.source_url().as_deref(), Some("https://x/y.md"));
        b.save("proj2", Scope::Project, None).unwrap();
        assert_eq!(b.source_url(), None);
    }

    #[test]
    fn trust_is_per_exact_content() {
        let (_d, b) = setup();
        b.save("run me", Scope::Project, None).unwrap();
        assert!(b.needs_trust());
        b.trust().unwrap();
        assert!(!b.needs_trust());
        b.save("run me, changed", Scope::Project, None).unwrap();
        assert!(b.needs_trust());
        assert_eq!(b.clear(Scope::Project).unwrap(), Some(b.path(Scope::Project)));
        assert!(!b.needs_trust());
    }

    #[test]
    fn input_helpers() {
        assert!(is_url(" https://a/b "));
        assert!(!is_url("https://a\nb"));
        assert_eq!(from_input("not a file.md"), ("not a file.md".to_string(), None));
    }
}
