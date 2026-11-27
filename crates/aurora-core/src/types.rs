use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Tool arguments: always a JSON object (R268 refuses anything else).
pub type Args = Map<String, Value>;

/// One tool call the model made, with its arguments already parsed.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: Args,
}

/// What one provider request produced (Python: `providers.base.TurnResult`).
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct TurnResult {
    pub text: String,
    pub tool_calls: Vec<ToolCall>,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub stop_reason: String,
    pub cached_input_tokens: u64,
    /// A subset of `output_tokens`, never additive (R133a).
    pub reasoning_tokens: u64,
    pub reasoning_chars: u64,
}

/// Aurora-shape tool spec (Python: an entry of `tools.SPEC`); each provider
/// converts it to its own wire format.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub parameters: Value,
}
