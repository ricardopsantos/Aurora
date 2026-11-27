//! `aurora-rs --core` (R499): the Rust agent loop behind Python's front ends.
//! Python's `Engine.send` keeps everything around a turn (the prompt's secret
//! check, budgets, compaction before the prompt, the user and assistant
//! records, costs) and hands the loop itself here: model requests, tool calls,
//! approvals, checkpoints and extensions. Protocol `aurora-core/1`, one JSON
//! object per line (documents/CORE_PROTOCOL.md):
//!
//!   ← {"ev":"ready","protocol":"aurora-core/1","model":…,"tools":[…]}
//!   → {"op":"turn","messages":[…],"prompt":…,"system":…,"auto_approve":…,"max_iterations":…,"compaction":{…}}
//!   ← {"ev":"event","type":"text"|"think"|"tool_start"|"tool_result"|"usage"|"notice",…}
//!   ← {"ev":"log","event":…,"data":{…}}          a session record for Python to write
//!   ← {"ev":"approve"|"continue"|"secret"|"budget","id":N,…}  → {"op":"answer","id":N,"value":…}
//!   → {"op":"cancel"}
//!   ← {"ev":"done","messages":[…],"turn":{…}}
//!
//! With `--provider-bridge` (R505) Python runs the model requests:
//!   ← {"ev":"provider","id":N,"model":…,"messages":[…],"system":…,"tools":[…]}
//!   → {"op":"provider_chunk","kind":"text"|"think"|"notice","chunk":…}   while it streams
//!   → {"op":"answer","id":N,"value":{"result":{…},"assistant_message":{…}} | {"error":…,"malformed":bool}}

use std::collections::HashMap;
use std::io::{BufRead, Write};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use aurora_core::agent::{Agent, AgentConfig};
use aurora_core::approval::ApprovalPolicy;
use aurora_core::provider::{ProviderError, StreamChunk, TurnRequest};
use aurora_core::{AgentEvent, Args, CancellationToken, Continue, Frontend, Provider, SecretChoice, SessionLog, ToolBox, ToolCall, TurnResult};
use aurora_core::frontend::Approval;
use serde_json::{json, Map, Value};
use tokio::sync::{mpsc, oneshot};

pub const PROTOCOL: &str = "aurora-core/1";

fn emit(v: &Value) {
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(serde_json::to_string(v).unwrap_or_default().as_bytes());
    let _ = out.write_all(b"\n");
    let _ = out.flush();
}

#[derive(Default)]
struct Pending {
    next: u64,
    waiting: HashMap<u64, oneshot::Sender<Value>>,
    /// The bridged request's stream, while one runs (R505).
    chunks: Option<mpsc::UnboundedSender<Value>>,
}

/// Register a question; the caller emits it with this id and awaits the answer.
fn register(pending: &Arc<Mutex<Pending>>) -> (u64, oneshot::Receiver<Value>) {
    let (tx, rx) = oneshot::channel();
    let mut p = pending.lock().expect("pending lock");
    p.next += 1;
    let id = p.next;
    p.waiting.insert(id, tx);
    (id, rx)
}

/// R505: a provider that is Python's. Each request is a question to the front end, which runs its provider object
/// and streams the chunks back. Tool results use the OpenAI shape (every Python provider's own).
#[derive(Default)]
pub struct BridgeProvider {
    pending: std::sync::OnceLock<Arc<Mutex<Pending>>>,
    last_assistant: Mutex<Option<Value>>,
}

impl BridgeProvider {
    fn pending(&self) -> &Arc<Mutex<Pending>> {
        self.pending.get_or_init(|| Arc::new(Mutex::new(Pending::default())))
    }
}

/// One `provider_chunk` from the front end, to the loop's sink by its kind.
fn forward_chunk(c: &Value, sink: &(dyn for<'c> Fn(StreamChunk<'c>) + Send + Sync)) {
    let text = c["chunk"].as_str().unwrap_or("");
    match c["kind"].as_str() {
        Some("think") => sink(StreamChunk::Think(text)),
        Some("notice") => sink(StreamChunk::Notice(text)),
        _ => sink(StreamChunk::Text(text)),
    }
}

fn u(v: &Value, k: &str) -> u64 {
    v[k].as_u64().unwrap_or(0)
}

#[async_trait]
impl Provider for BridgeProvider {
    fn name(&self) -> &str {
        "bridge"
    }

    async fn turn(&self, request: TurnRequest<'_>, sink: &(dyn for<'c> Fn(StreamChunk<'c>) + Send + Sync), cancel: &CancellationToken)
        -> Result<TurnResult, ProviderError> {
        let pending = self.pending();
        let (ctx, mut crx) = mpsc::unbounded_channel();
        pending.lock().expect("pending lock").chunks = Some(ctx);
        let (id, mut rx) = register(pending);
        emit(&json!({"ev": "provider", "id": id, "model": request.model, "messages": request.messages,
                     "system": request.system, "tools": request.tools}));
        let answer = loop {
            tokio::select! {
                v = &mut rx => break v.unwrap_or(Value::Null),
                Some(c) = crx.recv() => forward_chunk(&c, sink),
                _ = cancel.cancelled() => break json!({"result": {"stop_reason": "cancelled"}}),
            }
        };
        while let Ok(c) = crx.try_recv() {       // chunks sent just before the answer
            forward_chunk(&c, sink);              // R568: by kind, like the ones before (a late thought was shown as reply text)
        }
        {
            let mut p = pending.lock().expect("pending lock");
            p.chunks = None;
            p.waiting.remove(&id);                // R569: a cancelled request's slot is not kept for an answer that never comes
        }
        if let Some(e) = answer["error"].as_str() {
            return Err(if answer["malformed"].as_bool() == Some(true) { ProviderError::MalformedToolCall(e.into()) } else { ProviderError::Failed(e.into()) });
        }
        let r = &answer["result"];
        let calls = r["tool_calls"].as_array().map(|a| a.iter().map(|c| ToolCall {
            id: c["id"].as_str().unwrap_or("").into(),
            name: c["name"].as_str().unwrap_or("").into(),
            arguments: c["arguments"].as_object().cloned().unwrap_or_default(),
        }).collect()).unwrap_or_default();
        *self.last_assistant.lock().expect("assistant lock") = answer.get("assistant_message").cloned().filter(|v| !v.is_null());
        Ok(TurnResult {
            text: r["text"].as_str().unwrap_or("").into(),
            tool_calls: calls,
            input_tokens: u(r, "input_tokens"),
            output_tokens: u(r, "output_tokens"),
            stop_reason: r["stop_reason"].as_str().unwrap_or("").into(),
            cached_input_tokens: u(r, "cached_input_tokens"),
            reasoning_tokens: u(r, "reasoning_tokens"),
            reasoning_chars: u(r, "reasoning_chars"),
        })
    }

    fn assistant_message(&self, result: &TurnResult) -> Value {
        // Python's message keeps its own extra fields; content and calls come from the result, which the loop may
        // have redacted after the provider answered (R58)
        let mut m = self.last_assistant.lock().expect("assistant lock").take().unwrap_or_else(|| json!({"role": "assistant"}));
        m["content"] = if result.text.is_empty() { Value::Null } else { json!(result.text) };
        if result.tool_calls.is_empty() {
            if let Some(o) = m.as_object_mut() {
                o.remove("tool_calls");
            }
        } else {
            m["tool_calls"] = Value::Array(result.tool_calls.iter().map(|c| json!({"id": c.id, "type": "function",
                "function": {"name": c.name, "arguments": aurora_core::pyjson::dumps_ascii(&c.arguments)}})).collect());
        }
        m
    }

    fn tool_result_message(&self, call: &ToolCall, output: &str) -> Value {
        json!({"role": "tool", "tool_call_id": call.id, "content": output})
    }
}

struct CoreFrontend {
    pending: Arc<Mutex<Pending>>,
    cancel: CancellationToken,
}

impl CoreFrontend {
    async fn ask(&self, ev: &str, mut fields: Map<String, Value>) -> Value {
        let (id, rx) = register(&self.pending);
        fields.insert("ev".into(), json!(ev));
        fields.insert("id".into(), json!(id));
        emit(&Value::Object(fields));
        tokio::select! {
            v = rx => v.unwrap_or(Value::Null),
            _ = self.cancel.cancelled() => {
                self.pending.lock().expect("pending lock").waiting.remove(&id);    // R569
                Value::Null
            }
        }
    }
}

/// Python's approve answers: a key, or [key, note].
pub fn approval_from(v: &Value) -> Approval {
    let (key, note) = match v {
        Value::String(k) => (k.as_str(), String::new()),
        Value::Array(a) => (a.first().and_then(Value::as_str).unwrap_or(""), a.get(1).and_then(Value::as_str).unwrap_or("").to_string()),
        _ => ("s", String::new()),
    };
    match key {
        "y" => Approval::Once,
        "a" => Approval::Always,
        "d" => Approval::AlwaysDeny,
        "c" => Approval::Steer(note),
        "t" => Approval::SessionAllow,
        "o" => Approval::AutoApproveOn,
        "n" => Approval::Deny(note),
        _ => Approval::Stop,
    }
}

#[async_trait]
impl Frontend for CoreFrontend {
    fn event(&self, event: AgentEvent) {
        let mut v = serde_json::to_value(&event).unwrap_or_default();
        v["ev"] = json!("event");
        emit(&v);
    }

    async fn approve(&self, tool: &str, args: &Args, diff: &str) -> Approval {
        let f = json!({"tool": tool, "args": args, "diff": diff});
        approval_from(&self.ask("approve", f.as_object().cloned().unwrap_or_default()).await)
    }

    async fn ask_continue(&self, iterations: u32) -> Continue {
        let f = json!({"iterations": iterations});
        match self.ask("continue", f.as_object().cloned().unwrap_or_default()).await {
            Value::Bool(true) => Continue::Yes(String::new()),
            Value::Array(a) if a.first() == Some(&json!("silent")) => Continue::Silent,
            Value::Array(a) if a.first() == Some(&json!(true)) => Continue::Yes(a.get(1).and_then(Value::as_str).unwrap_or("").into()),
            _ => Continue::Stop,
        }
    }

    async fn secret_challenge(&self, context: &str, summary: &str) -> SecretChoice {
        let f = json!({"context": context, "summary": summary});
        match self.ask("secret", f.as_object().cloned().unwrap_or_default()).await.as_str() {
            Some("keep") => SecretChoice::Keep,
            Some("redact") => SecretChoice::Redact,
            Some("always") => SecretChoice::Always,
            _ => SecretChoice::Stop,
        }
    }

    async fn budget(&self) -> Option<(bool, String)> {
        let v = self.ask("budget", Map::new()).await;
        let msg = v.get(1).and_then(Value::as_str).unwrap_or("").to_string();
        match v.get(0).and_then(Value::as_str) {
            Some("stop") => Some((true, msg)),
            Some("warn") => Some((false, msg)),
            _ => None,
        }
    }

    fn cancel(&self) -> &CancellationToken {
        &self.cancel
    }
}

/// Records go to Python, which owns the session file in this mode.
struct ForwardLog {
    id: String,
}

impl SessionLog for ForwardLog {
    fn id(&self) -> &str {
        &self.id
    }
    fn log(&self, event: &str, data: Map<String, Value>) {
        emit(&json!({"ev": "log", "event": event, "data": data}));
    }
}

fn spawn_stdin() -> mpsc::UnboundedReceiver<Value> {
    let (tx, rx) = mpsc::unbounded_channel();
    std::thread::spawn(move || {
        for line in std::io::stdin().lock().lines() {
            let Ok(line) = line else { break };
            if line.trim().is_empty() {
                continue;
            }
            let v = serde_json::from_str(&line).unwrap_or_else(|e| json!({"op": "bad", "error": e.to_string()}));
            if tx.send(v).is_err() {
                break;
            }
        }
    });
    rx
}

fn route_answer(pending: &Arc<Mutex<Pending>>, msg: &Value) {
    let id = msg["id"].as_u64().unwrap_or(0);
    if let Some(tx) = pending.lock().expect("pending lock").waiting.remove(&id) {
        let _ = tx.send(msg["value"].clone());
    }
}

fn route_chunk(pending: &Arc<Mutex<Pending>>, msg: Value) {
    if let Some(tx) = &pending.lock().expect("pending lock").chunks {
        let _ = tx.send(msg);
    }
}

pub async fn serve(provider: &dyn Provider, tools: &ToolBox, policy: &dyn ApprovalPolicy, base: AgentConfig) -> u8 {
    serve_with(provider, tools, policy, base, None).await
}

/// `bridge`: the bridged provider shares the question table, so its answers are routed like the others.
pub async fn serve_bridged(bridge: &BridgeProvider, tools: &ToolBox, policy: &dyn ApprovalPolicy, base: AgentConfig) -> u8 {
    serve_with(bridge, tools, policy, base, Some(bridge.pending().clone())).await
}

async fn serve_with(provider: &dyn Provider, tools: &ToolBox, policy: &dyn ApprovalPolicy, base: AgentConfig,
                    shared: Option<Arc<Mutex<Pending>>>) -> u8 {
    let mut rx = spawn_stdin();
    let names: Vec<String> = tools.specs().into_iter().map(|s| s.name).collect();
    emit(&json!({"ev": "ready", "protocol": PROTOCOL, "model": base.model, "tools": names}));
    let pending = shared.unwrap_or_default();
    let log = ForwardLog { id: String::new() };
    let mut cfg = base;
    while let Some(msg) = rx.recv().await {
        match msg["op"].as_str() {
            Some("turn") => {}
            Some("shutdown") => break,
            Some("answer") => {
                route_answer(&pending, &msg);
                continue;
            }
            Some("cancel") => continue,
            _ => {
                emit(&json!({"ev": "error", "message": format!("unknown request: {msg}")}));
                continue;
            }
        }
        let mut messages: Vec<Value> = msg["messages"].as_array().cloned().unwrap_or_default();
        cfg.turn_prompt = msg["prompt"].as_str().unwrap_or("").to_string();
        if let Some(system) = msg["system"].as_str().filter(|s| !s.is_empty()) {
            cfg.system = system.to_string();        // R506: Python composed it (notes, skills, …)
        }
        cfg.auto_approve = msg["auto_approve"].as_bool().unwrap_or(false);
        cfg.no_tools = msg["tools"].as_bool() == Some(false);
        // the front end owns the secret allowlist and the session's "keep" answers (R329): this turn starts from them
        let hashes = |k: &str| msg[k].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect());
        if msg["session_rules"].is_object() {
            policy.set_session_rules(&msg["session_rules"]);      // R334: the front end keeps them for its process
        }
        if let Some(h) = hashes("secret_allowlist") {
            cfg.secret_allowlist = h;
        }
        if let Some(h) = hashes("secret_session_keep") {
            cfg.secret_session_keep = h;
        }
        if let Some(n) = msg["max_iterations"].as_u64() {
            cfg.max_iterations = n.max(1) as u32;
        }
        let c = &msg["compaction"];
        if c.is_object() {
            cfg.compaction.auto = c["auto"].as_bool().unwrap_or(false);
            cfg.compaction.threshold_pct = c["threshold_pct"].as_f64().unwrap_or(cfg.compaction.threshold_pct);
            cfg.compaction.keep_recent_tokens = c["keep_recent_tokens"].as_u64().unwrap_or(cfg.compaction.keep_recent_tokens);
            cfg.compaction.context_limit = c["context_limit"].as_u64().unwrap_or(cfg.compaction.context_limit);
            cfg.compaction.used = c["used"].as_u64().unwrap_or(0);
            cfg.compaction.count = c["count"].as_u64().unwrap_or(0) as u32;
        }
        let fe = CoreFrontend { pending: pending.clone(), cancel: CancellationToken::new() };
        let agent = Agent { provider, tools, policy, frontend: &fe, log: &log };
        let turn = {
            let fut = agent.run_turn(&mut cfg, &mut messages);
            tokio::pin!(fut);
            loop {
                tokio::select! {
                    t = &mut fut => break t,
                    m = rx.recv() => match m {
                        Some(m) if m["op"] == "answer" => route_answer(&pending, &m),
                        Some(m) if m["op"] == "provider_chunk" => route_chunk(&pending, m),
                        Some(m) if m["op"] == "cancel" => fe.cancel.cancel(),
                        Some(m) if m["op"] == "shutdown" => { fe.cancel.cancel(); }
                        Some(m) => emit(&json!({"ev": "error", "message": format!("busy: {}", m["op"])})),
                        None => fe.cancel.cancel(),
                    }
                }
            }
        };
        emit(&json!({"ev": "done", "messages": messages, "turn": {
            "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens, "last_output_tokens": turn.last_output_tokens,
            "secret_allowlist": cfg.secret_allowlist, "session_rules": policy.session_rules(), "secret_session_keep": cfg.secret_session_keep,
            "billed_input": turn.billed_input, "cached_input": turn.cached_input,
            "reasoning_tokens": turn.reasoning_tokens, "reasoning_chars": turn.reasoning_chars,
            "iterations": turn.iterations, "last_request_latency": turn.last_request_latency,
            "gen_tokens": turn.gen_tokens, "gen_secs": turn.gen_secs,
            "cancelled": turn.cancelled, "stopped": turn.stopped, "degraded": turn.degraded,
            "error": turn.error, "auto_approve": cfg.auto_approve,
            "compactions": cfg.compaction.count,
        }}));
    }
    aurora_tools::jobs::kill_all();
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_answers_map_to_approvals() {
        assert_eq!(approval_from(&json!("y")), Approval::Once);
        assert_eq!(approval_from(&json!(["n", "use ls"])), Approval::Deny("use ls".into()));
        assert_eq!(approval_from(&json!(["c", "try grep"])), Approval::Steer("try grep".into()));
        assert_eq!(approval_from(&json!(null)), Approval::Stop);
    }

    #[test]
    fn chunks_keep_their_kind() {
        // R568
        let got = Mutex::new(Vec::new());
        let sink = |c: StreamChunk<'_>| got.lock().unwrap().push(match c {
            StreamChunk::Text(t) => format!("text:{t}"),
            StreamChunk::Think(t) => format!("think:{t}"),
            StreamChunk::Notice(t) => format!("notice:{t}"),
        });
        for (k, t) in [("think", "a"), ("notice", "b"), ("text", "c"), ("", "d")] {
            forward_chunk(&json!({"kind": k, "chunk": t}), &sink);
        }
        assert_eq!(*got.lock().unwrap(), ["think:a", "notice:b", "text:c", "text:d"]);
    }

    #[tokio::test]
    async fn a_cancelled_bridged_request_leaves_no_waiting_slot() {
        // R569
        let bridge = BridgeProvider::default();
        let cancel = CancellationToken::new();
        cancel.cancel();
        let msgs = [json!({"role": "user", "content": "hi"})];
        let req = TurnRequest { model: "m", messages: &msgs, system: "", tools: &[] };
        let r = bridge.turn(req, &|_| {}, &cancel).await.unwrap();
        assert_eq!(r.stop_reason, "cancelled");
        let p = bridge.pending().lock().unwrap();
        assert!(p.waiting.is_empty() && p.chunks.is_none());
    }

    #[tokio::test]
    async fn a_cancelled_question_leaves_no_waiting_slot() {
        let fe = CoreFrontend { pending: Arc::new(Mutex::new(Pending::default())), cancel: CancellationToken::new() };
        fe.cancel.cancel();
        assert_eq!(fe.ask("continue", Map::new()).await, Value::Null);
        assert!(fe.pending.lock().unwrap().waiting.is_empty());
    }
}
