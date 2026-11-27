use serde_json::{Map, Value};

/// The append-only event log a session writes (Python `Session.log`). The
/// JSONL implementation lives in aurora-storage; tests use an in-memory one.
pub trait SessionLog: Send + Sync {
    fn id(&self) -> &str;
    /// Append one record `{"ts", "event", **data}`. Logging must never fail a
    /// turn (R42/R51): implementations swallow and report their own errors.
    fn log(&self, event: &str, data: Map<String, Value>);
}

/// A log that keeps records in memory, for tests and dry runs.
#[derive(Default)]
pub struct MemoryLog {
    pub records: std::sync::Mutex<Vec<(String, Map<String, Value>)>>,
}

impl SessionLog for MemoryLog {
    fn id(&self) -> &str {
        "memory"
    }
    fn log(&self, event: &str, data: Map<String, Value>) {
        self.records.lock().expect("log lock").push((event.to_string(), data));
    }
}
