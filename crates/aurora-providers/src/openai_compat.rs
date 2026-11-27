//! The OpenAI-compatible chat-completions client (Python
//! `providers/openai_compat.py`): streaming, tool calls, reasoning, usage,
//! endpoint fail-over, retries, 429 backoff, cancellation.

use std::collections::{BTreeMap, HashMap};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use aurora_core::{Provider, ProviderError, StreamChunk, ToolCall, ToolSpec, TurnRequest, TurnResult};
use futures_util::StreamExt;
use serde_json::{json, Map, Value};
use tokio_util::sync::CancellationToken;

use crate::net::{is_bare_private_ip, is_lan_host};
use crate::sse::{data_of, LineBuffer};

/// Below this the system prompt is not worth a cache breakpoint.
const CACHE_MIN_CHARS: usize = 4 * 1024;
/// How long a working endpoint is trusted without a new probe.
const ENDPOINT_TTL: Duration = Duration::from_secs(10);
/// R290: an endpoint that failed its probe is skipped this long.
const DEAD_URL_BACKOFF: Duration = Duration::from_secs(120);
const ATTEMPTS: usize = 3;
const RATE_LIMIT_BACKOFF: [f64; 2] = [1.0, 3.0];
const RATE_LIMIT_BACKOFF_CAP: f64 = 30.0;
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Default)]
struct Endpoints {
    working: Option<String>,
    working_at: Option<Instant>,
    dead_until: HashMap<String, Instant>,
}

pub struct OpenAiCompat {
    name: String,
    base_urls: Vec<String>,
    api_key: String,
    ollama: bool,
    /// Per-read timeout (Python's httpx timeout), not a whole-request limit.
    timeout: Duration,
    /// Mark the system prompt as a cache breakpoint (R91).
    pub cache_prompt: bool,
    pub extra_body: Map<String, Value>,
    clients: Mutex<HashMap<String, reqwest::Client>>,
    endpoints: Mutex<Endpoints>,
}

fn urls(raw: Option<&Value>) -> Vec<String> {
    let clean = |s: &str| s.trim_end_matches('/').to_string();
    match raw {
        Some(Value::Array(items)) => items.iter().filter_map(Value::as_str).filter(|s| !s.is_empty()).map(clean).collect(),
        Some(Value::String(s)) if !s.is_empty() => vec![clean(s)],
        _ => vec![],
    }
}

/// What a failed attempt was, for the retry decision and the user's notice.
enum Failure {
    /// The server answered 429; `Retry-After` when it sent one.
    RateLimited(String, Option<f64>),
    /// Connection-level trouble worth another attempt.
    Transport(&'static str, String),
    Fatal(ProviderError),
}

fn transport_kind(e: &reqwest::Error) -> &'static str {
    if e.is_connect() {
        "ConnectError"
    } else if e.is_timeout() {
        "ReadTimeout"
    } else if e.is_body() || e.is_decode() {
        "ReadError"
    } else {
        "RequestError"
    }
}

/// The error and every cause under it ("error sending request: ...: Connection refused"), so the front end can say why.
fn chain(e: &reqwest::Error) -> String {
    let mut out = e.to_string();
    let mut src = std::error::Error::source(e);
    while let Some(c) = src {
        let t = c.to_string();
        if !out.contains(&t) {
            out = format!("{out}: {t}");
        }
        src = c.source();
    }
    out
}

fn jitter() -> f64 {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
    f64::from(nanos % 200) / 1000.0
}

/// R381: a user message carrying `images` goes out as content parts.
fn with_image_parts(m: &Value) -> Value {
    let Some(imgs) = m.get("images").and_then(Value::as_array).filter(|a| !a.is_empty()) else {
        return m.clone();
    };
    let mut out: Map<String, Value> = m.as_object().cloned().unwrap_or_default();
    out.remove("images");
    let text = m.get("content").and_then(Value::as_str).unwrap_or_default();
    let mut parts = vec![json!({"type": "text", "text": text})];
    parts.extend(imgs.iter().map(|u| json!({"type": "image_url", "image_url": {"url": u}})));
    out.insert("content".into(), Value::Array(parts));
    Value::Object(out)
}

fn system_message(system: &str, cache: bool) -> Value {
    if !cache || system.chars().count() < CACHE_MIN_CHARS {
        return json!({"role": "system", "content": system});
    }
    json!({"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]})
}

fn to_openai_tools(specs: &[ToolSpec]) -> Value {
    Value::Array(
        specs
            .iter()
            .map(|s| json!({"type": "function", "function": {"name": s.name, "description": s.description, "parameters": s.parameters}}))
            .collect(),
    )
}

#[derive(Default)]
struct PendingCall {
    id: String,
    name: String,
    args: String,
}

fn u64_at(v: &Value, path: &[&str]) -> u64 {
    path.iter().try_fold(v, |acc, k| acc.get(k)).and_then(Value::as_u64).unwrap_or(0)
}

impl OpenAiCompat {
    /// `pcfg` is the provider's entry under `providers:` in config.yaml.
    pub fn new(name: &str, pcfg: &Value, api_key: String, timeout: Duration) -> Self {
        let ollama = pcfg.get("type").and_then(Value::as_str) == Some("ollama");
        let api_key = if api_key.is_empty() {
            pcfg.get("api_key").and_then(Value::as_str).unwrap_or_default().to_string()
        } else {
            api_key
        };
        Self {
            name: name.to_string(),
            base_urls: urls(pcfg.get("base_url")),
            api_key,
            ollama,
            timeout,
            cache_prompt: false,
            extra_body: Map::new(),
            clients: Mutex::new(HashMap::new()),
            endpoints: Mutex::new(Endpoints::default()),
        }
    }

    /// One pooled client per endpoint: a multi-tool turn makes many requests.
    fn client_for(&self, base: &str) -> reqwest::Client {
        let mut clients = self.clients.lock().expect("clients lock");
        if let Some(c) = clients.get(base) {
            return c.clone();
        }
        let lan = is_lan_host(base);
        let connect = if lan { 5 } else { 20 };
        let mut builder = reqwest::Client::builder();
        if lan {
            // R532: a local or LAN model is never sent through an environment `*_proxy` (Python's client never uses
            // one); a public provider still may, for a machine that can only reach the internet through it
            builder = builder.no_proxy();
        }
        let client = builder
            .connect_timeout(Duration::from_secs(connect))
            .read_timeout(self.timeout)
            .tls_danger_accept_invalid_certs(is_bare_private_ip(base))
            .build()
            .unwrap_or_default();
        clients.insert(base.to_string(), client.clone());
        client
    }

    /// R245: `api_key: none` is the keyless-server convention, not a token.
    fn auth(&self, req: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        if self.api_key.is_empty() || self.api_key == "none" {
            req
        } else {
            req.bearer_auth(&self.api_key)
        }
    }

    /// Public endpoints are assumed up; a local one must answer quickly.
    /// llama.cpp answers `/props`, Ollama a plain GET on its root (R223).
    async fn probe(&self, url: &str) -> bool {
        if !is_lan_host(url) {
            return true;
        }
        let base = url.strip_suffix("/v1").unwrap_or(url);
        let path = if self.ollama { "/" } else { "/props" };
        let req = self.client_for(url).get(format!("{base}{path}")).timeout(PROBE_TIMEOUT);
        matches!(self.auth(req).send().await, Ok(r) if r.status().is_success())
    }

    /// The first configured endpoint that answers, cached briefly; the first
    /// URL when none does, so the error names a real address.
    pub async fn pick_endpoint(&self) -> String {
        if self.base_urls.is_empty() {
            return String::new();
        }
        {
            let ep = self.endpoints.lock().expect("endpoint lock");
            if let (Some(url), Some(at)) = (&ep.working, ep.working_at) {
                if at.elapsed() < ENDPOINT_TTL {
                    return url.clone();
                }
            }
        }
        let now = Instant::now();
        let candidates: Vec<String> = {
            let ep = self.endpoints.lock().expect("endpoint lock");
            let live: Vec<String> =
                self.base_urls.iter().filter(|u| ep.dead_until.get(*u).is_none_or(|t| *t <= now)).cloned().collect();
            if live.is_empty() { self.base_urls.clone() } else { live }
        };
        for url in candidates {
            let up = self.probe(&url).await;
            let mut ep = self.endpoints.lock().expect("endpoint lock");
            if up {
                ep.dead_until.remove(&url);
                ep.working = Some(url.clone());
                ep.working_at = Some(Instant::now());
                return url;
            }
            ep.dead_until.insert(url, Instant::now() + DEAD_URL_BACKOFF);
        }
        let mut ep = self.endpoints.lock().expect("endpoint lock");
        ep.working = Some(self.base_urls[0].clone());
        ep.working_at = None;
        self.base_urls[0].clone()
    }

    fn forget_endpoint(&self) {
        self.endpoints.lock().expect("endpoint lock").working_at = None;
    }

    fn payload(&self, req: &TurnRequest<'_>) -> Value {
        let mut msgs = Vec::with_capacity(req.messages.len() + 1);
        if !req.system.is_empty() {
            msgs.push(system_message(req.system, self.cache_prompt));
        }
        msgs.extend(req.messages.iter().map(with_image_parts));
        let mut p = json!({"model": req.model, "messages": msgs, "stream": true, "stream_options": {"include_usage": true}});
        if !req.tools.is_empty() {
            p["tools"] = to_openai_tools(req.tools);
        }
        for (k, v) in &self.extra_body {
            p[k] = v.clone();
        }
        p
    }

    /// One HTTP attempt, streamed into `result`. Returns Ok when the stream
    /// ended (or was cancelled); Err says whether another attempt makes sense.
    async fn attempt(
        &self,
        base: &str,
        payload: &Value,
        result: &mut TurnResult,
        pending: &mut BTreeMap<u64, PendingCall>,
        sink: &(dyn for<'c> Fn(StreamChunk<'c>) + Send + Sync),
        cancel: &CancellationToken,
    ) -> Result<(), Failure> {
        let req = self.auth(self.client_for(base).post(format!("{base}/chat/completions")).json(payload));
        let resp = tokio::select! {
            _ = cancel.cancelled() => return Ok(()),
            r = req.send() => r.map_err(|e| Failure::Transport(transport_kind(&e), chain(&e)))?,
        };
        let status = resp.status().as_u16();
        if status >= 400 {
            let retry_after = resp
                .headers()
                .get("retry-after")
                .and_then(|v| v.to_str().ok())
                .and_then(|s| s.trim().parse::<f64>().ok());
            let body: String = resp.text().await.unwrap_or_default().chars().take(300).collect();
            if status == 429 {
                return Err(Failure::RateLimited(body, retry_after));
            }
            if body.to_lowercase().contains("parse") {
                return Err(Failure::Fatal(ProviderError::MalformedToolCall(body)));
            }
            return Err(Failure::Fatal(ProviderError::Failed(format!("{} HTTP {status}: {body}", self.name))));
        }
        let mut stream = resp.bytes_stream();
        let mut lines = LineBuffer::default();
        loop {
            let chunk = tokio::select! {
                _ = cancel.cancelled() => return Ok(()),
                c = stream.next() => c,
            };
            let (batch, ended): (Vec<String>, bool) = match chunk {
                None => (lines.finish().into_iter().collect(), true),
                Some(Ok(bytes)) => (lines.push(&bytes), false),
                Some(Err(e)) => return Err(Failure::Transport(transport_kind(&e), chain(&e))),
            };
            for line in &batch {
                let Some(data) = data_of(line) else { continue };
                if data == "[DONE]" {
                    return Ok(());
                }
                // one garbled SSE line must not kill the stream
                let Ok(v) = serde_json::from_str::<Value>(data) else { continue };
                apply_chunk(&v, result, pending, sink);
            }
            if ended {
                return Ok(());
            }
        }
    }
}

fn apply_chunk(
    v: &Value,
    result: &mut TurnResult,
    pending: &mut BTreeMap<u64, PendingCall>,
    sink: &(dyn for<'c> Fn(StreamChunk<'c>) + Send + Sync),
) {
    if let Some(usage) = v.get("usage").filter(|u| u.is_object()) {
        let count = |path: &[&str]| u64_at(usage, path);
        (result.input_tokens, result.output_tokens, result.cached_input_tokens, result.reasoning_tokens) = (
            count(&["prompt_tokens"]),
            count(&["completion_tokens"]),
            count(&["prompt_tokens_details", "cached_tokens"]),
            count(&["completion_tokens_details", "reasoning_tokens"]),
        );
    }
    let Some(ch) = v.get("choices").and_then(Value::as_array).and_then(|c| c.first()) else { return };
    if let Some(fr) = ch.get("finish_reason").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        result.stop_reason = fr.to_string();
    }
    let Some(delta) = ch.get("delta") else { return };
    if let Some(rc) = delta.get("reasoning_content").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        result.reasoning_chars += rc.chars().count() as u64;
        sink(StreamChunk::Think(rc));
    }
    if let Some(text) = delta.get("content").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        result.text.push_str(text);
        sink(StreamChunk::Text(text));
    }
    for tc in delta.get("tool_calls").and_then(Value::as_array).into_iter().flatten() {
        let slot = pending.entry(call_index(tc, pending)).or_default();
        if let Some(id) = tc.get("id").and_then(Value::as_str).filter(|s| !s.is_empty()) {
            slot.id = id.to_string();
        }
        if let Some(f) = tc.get("function") {
            if let Some(n) = f.get("name").and_then(Value::as_str) {
                slot.name.push_str(n);
            }
            if let Some(a) = f.get("arguments").and_then(Value::as_str) {
                slot.args.push_str(a);
            }
        }
    }
}

/// R560: the slot a tool-call delta belongs to. Some servers send no `index`; then a delta with a new `id` is a new
/// call (two calls used to be glued into one, with unparseable arguments), and one without an id continues the last.
fn call_index(tc: &Value, pending: &BTreeMap<u64, PendingCall>) -> u64 {
    if let Some(i) = tc.get("index").and_then(Value::as_u64) {
        return i;
    }
    let last = pending.iter().next_back();
    match (tc.get("id").and_then(Value::as_str).filter(|s| !s.is_empty()), last) {
        (_, None) => 0,
        (Some(id), Some((&i, slot))) if !slot.id.is_empty() && slot.id != id => i + 1,
        (_, Some((&i, _))) => i,
    }
}

/// Turn the collected fragments into calls (R268: arguments must be an object).
fn finish_calls(pending: BTreeMap<u64, PendingCall>) -> Result<Vec<ToolCall>, ProviderError> {
    let mut out = Vec::new();
    for (i, slot) in pending {
        let raw = if slot.args.is_empty() { "{}".to_string() } else { slot.args };
        let clip: String = raw.chars().take(200).collect();
        let name_or = if slot.name.is_empty() { "?" } else { &slot.name };
        let parsed: Value = serde_json::from_str(&raw)
            .map_err(|_| ProviderError::MalformedToolCall(format!("unparseable tool arguments for {name_or}: {clip}")))?;
        if slot.name.is_empty() {
            return Err(ProviderError::MalformedToolCall(format!("tool call with no name: {clip}")));
        }
        let Value::Object(arguments) = parsed else {
            return Err(ProviderError::MalformedToolCall(format!(
                "tool arguments for {} are not a JSON object: {clip}",
                slot.name
            )));
        };
        let id = if slot.id.is_empty() { format!("call_{i}") } else { slot.id };
        out.push(ToolCall { id, name: slot.name, arguments });
    }
    Ok(out)
}

/// Sleep unless cancelled first; true when cancelled.
async fn sleep_or_cancel(secs: f64, cancel: &CancellationToken) -> bool {
    tokio::select! {
        _ = cancel.cancelled() => true,
        _ = tokio::time::sleep(Duration::from_secs_f64(secs.max(0.0))) => false,
    }
}

#[async_trait]
impl Provider for OpenAiCompat {
    fn name(&self) -> &str {
        &self.name
    }

    async fn turn(
        &self,
        request: TurnRequest<'_>,
        sink: &(dyn for<'c> Fn(StreamChunk<'c>) + Send + Sync),
        cancel: &CancellationToken,
    ) -> Result<TurnResult, ProviderError> {
        // Pinned for the whole request and its retries.
        let base = self.pick_endpoint().await;
        let payload = self.payload(&request);
        let mut result = TurnResult::default();
        let mut pending = BTreeMap::new();
        for attempt in 0..ATTEMPTS {
            result = TurnResult::default();
            pending.clear();
            match self.attempt(&base, &payload, &mut result, &mut pending, sink, cancel).await {
                Ok(()) => break,
                Err(Failure::Fatal(e)) => return Err(e),
                Err(failure) => {
                    let kind = match &failure {
                        Failure::RateLimited(..) => "RateLimited",
                        Failure::Transport(k, _) => k,
                        Failure::Fatal(_) => unreachable!(),
                    };
                    if !result.text.is_empty() {
                        // a partial answer is worth more than a retry that re-bills it
                        let note = format!("\n[stream interrupted: {kind} — partial answer kept]");
                        sink(StreamChunk::Text(&note));
                        result.text.push_str(&note);
                        result.stop_reason = "interrupted".into();
                        return Ok(result);
                    }
                    let last = attempt + 1 == ATTEMPTS;
                    match failure {
                        Failure::RateLimited(body, retry_after) => {
                            if last {
                                return Err(ProviderError::Failed(format!("{} rate-limited (429): {body}", self.name)));
                            }
                            let wait = retry_after.map_or(RATE_LIMIT_BACKOFF.get(attempt).copied().unwrap_or(RATE_LIMIT_BACKOFF_CAP), |r| r.min(RATE_LIMIT_BACKOFF_CAP));
                            sink(StreamChunk::Notice(&format!("rate-limited — waiting {wait:.0}s before retrying")));
                            if sleep_or_cancel(wait, cancel).await {
                                result.stop_reason = "cancelled".into();
                                return Ok(result);
                            }
                        }
                        Failure::Transport(kind, msg) => {
                            // R559: a read timeout is a server that is slow, not a stale connection: sending the whole
                            // prompt again (Python never does) made a stuck turn wait three times the timeout
                            if last || kind == "ReadTimeout" {
                                self.forget_endpoint();
                                return Err(ProviderError::Failed(format!("{} request failed ({kind}): {msg}", self.name)));
                            }
                            sink(StreamChunk::Notice(&format!("{kind} — retrying (attempt {}/{ATTEMPTS})", attempt + 2)));
                            let delay = 0.3 * (attempt as f64 + 1.0) + jitter();
                            if sleep_or_cancel(delay, cancel).await {
                                result.stop_reason = "cancelled".into();
                                return Ok(result);
                            }
                        }
                        Failure::Fatal(_) => unreachable!(),
                    }
                }
            }
        }
        if cancel.is_cancelled() {
            // never act on a partial stream
            result.stop_reason = "cancelled".into();
            return Ok(result);
        }
        result.tool_calls = finish_calls(pending)?;
        Ok(result)
    }

    fn assistant_message(&self, result: &TurnResult) -> Value {
        let content = if result.text.is_empty() { Value::Null } else { Value::String(result.text.clone()) };
        let mut msg = json!({"role": "assistant", "content": content});
        if !result.tool_calls.is_empty() {
            msg["tool_calls"] = Value::Array(
                result
                    .tool_calls
                    .iter()
                    .map(|c| json!({"id": c.id, "type": "function", "function": {"name": c.name,
                        "arguments": aurora_core::pyjson::dumps_ascii(&c.arguments)}}))
                    .collect(),
            );
        }
        msg
    }

    fn tool_result_message(&self, call: &ToolCall, output: &str) -> Value {
        json!({"role": "tool", "tool_call_id": call.id, "content": output})
    }
}

#[cfg(test)]
mod tests;
