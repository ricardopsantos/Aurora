use async_trait::async_trait;
use tokio_util::sync::CancellationToken;

use crate::events::AgentEvent;
use crate::types::Args;

/// An answer at the approval gate (Python `Frontend.approve` keys).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Approval {
    /// `y`: run it once.
    Once,
    /// `n`: don't run it; the note (may be empty) goes back to the model.
    Deny(String),
    /// `a`: run it and add an allowlist rule.
    Always,
    /// `d`: refuse it and add a denylist rule.
    AlwaysDeny,
    /// `s`: stop the whole turn.
    Stop,
    /// `c`: don't run it; steer the model with this guidance.
    Steer(String),
    /// `t`: run it and allow the same rule for the rest of this process.
    SessionAllow,
    /// `o`: run it and turn auto-approve on (R348).
    AutoApproveOn,
}

/// An answer at the iteration cap (Python `Frontend.ask_continue`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Continue {
    Stop,
    /// Keep going; a non-empty string is a steer for the model.
    Yes(String),
    /// Keep going and don't ask again this turn.
    Silent,
}

/// The engine ⇄ UI contract, the only surface between the two halves.
/// A front end must be cheap to call from the agent task: `event` is called
/// for every streamed chunk.
/// The answer to a secret challenge (R58).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SecretChoice {
    /// Send it as it is.
    Keep,
    /// Replace each match with `<secret>`.
    Redact,
    /// Do not send; end the turn.
    Stop,
    /// Keep, and never flag these exact values again (this run).
    Always,
}

#[async_trait]
pub trait Frontend: Send + Sync {
    fn event(&self, event: AgentEvent);

    /// Gate a write or a command. `diff` is a preview (may be empty).
    async fn approve(&self, tool: &str, args: &Args, diff: &str) -> Approval;

    /// The tool loop reached `iterations` rounds: keep going?
    async fn ask_continue(&self, iterations: u32) -> Continue;

    /// Cancelled once the human asked to stop (which key is the front end's business).
    /// A likely secret was found in `context` (`prompt`, `reply`, `write:<tool>`
    /// or `tool:<name>`). `summary` names the kinds found, never the values.
    /// Fails closed by default.
    async fn secret_challenge(&self, _context: &str, _summary: &str) -> SecretChoice {
        SecretChoice::Stop
    }

    fn cancel(&self) -> &CancellationToken;

    /// The spend limit after a reply that asks for tools (Python `cb.budget`): `None` (no limit, or under it),
    /// `Some((false, msg))` a warning, `Some((true, msg))` stop the turn.
    async fn budget(&self) -> Option<(bool, String)> {
        None
    }
}
