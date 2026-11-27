//! R450: the web page's path guard (Python `aurora/guard.py`). On the web page,
//! file tools refuse secret locations (reads and writes) and Aurora's own
//! files outside `workspace/` (writes). Paths are resolved (symlinks, `..`)
//! before they are compared. Off unless the core serves the web page.

use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use async_trait::async_trait;
use aurora_core::{Args, CancellationToken, Tool, ToolBox, ToolSpec};
use serde_json::Value;

static WEB: AtomicBool = AtomicBool::new(false);

pub fn set_web(on: bool) {
    WEB.store(on, Ordering::SeqCst);
}

pub fn web() -> bool {
    WEB.load(Ordering::SeqCst)
}

const SECRET_DIRS: [&str; 3] = [".ssh", ".gnupg", ".aws"];
const SECRET_NAMES: [&str; 4] = [".netrc", ".git-credentials", "keys.enc", ".pgpass"];
const SECRET_ETC: [&str; 5] = ["/etc/environment", "/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/master.passwd"];
const SECRET_SUFFIXES: [&str; 3] = [".token", ".pem", ".key"];
const READ_TOOLS: [&str; 4] = ["read_file", "list_dir", "grep", "find_files"];
const WRITE_TOOLS: [&str; 3] = ["write_file", "edit_file", "apply_patch"];

/// `os.path.realpath(os.path.expanduser(p or "."))`: the existing part resolved, the rest folded.
pub fn real(raw: &str) -> PathBuf {
    let raw = if raw.is_empty() { "." } else { raw };
    let p = if raw == "~" || raw.starts_with("~/") {
        PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(raw.trim_start_matches('~').trim_start_matches('/'))
    } else {
        PathBuf::from(raw)
    };
    let abs = if p.is_absolute() { p } else { std::env::current_dir().unwrap_or_default().join(p) };
    let mut out = PathBuf::from("/");
    for c in abs.components() {
        match c {
            Component::RootDir | Component::Prefix(_) | Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(n) => {
                out.push(n);
                if let Ok(r) = std::fs::canonicalize(&out) {
                    out = r;
                }
            }
        }
    }
    out
}

fn proc_environ(s: &str) -> bool {
    let parts: Vec<&str> = s.split('/').collect();
    // /proc/<pid>/environ or /proc/<pid>/task/<tid>/environ
    match parts.as_slice() {
        ["", "proc", pid, "environ"] => !pid.is_empty(),
        ["", "proc", pid, "task", tid, "environ"] => !pid.is_empty() && !tid.is_empty(),
        _ => false,
    }
}

pub fn secret_reason(p: &Path) -> &'static str {
    let s = p.to_string_lossy();
    // macOS: /etc is a link to /private/etc, and paths arrive resolved (Python `secret_reason`)
    let s = match s.strip_prefix("/private") { Some(rest) if rest.starts_with("/etc/") => std::borrow::Cow::Owned(rest.to_string()), _ => s };
    if proc_environ(&s) {
        return "a process environment holds secrets";
    }
    if p.components().any(|c| matches!(c, Component::Normal(n) if SECRET_DIRS.iter().any(|d| n == *d))) {
        return "key/credential folder";
    }
    let name = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    if SECRET_ETC.contains(&s.as_ref()) || SECRET_NAMES.contains(&name.as_str()) || s.starts_with("/etc/sudoers.d/") {
        return "credential file";
    }
    let lower = name.to_lowercase();
    if SECRET_SUFFIXES.iter().any(|x| lower.ends_with(x)) {
        return "token/key file";
    }
    ""
}

fn policy_reason(p: &Path, home: &Path) -> &'static str {
    let home = std::fs::canonicalize(home).unwrap_or_else(|_| home.to_path_buf());
    match p.strip_prefix(&home) {
        Ok(rel) if rel.components().next().map(|c| c.as_os_str() == "workspace").unwrap_or(false) => "",
        Ok(_) => "Aurora's own settings, policy and code files are not editable by the agent",
        Err(_) => "",
    }
}

/// "" when the call may run, else the refusal the model gets back.
pub fn check(name: &str, args: &Args, home: &Path) -> String {
    if !web() || !(READ_TOOLS.contains(&name) || WRITE_TOOLS.contains(&name)) {
        return String::new();
    }
    let raw = match args.get("path") {
        None => ".".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(_) => return String::new(),
    };
    let p = real(&raw);
    let mut why = secret_reason(&p);
    if why.is_empty() && WRITE_TOOLS.contains(&name) {
        why = policy_reason(&p, home);
    }
    if why.is_empty() {
        String::new()
    } else {
        format!("[blocked on the web page: {raw} ({why})]")
    }
}

/// Extra `grep -r` excludes in web mode, so a search of `~` or `/` cannot walk into them.
pub fn grep_excludes() -> Vec<String> {
    if !web() {
        return Vec::new();
    }
    let mut dirs: Vec<&str> = SECRET_DIRS.iter().copied().chain(["proc"]).collect();
    dirs.sort();
    let mut names: Vec<&str> = SECRET_NAMES.iter().copied().chain(["environment", "shadow"]).collect();
    names.sort();
    dirs.iter()
        .map(|d| format!("--exclude-dir={d}"))
        .chain(names.iter().map(|n| format!("--exclude={n}")))
        .chain(SECRET_SUFFIXES.iter().map(|s| format!("--exclude=*{s}")))
        .collect()
}

struct Guarded {
    inner: Arc<dyn Tool>,
    home: PathBuf,
}

#[async_trait]
impl Tool for Guarded {
    fn spec(&self) -> ToolSpec {
        self.inner.spec()
    }
    fn needs_approval(&self, args: &Args) -> bool {
        self.inner.needs_approval(args)
    }
    fn parallel_safe(&self) -> bool {
        self.inner.parallel_safe()
    }
    fn preview(&self, args: &Args) -> String {
        self.inner.preview(args)
    }
    async fn run(&self, args: &Args, cancel: &CancellationToken) -> String {
        let blocked = check(&self.spec().name, args, &self.home);
        if !blocked.is_empty() {
            return blocked;
        }
        self.inner.run(args, cancel).await
    }
}

/// Every tool passes `check` first (Python `run_tool` does it for every name).
pub fn guarded(tools: ToolBox, home: PathBuf) -> ToolBox {
    ToolBox::new(tools.into_tools().into_iter().map(|t| Arc::new(Guarded { inner: t, home: home.clone() }) as Arc<dyn Tool>).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proc_environ_shapes() {
        assert!(proc_environ("/proc/self/environ"));
        assert!(proc_environ("/proc/12/task/13/environ"));
        assert!(!proc_environ("/proc/environ"));
    }

    #[test]
    fn macos_etc_paths_are_secret_too() {
        for p in ["/private/etc/shadow", "/private/etc/environment", "/etc/master.passwd", "/private/etc/sudoers.d/x"] {
            assert_eq!(secret_reason(Path::new(p)), "credential file", "{p}");
        }
        assert_eq!(secret_reason(Path::new("/private/etc/hosts")), "");
    }
}
