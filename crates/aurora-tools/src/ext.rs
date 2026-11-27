//! The `aurora-ext/1` client (R477): runs `python -m aurora.exthost` as a
//! child process and offers its tools (Python's bundled web, MCP and lint
//! extensions, and the user's own) as ordinary tools. A host that cannot start
//! means no extension tools and a warning, never a failed run.

use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use aurora_core::ext::{self, CallParams, CallResult, InitializeParams, InitializeResult, Request, Response};
use aurora_core::{Args, CancellationToken, Tool, ToolSpec};
use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::arg_str;

struct Conn {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
}

impl Conn {
    fn call<P: serde::Serialize, R: DeserializeOwned>(&mut self, method: &str, params: P) -> Result<R, String> {
        self.next_id += 1;
        let id = self.next_id;
        let mut line = serde_json::to_string(&Request::new(id, method, params)).map_err(|e| e.to_string())?;
        line.push('\n');
        self.stdin.write_all(line.as_bytes()).and_then(|_| self.stdin.flush()).map_err(|e| format!("extension host: {e}"))?;
        let mut buf = String::new();
        loop {
            buf.clear();
            let n = self.stdout.read_line(&mut buf).map_err(|e| format!("extension host: {e}"))?;
            if n == 0 {
                return Err("extension host exited".into());
            }
            if buf.trim().is_empty() {
                continue;
            }
            let resp: Response<R> = serde_json::from_str(&buf).map_err(|e| format!("extension host sent bad JSON: {e}"))?;
            if resp.id != Some(id) {
                continue;
            }
            if let Some(err) = resp.error {
                return Err(format!("extension host error {}: {}", err.code, err.message));
            }
            return resp.result.ok_or_else(|| "extension host sent no result".into());
        }
    }
}

/// A running host, shared by its tools. Calls are answered one at a time.
pub struct Host {
    conn: Mutex<Option<Conn>>,
}

impl Drop for Host {
    fn drop(&mut self) {
        if let Ok(mut g) = self.conn.lock() {
            if let Some(mut c) = g.take() {
                let _ = c.call::<_, Value>("shutdown", serde_json::json!({}));
                let _ = c.child.kill();
                let _ = c.child.wait();
            }
        }
    }
}

impl Host {
    fn kill(&self) {
        if let Ok(mut g) = self.conn.lock() {
            if let Some(mut c) = g.take() {
                let _ = c.child.kill();
                let _ = c.child.wait();
            }
        }
    }

    fn call_tool(&self, name: &str, args: &Args) -> String {
        let Ok(mut g) = self.conn.lock() else { return "[tool error: extension host lock poisoned]".into() };
        let Some(conn) = g.as_mut() else { return "[tool error: the extension host is not running]".into() };
        match conn.call::<_, CallResult>("tools/call", CallParams { name, arguments: args }) {
            // the in-process engine names the tool in a raised error: `[tool error: <name>: Type: msg]`
            Ok(r) if r.is_error => match r.output.strip_prefix("[tool error: ") {
                Some(rest) => format!("[tool error: {name}: {rest}"),
                None => r.output,
            },
            Ok(r) => r.output,
            Err(e) => {
                *g = None;
                format!("[tool error: {name}: {e}]")
            }
        }
    }
}

/// Start the host and load `paths`. Returns the tools and the warnings to show.
pub fn start(python: &str, paths: &[PathBuf], config: &Value) -> (Vec<Arc<dyn Tool>>, Vec<String>) {
    let child = Command::new(python)
        .args(["-m", "aurora.exthost"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => return (Vec::new(), vec![format!("extensions unavailable: cannot start {python} -m aurora.exthost ({e})")]),
    };
    let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
        return (Vec::new(), vec!["extensions unavailable: no pipes to the extension host".into()]);
    };
    let mut conn = Conn { child, stdin, stdout: BufReader::new(stdout), next_id: 0 };
    let params = InitializeParams {
        protocol: ext::PROTOCOL,
        paths: paths.iter().map(|p| p.display().to_string()).collect(),
        config,
    };
    let init: InitializeResult = match conn.call("initialize", params) {
        Ok(r) => r,
        Err(e) => {
            let _ = conn.child.kill();
            let _ = conn.child.wait();
            return (Vec::new(), vec![format!("extensions unavailable: {e}")]);
        }
    };
    let host = Arc::new(Host { conn: Mutex::new(Some(conn)) });
    let tools = init
        .tools
        .into_iter()
        .map(|spec| Arc::new(ExtTool { spec, host: host.clone() }) as Arc<dyn Tool>)
        .collect();
    (tools, init.warnings)
}

/// Where Python keeps its bundled extensions, asked of that same interpreter.
pub fn bundled_dir(python: &str) -> Option<PathBuf> {
    let out = Command::new(python)
        .args(["-c", "import aurora.extensions as e; print(e._BUNDLED_DIR)"])
        .stderr(Stdio::null())
        .output()
        .ok()?;
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (out.status.success() && !s.is_empty()).then(|| PathBuf::from(s))
}

pub struct ExtTool {
    spec: ToolSpec,
    host: Arc<Host>,
}

#[async_trait]
impl Tool for ExtTool {
    fn spec(&self) -> ToolSpec {
        self.spec.clone()
    }
    /// R119: every `mcp_*` call asks; R283: web_fetch asks for a non-public or data-carrying URL.
    fn needs_approval(&self, args: &Args) -> bool {
        let name = self.spec.name.as_str();
        name.starts_with("mcp_") || (name == "web_fetch" && crate::url::url_needs_approval(arg_str(args, "url")))
    }
    fn parallel_safe(&self) -> bool {
        matches!(self.spec.name.as_str(), "web_search" | "web_fetch")
    }
    fn preview(&self, args: &Args) -> String {
        format!("{}({})", self.spec.name, aurora_core::pyjson::dumps(&Value::Object(args.clone())))
    }
    async fn run(&self, args: &Args, cancel: &CancellationToken) -> String {
        let (host, name, a) = (self.host.clone(), self.spec.name.clone(), args.clone());
        let job = tokio::task::spawn_blocking(move || host.call_tool(&name, &a));
        tokio::select! {
            r = job => r.unwrap_or_else(|e| format!("[tool error: {}: {e}]", self.spec.name)),
            _ = cancel.cancelled() => {
                // aurora-ext/1 cannot cancel a call: stop the host so the turn is not held hostage
                self.host.kill();
                "[cancelled by user — the extension host was stopped]".into()
            }
        }
    }
}
