use async_trait::async_trait;
use serde_json::Value;
use tokio_util::sync::CancellationToken;

use crate::types::{ToolCall, ToolSpec, TurnResult};

/// One model request: the running history (provider-native messages, as in
/// Python), the system prompt and the tools on offer.
pub struct TurnRequest<'a> {
    pub model: &'a str,
    pub messages: &'a [Value],
    pub system: &'a str,
    pub tools: &'a [ToolSpec],
}

/// What a provider streams while a request runs.
#[derive(Clone, Copy, Debug)]
pub enum StreamChunk<'a> {
    Text(&'a str),
    Think(&'a str),
    /// A notice for the human (a retry, a backoff).
    Notice(&'a str),
}

#[derive(Debug, thiserror::Error)]
pub enum ProviderError {
    #[error("{0}")]
    Failed(String),
    /// The model emitted a tool call the parser rejects (the local-model
    /// failure mode); the agent may retry without tools.
    #[error("{0}")]
    MalformedToolCall(String),
}

#[async_trait]
pub trait Provider: Send + Sync {
    fn name(&self) -> &str;

    /// Run one request. Text is streamed through `sink` as it arrives; on
    /// cancel the provider returns early with `stop_reason = "cancelled"` and
    /// never returns a half-received tool call.
    async fn turn(
        &self,
        request: TurnRequest<'_>,
        sink: &(dyn for<'c> Fn(StreamChunk<'c>) + Send + Sync),
        cancel: &CancellationToken,
    ) -> Result<TurnResult, ProviderError>;

    /// Provider-native assistant message for the history (text + calls).
    fn assistant_message(&self, result: &TurnResult) -> Value;

    /// Provider-native message carrying one tool result.
    fn tool_result_message(&self, call: &ToolCall, output: &str) -> Value;
}
