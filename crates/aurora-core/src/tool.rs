use std::sync::Arc;

use async_trait::async_trait;
use tokio_util::sync::CancellationToken;

use crate::text::{char_len, head};
use crate::types::{Args, ToolSpec};

/// Python `tools.TOOL_OUTPUT_LIMIT`: what one result may hand the model.
pub const TOOL_OUTPUT_LIMIT: usize = 60_000;

const ERROR_MARKERS: [&str; 3] = ["[error:", "[tool error:", "[grep error:"];
const SKIPPED_MARKERS: [&str; 3] = ["[skipped:", "[denied", "[not run"];

#[async_trait]
pub trait Tool: Send + Sync {
    fn spec(&self) -> ToolSpec;
    /// Writes, edits and commands ask first; reads never do.
    fn needs_approval(&self, args: &Args) -> bool;
    /// Read-only calls a round may run at once (R94).
    fn parallel_safe(&self) -> bool {
        false
    }
    /// A preview shown at the approval gate (a diff for writes).
    fn preview(&self, _args: &Args) -> String {
        String::new()
    }
    /// Run the call. Failures are results (`[error: …]`), never panics: a
    /// missing tool result would make every later request invalid.
    async fn run(&self, args: &Args, cancel: &CancellationToken) -> String;
}

/// The tools on offer in one engine.
#[derive(Clone, Default)]
pub struct ToolBox {
    tools: Vec<Arc<dyn Tool>>,
}

impl ToolBox {
    pub fn new(tools: Vec<Arc<dyn Tool>>) -> Self {
        Self { tools }
    }

    pub fn into_tools(self) -> Vec<Arc<dyn Tool>> {
        self.tools
    }

    pub fn get(&self, name: &str) -> Option<&Arc<dyn Tool>> {
        self.tools.iter().find(|t| t.spec().name == name)
    }

    pub fn specs(&self) -> Vec<ToolSpec> {
        self.tools.iter().map(|t| t.spec()).collect()
    }

    /// Python `tools.needs_approval`: an unknown tool and every `mcp_*` tool
    /// count as gated, so nothing unrecognised runs unasked.
    pub fn needs_approval(&self, name: &str, args: &Args) -> bool {
        if name.starts_with("mcp_") {
            return true;
        }
        self.get(name).is_none_or(|t| t.needs_approval(args))
    }

    pub fn parallel_safe(&self, name: &str) -> bool {
        self.get(name).is_some_and(|t| t.parallel_safe())
    }

    pub fn preview(&self, name: &str, args: &Args) -> String {
        self.get(name).map(|t| t.preview(args)).unwrap_or_default()
    }

    /// Python `tools.run_tool`: run by name and cap the output.
    pub async fn run(&self, name: &str, args: &Args, cancel: &CancellationToken) -> String {
        let Some(tool) = self.get(name) else {
            return format!("[error: unknown tool '{name}']");
        };
        cap_output(tool.run(args, cancel).await)
    }
}

pub fn cap_output(out: String) -> String {
    let total = char_len(&out);
    if total <= TOOL_OUTPUT_LIMIT {
        return out;
    }
    format!(
        "{}\n[output truncated at {TOOL_OUTPUT_LIMIT} chars ({total} total) — narrow the query/read a specific range]",
        head(&out, TOOL_OUTPUT_LIMIT)
    )
}

/// Python `tools.result_status` (R134g): a marker counts only when it
/// brackets the whole result.
pub fn result_status(out: &str) -> &'static str {
    if !out.trim_end().ends_with(']') {
        return "ok";
    }
    let start = head(out, 32);
    if SKIPPED_MARKERS.iter().any(|m| start.starts_with(m)) {
        "skipped"
    } else if ERROR_MARKERS.iter().any(|m| start.starts_with(m)) {
        "error"
    } else {
        "ok"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_needs_the_closing_bracket() {
        assert_eq!(result_status("[error: no such file: x]"), "error");
        assert_eq!(result_status("[error: in a log\nmore text"), "ok");
        assert_eq!(result_status("[denied by user]"), "skipped");
        assert_eq!(result_status("[skipped: interrupted]"), "skipped");
        assert_eq!(result_status("hello"), "ok");
    }

    #[test]
    fn output_cap_matches_python() {
        let out = cap_output("é".repeat(TOOL_OUTPUT_LIMIT + 5));
        assert!(out.ends_with(&format!(
            "[output truncated at {TOOL_OUTPUT_LIMIT} chars ({} total) — narrow the query/read a specific range]",
            TOOL_OUTPUT_LIMIT + 5
        )));
        assert_eq!(cap_output("short".into()), "short");
    }
}
