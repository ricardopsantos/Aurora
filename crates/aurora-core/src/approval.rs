use crate::types::Args;

/// R133c: the closed set of gate outcomes the session log records, shared
/// with Python so both write the same vocabulary.
pub const DECISIONS: [&str; 11] = [
    "allowlisted", "approved", "always_allow", "denied", "denied_policy", "always_deny",
    "steered", "stopped", "auto_approved", "plan_mode", "session_allow",
];

/// Standing rules consulted before anybody is asked: the denylist always wins
/// (R120), then the allowlist skips the question.
pub trait ApprovalPolicy: Send + Sync {
    fn is_denied(&self, tool: &str, args: &Args) -> bool;
    fn is_allowed(&self, tool: &str, args: &Args) -> bool;
    /// Add an "always allow" rule; returns the rule text, or None when this
    /// policy cannot store one (the call still runs once).
    fn add_allow(&self, _tool: &str, _args: &Args) -> Option<String> {
        None
    }
    fn add_deny(&self, _tool: &str, _args: &Args) -> Option<String> {
        None
    }
    /// "Allow for this session" (R334): a rule kept in memory only.
    fn add_session(&self, _tool: &str, _args: &Args) -> Option<String> {
        None
    }
    /// Re-read the stored rules at a turn's start; the notices to show (unreadable files).
    fn reload(&self) -> Vec<String> {
        Vec::new()
    }
    /// The session rules (R334) as `{tool: [rule, …]}`, for a front end that keeps them across cores.
    fn session_rules(&self) -> serde_json::Value {
        serde_json::Value::Null
    }
    /// Start from the front end's session rules.
    fn set_session_rules(&self, _rules: &serde_json::Value) {}
    /// What an "always allow" would store and how far it reaches, shown in the
    /// approval prompt before the user commits (R335). Empty when unknown.
    fn rule_scope(&self, _tool: &str, _args: &Args) -> String {
        String::new()
    }
    /// False while the policy cannot honour every standing deny rule; the
    /// agent then never runs a gated call without asking (no auto-approve).
    fn complete(&self) -> bool {
        true
    }
}

/// No rules at all: every gated call is asked.
pub struct AskEveryTime;

impl ApprovalPolicy for AskEveryTime {
    fn is_denied(&self, _: &str, _: &Args) -> bool {
        false
    }
    fn is_allowed(&self, _: &str, _: &Args) -> bool {
        false
    }
}
