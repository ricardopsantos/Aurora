//! The standing approval rules in AURORA_HOME (Python `aurora/approve.py`):
//! `allowlist.yaml` and `denylist.yaml`, plus the "allow for this session"
//! rules kept in memory (R334). The matching lives in `rules`.
//!
//! An unreadable denylist refuses every gated call, as in Python
//! (`deny_broken`, R170a). An unreadable allowlist is treated as empty, so
//! everything asks (R196).

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use aurora_core::approval::ApprovalPolicy;
use aurora_core::Args;
use serde_json::Value;

use crate::rules::{self, Rules};

#[derive(Clone, Debug)]
pub enum DenyState {
    Empty,
    HasRules(usize),
    Broken(String),
}

fn load_rules(path: &Path, prepopulate: bool) -> Result<Rules, String> {
    let mut data = if path.exists() {
        match crate::config::read_yaml(path) {
            Ok(Value::Object(m)) => m,
            Ok(Value::Null) => Rules::new(),
            Ok(_) => return Err(format!("{}: expected a mapping", path.display())),
            Err(e) => return Err(format!("{}: {e}", path.display())),
        }
    } else {
        Rules::new()
    };
    if prepopulate {
        for k in ["run_command", "write_file", "edit_file", "apply_patch", "wait_until"] {
            data.entry(k.to_string()).or_insert_with(|| Value::Array(vec![]));
        }
    }
    Ok(data)
}

fn save_rules(path: &Path, data: &Rules) -> std::io::Result<()> {
    let text = serde_yaml_ng::to_string(&Value::Object(data.clone())).map_err(std::io::Error::other)?;
    crate::paths::write_atomic(path, text.as_bytes(), Some(0o600)).map_err(std::io::Error::other)
}

pub fn deny_state(home: &Path) -> DenyState {
    match load_rules(&home.join("denylist.yaml"), false) {
        Err(e) => DenyState::Broken(e),
        Ok(m) => {
            let n: usize = m.values().map(|v| v.as_array().map_or(0, Vec::len)).sum();
            if n == 0 { DenyState::Empty } else { DenyState::HasRules(n) }
        }
    }
}

pub struct HomePolicy {
    home: PathBuf,
    allow: Mutex<Rules>,
    deny: Mutex<Rules>,
    session: Mutex<Rules>,
    state: Mutex<DenyState>,
}

impl HomePolicy {
    pub fn load(home: &Path) -> Self {
        let state = deny_state(home);
        let deny = load_rules(&home.join("denylist.yaml"), false).unwrap_or_default();
        let allow = load_rules(&home.join("allowlist.yaml"), true).unwrap_or_default();
        Self { home: home.to_path_buf(), allow: Mutex::new(allow), deny: Mutex::new(deny), session: Mutex::new(Rules::new()),
               state: Mutex::new(state) }
    }

    pub fn state(&self) -> DenyState {
        self.state.lock().expect("state lock").clone()
    }

    fn persist(&self, file: &str, bucket: &Mutex<Rules>, tool: &str, args: &Args) -> Option<String> {
        let rule = rules::rule_for(tool, args);
        let path = self.home.join(file);
        // R531: the same `<file>.lock` flock Python's approve.add_rule takes, so two Auroras (either language)
        // answering "always" at once both keep their rule; re-read under it
        let _lock = FileLock::take(&self.home.join(format!("{file}.lock")));
        let mut data = load_rules(&path, file == "allowlist.yaml").ok()?;
        if rules::add_rule_to(&mut data, tool, &rule) {
            save_rules(&path, &data).ok()?;
        }
        *bucket.lock().expect("rules lock") = data;
        (!rule.is_empty()).then_some(rule)
    }
}

/// R531: an exclusive `flock` held until dropped; without the lock file the write goes ahead unlocked.
struct FileLock {
    _file: Option<std::fs::File>,
}

impl FileLock {
    fn take(path: &std::path::Path) -> Self {
        use std::os::unix::io::AsRawFd;
        let f = std::fs::OpenOptions::new().create(true).append(true).open(path).ok();
        if let Some(f) = &f {
            // SAFETY: flock on a descriptor this struct owns; released when the file is closed
            unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX) };
        }
        FileLock { _file: f }
    }
}

impl ApprovalPolicy for HomePolicy {
    fn is_denied(&self, tool: &str, args: &Args) -> bool {
        matches!(*self.state.lock().expect("state lock"), DenyState::Broken(_)) || rules::is_denied(tool, args, &self.deny.lock().expect("rules lock"))
    }
    fn is_allowed(&self, tool: &str, args: &Args) -> bool {
        rules::is_allowed(tool, args, &self.allow.lock().expect("rules lock"), &self.session.lock().expect("rules lock"))
    }
    fn add_allow(&self, tool: &str, args: &Args) -> Option<String> {
        self.persist("allowlist.yaml", &self.allow, tool, args)
    }
    fn add_deny(&self, tool: &str, args: &Args) -> Option<String> {
        self.persist("denylist.yaml", &self.deny, tool, args)
    }
    fn add_session(&self, tool: &str, args: &Args) -> Option<String> {
        let rule = rules::rule_for(tool, args);
        rules::add_rule_to(&mut self.session.lock().expect("rules lock"), tool, &rule);
        (!rule.is_empty()).then_some(rule)
    }
    /// Python's `run_turn` reads both lists at the start of every turn (R170a, R196): so does this, with its notices.
    fn reload(&self) -> Vec<String> {
        let mut notes = Vec::new();
        match load_rules(&self.home.join("allowlist.yaml"), true) {
            Ok(a) => *self.allow.lock().expect("rules lock") = a,
            Err(e) => {
                notes.push(format!("allowlist.yaml is unreadable ({e}) — asking for approval on every gated tool call until it's fixed"));
                *self.allow.lock().expect("rules lock") = Rules::new();
            }
        }
        let state = deny_state(&self.home);
        if let DenyState::Broken(e) = &state {
            notes.push(format!("denylist.yaml is unreadable ({e}) — blocking all gated tool calls until it's fixed"));
        } else {
            *self.deny.lock().expect("rules lock") = load_rules(&self.home.join("denylist.yaml"), false).unwrap_or_default();
        }
        *self.state.lock().expect("state lock") = state;
        notes
    }
    fn session_rules(&self) -> serde_json::Value {
        serde_json::to_value(&*self.session.lock().expect("rules lock")).unwrap_or_default()
    }
    fn set_session_rules(&self, rules: &serde_json::Value) {
        let mut s = self.session.lock().expect("rules lock");
        s.clear();
        for (tool, list) in rules.as_object().into_iter().flatten() {
            for r in list.as_array().into_iter().flatten().filter_map(|r| r.as_str()) {
                rules::add_rule_to(&mut s, tool, r);
            }
        }
    }
    fn rule_scope(&self, tool: &str, args: &Args) -> String {
        let (rule, words) = rules::rule_scope(tool, args);
        if rule.is_empty() { String::new() } else { format!("always allow would store `{rule}`: {words}") }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn cmd(c: &str) -> Args {
        json!({"command": c}).as_object().unwrap().clone()
    }

    #[test]
    fn deny_rules_apply_and_a_broken_denylist_refuses_everything() {
        let d = tempfile::tempdir().unwrap();
        let none = HomePolicy::load(d.path());
        assert!(!none.is_denied("run_command", &cmd("rm x")));
        std::fs::write(d.path().join("denylist.yaml"), "run_command:\n  - rm\n").unwrap();
        let p = HomePolicy::load(d.path());
        assert!(matches!(p.state(), DenyState::HasRules(1)));
        assert!(p.is_denied("run_command", &cmd("rm -rf x")) && !p.is_denied("run_command", &cmd("ls")));
        std::fs::write(d.path().join("denylist.yaml"), "- not a mapping\n").unwrap();
        assert!(HomePolicy::load(d.path()).is_denied("run_command", &cmd("ls")));
    }

    #[test]
    fn always_allow_persists_and_the_next_call_matches() {
        let d = tempfile::tempdir().unwrap();
        let p = HomePolicy::load(d.path());
        assert!(!p.is_allowed("run_command", &cmd("git push origin main")));
        assert_eq!(p.add_allow("run_command", &cmd("git push origin main")).as_deref(), Some("git push"));
        assert!(p.is_allowed("run_command", &cmd("git push origin dev")));
        let again = HomePolicy::load(d.path());
        assert!(again.is_allowed("run_command", &cmd("git push -f")));
        assert_eq!(p.add_session("run_command", &cmd("cargo build")).as_deref(), Some("cargo build"));
    }
}
