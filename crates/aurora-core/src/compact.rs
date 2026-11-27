//! History folding (Python `aurora/compact.py` + `tokens.py`): the pure half of
//! /compact and auto-compact (R14, R118, R154, R156, R222, R229). The agent
//! decides when to fold; this module says where to cut and what to carry.

use serde_json::{json, Value};

use crate::text::{char_len, head, tail};

pub const CHARS_PER_TOKEN: usize = 4;

/// `tokens.estimate_tokens`: characters / 4.
pub fn estimate_tokens(text: &str) -> u64 {
    (char_len(text) / CHARS_PER_TOKEN) as u64
}

/// `_stringify` (R222): a missing content is "", never "None".
fn stringify(content: Option<&Value>) -> String {
    match content {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => crate::pyjson::dumps(other),
    }
}

fn msg_tokens(m: &Value) -> u64 {
    let images = m.get("images").and_then(Value::as_array).map_or(0, Vec::len) as u64;
    let mut n = estimate_tokens(&stringify(m.get("content"))) + 1500 * images;
    for tc in m.get("tool_calls").and_then(Value::as_array).into_iter().flatten() {
        n += estimate_tokens(&stringify(tc.get("function").and_then(|f| f.get("arguments"))));
    }
    n
}

/// R156: may a request start at this message?
pub fn is_cut_boundary(m: &Value) -> bool {
    match m.get("role").and_then(Value::as_str) {
        Some("user") => true,
        Some("assistant") => m.get("tool_calls").and_then(Value::as_array).is_some_and(|a| !a.is_empty()),
        _ => false,
    }
}

/// R118/R156: where the kept raw tail starts; 0 = leave history alone.
pub fn cut_index(messages: &[Value], keep_recent_tokens: u64) -> usize {
    let mut total = 0;
    let mut idx = None;
    for i in (0..messages.len()).rev() {
        total += msg_tokens(&messages[i]);
        if total > keep_recent_tokens {
            idx = Some(i);
            break;
        }
    }
    let Some(mut idx) = idx else { return 0 };
    let stop = idx;
    while idx < messages.len() && !is_cut_boundary(&messages[idx]) {
        idx += 1;
    }
    if idx < messages.len() {
        return idx;
    }
    (0..stop).rev().find(|&i| is_cut_boundary(&messages[i])).unwrap_or(0)
}

/// The whole transcript as readable text.
pub fn flatten_history(messages: &[Value]) -> String {
    let mut lines = Vec::new();
    for m in messages {
        let role = m.get("role").and_then(Value::as_str).unwrap_or("?");
        if role == "tool" {
            lines.push(format!("[tool result: {}]", stringify(m.get("content"))));
            continue;
        }
        let mut text = stringify(m.get("content"));
        if role == "assistant" {
            for tc in m.get("tool_calls").and_then(Value::as_array).into_iter().flatten() {
                let f = tc.get("function");
                let name = f.and_then(|f| f.get("name")).and_then(Value::as_str).unwrap_or("None");
                let args = match f.and_then(|f| f.get("arguments")) {
                    None | Some(Value::Null) => "None".to_string(),
                    Some(v) => stringify(Some(v)),
                };
                text.push_str(&format!("\n[called {name}({args})]"));
            }
        }
        let prefix = match role {
            "user" => "User",
            "assistant" => "Assistant",
            r => r,
        };
        if !text.trim().is_empty() {
            lines.push(format!("{prefix}: {text}"));
        }
    }
    lines.join("\n\n")
}

fn clip_marker(dropped: usize) -> String {
    format!("\n\n[... {dropped} characters dropped to fit the context window — middle of the folded region ...]\n\n")
}

/// R156/R229: at most `max_tokens`, keeping the head (60%) and the tail.
pub fn clip_transcript(text: &str, max_tokens: u64) -> String {
    if max_tokens == 0 {
        return text.to_string();
    }
    let limit = max_tokens as usize * CHARS_PER_TOKEN;
    let len = char_len(text);
    if len <= limit {
        return text.to_string();
    }
    let reserve = char_len(&clip_marker(len));
    if limit < reserve + 2 {
        return head(text, limit).to_string();
    }
    let budget = limit - reserve;
    let h = (budget as f64 * 0.6) as usize;
    let t = budget - h;
    let dropped = len - h - t;
    format!("{}{}{}", head(text, h), clip_marker(dropped), tail(text, t))
}

/// R4: the transcript wrapped as one user message.
pub fn flattened_as_user_message(messages: &[Value]) -> Value {
    json!({"role": "user", "content": format!("[Earlier conversation, carried over on model switch:]\n\n{}", flatten_history(messages))})
}

/// Python engine constants for the summarization request.
pub const COMPACT_INPUT_FRACTION: f64 = 0.5;
pub const COMPACT_INPUT_TOKENS_FALLBACK: u64 = 16_000;
pub const COMPACT_HEADROOM_TOKENS: i64 = 4_000;
pub const COMPACT_MIN_SUMMARY_TOKENS: u64 = 2_000;

pub const SUMMARY_ASK: &str = "Summarize this conversation for your own continued use. Keep: decisions made, exact file paths and commands, open tasks, constraints the user stated. Drop: pleasantries, superseded attempts, full file dumps. Reply with ONLY the summary.\n\n";

/// The token budget for the folded transcript (`Engine.compact_history`).
pub fn summary_budget(limit: u64, system: &str, recent: &[Value]) -> u64 {
    if limit == 0 {
        return COMPACT_INPUT_TOKENS_FALLBACK;
    }
    let recent_tokens: u64 = recent.iter().map(|m| estimate_tokens(&content_str(m))).sum();
    let room = limit as i64 - estimate_tokens(system) as i64 - recent_tokens as i64 - COMPACT_HEADROOM_TOKENS;
    let frac = (limit as f64 * COMPACT_INPUT_FRACTION) as i64;
    (frac.min(room)).max(COMPACT_MIN_SUMMARY_TOKENS as i64) as u64
}

/// Python `str(m.get("content", ""))`, close enough for estimates.
pub fn content_str(m: &Value) -> String {
    match m.get("content") {
        None => String::new(),
        Some(Value::Null) => "None".into(),
        Some(v) => stringify(Some(v)),
    }
}

/// Replace `messages` with `body` + the kept tail, merging into a leading user message.
pub fn splice(messages: &mut Vec<Value>, cut: usize, body: String) {
    let recent: Vec<Value> = messages.split_off(cut);
    messages.clear();
    let mut it = recent.into_iter();
    match it.next() {
        Some(mut first) if first.get("role").and_then(Value::as_str) == Some("user") => {
            let rest = first.get("content").and_then(Value::as_str).unwrap_or("").to_string();
            first["content"] = Value::String(format!("{body}\n\n{rest}"));
            messages.push(first);
        }
        Some(first) => {
            messages.push(json!({"role": "user", "content": body}));
            messages.push(first);
        }
        None => messages.push(json!({"role": "user", "content": body})),
    }
    messages.extend(it);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cut_never_splits_a_round() {
        let big = "x".repeat(4000);
        let msgs = vec![
            json!({"role": "user", "content": "task"}),
            json!({"role": "assistant", "content": null, "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}]}),
            json!({"role": "tool", "content": big}),
            json!({"role": "assistant", "content": null, "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}]}),
            json!({"role": "tool", "content": "small"}),
        ];
        assert_eq!(cut_index(&msgs, 100), 3);
        assert_eq!(cut_index(&msgs, 1_000_000), 0);
        assert!(flatten_history(&msgs).starts_with("User: task\n\nAssistant: \n[called read_file({})]"));
    }

    #[test]
    fn clip_keeps_head_and_tail() {
        let t = "a".repeat(1000) + &"b".repeat(1000);
        let c = clip_transcript(&t, 100);
        assert_eq!(char_len(&c), 400);
        assert!(c.starts_with('a') && c.ends_with('b') && c.contains("characters dropped"));
        assert_eq!(clip_transcript("short", 100), "short");
    }

    #[test]
    fn splice_merges_into_a_leading_user_message() {
        let mut m = vec![json!({"role": "user", "content": "a"}), json!({"role": "user", "content": "b"})];
        splice(&mut m, 1, "S".into());
        assert_eq!(m, vec![json!({"role": "user", "content": "S\n\nb"})]);
    }
}
