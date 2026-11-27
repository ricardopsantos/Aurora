use serde::Serialize;

use crate::types::Args;

/// Everything the engine tells a front end, as data. The Python `Frontend`
/// protocol has one method per kind (`on_text`, `on_tool_start`, …); here they
/// are one serialisable enum so the same stream can feed a terminal, a test,
/// or a web page over SSE without a new method per front end.
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum AgentEvent {
    /// A chunk of streamed assistant text.
    Text { chunk: String },
    /// A chunk of a thinking model's reasoning; display only, never history.
    Think { chunk: String },
    /// A tool is about to run (already approved).
    ToolStart { name: String, args: Args },
    /// A tool finished (or was skipped); `output` is what the model will see, `args` what it was called with.
    ToolResult { name: String, output: String, args: Args },
    /// Provider-reported token counts for one request (0 when not reported).
    Usage { input_tokens: u64, output_tokens: u64, cached_input_tokens: u64 },
    /// An out-of-band notice (retry, interrupt, policy refusal, error).
    Notice { message: String },
}
