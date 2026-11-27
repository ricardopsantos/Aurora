//! The agent loop (Python `agent.run_turn` + the logging half of
//! `Engine.send`): request → stream → gate each tool call → run → feed the
//! results back, until the model answers without tools.

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use futures_util::future::join_all;
use serde_json::{json, Map, Value};

use crate::approval::ApprovalPolicy;
use crate::events::AgentEvent;
use crate::frontend::{Approval, Continue, Frontend, SecretChoice};
use crate::secrets;
use crate::provider::{Provider, ProviderError, StreamChunk, TurnRequest};
use crate::session::SessionLog;
use crate::text::head;
use crate::tool::{result_status, ToolBox};
use crate::types::{Args, ToolCall, ToolSpec, TurnResult};

/// What the session log keeps of a tool result or a string argument (R133b).
const LOG_CLIP: usize = 4000;

pub struct AgentConfig {
    pub model: String,
    /// This turn runs without tools (the model entry's `tools: false`, as the front end sees it).
    pub no_tools: bool,
    pub system: String,
    /// Rounds before the front end is asked whether to keep going.
    pub max_iterations: u32,
    /// Skip the question for gated calls (`--yes`, `/auto-approve on`).
    /// Never bypasses the denylist (R262).
    pub auto_approve: bool,
    /// Run a round's leading read-only calls at once (R94).
    pub parallel_tools: bool,
    /// R58: scan prompts, replies, written content and tool output for secrets.
    pub redact_secrets: bool,
    /// `secrets::hash_value`s of confirmed false positives (never the values).
    pub secret_allowlist: HashSet<String>,
    /// Hashes of tool output the user chose to keep this session (R329).
    pub secret_session_keep: HashSet<String>,
    /// R118/R154: auto-compact settings and the live context gauge.
    pub compaction: Compaction,
    /// R47/R181: snapshots taken before every approved mutation, if any.
    pub checkpoints: Option<std::sync::Arc<dyn Checkpointer>>,
    /// The prompt of the turn in progress (labels the checkpoints).
    pub turn_prompt: String,
}

/// R47/R181/R273: called right before an approved gated call runs. Must never fail the turn.
pub trait Checkpointer: Send + Sync {
    fn before_mutation(&self, tool: &str, args: &Args, prompt: &str);
}

/// Python's `auto_compact*` settings plus `Engine._used` / `compactions`.
#[derive(Debug, Clone)]
pub struct Compaction {
    pub auto: bool,
    pub threshold_pct: f64,
    pub keep_recent_tokens: u64,
    /// The model's context window; 0 = unknown, and then nothing auto-folds.
    pub context_limit: u64,
    /// Tokens in the context now: the last request's prompt + reply, plus estimates of tool output since.
    pub used: u64,
    /// Folds in this session (R159).
    pub count: u32,
}

impl Default for Compaction {
    fn default() -> Self {
        Self { auto: true, threshold_pct: 80.0, keep_recent_tokens: 20_000, context_limit: 0, used: 0, count: 0 }
    }
}

impl Compaction {
    pub fn pct(&self) -> f64 {
        if self.context_limit == 0 { 0.0 } else { self.used as f64 * 100.0 / self.context_limit as f64 }
    }
}

/// One turn's totals (Python `agent.TurnStats`).
#[derive(Debug, Default, Clone, PartialEq)]
pub struct TurnOutcome {
    /// The last request's prompt size.
    pub input_tokens: u64,
    pub output_tokens: u64,
    /// The last reply's size: what occupies the context (the gauge), not the sum.
    pub last_output_tokens: u64,
    /// Sum of every request's prompt tokens: what was billed (R92).
    pub billed_input: u64,
    pub cached_input: u64,
    pub reasoning_tokens: u64,
    pub reasoning_chars: u64,
    pub iterations: u32,
    pub last_request_latency: f64,
    /// R352: output tokens and seconds from each request's first streamed text to its end (the tokens/s display).
    pub gen_tokens: u64,
    pub gen_secs: f64,
    pub cancelled: bool,
    /// The user stopped the turn (approval gate or iteration cap).
    pub stopped: bool,
    /// Tools were dropped after repeated malformed calls (R5).
    pub degraded: bool,
    pub error: Option<String>,
}

pub struct Agent<'a> {
    pub provider: &'a dyn Provider,
    pub tools: &'a ToolBox,
    pub policy: &'a dyn ApprovalPolicy,
    pub frontend: &'a dyn Frontend,
    pub log: &'a dyn SessionLog,
}

fn notice(fe: &dyn Frontend, message: impl Into<String>) {
    fe.event(AgentEvent::Notice { message: message.into() });
}

fn record(pairs: Vec<(&str, Value)>) -> Map<String, Value> {
    pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

fn clip_args(args: &Args) -> Value {
    Value::Object(
        args.iter()
            .map(|(k, v)| match v {
                Value::String(s) => (k.clone(), Value::String(head(s, LOG_CLIP).to_string())),
                _ => (k.clone(), v.clone()),
            })
            .collect(),
    )
}

/// The last assistant message's text (Python `engine._assistant_text`).
pub fn assistant_text(msg: &Value) -> String {
    match msg.get("content") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(blocks)) => blocks
            .iter()
            .filter(|b| b.get("type").and_then(Value::as_str) == Some("text"))
            .filter_map(|b| b.get("text").and_then(Value::as_str))
            .collect(),
        _ => String::new(),
    }
}

impl Agent<'_> {
    /// One user message, start to finish: log it, run the loop, log the
    /// answer with the turn's token totals. Same records as Python's
    /// `Engine.send`, so either Aurora can read the other's sessions.
    pub async fn send(&self, cfg: &mut AgentConfig, messages: &mut Vec<Value>, user_text: &str) -> TurnOutcome {
        let user_text = match self.guard(cfg, "prompt", user_text).await {
            Guarded::Same => user_text.to_string(),
            Guarded::Redacted(t) => t,
            Guarded::Stop(_) => {
                notice(self.frontend, "stopped: secret detected — prompt not sent");
                return TurnOutcome { stopped: true, ..TurnOutcome::default() };
            }
        };
        let user_text = user_text.as_str();
        // R118/R180: fold before the new prompt joins, while history still ends on an assistant message
        self.maybe_compact(cfg, messages, false).await;
        cfg.turn_prompt = user_text.to_string();
        messages.push(json!({"role": "user", "content": user_text}));
        self.log.log("user", record(vec![("text", json!(user_text)), ("model", json!(cfg.model))]));
        let turn = self.run_turn(cfg, messages).await;
        let last = messages.iter().rev().find(|m| m.get("role").and_then(Value::as_str) == Some("assistant"));
        let text = last.map(assistant_text).unwrap_or_default();
        self.log.log(
            "assistant",
            record(vec![
                ("text", json!(text)),
                ("model", json!(cfg.model)),
                ("input_tokens", json!(turn.input_tokens)),
                ("output_tokens", json!(turn.output_tokens)),
                ("billed_input", json!(turn.billed_input)),
                ("cached_input", json!(turn.cached_input)),
                ("reasoning_tokens", json!(turn.reasoning_tokens)),
                ("reasoning_chars", json!(turn.reasoning_chars)),
                ("latency_s", json!((turn.last_request_latency * 1000.0).round() / 1000.0)),
                ("degraded", json!(turn.degraded)),
                ("extra_body_keys", json!([])),
            ]),
        );
        turn
    }

    /// R118 (before a new prompt) and R154 (mid-turn, at a round boundary): fold older
    /// history once the gauge crosses the threshold. Unknown limit = no fold.
    async fn maybe_compact(&self, cfg: &mut AgentConfig, messages: &mut Vec<Value>, mid_turn: bool) {
        let c = &cfg.compaction;
        if !c.auto || c.context_limit == 0 || c.pct() < c.threshold_pct {
            return;
        }
        let pct = c.pct();
        let folded = self.compact_history(cfg, messages, cfg.compaction.keep_recent_tokens).await;
        if folded > 0 {
            notice(
                self.frontend,
                if mid_turn {
                    format!("auto-compact: context was at {pct:.0}% mid-task — folded {folded} older message(s) and continued")
                } else {
                    format!("auto-compact: folded {folded} older message(s) — context was at {pct:.0}%")
                },
            );
        }
    }

    /// /compact (R14) and auto-compact (R118): summarize older history with the
    /// current model and carry only the summary; a clipped flatten is the
    /// fallback. `keep_recent_tokens = 0` folds everything. Returns messages folded.
    pub async fn compact_history(&self, cfg: &mut AgentConfig, messages: &mut Vec<Value>, keep_recent_tokens: u64) -> usize {
        use crate::compact;
        if messages.is_empty() {
            return 0;
        }
        let cut = if keep_recent_tokens > 0 { compact::cut_index(messages, keep_recent_tokens) } else { messages.len() };
        if cut == 0 {
            return 0;
        }
        let transcript = compact::flatten_history(&messages[..cut]);
        let budget = compact::summary_budget(cfg.compaction.context_limit, &cfg.system, &messages[cut..]);
        let ask = format!("{}{}", compact::SUMMARY_ASK, compact::clip_transcript(&transcript, budget));
        let req_msgs = [json!({"role": "user", "content": ask})];
        let req = TurnRequest { model: &cfg.model, messages: &req_msgs, system: "", tools: &[] };
        // R562: the summary request honours the turn's cancel (Esc during a fold used to wait for the whole
        // summary); a cancelled or interrupted summary folds nothing
        let cancel = self.frontend.cancel();
        let summary = match self.provider.turn(req, &|_| {}, cancel).await {
            Ok(r) if cancel.is_cancelled() || r.stop_reason == "cancelled" => return 0,
            Ok(r) if r.stop_reason == "interrupted" => String::new(),
            Ok(r) => r.text.trim().to_string(),
            Err(_) if cancel.is_cancelled() => return 0,
            Err(_) => String::new(),
        };
        let body = if summary.is_empty() {
            let flat = compact::flattened_as_user_message(&messages[..cut]);
            compact::clip_transcript(flat["content"].as_str().unwrap_or(""), budget)
        } else {
            format!("[Summary of the earlier conversation:]\n\n{summary}")
        };
        let kept = messages.len() - cut;
        compact::splice(messages, cut, body);
        let before = cfg.compaction.used;
        cfg.compaction.used = compact::estimate_tokens(&cfg.system)
            + messages.iter().map(|m| compact::estimate_tokens(&compact::content_str(m))).sum::<u64>();
        cfg.compaction.count += 1;
        self.log.log(
            "compact",
            record(vec![
                ("folded", json!(cut)),
                ("summarized", json!(!summary.is_empty())),
                ("kept_recent", json!(kept)),
                ("used_before", json!(before)),
                ("used_after", json!(cfg.compaction.used)),
            ]),
        );
        cut
    }

    /// R58: scan `text`; on a hit ask the front end what to do.
    async fn guard(&self, cfg: &mut AgentConfig, context: &str, text: &str) -> Guarded {
        if !cfg.redact_secrets || text.is_empty() {
            return Guarded::Same;
        }
        let tool = context.starts_with("tool:");
        let found = if tool {
            let allow: HashSet<String> = cfg.secret_allowlist.union(&cfg.secret_session_keep).cloned().collect();
            secrets::scan(text, &allow)
        } else {
            secrets::scan(text, &cfg.secret_allowlist)
        };
        if found.is_empty() {
            return Guarded::Same;
        }
        let mut choice = self.frontend.secret_challenge(context, &secrets::preview(&found, 5)).await;
        let hashes = || found.iter().map(|m| secrets::hash_value(&m.text));
        if choice == SecretChoice::Always {
            cfg.secret_allowlist.extend(hashes());
            notice(self.frontend, format!("allowlisted {} value(s) — won't be flagged again this run", found.len()));
            choice = SecretChoice::Keep;
        } else if choice == SecretChoice::Keep && tool {
            cfg.secret_session_keep.extend(hashes()); // R329: not asked again for the same output
        }
        match choice {
            SecretChoice::Stop => Guarded::Stop(secrets::redact(text, &found)),
            SecretChoice::Redact => Guarded::Redacted(secrets::redact(text, &found)),
            SecretChoice::Keep | SecretChoice::Always => Guarded::Same,
        }
    }

    async fn request(
        &self,
        cfg: &AgentConfig,
        messages: &[Value],
        specs: &[ToolSpec],
        turn: &mut TurnOutcome,
    ) -> Result<TurnResult, ProviderError> {
        let fe = self.frontend;
        let first: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
        let sink = |chunk: StreamChunk<'_>| match chunk {
            StreamChunk::Text(t) => {
                if !t.is_empty() {
                    first.get_or_init(Instant::now);
                }
                fe.event(AgentEvent::Text { chunk: t.to_string() })
            }
            StreamChunk::Think(t) => {
                // R563: a thinking model's output tokens include its reasoning, so the tokens/s clock starts there too
                if !t.is_empty() {
                    first.get_or_init(Instant::now);
                }
                fe.event(AgentEvent::Think { chunk: t.to_string() })
            }
            StreamChunk::Notice(t) => notice(fe, t),
        };
        let started = Instant::now();
        let req = TurnRequest { model: &cfg.model, messages, system: &cfg.system, tools: specs };
        let result = self.provider.turn(req, &sink, fe.cancel()).await;
        if let Ok(r) = &result {
            turn.last_request_latency = started.elapsed().as_secs_f64();
            if let (Some(t0), true) = (first.get(), r.output_tokens > 0) {
                turn.gen_tokens += r.output_tokens;
                turn.gen_secs += t0.elapsed().as_secs_f64();
            }
        }
        result
    }

    fn provider_error(&self, turn: &mut TurnOutcome, err: &ProviderError) {
        let msg = err.to_string();
        let lower = msg.to_lowercase();
        notice(self.frontend, format!("provider error: {msg}"));
        if lower.contains("exceed") && lower.contains("context") {
            notice(self.frontend, "context is full — /compact to summarize and continue, or /clear to start fresh");
        } else if has_number(&msg, "429") || (lower.contains("rate") && lower.contains("limit")) {
            notice(self.frontend, "rate-limited by the provider — try again shortly, or switch to another backend");
        }
        turn.error = Some(msg);
    }

    /// Python `agent.run_turn`. History stays valid on every exit: each tool
    /// call the model made gets exactly one result, run or not.
    pub async fn run_turn(&self, cfg: &mut AgentConfig, messages: &mut Vec<Value>) -> TurnOutcome {
        let fe = self.frontend;
        let mut turn = TurnOutcome::default();
        let mut specs = if cfg.no_tools { Vec::new() } else { self.tools.specs() };
        let mut checkpoint = cfg.max_iterations.max(1);
        let mut silent_continue = false;
        for n in self.policy.reload() {
            notice(fe, n);
        }
        let mut last_results: HashMap<String, String> = HashMap::new();
        let mut repeat_rounds = 0u32;
        if cfg.auto_approve && !self.policy.complete() {
            notice(fe, "auto-approve is off: this build cannot apply every denylist rule, so gated calls are asked");
        }
        loop {
            if fe.cancel().is_cancelled() {
                notice(fe, "interrupted");
                turn.cancelled = true;
                return turn;
            }
            if turn.iterations > 0 {
                self.maybe_compact(cfg, messages, true).await; // R154: at a round boundary, before the next request
            }
            turn.iterations += 1;
            let iteration = turn.iterations;
            let mut result = match self.request(cfg, messages, &specs, &mut turn).await {
                Ok(r) => r,
                Err(ProviderError::MalformedToolCall(e)) if !turn.degraded && !specs.is_empty() => {
                    notice(fe, format!("local model emitted a malformed tool call ({e}); retrying once"));
                    messages.push(json!({"role": "user", "content":
                        "Your previous tool call was malformed. Emit a single valid tool call, or answer in plain text."}));
                    let retry = self.request(cfg, messages, &specs, &mut turn).await;
                    messages.pop();
                    match retry {
                        Ok(r) => r,
                        Err(ProviderError::MalformedToolCall(_)) => {
                            notice(fe, "still malformed — dropping tools for this session (chat only)");
                            turn.degraded = true;
                            specs.clear();
                            continue;
                        }
                        Err(e) => {
                            self.provider_error(&mut turn, &e);
                            return turn;
                        }
                    }
                }
                Err(e) => {
                    self.provider_error(&mut turn, &e);
                    return turn;
                }
            };

            if result.input_tokens > 0 {
                turn.input_tokens = result.input_tokens;
                cfg.compaction.used = result.input_tokens + result.output_tokens; // R154: the live gauge
            }
            turn.billed_input += result.input_tokens;
            turn.output_tokens += result.output_tokens;
            if result.output_tokens > 0 {
                turn.last_output_tokens = result.output_tokens;
            }
            turn.cached_input += result.cached_input_tokens;
            turn.reasoning_tokens += result.reasoning_tokens;
            turn.reasoning_chars += result.reasoning_chars;
            fe.event(AgentEvent::Usage {
                input_tokens: result.input_tokens,
                output_tokens: result.output_tokens,
                cached_input_tokens: result.cached_input_tokens,
            });
            if result.stop_reason == "cancelled" {
                notice(fe, "interrupted");
                turn.cancelled = true;
                return turn;
            }
            // R58: the assistant's own text, then what it is about to write. A "redact" keeps the secret out
            // of history and the log; "stop" closes the turn with a valid, call-free assistant message.
            match self.guard(cfg, "reply", &result.text).await {
                Guarded::Same => {}
                Guarded::Redacted(t) => result.text = t,
                Guarded::Stop(t) => {
                    notice(fe, "stopped: secret detected in the assistant's reply");
                    result.text = t;
                    result.tool_calls.clear();
                    messages.push(self.provider.assistant_message(&result));
                    turn.cancelled = true;
                    return turn;
                }
            }
            for i in 0..result.tool_calls.len() {
                let name = result.tool_calls[i].name.clone();
                let Some(field) = write_field(&name) else { continue };
                let text = match result.tool_calls[i].arguments.get(field) {
                    Some(Value::String(t)) => t.clone(),
                    Some(Value::Null) | None => String::new(),
                    Some(other) => other.to_string(),
                };
                match self.guard(cfg, &format!("write:{name}"), &text).await {
                    Guarded::Same => {}
                    Guarded::Redacted(t) => {
                        result.tool_calls[i].arguments.insert(field.to_string(), Value::String(t));
                    }
                    Guarded::Stop(_) => {
                        notice(fe, format!("stopped: secret detected in a {name} argument"));
                        // the skip records below log each call's arguments: never the secret itself
                        for c in result.tool_calls.iter_mut() {
                            let Some(f) = write_field(&c.name) else { continue };
                            if let Some(Value::String(t)) = c.arguments.get(f) {
                                let hits = secrets::scan(t, &cfg.secret_allowlist);
                                let clean = secrets::redact(t, &hits);
                                c.arguments.insert(f.to_string(), Value::String(clean));
                            }
                        }
                        {
                            let mut r = Round { agent: self, calls: &result.tool_calls, out: Vec::new(), guidance: String::new(), last: HashMap::new(), this: HashMap::new() };
                            r.skip(0, "[skipped: secret detected — user stopped the turn]");
                        }
                        result.tool_calls.clear();
                        if result.text.is_empty() {
                            result.text = "[stopped: secret detected in a tool argument]".into();
                        }
                        messages.push(self.provider.assistant_message(&result));
                        turn.cancelled = true;
                        return turn;
                    }
                }
            }
            messages.push(self.provider.assistant_message(&result));
            if result.tool_calls.is_empty() {
                return turn;
            }

            let mut round = Round { agent: self, calls: &result.tool_calls, out: Vec::new(), guidance: String::new(),
                last: std::mem::take(&mut last_results), this: HashMap::new() };
            match fe.budget().await {
                Some((true, msg)) => {
                    notice(fe, msg);
                    round.skip(0, "[skipped: spend limit reached — the user must raise /budget to continue]");
                    round.flush(messages);
                    return turn;
                }
                Some((false, msg)) if !msg.is_empty() => notice(fe, msg),
                _ => {}
            }
            if !silent_continue && iteration >= checkpoint {
                match fe.ask_continue(iteration).await {
                    Continue::Stop => {
                        notice(fe, "stopped at iteration cap");
                        round.skip(0, "[skipped: user stopped at the iteration cap]");
                        round.flush(messages);
                        turn.stopped = true;
                        return turn;
                    }
                    Continue::Silent => {
                        silent_continue = true;
                        notice(fe, "continuing — won't ask again this turn");
                    }
                    Continue::Yes(guidance) => {
                        checkpoint = iteration + cfg.max_iterations.max(1);
                        round.guidance = guidance;
                    }
                }
            }
            if round.gate_and_run(cfg).await == RoundEnd::Stopped {
                round.flush(messages);
                turn.stopped = true;
                return turn;
            }
            let cancelled = fe.cancel().is_cancelled();
            // R154: tool output counts before the provider measures it on the next request
            let chars: usize = round.out.iter().map(|(_, o)| crate::text::char_len(o)).sum();
            cfg.compaction.used += (chars / crate::compact::CHARS_PER_TOKEN) as u64;
            // R27: a round that only repeated the last one, with the same results
            let repeated = !round.this.is_empty() && round.this.iter().all(|(k, v)| round.last.get(k) == Some(v));
            repeat_rounds = if repeated { repeat_rounds + 1 } else { 0 };
            last_results = std::mem::take(&mut round.this);
            round.flush(messages);
            if repeat_rounds >= REPEAT_ROUNDS_STOP {
                notice(fe, "stopped: the model keeps repeating the same call");
                return turn;
            }
            if cancelled {
                turn.cancelled = true;
                return turn;
            }
        }
    }
}

/// R564 (the Rust side of R540): `num` as a whole number in `text` (a port 4291 or an id 14290 is not a 429).
pub fn has_number(text: &str, num: &str) -> bool {
    text.match_indices(num).any(|(i, _)| {
        let before = text[..i].chars().next_back().is_none_or(|c| !c.is_ascii_digit());
        let after = text[i + num.len()..].chars().next().is_none_or(|c| !c.is_ascii_digit());
        before && after
    })
}

/// The one argument of a writing tool that carries new content (Python `_WRITE_ARG_FIELD`).
fn write_field(tool: &str) -> Option<&'static str> {
    match tool {
        "write_file" => Some("content"),
        "edit_file" => Some("new"),
        "apply_patch" => Some("diff"),
        _ => None,
    }
}

/// What a secret scan decided about one piece of text.
enum Guarded {
    Same,
    Redacted(String),
    /// Stop the turn; carries the text with the secrets removed.
    Stop(String),
}

#[derive(PartialEq)]
enum RoundEnd {
    Done,
    Stopped,
}

/// One round's tool calls and the results collected for them, in the
/// model's order (approvals and the transcript stay strictly sequential;
/// only the waiting of parallel reads overlaps).
struct Round<'r, 'a> {
    agent: &'r Agent<'a>,
    calls: &'r [ToolCall],
    out: Vec<(ToolCall, String)>,
    guidance: String,
    /// R27: last round's results by call, and this round's (without the note).
    last: HashMap<String, String>,
    this: HashMap<String, String>,
}

/// Python `REPEAT_ROUNDS_STOP`: this many rounds in a row that only repeat the last one end the turn (R27).
const REPEAT_ROUNDS_STOP: u32 = 3;
const REPEAT_NOTE: &str = "\n[note: you already ran this exact call with this exact result — do not repeat it; use the result above or give your final answer]";

fn call_key(call: &ToolCall) -> String {
    let sorted: std::collections::BTreeMap<&String, &Value> = call.arguments.iter().collect();
    format!("{}\u{0}{}", call.name, serde_json::to_string(&sorted).unwrap_or_default())
}

impl Round<'_, '_> {
    fn finish(&mut self, idx: usize, output: String) {
        self.answer(idx, output, true);
    }

    /// `with_args: false` for a call answered without being considered (Python `_skip` logs `args: {}`).
    fn answer(&mut self, idx: usize, mut output: String, with_args: bool) {
        let call = &self.calls[idx];
        let args = if with_args { call.arguments.clone() } else { Args::new() };
        if idx == self.calls.len() - 1 && !self.guidance.is_empty() {
            output.push_str(&format!("\n[user guidance: {}]", self.guidance));
        }
        self.agent.frontend.event(AgentEvent::ToolResult { name: call.name.clone(), output: output.clone(), args: args.clone() });
        self.agent.log.log(
            "tool",
            record(vec![
                ("name", json!(call.name)),
                ("output", json!(head(&output, LOG_CLIP))),
                ("args", clip_args(&args)),
                ("chars", json!(crate::text::char_len(&output))),
                ("status", json!(result_status(&output))),
            ]),
        );
        self.out.push((call.clone(), output));
    }

    /// Answer calls `from..` without running them (R134g: still logged).
    fn skip(&mut self, from: usize, reason: &str) {
        for idx in from..self.calls.len() {
            self.answer(idx, reason.to_string(), false);
        }
    }

    fn flush(&mut self, messages: &mut Vec<Value>) {
        for (call, output) in self.out.drain(..) {
            messages.push(self.agent.provider.tool_result_message(&call, &output));
        }
    }

    fn gate(&self, tool: &str, decision: &str, detail: &str) {
        let mut data = record(vec![("tool", json!(tool)), ("decision", json!(decision))]);
        if !detail.is_empty() {
            data.insert("detail".into(), json!(detail));
        }
        self.agent.log.log("approval", data);
    }

    async fn prefetch(&self, cfg: &AgentConfig) -> HashMap<usize, String> {
        let a = self.agent;
        if !cfg.parallel_tools {
            return HashMap::new();
        }
        let first_write = self.calls.iter().position(|c| !a.tools.parallel_safe(&c.name)).unwrap_or(self.calls.len());
        let batch: Vec<usize> = (0..first_write)
            .filter(|&i| !a.tools.needs_approval(&self.calls[i].name, &self.calls[i].arguments))
            .collect();
        if batch.len() < 2 {
            return HashMap::new();
        }
        for &i in &batch {
            let c = &self.calls[i];
            a.frontend.event(AgentEvent::ToolStart { name: c.name.clone(), args: c.arguments.clone() });
        }
        let cancel = a.frontend.cancel();
        let outs = join_all(batch.iter().map(|&i| a.tools.run(&self.calls[i].name, &self.calls[i].arguments, cancel))).await;
        batch.into_iter().zip(outs).collect()
    }

    async fn gate_and_run(&mut self, cfg: &mut AgentConfig) -> RoundEnd {
        let a = self.agent;
        let fe = a.frontend;
        let mut prefetched = self.prefetch(cfg).await;
        for idx in 0..self.calls.len() {
            let call = &self.calls[idx];
            if fe.cancel().is_cancelled() {
                notice(fe, "interrupted");
                self.skip(idx, "[skipped: interrupted]");
                return RoundEnd::Done;
            }
            let gated = a.tools.needs_approval(&call.name, &call.arguments);
            if gated && a.policy.is_denied(&call.name, &call.arguments) {
                notice(fe, format!("denied by policy: {}", call.name));
                self.gate(&call.name, "denied_policy", "");
                self.finish(idx, "[denied by policy]".into());
                continue;
            }
            let allowed = gated && a.policy.is_allowed(&call.name, &call.arguments);
            let auto = cfg.auto_approve && a.policy.complete();
            if gated && !allowed && !auto {
                let mut preview = a.tools.preview(&call.name, &call.arguments);
                let scope = a.policy.rule_scope(&call.name, &call.arguments);
                if !scope.is_empty() {
                    preview.push_str(&format!("\n[{scope}]"));
                }
                match fe.approve(&call.name, &call.arguments, &preview).await {
                    Approval::Once => self.gate(&call.name, "approved", ""),
                    Approval::SessionAllow => match a.policy.add_session(&call.name, &call.arguments) {
                        Some(rule) => {
                            notice(fe, format!("allowed for this session: {} · {rule}", call.name));
                            self.gate(&call.name, "session_allow", &rule);
                        }
                        None => self.gate(&call.name, "approved", ""),
                    },
                    Approval::Always => match a.policy.add_allow(&call.name, &call.arguments) {
                        Some(rule) => {
                            notice(fe, format!("always-allow added: {} · {rule}", call.name));
                            self.gate(&call.name, "always_allow", &rule);
                        }
                        None => {
                            notice(fe, "this build cannot store allow rules yet — allowed once");
                            self.gate(&call.name, "approved", "");
                        }
                    },
                    Approval::AlwaysDeny => {
                        let rule = a.policy.add_deny(&call.name, &call.arguments).unwrap_or_default();
                        if rule.is_empty() {
                            notice(fe, "this build cannot store deny rules yet — denied once");
                        } else {
                            notice(fe, format!("always-deny added: {} · {rule}", call.name));
                        }
                        self.gate(&call.name, "always_deny", &rule);
                        self.finish(idx, "[denied by policy]".into());
                        continue;
                    }
                    Approval::Stop => {
                        notice(fe, "stopped by user");
                        self.gate(&call.name, "stopped", "");
                        self.skip(idx, "[skipped: user stopped the turn]");
                        return RoundEnd::Stopped;
                    }
                    Approval::Steer(note) => {
                        self.gate(&call.name, "steered", &note);
                        self.finish(idx, format!("[not run — user guidance: {note}]"));
                        continue;
                    }
                    Approval::AutoApproveOn => {
                        cfg.auto_approve = true;
                        notice(fe, "auto-approve ON for this session");
                        self.gate(&call.name, "approved", "auto-approve turned on");
                    }
                    Approval::Deny(note) => {
                        self.gate(&call.name, "denied", &note);
                        let out = if note.is_empty() { "[denied by user]".to_string() } else { format!("[denied by user: {note}]") };
                        self.finish(idx, out);
                        continue;
                    }
                }
            } else if gated && !allowed {
                self.gate(&call.name, "auto_approved", "");
            } else if gated {
                self.gate(&call.name, "allowlisted", "");
            }
            if cfg.redact_secrets && matches!(call.name.as_str(), "run_command" | "wait_until") {
                // R58/R131: a notice only. The command needs its real value, and the approval gate already ran.
                let mut scanned = call.arguments.get("command").and_then(Value::as_str).unwrap_or("").to_string();
                if let Some(then) = call.arguments.get("then").and_then(Value::as_str).filter(|t| !t.is_empty()) {
                    scanned.push('\n');
                    scanned.push_str(then);
                }
                let found = secrets::scan(&scanned, &cfg.secret_allowlist);
                if !found.is_empty() {
                    notice(
                        fe,
                        format!(
                            "possible secret in this command (raw-text scan, easy to evade via encoding — not a guarantee): {}",
                            secrets::preview(&found, 5)
                        ),
                    );
                }
            }
            if gated {
                if let Some(cp) = &cfg.checkpoints {
                    cp.before_mutation(&call.name, &call.arguments, &cfg.turn_prompt);
                }
            }
            let output = match prefetched.remove(&idx) {
                Some(out) => out,
                None => {
                    fe.event(AgentEvent::ToolStart { name: call.name.clone(), args: call.arguments.clone() });
                    a.tools.run(&call.name, &call.arguments, fe.cancel()).await
                }
            };
            // R58: every tool's output is scanned before the model, the screen or the log sees it
            let output = match a.guard(cfg, &format!("tool:{}", call.name), &output).await {
                Guarded::Same => output,
                Guarded::Redacted(t) => t,
                Guarded::Stop(_) => {
                    notice(fe, "stopped: secret detected in tool output");
                    self.skip(idx, "[skipped: secret detected — user stopped the turn]");
                    return RoundEnd::Stopped;
                }
            };
            // R27: the same call with the same result as last round is told so (keyed on the result too)
            let key = call_key(call);
            let shown = if self.last.get(&key) == Some(&output) { format!("{output}{REPEAT_NOTE}") } else { output.clone() };
            self.this.insert(key, output);
            self.finish(idx, shown);
        }
        RoundEnd::Done
    }
}

/// Convenience wrapper: build an `Agent` and run one message.
#[allow(clippy::too_many_arguments)]
pub async fn run_turn(
    provider: &dyn Provider,
    tools: &ToolBox,
    policy: &dyn ApprovalPolicy,
    frontend: &dyn Frontend,
    log: &dyn SessionLog,
    cfg: &mut AgentConfig,
    messages: &mut Vec<Value>,
    user_text: &str,
) -> TurnOutcome {
    Agent { provider, tools, policy, frontend, log }.send(cfg, messages, user_text).await
}

#[cfg(test)]
mod tests;
