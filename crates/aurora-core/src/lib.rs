//! Aurora's engine half: the contracts every other crate implements, and the
//! agent loop that drives them. Like Python's engine (tests/test_architecture.py),
//! this crate never touches a terminal, a socket or the filesystem itself.

pub mod agent;
pub mod approval;
pub mod compact;
pub mod events;
pub mod ext;
pub mod frontend;
pub mod provider;
pub mod secrets;
pub mod pyjson;
pub mod session;
pub mod text;
pub mod tool;
pub mod types;

pub use agent::{run_turn, AgentConfig, Compaction, TurnOutcome};
pub use events::AgentEvent;
pub use frontend::{Approval, Continue, Frontend, SecretChoice};
pub use provider::{Provider, ProviderError, StreamChunk, TurnRequest};
pub use session::SessionLog;
pub use tool::{Tool, ToolBox};
pub use types::{Args, ToolCall, ToolSpec, TurnResult};
pub use tokio_util::sync::CancellationToken;
