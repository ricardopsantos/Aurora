//! The client against a scripted HTTP server on 127.0.0.1.

use std::sync::{Arc, Mutex};

use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use super::*;

#[derive(Clone)]
struct Reply {
    status: u16,
    headers: Vec<(&'static str, String)>,
    /// Chunked-encoding pieces, written one by one.
    pieces: Vec<String>,
    /// Close before the terminating chunk: a mid-stream failure.
    cut: bool,
    delay_ms: u64,
}

fn sse(events: &[Value]) -> Reply {
    let mut body: Vec<String> = events.iter().map(|e| format!("data: {e}\n\n")).collect();
    body.push("data: [DONE]\n\n".into());
    Reply { status: 200, headers: vec![], pieces: body, cut: false, delay_ms: 0 }
}

fn status(code: u16, body: &str) -> Reply {
    Reply { status: code, headers: vec![], pieces: vec![body.into()], cut: false, delay_ms: 0 }
}

struct Server {
    url: String,
    requests: Arc<Mutex<Vec<(String, String)>>>,
}

async fn serve(replies: Vec<Reply>) -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/v1", listener.local_addr().unwrap());
    let requests = Arc::new(Mutex::new(Vec::new()));
    let seen = requests.clone();
    let queue = Arc::new(Mutex::new(std::collections::VecDeque::from(replies)));
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else { return };
            let seen = seen.clone();
            let queue = queue.clone();
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut tmp = [0u8; 4096];
                let (head, body_start) = loop {
                    let n = sock.read(&mut tmp).await.unwrap_or(0);
                    if n == 0 { return; }
                    buf.extend_from_slice(&tmp[..n]);
                    if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        break (String::from_utf8_lossy(&buf[..i]).to_string(), i + 4);
                    }
                };
                let len = head.lines().find_map(|l| l.to_lowercase().strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().unwrap())).unwrap_or(0);
                while buf.len() < body_start + len {
                    let n = sock.read(&mut tmp).await.unwrap_or(0);
                    if n == 0 { break; }
                    buf.extend_from_slice(&tmp[..n]);
                }
                let path = head.split_whitespace().nth(1).unwrap_or("").to_string();
                let body = String::from_utf8_lossy(&buf[body_start..]).to_string();
                seen.lock().unwrap().push((path.clone(), body));
                if path.ends_with("/props") {
                    let _ = sock.write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\n{}").await;
                    return;
                }
                let reply = queue.lock().unwrap().pop_front().expect("a scripted reply");
                let mut out = format!("HTTP/1.1 {} X\r\ntransfer-encoding: chunked\r\nconnection: close\r\n", reply.status);
                for (k, v) in &reply.headers {
                    out.push_str(&format!("{k}: {v}\r\n"));
                }
                out.push_str("\r\n");
                let _ = sock.write_all(out.as_bytes()).await;
                for p in &reply.pieces {
                    let _ = sock.write_all(format!("{:x}\r\n{p}\r\n", p.len()).as_bytes()).await;
                    let _ = sock.flush().await;
                    if reply.delay_ms > 0 {
                        tokio::time::sleep(Duration::from_millis(reply.delay_ms)).await;
                    }
                }
                if !reply.cut {
                    let _ = sock.write_all(b"0\r\n\r\n").await;
                }
            });
        }
    });
    Server { url, requests }
}

fn provider(url: &str) -> OpenAiCompat {
    OpenAiCompat::new("local", &json!({"type": "openai", "base_url": url}), "none".into(), Duration::from_secs(5))
}

struct Collect(Mutex<Vec<String>>);

impl Collect {
    fn new() -> Self {
        Self(Mutex::new(Vec::new()))
    }
    fn sink(&self) -> impl Fn(StreamChunk<'_>) + Send + Sync + '_ {
        move |c| {
            let s = match c {
                StreamChunk::Text(t) => format!("text:{t}"),
                StreamChunk::Think(t) => format!("think:{t}"),
                StreamChunk::Notice(t) => format!("notice:{t}"),
            };
            self.0.lock().unwrap().push(s);
        }
    }
}

async fn turn(p: &OpenAiCompat, tools: &[ToolSpec], out: &Collect, cancel: &CancellationToken) -> Result<TurnResult, ProviderError> {
    let msgs = [json!({"role": "user", "content": "hi"})];
    let req = TurnRequest { model: "m", messages: &msgs, system: "sys", tools };
    p.turn(req, &out.sink(), cancel).await
}

fn delta(d: Value) -> Value {
    json!({"choices": [{"delta": d}]})
}

#[tokio::test]
async fn streams_text_reasoning_and_usage() {
    let s = serve(vec![sse(&[
        delta(json!({"reasoning_content": "hmm"})),
        delta(json!({"content": "Hel"})),
        delta(json!({"content": "lo"})),
        json!({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        json!({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3,
               "prompt_tokens_details": {"cached_tokens": 4}, "completion_tokens_details": {"reasoning_tokens": 1}}}),
    ])])
    .await;
    let out = Collect::new();
    let r = turn(&provider(&s.url), &[], &out, &CancellationToken::new()).await.unwrap();
    assert_eq!(r.text, "Hello");
    assert_eq!((r.input_tokens, r.output_tokens, r.cached_input_tokens, r.reasoning_tokens, r.reasoning_chars), (12, 3, 4, 1, 3));
    assert_eq!(r.stop_reason, "stop");
    assert_eq!(*out.0.lock().unwrap(), ["think:hmm", "text:Hel", "text:lo"]);
    let reqs = s.requests.lock().unwrap();
    assert_eq!(reqs[0].0, "/props");
    let body: Value = serde_json::from_str(&reqs[1].1).unwrap();
    assert_eq!(body["stream_options"], json!({"include_usage": true}));
    assert_eq!(body["messages"][0], json!({"role": "system", "content": "sys"}));
}

#[tokio::test]
async fn assembles_fragmented_tool_calls_in_index_order() {
    let s = serve(vec![sse(&[
        delta(json!({"tool_calls": [{"index": 1, "id": "b", "function": {"name": "list_dir", "arguments": ""}}]})),
        delta(json!({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read_", "arguments": "{\"pa"}}]})),
        delta(json!({"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": "th\": \"x\"}"}}]})),
    ])])
    .await;
    let tools = [ToolSpec { name: "read_file".into(), description: "d".into(), parameters: json!({"type": "object"}) }];
    let r = turn(&provider(&s.url), &tools, &Collect::new(), &CancellationToken::new()).await.unwrap();
    assert_eq!(r.tool_calls.len(), 2);
    assert_eq!(r.tool_calls[0].name, "read_file");
    assert_eq!(r.tool_calls[0].arguments["path"], "x");
    assert_eq!(r.tool_calls[1].id, "b");
    assert!(r.tool_calls[1].arguments.is_empty());
    let body: Value = serde_json::from_str(&s.requests.lock().unwrap()[1].1).unwrap();
    assert_eq!(body["tools"][0]["function"]["name"], "read_file");
}

#[tokio::test]
async fn non_object_arguments_are_a_malformed_call() {
    let s = serve(vec![sse(&[delta(json!({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "x", "arguments": "[1]"}}]}))])]).await;
    let e = turn(&provider(&s.url), &[], &Collect::new(), &CancellationToken::new()).await.unwrap_err();
    assert!(matches!(e, ProviderError::MalformedToolCall(m) if m.contains("not a JSON object")));
}

#[tokio::test]
async fn http_errors_name_the_provider_and_status() {
    let s = serve(vec![status(500, "boom")]).await;
    let e = turn(&provider(&s.url), &[], &Collect::new(), &CancellationToken::new()).await.unwrap_err();
    assert_eq!(e.to_string(), "local HTTP 500: boom");
    let s = serve(vec![status(400, "failed to parse tool call")]).await;
    let e = turn(&provider(&s.url), &[], &Collect::new(), &CancellationToken::new()).await.unwrap_err();
    assert!(matches!(e, ProviderError::MalformedToolCall(_)));
}

#[tokio::test]
async fn rate_limit_waits_for_retry_after_then_retries() {
    let mut limited = status(429, "slow down");
    limited.headers.push(("retry-after", "0".into()));
    let s = serve(vec![limited, sse(&[delta(json!({"content": "ok"}))])]).await;
    let out = Collect::new();
    let r = turn(&provider(&s.url), &[], &out, &CancellationToken::new()).await.unwrap();
    assert_eq!(r.text, "ok");
    assert!(out.0.lock().unwrap()[0].starts_with("notice:rate-limited"));
}

#[tokio::test]
async fn a_cut_stream_keeps_the_partial_answer() {
    let mut r = sse(&[delta(json!({"content": "part"}))]);
    r.pieces.pop();
    r.cut = true;
    let s = serve(vec![r]).await;
    let res = turn(&provider(&s.url), &[], &Collect::new(), &CancellationToken::new()).await.unwrap();
    assert_eq!(res.stop_reason, "interrupted");
    assert!(res.text.starts_with("part\n[stream interrupted: ReadError"));
}

#[tokio::test]
async fn cancel_returns_promptly_without_tool_calls() {
    let mut r = sse(&[
        delta(json!({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "x", "arguments": "{}"}}]})),
        delta(json!({"content": "never"})),
    ]);
    r.delay_ms = 2000;
    let s = serve(vec![r]).await;
    let cancel = CancellationToken::new();
    let c2 = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(300)).await;
        c2.cancel();
    });
    let started = Instant::now();
    let res = turn(&provider(&s.url), &[], &Collect::new(), &cancel).await.unwrap();
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(res.stop_reason, "cancelled");
    assert!(res.tool_calls.is_empty());
}

#[tokio::test]
async fn fails_over_past_a_dead_endpoint_and_remembers_it() {
    let s = serve(vec![sse(&[delta(json!({"content": "up"}))]), sse(&[delta(json!({"content": "again"}))])]).await;
    let dead = "http://127.0.0.1:9/v1";
    let p = OpenAiCompat::new("local", &json!({"base_url": [dead, s.url]}), String::new(), Duration::from_secs(5));
    assert_eq!(turn(&p, &[], &Collect::new(), &CancellationToken::new()).await.unwrap().text, "up");
    assert!(p.endpoints.lock().unwrap().dead_until.contains_key(dead));
    assert_eq!(turn(&p, &[], &Collect::new(), &CancellationToken::new()).await.unwrap().text, "again");
}

#[test]
fn messages_round_trip_in_openai_shape() {
    let p = provider("http://localhost/v1");
    let mut args = Map::new();
    args.insert("path".into(), json!("x"));
    let r = TurnResult { text: String::new(), tool_calls: vec![ToolCall { id: "1".into(), name: "read_file".into(), arguments: args }], ..Default::default() };
    let m = p.assistant_message(&r);
    assert_eq!(m["content"], Value::Null);
    assert_eq!(m["tool_calls"][0]["function"]["arguments"], r#"{"path": "x"}"#);
    let t = p.tool_result_message(&r.tool_calls[0], "out");
    assert_eq!(t, json!({"role": "tool", "tool_call_id": "1", "content": "out"}));
}

#[test]
fn a_long_system_prompt_becomes_a_cache_breakpoint() {
    let long = "x".repeat(CACHE_MIN_CHARS);
    assert_eq!(system_message(&long, true)["content"][0]["cache_control"], json!({"type": "ephemeral"}));
    assert_eq!(system_message(&long, false)["content"], json!(long));
    assert_eq!(system_message("short", true)["content"], json!("short"));
}

#[tokio::test]
async fn calls_without_an_index_are_told_apart_by_id() {
    // R560: no `index` field; a new id starts a new call, a fragment with no id continues the last one
    let s = serve(vec![sse(&[
        delta(json!({"tool_calls": [{"id": "a", "function": {"name": "read_file", "arguments": "{\"path\": "}}]})),
        delta(json!({"tool_calls": [{"function": {"arguments": "\"x\"}"}}]})),
        delta(json!({"tool_calls": [{"id": "b", "function": {"name": "list_dir", "arguments": "{}"}}]})),
    ])])
    .await;
    let r = turn(&provider(&s.url), &[], &Collect::new(), &CancellationToken::new()).await.unwrap();
    let got: Vec<(&str, &str)> = r.tool_calls.iter().map(|c| (c.id.as_str(), c.name.as_str())).collect();
    assert_eq!(got, [("a", "read_file"), ("b", "list_dir")]);
    assert_eq!(r.tool_calls[0].arguments["path"], "x");
}

#[tokio::test]
async fn a_read_timeout_is_not_sent_again() {
    // R559: the slow reply is never re-sent (the second scripted reply must stay unused)
    let mut slow = sse(&[delta(json!({})), delta(json!({"content": "late"}))]);
    slow.delay_ms = 1500;
    let s = serve(vec![slow, sse(&[delta(json!({"content": "again"}))])]).await;
    let p = OpenAiCompat::new("local", &json!({"type": "openai", "base_url": s.url}), "none".into(), Duration::from_millis(300));
    let e = turn(&p, &[], &Collect::new(), &CancellationToken::new()).await.unwrap_err();
    assert!(e.to_string().contains("ReadTimeout"), "{e}");
    let posts = s.requests.lock().unwrap().iter().filter(|(path, _)| path.ends_with("/chat/completions")).count();
    assert_eq!(posts, 1);
}
