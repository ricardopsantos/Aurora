//! Agent-loop rules, with a scripted provider and a scripted front end.

use std::collections::VecDeque;
use std::sync::Mutex;

use async_trait::async_trait;
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

use super::*;
use crate::frontend::SecretChoice;
use crate::approval::AskEveryTime;
use crate::session::MemoryLog;
use crate::tool::Tool;

struct Scripted {
    replies: Mutex<VecDeque<Result<TurnResult, ProviderError>>>,
    seen: Mutex<Vec<usize>>,
}

impl Scripted {
    fn new(replies: Vec<Result<TurnResult, ProviderError>>) -> Self {
        Self { replies: Mutex::new(replies.into()), seen: Mutex::new(Vec::new()) }
    }
}

#[async_trait]
impl Provider for Scripted {
    fn name(&self) -> &str {
        "scripted"
    }
    async fn turn(&self, req: TurnRequest<'_>, sink: &(dyn for<'c> Fn(StreamChunk<'c>) + Send + Sync), _c: &CancellationToken) -> Result<TurnResult, ProviderError> {
        self.seen.lock().unwrap().push(req.tools.len());
        let r = self.replies.lock().unwrap().pop_front().expect("a scripted reply");
        if let Ok(t) = &r {
            sink(StreamChunk::Text(&t.text));
        }
        r
    }
    fn assistant_message(&self, r: &TurnResult) -> Value {
        json!({"role": "assistant", "content": r.text, "calls": r.tool_calls.len()})
    }
    fn tool_result_message(&self, c: &ToolCall, out: &str) -> Value {
        json!({"role": "tool", "tool_call_id": c.id, "content": out})
    }
}

struct Echo {
    name: &'static str,
    gated: bool,
}

#[async_trait]
impl Tool for Echo {
    fn spec(&self) -> ToolSpec {
        ToolSpec { name: self.name.into(), description: String::new(), parameters: json!({"type": "object"}) }
    }
    fn needs_approval(&self, _: &Args) -> bool {
        self.gated
    }
    fn parallel_safe(&self) -> bool {
        !self.gated
    }
    async fn run(&self, args: &Args, _c: &CancellationToken) -> String {
        format!("ran {} {}", self.name, Value::Object(args.clone()))
    }
}

struct Fe {
    answers: Mutex<VecDeque<Approval>>,
    events: Mutex<Vec<AgentEvent>>,
    asked: Mutex<u32>,
    cancel: CancellationToken,
    secret: Mutex<VecDeque<SecretChoice>>,
    challenges: Mutex<Vec<(String, String)>>,
}

impl Fe {
    fn new(answers: Vec<Approval>) -> Self {
        Self { answers: Mutex::new(answers.into()), events: Mutex::new(Vec::new()), asked: Mutex::new(0), cancel: CancellationToken::new(),
            secret: Mutex::new(VecDeque::new()), challenges: Mutex::new(Vec::new()) }
    }
    fn notices(&self) -> Vec<String> {
        self.events.lock().unwrap().iter().filter_map(|e| match e {
            AgentEvent::Notice { message } => Some(message.clone()),
            _ => None,
        }).collect()
    }
}

#[async_trait]
impl Frontend for Fe {
    fn event(&self, e: AgentEvent) {
        self.events.lock().unwrap().push(e);
    }
    async fn approve(&self, _t: &str, _a: &Args, _d: &str) -> Approval {
        *self.asked.lock().unwrap() += 1;
        self.answers.lock().unwrap().pop_front().unwrap_or(Approval::Deny(String::new()))
    }
    async fn ask_continue(&self, _i: u32) -> Continue {
        Continue::Stop
    }
    async fn secret_challenge(&self, context: &str, summary: &str) -> SecretChoice {
        self.challenges.lock().unwrap().push((context.into(), summary.into()));
        self.secret.lock().unwrap().pop_front().unwrap_or(SecretChoice::Stop)
    }
    fn cancel(&self) -> &CancellationToken {
        &self.cancel
    }
}

fn call(id: &str, name: &str) -> ToolCall {
    ToolCall { id: id.into(), name: name.into(), arguments: json!({"path": "x"}).as_object().unwrap().clone() }
}

fn calls(text: &str, cs: Vec<ToolCall>) -> Result<TurnResult, ProviderError> {
    Ok(TurnResult { text: text.into(), tool_calls: cs, input_tokens: 10, output_tokens: 2, ..Default::default() })
}

fn tools() -> ToolBox {
    ToolBox::new(vec![
        std::sync::Arc::new(Echo { name: "read_file", gated: false }),
        std::sync::Arc::new(Echo { name: "write_file", gated: true }),
    ])
}

fn cfg(auto: bool) -> AgentConfig {
    AgentConfig { model: "m".into(), no_tools: false, system: "s".into(), max_iterations: 10, auto_approve: auto, parallel_tools: true,
        redact_secrets: false, secret_allowlist: Default::default(), secret_session_keep: Default::default(),
        compaction: Default::default(), checkpoints: None, turn_prompt: String::new() }
}

async fn run(p: &Scripted, fe: &Fe, log: &MemoryLog, auto: bool) -> (TurnOutcome, Vec<Value>) {
    let tb = tools();
    let mut msgs = Vec::new();
    let out = run_turn(p, &tb, &AskEveryTime, fe, log, &mut cfg(auto), &mut msgs, "go").await;
    (out, msgs)
}

fn events(log: &MemoryLog) -> Vec<String> {
    log.records.lock().unwrap().iter().map(|(e, _)| e.clone()).collect()
}

#[tokio::test]
async fn approved_write_runs_and_every_step_is_logged() {
    let p = Scripted::new(vec![calls("", vec![call("1", "write_file")]), calls("done", vec![])]);
    let fe = Fe::new(vec![Approval::Once]);
    let log = MemoryLog::default();
    let (out, msgs) = run(&p, &fe, &log, false).await;
    assert_eq!(events(&log), ["user", "approval", "tool", "assistant"]);
    assert_eq!(msgs[2]["content"], r#"ran write_file {"path":"x"}"#);
    assert_eq!(out.billed_input, 20);
    assert_eq!(out.output_tokens, 4);
    let recs = log.records.lock().unwrap();
    assert_eq!(recs[1].1["decision"], "approved");
    assert_eq!(recs[2].1["status"], "ok");
    assert_eq!(recs[3].1["text"], "done");
}

#[tokio::test]
async fn denied_write_never_runs_and_history_stays_paired() {
    let p = Scripted::new(vec![calls("", vec![call("1", "write_file"), call("2", "read_file")]), calls("ok", vec![])]);
    let fe = Fe::new(vec![Approval::Deny("not now".into())]);
    let log = MemoryLog::default();
    let (_, msgs) = run(&p, &fe, &log, false).await;
    assert_eq!(msgs[2]["content"], "[denied by user: not now]");
    assert_eq!(msgs[3]["tool_call_id"], "2");
    assert_eq!(log.records.lock().unwrap()[2].1["status"], "skipped");
}

#[tokio::test]
async fn stop_answers_the_rest_of_the_round_as_skipped() {
    let p = Scripted::new(vec![calls("", vec![call("1", "write_file"), call("2", "write_file")])]);
    let fe = Fe::new(vec![Approval::Stop]);
    let log = MemoryLog::default();
    let (out, msgs) = run(&p, &fe, &log, false).await;
    assert!(out.stopped);
    let results: Vec<_> = msgs.iter().filter(|m| m["role"] == "tool").map(|m| m["content"].clone()).collect();
    assert_eq!(results, [json!("[skipped: user stopped the turn]"), json!("[skipped: user stopped the turn]")]);
}

#[tokio::test]
async fn auto_approve_skips_the_question_but_is_logged() {
    let p = Scripted::new(vec![calls("", vec![call("1", "write_file")]), calls("done", vec![])]);
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    run(&p, &fe, &log, true).await;
    assert_eq!(*fe.asked.lock().unwrap(), 0);
    assert_eq!(log.records.lock().unwrap()[1].1["decision"], "auto_approved");
}

#[tokio::test]
async fn reads_never_ask() {
    let p = Scripted::new(vec![calls("", vec![call("1", "read_file"), call("2", "read_file")]), calls("done", vec![])]);
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    let (_, msgs) = run(&p, &fe, &log, false).await;
    assert_eq!(*fe.asked.lock().unwrap(), 0);
    assert_eq!(msgs[2]["tool_call_id"], "1");
    assert_eq!(msgs[3]["tool_call_id"], "2");
}

#[tokio::test]
async fn iteration_cap_stop_skips_the_calls() {
    let p = Scripted::new(vec![calls("", vec![call("1", "read_file")]), calls("", vec![call("2", "read_file")])]);
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    let tb = tools();
    let mut msgs = Vec::new();
    let mut c = cfg(false);
    c.max_iterations = 2;
    let out = run_turn(&p, &tb, &AskEveryTime, &fe, &log, &mut c, &mut msgs, "go").await;
    assert!(out.stopped);
    assert_eq!(msgs.last().unwrap()["content"], "[skipped: user stopped at the iteration cap]");
    assert!(fe.notices().contains(&"stopped at iteration cap".to_string()));
}

#[tokio::test]
async fn malformed_twice_drops_tools_and_answers_in_chat() {
    let bad = || Err(ProviderError::MalformedToolCall("bad".into()));
    let p = Scripted::new(vec![bad(), bad(), calls("plain answer", vec![])]);
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    let (out, msgs) = run(&p, &fe, &log, false).await;
    assert!(out.degraded);
    assert_eq!(*p.seen.lock().unwrap(), [2, 2, 0]);
    assert!(msgs.iter().all(|m| m["content"] != json!("Your previous tool call was malformed. Emit a single valid tool call, or answer in plain text.")));
}

#[tokio::test]
async fn provider_error_is_a_notice_not_a_panic() {
    let p = Scripted::new(vec![Err(ProviderError::Failed("local HTTP 500: boom".into()))]);
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    let (out, _) = run(&p, &fe, &log, false).await;
    assert_eq!(out.error.as_deref(), Some("local HTTP 500: boom"));
    assert!(fe.notices()[0].starts_with("provider error:"));
}

struct DenyAll;
impl ApprovalPolicy for DenyAll {
    fn is_denied(&self, _: &str, _: &Args) -> bool { true }
    fn is_allowed(&self, _: &str, _: &Args) -> bool { false }
}

#[tokio::test]
async fn the_denylist_wins_over_auto_approve() {
    let p = Scripted::new(vec![calls("", vec![call("1", "write_file")]), calls("done", vec![])]);
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    let tb = tools();
    let mut msgs = Vec::new();
    run_turn(&p, &tb, &DenyAll, &fe, &log, &mut cfg(true), &mut msgs, "go").await;
    assert_eq!(msgs[2]["content"], "[denied by policy]");
}

struct Incomplete;
impl ApprovalPolicy for Incomplete {
    fn is_denied(&self, _: &str, _: &Args) -> bool { false }
    fn is_allowed(&self, _: &str, _: &Args) -> bool { false }
    fn complete(&self) -> bool { false }
}

#[tokio::test]
async fn an_incomplete_policy_turns_auto_approve_into_asking() {
    let p = Scripted::new(vec![calls("", vec![call("1", "write_file")]), calls("done", vec![])]);
    let fe = Fe::new(vec![Approval::Deny(String::new())]);
    let log = MemoryLog::default();
    let tb = tools();
    let mut msgs = Vec::new();
    run_turn(&p, &tb, &Incomplete, &fe, &log, &mut cfg(true), &mut msgs, "go").await;
    assert_eq!(*fe.asked.lock().unwrap(), 1);
    assert_eq!(msgs[2]["content"], "[denied by user]");
}

// ── R58: secrets ──────────────────────────────────────────────────────────

// built from pieces so no real-looking token sits in the source (the push scan reads it)
const KEY: &str = concat!("ghp", "_abcdefghijklmnopqrstuvwxyz0123456789");

struct Leak;

#[async_trait]
impl Tool for Leak {
    fn spec(&self) -> ToolSpec {
        ToolSpec { name: "read_file".into(), description: String::new(), parameters: json!({"type": "object"}) }
    }
    fn needs_approval(&self, _: &Args) -> bool {
        false
    }
    async fn run(&self, _a: &Args, _c: &CancellationToken) -> String {
        format!("TOKEN line\nkey {KEY}")
    }
}

fn secret_cfg() -> AgentConfig {
    AgentConfig { redact_secrets: true, ..cfg(false) }
}

async fn run_with(p: &Scripted, fe: &Fe, tb: &ToolBox, c: &mut AgentConfig) -> (TurnOutcome, Vec<Value>, MemoryLog) {
    let log = MemoryLog::default();
    let mut msgs = Vec::new();
    let out = run_turn(p, tb, &AskEveryTime, fe, &log, c, &mut msgs, "go").await;
    (out, msgs, log)
}

fn write_call(content: &str) -> ToolCall {
    ToolCall { id: "w".into(), name: "write_file".into(), arguments: json!({"path": "x", "content": content}).as_object().unwrap().clone() }
}

#[tokio::test]
async fn tool_output_is_redacted_before_the_model_and_the_log_see_it() {
    let tb = ToolBox::new(vec![std::sync::Arc::new(Leak)]);
    let p = Scripted::new(vec![calls("", vec![call("1", "read_file")]), calls("fine", vec![])]);
    let fe = Fe::new(vec![]);
    fe.secret.lock().unwrap().push_back(SecretChoice::Redact);
    let (_, msgs, log) = run_with(&p, &fe, &tb, &mut secret_cfg()).await;
    assert_eq!(fe.challenges.lock().unwrap()[0], ("tool:read_file".to_string(), "GitHub token".to_string()));
    assert_eq!(msgs[2]["content"], "TOKEN line\nkey <secret>");
    assert!(!format!("{:?}", log.records.lock().unwrap()).contains(KEY));
}

#[tokio::test]
async fn keeping_tool_output_is_remembered_for_the_session_but_a_stop_skips_the_rest() {
    let tb = ToolBox::new(vec![std::sync::Arc::new(Leak)]);
    let p = Scripted::new(vec![calls("", vec![call("1", "read_file")]), calls("", vec![call("2", "read_file")]), calls("fine", vec![])]);
    let fe = Fe::new(vec![]);
    fe.secret.lock().unwrap().push_back(SecretChoice::Keep);
    let (_, msgs, _) = run_with(&p, &fe, &tb, &mut secret_cfg()).await;
    assert_eq!(fe.challenges.lock().unwrap().len(), 1, "the same output is not asked about twice");
    assert!(msgs[2]["content"].as_str().unwrap().contains(KEY));

    let p = Scripted::new(vec![calls("", vec![call("1", "read_file"), call("2", "read_file")])]);
    let fe = Fe::new(vec![]); // no answer queued: the default fails closed
    let (out, msgs, _) = run_with(&p, &fe, &tb, &mut secret_cfg()).await;
    assert!(out.stopped);
    let results: Vec<_> = msgs.iter().filter(|m| m["role"] == "tool").map(|m| m["content"].as_str().unwrap().to_string()).collect();
    assert_eq!(results, ["[skipped: secret detected — user stopped the turn]"; 2]);
}

#[tokio::test]
async fn a_secret_in_written_content_is_redacted_in_the_call_that_runs() {
    let p = Scripted::new(vec![calls("", vec![write_call(&format!("x = '{KEY}'"))]), calls("done", vec![])]);
    let fe = Fe::new(vec![Approval::Once]);
    fe.secret.lock().unwrap().push_back(SecretChoice::Redact);
    let (_, msgs, _) = run_with(&p, &fe, &tools(), &mut secret_cfg()).await;
    assert_eq!(fe.challenges.lock().unwrap()[0].0, "write:write_file");
    assert!(msgs[2]["content"].as_str().unwrap().contains("<secret>") && !msgs[2]["content"].as_str().unwrap().contains(KEY));
}

#[tokio::test]
async fn stopping_on_a_written_secret_closes_the_turn_without_running_anything() {
    let p = Scripted::new(vec![calls("", vec![write_call(KEY)])]);
    let fe = Fe::new(vec![Approval::Once]);
    let (out, msgs, log) = run_with(&p, &fe, &tools(), &mut secret_cfg()).await;
    assert!(out.cancelled);
    assert_eq!(*fe.asked.lock().unwrap(), 0, "never reached the approval gate");
    let last = msgs.last().unwrap();
    assert_eq!(last["role"], "assistant");
    assert_eq!(last["calls"], 0);
    assert_eq!(last["content"], "[stopped: secret detected in a tool argument]");
    assert_eq!(log.records.lock().unwrap().iter().filter(|(e, _)| e == "tool").count(), 1);
    assert!(!format!("{:?}", log.records.lock().unwrap()).contains(KEY), "the skip record must not carry the secret");
}

#[tokio::test]
async fn a_secret_in_the_reply_never_reaches_history() {
    let p = Scripted::new(vec![calls(&format!("here: {KEY}"), vec![call("1", "write_file")])]);
    let fe = Fe::new(vec![]);
    let (out, msgs, _) = run_with(&p, &fe, &tools(), &mut secret_cfg()).await;
    assert!(out.cancelled);
    assert_eq!(msgs.last().unwrap()["content"], "here: <secret>");
    assert_eq!(msgs.last().unwrap()["calls"], 0);
}

#[tokio::test]
async fn always_allowlists_the_value_for_the_rest_of_the_run() {
    let tb = ToolBox::new(vec![std::sync::Arc::new(Leak)]);
    let p = Scripted::new(vec![calls("", vec![call("1", "read_file")]), calls("fine", vec![])]);
    let fe = Fe::new(vec![]);
    fe.secret.lock().unwrap().push_back(SecretChoice::Always);
    let mut c = secret_cfg();
    run_with(&p, &fe, &tb, &mut c).await;
    assert!(c.secret_allowlist.contains(&secrets::hash_value(KEY)));
}

#[tokio::test]
async fn a_command_with_a_secret_only_gets_a_notice_and_the_switch_turns_it_all_off() {
    let tb = ToolBox::new(vec![std::sync::Arc::new(Echo { name: "run_command", gated: true })]);
    let c = ToolCall { id: "c".into(), name: "run_command".into(),
        arguments: json!({"command": format!("curl -H 'Authorization: Bearer {KEY}' x")}).as_object().unwrap().clone() };
    let p = Scripted::new(vec![calls("", vec![c.clone()]), calls("ok", vec![])]);
    let fe = Fe::new(vec![Approval::Once]);
    let (_, _, _) = run_with(&p, &fe, &tb, &mut secret_cfg()).await;
    assert!(fe.notices().iter().any(|n| n.starts_with("possible secret in this command") && n.contains("GitHub token")));

    let p = Scripted::new(vec![calls(&format!("k {KEY}"), vec![]) ]);
    let fe = Fe::new(vec![]);
    let (out, _, _) = run_with(&p, &fe, &tb, &mut cfg(false)).await;
    assert!(!out.cancelled && fe.challenges.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_secret_in_the_prompt_stops_before_anything_is_logged_or_sent() {
    let p = Scripted::new(vec![]);
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    let tb = tools();
    let agent = Agent { provider: &p, tools: &tb, policy: &AskEveryTime, frontend: &fe, log: &log };
    let mut msgs = Vec::new();
    let out = agent.send(&mut secret_cfg(), &mut msgs, &format!("does {KEY} still work?")).await;
    assert!(out.stopped && msgs.is_empty() && log.records.lock().unwrap().is_empty());
    assert!(fe.notices().contains(&"stopped: secret detected — prompt not sent".to_string()));
    fe.secret.lock().unwrap().push_back(SecretChoice::Redact);
    let p = Scripted::new(vec![calls("ok", vec![])]);
    let agent = Agent { provider: &p, tools: &tb, policy: &AskEveryTime, frontend: &fe, log: &log };
    agent.send(&mut secret_cfg(), &mut msgs, &format!("does {KEY} still work?")).await;
    assert_eq!(msgs[0]["content"], "does <secret> still work?");
    assert_eq!(log.records.lock().unwrap()[0].1["text"], "does <secret> still work?");
}

/// A tool whose output changes every call (the file changed between two identical calls).
struct Counter(std::sync::atomic::AtomicU32);

#[async_trait]
impl Tool for Counter {
    fn spec(&self) -> ToolSpec {
        ToolSpec { name: "read_file".into(), description: String::new(), parameters: json!({"type": "object"}) }
    }
    fn needs_approval(&self, _: &Args) -> bool {
        false
    }
    fn parallel_safe(&self) -> bool {
        true
    }
    async fn run(&self, _args: &Args, _c: &CancellationToken) -> String {
        format!("output {}", self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst))
    }
}

async fn repeat_run(tb: &ToolBox, rounds: usize) -> (Vec<Value>, Vec<String>) {
    let mut replies: Vec<_> = (0..rounds).map(|i| calls("", vec![call(&i.to_string(), "read_file")])).collect();
    replies.push(calls("done", vec![]));
    let p = Scripted::new(replies);
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    let mut msgs = Vec::new();
    run_turn(&p, tb, &AskEveryTime, &fe, &log, &mut cfg(false), &mut msgs, "go").await;
    let notes = fe.notices();
    (msgs, notes)
}

#[tokio::test]
async fn the_repeat_nudge_needs_the_same_call_and_the_same_result() {
    // R27: same call, same result: told so
    let (msgs, _) = repeat_run(&tools(), 2).await;
    let tool_out: Vec<String> = msgs.iter().filter(|m| m["role"] == "tool").map(|m| m["content"].as_str().unwrap().to_string()).collect();
    assert!(!tool_out[0].contains("you already ran this exact call"));
    assert!(tool_out[1].contains("you already ran this exact call"));
    // same call, changed result: never told
    let changing = ToolBox::new(vec![std::sync::Arc::new(Counter(Default::default()))]);
    let (msgs, _) = repeat_run(&changing, 2).await;
    assert!(msgs.iter().all(|m| !m["content"].as_str().unwrap_or("").contains("you already ran")));
}

#[tokio::test]
async fn a_model_that_keeps_repeating_is_stopped() {
    // R350: REPEAT_ROUNDS_STOP rounds in a row that only repeat the last one end the turn
    let (msgs, notes) = repeat_run(&tools(), 6).await;
    assert!(notes.iter().any(|n| n == "stopped: the model keeps repeating the same call"), "{notes:?}");
    assert_eq!(msgs.iter().filter(|m| m["role"] == "tool").count(), 4);
}

#[test]
fn has_number_matches_whole_numbers_only() {
    assert!(has_number("local HTTP 429: slow down", "429"));
    assert!(has_number("429", "429"));
    assert!(has_number("port 4291 then 429.", "429"));
    assert!(!has_number("connect to 10.0.0.1:4291 refused", "429"));
    assert!(!has_number("id 14290", "429"));
}

#[tokio::test]
async fn a_cancelled_summary_folds_nothing() {
    // R562: the fold's summary request sees the turn's cancel; when it is cancelled the history stays as it was
    let p = Scripted::new(vec![Ok(TurnResult { text: "partial sum".into(), stop_reason: "cancelled".into(), ..Default::default() })]);
    let fe = Fe::new(vec![]);
    fe.cancel.cancel();
    let log = MemoryLog::default();
    let tb = tools();
    let agent = Agent { provider: &p, tools: &tb, policy: &AskEveryTime, frontend: &fe, log: &log };
    let mut c = cfg(false);
    let mut msgs: Vec<Value> = (0..6).map(|i| json!({"role": if i % 2 == 0 { "user" } else { "assistant" }, "content": "x".repeat(400)})).collect();
    let before = msgs.clone();
    assert_eq!(agent.compact_history(&mut c, &mut msgs, 0).await, 0);
    assert_eq!(msgs, before);
    assert_eq!(c.compaction.count, 0);
}

#[tokio::test]
async fn an_interrupted_summary_is_not_kept_as_the_summary() {
    let p = Scripted::new(vec![Ok(TurnResult { text: "half\n[stream interrupted: ReadError — partial answer kept]".into(),
        stop_reason: "interrupted".into(), ..Default::default() })]);
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    let tb = tools();
    let agent = Agent { provider: &p, tools: &tb, policy: &AskEveryTime, frontend: &fe, log: &log };
    let mut c = cfg(false);
    let mut msgs = vec![json!({"role": "user", "content": "hello there"}), json!({"role": "assistant", "content": "hi"})];
    assert_eq!(agent.compact_history(&mut c, &mut msgs, 0).await, 2);
    let body = msgs[0]["content"].as_str().unwrap();
    assert!(!body.contains("stream interrupted"), "{body}");
    assert!(body.contains("hello there"));
}

struct Thinker;

#[async_trait]
impl Provider for Thinker {
    fn name(&self) -> &str {
        "thinker"
    }
    async fn turn(&self, _r: TurnRequest<'_>, sink: &(dyn for<'c> Fn(StreamChunk<'c>) + Send + Sync), _c: &CancellationToken) -> Result<TurnResult, ProviderError> {
        sink(StreamChunk::Think("hmm"));
        tokio::time::sleep(std::time::Duration::from_millis(120)).await;
        sink(StreamChunk::Text("ok"));
        Ok(TurnResult { text: "ok".into(), output_tokens: 50, ..Default::default() })
    }
    fn assistant_message(&self, r: &TurnResult) -> Value {
        json!({"role": "assistant", "content": r.text})
    }
    fn tool_result_message(&self, c: &ToolCall, out: &str) -> Value {
        json!({"role": "tool", "tool_call_id": c.id, "content": out})
    }
}

#[tokio::test]
async fn tokens_per_second_counts_from_the_first_thought() {
    // R563: the reasoning time is part of the generation time
    let fe = Fe::new(vec![]);
    let log = MemoryLog::default();
    let tb = tools();
    let mut msgs = Vec::new();
    let t = run_turn(&Thinker, &tb, &AskEveryTime, &fe, &log, &mut cfg(false), &mut msgs, "q").await;
    assert_eq!(t.gen_tokens, 50);
    assert!(t.gen_secs >= 0.1, "{}", t.gen_secs);
}
