//! The allow/deny rule matcher: a port of Python's `aurora/approve.py`
//! (R7, R100, R120, R141, R149, R190, R195, R209, R242, R277, R283, R298,
//! R299, R303, R319, R334, R335, R450). Where this file and `approve.py`
//! differ, this file is wrong. The security rules there are the reason for
//! most of the odd-looking branches.

use std::collections::HashSet;
use std::path::Path;

use aurora_core::Args;
use serde_json::{Map, Value};

use crate::pathlike::{expanduser, fnmatch, normpath, resolve, shlex_join, shlex_split};

pub type Rules = Map<String, Value>;

pub const SAFE_COMMANDS: &[&str] =
    &["find", "ls", "tree", "grep", "cat", "pwd", "whoami", "which", "wc", "head", "tail", "file"];

const DANGEROUS: &[&str] = &[
    "dd", "mkfs", "fdisk", "parted", "shred", "hdparm", "diskutil", "newfs", "rm", "rmdir", "unlink", "srm", "sh", "bash",
    "zsh", "dash", "ksh", "fish", "eval", "source", "exec", "python", "python2", "python3", "perl", "ruby", "node",
    "osascript", "sudo", "su", "doas", "chown", "chmod", "chgrp", "launchctl", "systemctl", "curl", "wget", "mv", "cp",
    "ln", "install", "truncate", "tee", "docker", "podman", "pip", "pip3", "npm", "npx", "yarn", "pnpm", "gem", "cargo",
    "make", "apt", "apt-get", "brew", "sed", "awk", "gawk", "mawk", "ex", "vim", "vi", "ed", "tar", "unzip", "rsync", "scp",
    "sftp", "ssh", "uv", "uvx", "pipx", "poetry", "pdm", "hatch", "conda", "go", "deno", "bun", "php", "lua", "java",
    "Rscript", "xargs", "env", "nohup", "timeout", "nice", "watch", "crontab", "at", "kill", "pkill", "killall", "sqlite3",
    "psql", "mysql", "mariadb", "mongosh", "redis-cli", "csh", "tcsh", "pwsh", "tclsh", "wish", "expect", "julia", "swift",
    "irb", "luajit", "groovy", "scala", "kotlin", "dotnet", "sort", "nc", "ncat", "socat", "telnet", "ftp", "flock",
    "setsid", "stdbuf", "ionice", "chrt", "taskset", "unshare", "script", "gh",
];
const DANGEROUS_PREFIXES: &[&str] = &["mkfs.", "newfs_"];
const GIT_EXEC_OPTIONS: &[&str] = &["-c", "--config-env", "--exec-path"];
const GIT_RETARGET_OPTIONS: &[&str] = &["-C", "--git-dir", "--work-tree", "--namespace"];
const GIT_CONFIG_SUBCOMMANDS: &[&str] = &["config", "alias"];
const GIT_UNSAFE_FLAGS: &[&str] =
    &["--output", "--upload-pack", "--receive-pack", "--exec", "--extcmd", "--ext-diff", "--textconv", "--open-files-in-pager"];
const GIT_UNSAFE_WORDS: &[&str] = &["foreach", "-x"];
const SHELL_OPS: &[&str] = &["&", "|", ";", "<", ">", "`", "$(", "\n", "\r"];

pub const COMMAND_TOOLS: &[&str] = &["run_command", "wait_until"];
const URL_TOOLS: &[&str] = &["web_fetch"];

fn unsafe_flags(cmd: &str) -> &'static [&'static str] {
    match cmd {
        "find" => &["-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"],
        "tree" => &["-o", "--output", "--output="],
        "file" => &["-C", "--compile"],
        _ => &[],
    }
}

fn basename(t: &str) -> &str {
    t.rsplit('/').next().unwrap_or(t)
}

/// R242: `python3.11` is `python` (Python's `_VERSION_SUFFIX`).
fn unversioned(base: &str) -> &str {
    // [-_]?[0-9][0-9._-]*$ : find the earliest start of a trailing run
    let b = base.as_bytes();
    let mut start = b.len();
    while start > 0 && (b[start - 1].is_ascii_digit() || matches!(b[start - 1], b'.' | b'_' | b'-')) {
        start -= 1;
    }
    // the run must begin with a digit, optionally preceded by one - or _
    while start < b.len() && !b[start].is_ascii_digit() {
        start += 1;
    }
    if start >= b.len() {
        return base;
    }
    let cut = if start > 0 && matches!(b[start - 1], b'-' | b'_') { start - 1 } else { start };
    &base[..cut]
}

pub fn norm_command(cmd: &str) -> Vec<String> {
    let toks = shlex_split(cmd).unwrap_or_else(|()| cmd.split_whitespace().map(String::from).collect());
    toks.iter().map(|t| expanduser(t)).collect()
}

pub fn is_dangerous(toks: &[String]) -> bool {
    let mut names: HashSet<&str> = HashSet::new();
    for t in toks {
        let base = basename(t);
        if DANGEROUS.contains(&base) || DANGEROUS.contains(&unversioned(base)) || DANGEROUS_PREFIXES.iter().any(|p| base.starts_with(p)) {
            return true;
        }
        names.insert(base);
    }
    for cmd in ["find", "tree", "file"] {
        if names.contains(cmd)
            && toks.iter().any(|t| unsafe_flags(cmd).iter().any(|f| t == f || (f.ends_with('=') && t.starts_with(f))))
        {
            return true;
        }
    }
    if names.contains("git") {
        let with_eq = |t: &String, o: &str| t == o || t.starts_with(&format!("{o}="));
        if toks.iter().any(|t| GIT_EXEC_OPTIONS.iter().any(|o| with_eq(t, o))) {
            return true;
        }
        let i = toks.iter().position(|t| basename(t) == "git").unwrap_or(0);
        let rest = &toks[i + 1..];
        if let Some(first) = rest.first() {
            if GIT_RETARGET_OPTIONS.contains(&first.as_str()) || rest.iter().take(2).any(|t| GIT_CONFIG_SUBCOMMANDS.contains(&t.as_str())) {
                return true;
            }
        }
        if rest.iter().any(|t| GIT_UNSAFE_WORDS.contains(&t.as_str()) || t.starts_with("-O") || GIT_UNSAFE_FLAGS.iter().any(|f| with_eq(t, f))) {
            return true;
        }
        if rest.first().map(String::as_str) == Some("bisect") && rest.iter().any(|t| t == "run") {
            return true;
        }
    }
    false
}

pub fn is_compound(cmd: &str) -> bool {
    SHELL_OPS.iter().any(|op| cmd.contains(op))
}

pub fn norm_path(path: &str) -> String {
    if path.is_empty() { String::new() } else { normpath(&expanduser(path)) }
}

fn resolved_sig(path: &str) -> String {
    resolve(path)
}

/// R209: the literal prefix of a rule resolved, its glob tail kept.
fn resolved_rule(rule: &str) -> String {
    let r = norm_path(rule);
    let first_glob = ['*', '?', '['].iter().filter_map(|c| r.find(*c)).min();
    let (head, tail) = match first_glob {
        None => (r.clone(), String::new()),
        Some(g) => match r[..g].rfind('/') {
            None => return r,
            Some(cut) => (r[..cut].to_string(), r[cut..].to_string()),
        },
    };
    format!("{}{tail}", resolve(&head))
}

fn url_origin_rule(url: &str) -> String {
    let u = url.trim();
    let Some(colon) = u.find(':') else { return String::new() };
    let scheme = &u[..colon];
    let ok = scheme.chars().next().is_some_and(|c| c.is_ascii_alphabetic())
        && scheme.chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c));
    let rest = &u[colon + 1..];
    let Some(after) = rest.strip_prefix("//") else { return String::new() };
    let netloc = &after[..after.find(['/', '?', '#']).unwrap_or(after.len())];
    if ok && !netloc.is_empty() { format!("{}://{netloc}/*", scheme.to_lowercase()) } else { String::new() }
}

fn arg<'a>(args: &'a Args, key: &str) -> &'a str {
    args.get(key).and_then(Value::as_str).unwrap_or("")
}

fn signature(tool: &str, args: &Args) -> String {
    if COMMAND_TOOLS.contains(&tool) {
        arg(args, "command").to_string()
    } else if URL_TOOLS.contains(&tool) {
        arg(args, "url").trim().to_string()
    } else {
        norm_path(arg(args, "path"))
    }
}

fn rules_of<'a>(data: &'a Rules, tool: &str) -> Vec<&'a str> {
    data.get(tool).and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).filter(|s| !s.is_empty()).collect()).unwrap_or_default()
}

/// Python `_matches`. `strict` is the allowlist direction (R141, R149).
pub fn matches(tool: &str, args: &Args, data: &Rules, strict: bool) -> bool {
    let sig = signature(tool, args);
    if COMMAND_TOOLS.contains(&tool) {
        let sig_toks = norm_command(&sig);
        let exact_only = strict && (is_compound(&sig) || is_dangerous(&sig_toks));
        for p in rules_of(data, tool) {
            let rule = norm_command(p);
            if rule.is_empty() {
                continue;
            }
            if sig_toks == rule {
                return true;
            }
            if exact_only {
                continue;
            }
            if rule.len() == 2 && sig_toks.len() >= 2 && sig_toks[..2] == rule[..] {
                return true;
            }
            if rule.len() == 1 && sig_toks.first() == rule.first() && (!strict || SAFE_COMMANDS.contains(&rule[0].as_str())) {
                return true;
            }
        }
        return false;
    }
    if URL_TOOLS.contains(&tool) {
        let origin = url_origin_rule(&sig);
        return !origin.is_empty() && rules_of(data, tool).iter().any(|g| *g == origin);
    }
    let rules = rules_of(data, tool);
    if !strict {
        return rules.iter().any(|g| fnmatch(&sig, &norm_path(g)) || fnmatch(&resolved_sig(&sig), &resolved_rule(g)));
    }
    let res = resolved_sig(&sig);
    rules.iter().any(|g| fnmatch(&sig, &norm_path(g)) && fnmatch(&res, &resolved_rule(g)))
}

/// R303: a command aimed outside the working directory always prompts.
fn foreign_cwd(cwd: &str) -> bool {
    if cwd.trim().is_empty() {
        return false;
    }
    let here = resolve(".");
    let there = resolve(cwd);
    there != here && !Path::new(&there).starts_with(&here)
}

pub fn is_allowed(tool: &str, args: &Args, allow: &Rules, session: &Rules) -> bool {
    if tool == "wait_until" && !arg(args, "then").trim().is_empty() {
        return false;
    }
    if COMMAND_TOOLS.contains(&tool) && foreign_cwd(arg(args, "cwd")) {
        return false;
    }
    matches(tool, args, allow, true) || (!session.is_empty() && matches(tool, args, session, true))
}

pub fn is_denied(tool: &str, args: &Args, deny: &Rules) -> bool {
    if deny.is_empty() {
        return false;
    }
    if tool == "wait_until" {
        let then = arg(args, "then").trim();
        if !then.is_empty() {
            let c: Args = [("command".to_string(), Value::String(then.to_string()))].into_iter().collect();
            if matches("run_command", &c, deny, false) || matches("wait_until", &c, deny, false) {
                return true;
            }
        }
    }
    matches(tool, args, deny, false)
}

/// The rule "always allow" / "always deny" stores (Python `_rule_for`).
pub fn rule_for(tool: &str, args: &Args) -> String {
    if COMMAND_TOOLS.contains(&tool) {
        let cmd = arg(args, "command");
        let toks = norm_command(cmd);
        if is_compound(cmd) || is_dangerous(&toks) {
            return shlex_join(&toks);
        }
        if toks.first().is_some_and(|t| SAFE_COMMANDS.contains(&t.as_str())) {
            return toks[0].clone();
        }
        if toks.len() >= 2 && toks[1].starts_with('-') {
            return shlex_join(&toks);
        }
        return shlex_join(&toks[..toks.len().min(2)]);
    }
    if URL_TOOLS.contains(&tool) {
        return url_origin_rule(arg(args, "url"));
    }
    let p = norm_path(arg(args, "path"));
    if p.is_empty() { "*".into() } else { p }
}

/// R335: what "always allow" would store, and how far it reaches in words.
pub fn rule_scope(tool: &str, args: &Args) -> (String, String) {
    let rule = rule_for(tool, args);
    if rule.is_empty() {
        return (String::new(), String::new());
    }
    let words = if COMMAND_TOOLS.contains(&tool) {
        if rule == shlex_join(&norm_command(arg(args, "command"))) {
            "exactly this command".to_string()
        } else if norm_command(&rule).len() == 1 {
            format!("`{rule}` with any arguments")
        } else {
            format!("any command starting `{rule}`")
        }
    } else if URL_TOOLS.contains(&tool) {
        "any page on that site".into()
    } else if rule == "*" {
        "every call of this tool".into()
    } else {
        "this path".into()
    };
    (rule, words)
}

pub fn add_rule_to(data: &mut Rules, tool: &str, rule: &str) -> bool {
    if rule.is_empty() {
        return false;
    }
    let list = data.entry(tool.to_string()).or_insert_with(|| Value::Array(vec![]));
    if !list.is_array() {
        *list = Value::Array(vec![]);
    }
    let arr = list.as_array_mut().expect("array");
    if arr.iter().any(|v| v.as_str() == Some(rule)) {
        return false;
    }
    arr.push(Value::String(rule.to_string()));
    true
}

#[cfg(test)]
mod tests;
