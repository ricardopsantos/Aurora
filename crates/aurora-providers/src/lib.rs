//! Model providers. Python Aurora has one provider class for every backend
//! (`make_provider` always builds `OpenAICompatProvider`); so does this.

pub mod net;
pub mod openai_compat;
pub mod sse;

pub use openai_compat::OpenAiCompat;

/// The per-model catalog Python ships (`aurora/providers/remote_context_limits.json`).
const REMOTE_CONTEXT_LIMITS: &str = include_str!("../../../aurora/providers/remote_context_limits.json");

/// Python `static_context_limit` (R95i): the catalog's `context_size` for the
/// model, else the provider's `context_limit:`, else 128 000. No network: the
/// live llama.cpp `/props` lookup is not ported yet.
pub fn static_context_limit(model: &str, provider_cfg: &serde_json::Value) -> u64 {
    let listed = serde_json::from_str::<serde_json::Value>(REMOTE_CONTEXT_LIMITS)
        .ok()
        .and_then(|v| v.as_array().cloned())
        .and_then(|a| a.into_iter().find(|e| e["model"] == model))
        .and_then(|e| e["context_size"].as_u64())
        .filter(|n| *n > 0);
    listed
        .or_else(|| match &provider_cfg["context_limit"] {
            serde_json::Value::Number(n) => n.as_f64().map(|f| f as u64),
            serde_json::Value::String(s) => s.trim().parse::<f64>().ok().map(|f| f as u64),
            _ => None,
        }.filter(|n| *n > 0))
        .unwrap_or(128_000)
}

#[cfg(test)]
mod limit_tests {
    #[test]
    fn catalog_then_config_then_default() {
        let none = serde_json::json!({});
        assert_eq!(super::static_context_limit("moonshotai/kimi-k3", &none), 1_048_576);
        assert_eq!(super::static_context_limit("x", &serde_json::json!({"context_limit": 32768})), 32768);
        assert_eq!(super::static_context_limit("x", &none), 128_000);
    }
}
