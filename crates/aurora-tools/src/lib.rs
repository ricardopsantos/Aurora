//! Built-in tools, same names, arguments and result strings as Python's
//! `aurora/tools.py`, so a model (and a session log reader) sees no change.
//! Ported so far: read_file, list_dir, grep, find_files, write_file, edit_file,
//! run_command, apply_patch, wait_until, check_command, cancel_command.

pub mod command;
pub mod ext;
pub mod fs;
pub mod guard;
pub mod jobs;
pub mod patch;
pub mod sandbox;
pub mod search;
pub mod skills;
pub mod url;

use std::sync::Arc;
use std::time::Duration;

use aurora_core::{Args, Tool, ToolBox};
use serde_json::Value;

/// `command_timeout` is runtime.timeout (Python `set_command_timeout`).
pub fn builtin(command_timeout: Duration) -> ToolBox {
    builtin_boxed(command_timeout, None)
}

/// Like `builtin`, with the model's shell commands run inside the R475 box when a policy is given.
pub fn builtin_boxed(command_timeout: Duration, sandbox: Option<sandbox::Policy>) -> ToolBox {
    let tools: Vec<Arc<dyn Tool>> = vec![
        Arc::new(fs::ReadFile),
        Arc::new(fs::ListDir),
        Arc::new(search::Grep),
        Arc::new(search::FindFiles),
        Arc::new(fs::WriteFile),
        Arc::new(fs::EditFile),
        Arc::new(command::RunCommand { timeout: command_timeout, sandbox: sandbox.clone() }),
        Arc::new(fs::ApplyPatch),
        Arc::new(command::WaitUntil { timeout: command_timeout, sandbox: sandbox.clone() }),
        Arc::new(jobs::CheckCommand),
        Arc::new(jobs::CancelCommand),
    ];
    ToolBox::new(tools)
}

pub(crate) fn arg_str<'a>(args: &'a Args, key: &str) -> &'a str {
    args.get(key).and_then(Value::as_str).unwrap_or_default()
}

/// Python's `int(x or 0)`: integers, integral floats and numeric strings.
pub(crate) fn arg_int(args: &Args, key: &str) -> Result<i64, ()> {
    match args.get(key) {
        None | Some(Value::Null) => Ok(0),
        Some(Value::Bool(b)) => Ok(i64::from(*b)),
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)).ok_or(()),
        Some(Value::String(s)) if s.is_empty() => Ok(0),
        Some(Value::String(s)) => s.trim().parse().map_err(|_| ()),
        Some(_) => Err(()),
    }
}

pub(crate) fn arg_bool(args: &Args, key: &str) -> bool {
    match args.get(key) {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty() && s != "false" && s != "0",
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        _ => false,
    }
}
