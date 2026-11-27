//! `run_command` (Python `tools.run_command` + `_run_command_once`): bash
//! (else sh) in its own process group, secrets stripped from the environment
//! (R448), head+tail capture (R310), timeout and cancel kill the whole group.
//!
//! Background jobs (R245) live in `jobs`. With a sandbox policy (R475, `runtime.sandbox: all`) the command
//! runs inside bubblewrap; Python boxes only the web page's commands by default, so the default is unboxed.

use std::collections::VecDeque;
use std::process::Stdio;
use std::time::Duration;

use async_trait::async_trait;
use aurora_core::text::{char_len, head, tail};
use aurora_core::{Args, CancellationToken, Tool, ToolSpec};
use serde_json::json;
use tokio::io::AsyncReadExt;
use tokio::process::Command;

use crate::sandbox::Policy;
use crate::{arg_bool, arg_str};

pub const COMMAND_TIMEOUT_S: u64 = 300;
pub const COMMAND_OUTPUT_CAP: usize = 5 * 1024 * 1024;
pub const COMMAND_RESULT_LIMIT: usize = 50_000;

/// Python `_SECRET_ENV`: variables a command never inherits.
pub fn is_secret_env(name: &str) -> bool {
    let up = name.to_uppercase();
    ["TOKEN", "SECRET", "PASSWORD", "PASSWD", "PRIVATE", "CREDENTIAL", "APIKEY", "API_KEY"].iter().any(|w| up.contains(w))
        || up.ends_with("_KEY")
        || up.ends_with("_PASS")
        || up.ends_with("_DSN")
        || up == "SSH_AUTH_SOCK"
        || up == "DATABASE_URL"
        || up == "DSN"
        || up == "PAT"
        || up.ends_with("_PAT")
}

/// Bounded capture keeping the start and the end of a stream (R310).
struct HeadTail {
    half: usize,
    head: Vec<u8>,
    tail: VecDeque<Vec<u8>>,
    tail_len: usize,
    dropped: usize,
}

impl HeadTail {
    fn new(cap: usize) -> Self {
        Self { half: cap / 2, head: Vec::new(), tail: VecDeque::new(), tail_len: 0, dropped: 0 }
    }

    fn add(&mut self, chunk: &[u8]) {
        if self.head.len() < self.half {
            self.head.extend_from_slice(chunk);
            return;
        }
        self.tail.push_back(chunk.to_vec());
        self.tail_len += chunk.len();
        while self.tail.len() > 1 && self.tail_len - self.tail[0].len() >= self.half {
            let old = self.tail.pop_front().expect("non-empty tail");
            self.tail_len -= old.len();
            self.dropped += old.len();
        }
    }

    fn text(&self) -> String {
        let mut s = String::from_utf8_lossy(&self.head).into_owned();
        if self.dropped > 0 {
            s.push_str(&format!(
                "\n[... {} bytes omitted — the command kept running; redirect to a file if you need all of it ...]\n",
                self.dropped
            ));
        }
        let t: Vec<u8> = self.tail.iter().flatten().copied().collect();
        s + &String::from_utf8_lossy(&t)
    }
}

/// Python `_squeeze`: what the model sees, head and tail, never head alone.
pub fn squeeze(out: &str, limit: usize) -> String {
    let len = char_len(out);
    if len <= limit {
        return out.to_string();
    }
    // R566: the two halves add up to `limit` (an odd limit lost a character), and the count says what was dropped
    let front = limit.div_ceil(2);
    let back = limit - front;
    format!("{}\n[... {} chars omitted from the middle ...]\n{}", head(out, front), len - front - back, tail(out, back))
}

pub enum Ran {
    Exited(String, i32),
    TimedOut(String),
    Cancelled(String),
}

fn shell() -> &'static str {
    if std::path::Path::new("/bin/bash").exists() || std::path::Path::new("/usr/bin/bash").exists() { "bash" } else { "sh" }
}

fn kill_group(pid: Option<u32>) {
    if let Some(pid) = pid {
        // SAFETY: plain syscall; the group may already be gone, which is fine.
        unsafe { libc::killpg(pid as libc::pid_t, libc::SIGKILL) };
    }
}

/// The process for one model command: bash (else sh), secrets stripped from its environment, inside the box when a
/// policy is given (R475). Callers add stdio and process-group setup.
pub fn build(command: &str, workdir: Option<&str>, sandbox: Option<&Policy>) -> std::io::Result<Command> {
    let vars = std::env::vars().filter(|(k, _)| !is_secret_env(k));
    let mut cmd = match sandbox {
        Some(p) => {
            let ws = std::env::current_dir()?;
            let chdir = workdir.map(std::path::absolute).transpose()?;
            let a = crate::sandbox::argv(p, command, &ws, shell(), chdir.as_deref());
            let mut c = Command::new(&a[0]);
            c.args(&a[1..]).env_clear().envs(crate::sandbox::env(vars));
            c
        }
        None => {
            let mut c = Command::new(shell());
            c.arg("-c").arg(command).env_clear().envs(vars);
            c
        }
    };
    if let Some(dir) = workdir {
        cmd.current_dir(dir);
    }
    Ok(cmd)
}

pub async fn run_once(
    command: &str,
    workdir: Option<&str>,
    timeout: Duration,
    cancel: &CancellationToken,
    sandbox: Option<&Policy>,
) -> std::io::Result<Ran> {
    let mut cmd = build(command, workdir, sandbox)?;
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).process_group(0).kill_on_drop(true);
    let mut child = cmd.spawn()?;
    let pid = child.id();
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let (mut out, mut err) = (HeadTail::new(COMMAND_OUTPUT_CAP), HeadTail::new(COMMAND_OUTPUT_CAP));
    let (mut ob, mut eb) = (vec![0u8; 65536], vec![0u8; 65536]);
    let (mut out_open, mut err_open) = (true, true);
    let deadline = tokio::time::Instant::now() + timeout;
    let mut status = None;
    loop {
        if !out_open && !err_open {
            tokio::select! {
                s = child.wait() => { status = Some(s?); break; }
                _ = tokio::time::sleep_until(deadline) => break,
                _ = cancel.cancelled() => {
                    kill_group(pid);
                    return Ok(Ran::Cancelled((out.text() + &err.text()).trim().to_string()));
                }
            }
        }
        tokio::select! {
            n = stdout.read(&mut ob), if out_open => match n {
                Ok(0) | Err(_) => out_open = false,
                Ok(n) => out.add(&ob[..n]),
            },
            n = stderr.read(&mut eb), if err_open => match n {
                Ok(0) | Err(_) => err_open = false,
                Ok(n) => err.add(&eb[..n]),
            },
            _ = tokio::time::sleep_until(deadline) => break,
            _ = cancel.cancelled() => {
                kill_group(pid);
                let _ = tokio::time::timeout(Duration::from_secs(1), child.wait()).await;
                return Ok(Ran::Cancelled((out.text() + &err.text()).trim().to_string()));
            }
        }
    }
    let text = out.text() + &err.text();
    match status {
        Some(s) => {
            use std::os::unix::process::ExitStatusExt;
            Ok(Ran::Exited(text, s.code().unwrap_or_else(|| -s.signal().unwrap_or(0))))
        }
        None => {
            kill_group(pid);
            let mut partial = text.trim().to_string();
            if tokio::time::timeout(Duration::from_secs(1), child.wait()).await.is_err() {
                if !partial.is_empty() {
                    partial.push('\n');
                }
                partial.push_str("[warning: a grandchild process may have escaped cleanup and could still be running]");
            }
            Ok(Ran::TimedOut(partial))
        }
    }
}

pub struct RunCommand {
    pub timeout: Duration,
    pub sandbox: Option<Policy>,
}

impl Default for RunCommand {
    fn default() -> Self {
        Self { timeout: Duration::from_secs(COMMAND_TIMEOUT_S), sandbox: None }
    }
}

#[async_trait]
impl Tool for RunCommand {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "run_command".into(),
            description: "Run a shell command (asks approval).".into(),
            parameters: json!({"type": "object", "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "directory to run it in (optional)"},
                "background": {"type": "boolean", "description": "start it detached and return immediately with a job id instead of blocking until it exits — for anything expected to run longer than this turn (a big download, a long build); poll it with check_command (optional)"}},
                "required": ["command"]}),
        }
    }
    fn needs_approval(&self, _: &Args) -> bool {
        true
    }
    fn preview(&self, args: &Args) -> String {
        let cwd = arg_str(args, "cwd");
        if cwd.is_empty() { format!("$ {}", arg_str(args, "command")) } else { format!("(in {cwd})\n$ {}", arg_str(args, "command")) }
    }
    async fn run(&self, args: &Args, cancel: &CancellationToken) -> String {
        let command = arg_str(args, "command");
        let cwd = arg_str(args, "cwd");
        let workdir = if cwd.is_empty() { None } else { Some(crate::fs::resolve(cwd).to_string_lossy().into_owned()) };
        if let Some(w) = &workdir {
            if !std::path::Path::new(w).is_dir() {
                return format!("[error: no such directory: {cwd}]");
            }
        }
        if arg_bool(args, "background") {
            return match crate::jobs::start(command, workdir.as_deref(), self.sandbox.as_ref()) {
                Ok(id) => format!("[background job {id} started]\ncheck on it with check_command(job_id=\"{id}\")"),
                Err(e) => e,
            };
        }
        match run_once(command, workdir.as_deref(), self.timeout, cancel, self.sandbox.as_ref()).await {
            Err(e) => format!("[error: cannot run the command: {e}]"),
            Ok(Ran::Cancelled(partial)) => {
                let p = squeeze(&partial, COMMAND_RESULT_LIMIT);
                format!("{}[cancelled by user — the command's process group was killed]", if p.is_empty() { String::new() } else { p + "\n" })
            }
            Ok(Ran::TimedOut(out)) => {
                let head_line = format!("[timeout after {}s]", self.timeout.as_secs());
                if out.is_empty() { head_line } else { format!("{}\n{head_line}", squeeze(&out, COMMAND_RESULT_LIMIT)) }
            }
            Ok(Ran::Exited(out, code)) => {
                let shown = squeeze(out.trim(), COMMAND_RESULT_LIMIT);
                let shown = if shown.is_empty() { "[no output]".to_string() } else { shown };
                if code != 0 { format!("{shown}\n[exit {code}]") } else { shown }
            }
        }
    }
}

fn cancelled_text(partial: &str) -> String {
    let p = squeeze(partial, COMMAND_RESULT_LIMIT);
    format!("{}[cancelled by user — the command's process group was killed]", if p.is_empty() { String::new() } else { p + "\n" })
}

/// Python's `float(x or default)` for a numeric argument.
fn arg_float(args: &Args, key: &str, default: f64) -> Result<f64, String> {
    match args.get(key) {
        None | Some(serde_json::Value::Null) | Some(serde_json::Value::Bool(false)) => Ok(default),
        Some(serde_json::Value::Bool(true)) => Ok(1.0),
        Some(serde_json::Value::Number(n)) => Ok(n.as_f64().filter(|f| *f != 0.0).unwrap_or(default)),
        Some(serde_json::Value::String(s)) if s.is_empty() => Ok(default),
        Some(serde_json::Value::String(s)) => match s.trim().parse::<f64>() {
            Ok(0.0) => Ok(default),
            Ok(f) => Ok(f),
            Err(_) => Err(format!("ValueError: could not convert string to float: {}", crate::patch::py_repr(s))),
        },
        Some(_) => Err("TypeError: float() argument must be a string or a real number".into()),
    }
}

/// R100: poll `command` until it exits 0 or `timeout` passes; `then` runs once after success.
pub struct WaitUntil {
    /// runtime.timeout: the bound for `then`.
    pub timeout: Duration,
    pub sandbox: Option<Policy>,
}

const CANCEL_POLL: Duration = Duration::from_millis(150);

#[async_trait]
impl Tool for WaitUntil {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "wait_until".into(),
            description: "Repeatedly run a shell command until it exits 0 or a timeout passes (asks approval once, not per attempt). Use for 'wait until the server is listening', 'wait for the build to finish producing this file', etc. instead of guessing a single sleep duration. Pass `then` to run a second command once, immediately after success, in this same call — e.g. wait for the server to be listening, then curl it — instead of a separate follow-up call that would re-race the same condition.".into(),
            parameters: json!({"type": "object", "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "directory to run it in (optional)"},
                "interval": {"type": "number", "description": "seconds between attempts (default 2)"},
                "timeout": {"type": "number", "description": "give up after this many seconds (default 60, max 300)"},
                "then": {"type": "string", "description": "a second command to run once, immediately after `command` first succeeds (optional)"}},
                "required": ["command"]}),
        }
    }
    fn needs_approval(&self, _: &Args) -> bool {
        true
    }
    fn preview(&self, args: &Args) -> String {
        let then = arg_str(args, "then");
        let mut s = format!("wait until: $ {}", arg_str(args, "command"));
        if !then.is_empty() {
            s.push_str(&format!("\nthen: $ {then}"));
        }
        s
    }
    async fn run(&self, args: &Args, cancel: &CancellationToken) -> String {
        let (command, cwd, then) = (arg_str(args, "command"), arg_str(args, "cwd"), arg_str(args, "then"));
        let workdir = if cwd.is_empty() { None } else { Some(crate::fs::resolve(cwd).to_string_lossy().into_owned()) };
        if let Some(w) = &workdir {
            if !std::path::Path::new(w).is_dir() {
                return format!("[error: no such directory: {cwd}]");
            }
        }
        let (timeout, interval) = match (arg_float(args, "timeout", 60.0), arg_float(args, "interval", 2.0)) {
            (Ok(t), Ok(i)) => (t.clamp(1.0, 300.0), i.max(0.5)),
            (Err(e), _) | (_, Err(e)) => return format!("[tool error: wait_until: {e}]"),
        };
        let start = std::time::Instant::now();
        let mut attempt = 0;
        loop {
            attempt += 1;
            let remaining = (timeout - start.elapsed().as_secs_f64()).max(0.1);
            let ran = run_once(command, workdir.as_deref(), Duration::from_secs_f64(remaining), cancel, self.sandbox.as_ref()).await;
            let (out, code) = match ran {
                Err(e) => return format!("[error: cannot run the command: {e}]"),
                Ok(Ran::Cancelled(p)) => return cancelled_text(&p),
                Ok(Ran::TimedOut(o)) => (o, None),
                Ok(Ran::Exited(o, c)) => (o, Some(c)),
            };
            let shown = squeeze(out.trim(), COMMAND_RESULT_LIMIT);
            let shown = if shown.is_empty() { "[no output]".to_string() } else { shown };
            if code == Some(0) {
                let head = format!("[wait_until: succeeded after {attempt} attempt(s), {:.1}s]\n{shown}", start.elapsed().as_secs_f64());
                if then.is_empty() {
                    return head;
                }
                let (tout, tcode) = match run_once(then, workdir.as_deref(), self.timeout, cancel, self.sandbox.as_ref()).await {
                    Err(e) => return format!("{head}\n[error: cannot run the command: {e}]"),
                    Ok(Ran::Cancelled(p)) => return format!("{head}\n{}", cancelled_text(&p)),
                    Ok(Ran::TimedOut(o)) => (o, None),
                    Ok(Ran::Exited(o, c)) => (o, Some(c)),
                };
                let tshown = squeeze(tout.trim(), COMMAND_RESULT_LIMIT);
                let tshown = if tshown.is_empty() { "[no output]".to_string() } else { tshown };
                let thead = tcode.map_or("[then: timed out]".to_string(), |c| format!("[then: exit {c}]"));
                return format!("{head}\n{thead}\n{tshown}");
            }
            if start.elapsed().as_secs_f64() + interval > timeout {
                let status = code.map_or("timed out mid-command".to_string(), |c| format!("exit {c}"));
                return format!("[wait_until: gave up after {attempt} attempt(s), ~{timeout:.0}s ({status}) — last output:]\n{shown}");
            }
            let end = tokio::time::Instant::now() + Duration::from_secs_f64(interval);
            while tokio::time::Instant::now() < end {
                if cancel.is_cancelled() {
                    return "[cancelled by user between wait_until attempts]".into();
                }
                tokio::time::sleep(CANCEL_POLL.min(end - tokio::time::Instant::now())).await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn squeeze_keeps_exactly_limit_chars() {
        let s = "x".repeat(100);
        let out = squeeze(&s, 21);
        assert!(out.starts_with(&"x".repeat(11)));
        assert!(out.contains("[... 79 chars omitted"));
        assert!(out.ends_with(&format!("\n{}", "x".repeat(10))));
        assert_eq!(squeeze("abcdef", 1), "a\n[... 5 chars omitted from the middle ...]\n");
        assert_eq!(squeeze("abc", 3), "abc");
    }

    fn args(v: serde_json::Value) -> Args {
        v.as_object().unwrap().clone()
    }

    async fn run(v: serde_json::Value) -> String {
        RunCommand::default().run(&args(v), &CancellationToken::new()).await
    }

    #[tokio::test]
    async fn output_exit_code_and_cwd() {
        assert_eq!(run(json!({"command": "echo hi; echo err >&2"})).await, "hi\nerr");
        assert_eq!(run(json!({"command": "exit 3"})).await, "[no output]\n[exit 3]");
        assert_eq!(run(json!({"command": "pwd", "cwd": "/"})).await, "/");
        assert_eq!(run(json!({"command": "true", "cwd": "/no/such"})).await, "[error: no such directory: /no/such]");
    }

    #[tokio::test]
    async fn secrets_never_reach_the_command() {
        unsafe { std::env::set_var("AURORA_T_API_KEY", "s3cret") };
        unsafe { std::env::set_var("AURORA_T_PLAIN", "visible") };
        let out = run(json!({"command": "echo \"[$AURORA_T_API_KEY][$AURORA_T_PLAIN]\""})).await;
        assert_eq!(out, "[][visible]");
    }

    #[tokio::test]
    async fn timeout_kills_the_whole_group() {
        let tool = RunCommand { timeout: Duration::from_millis(300), ..Default::default() };
        let started = std::time::Instant::now();
        let out = tool.run(&args(json!({"command": "sleep 30 & sleep 30; echo never"})), &CancellationToken::new()).await;
        assert!(started.elapsed() < Duration::from_secs(5));
        assert_eq!(out, "[timeout after 0s]");
    }

    #[tokio::test]
    async fn cancel_stops_a_running_command() {
        let cancel = CancellationToken::new();
        let c2 = cancel.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(200)).await;
            c2.cancel();
        });
        let out = RunCommand::default().run(&args(json!({"command": "echo started; sleep 30"})), &cancel).await;
        assert_eq!(out, "started\n[cancelled by user — the command's process group was killed]");
    }

    #[test]
    fn squeeze_keeps_head_and_tail() {
        let s = format!("{}{}", "a".repeat(30), "b".repeat(30));
        assert_eq!(squeeze(&s, 20), format!("{}\n[... 40 chars omitted from the middle ...]\n{}", "a".repeat(10), "b".repeat(10)));
    }

    #[test]
    fn secret_names_match_the_python_pattern() {
        for s in ["OPENROUTER_API_KEY", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "DATABASE_URL", "MY_PAT", "DB_PASS", "APIKEY"] {
            assert!(is_secret_env(s), "{s}");
        }
        for s in ["PATH", "HOME", "LANG", "KEYBOARD", "PATTERN"] {
            assert!(!is_secret_env(s), "{s}");
        }
    }
}
