//! The headless front end (Python `headless.HeadlessFrontend`).

use std::io::{BufRead, BufReader, Write};
use std::sync::Mutex;

use async_trait::async_trait;
use aurora_core::{AgentEvent, Approval, Args, CancellationToken, Continue, Frontend, SecretChoice};
use serde_json::{json, Value};

#[derive(Default)]
struct State {
    reply: String,
    calls: Vec<Value>,
    reasons: Vec<String>,
    failed: bool,
    wrote: bool,
    tools: usize,
}

pub struct Headless {
    as_json: bool,
    ask: bool,
    state: Mutex<State>,
    cancel: CancellationToken,
    /// Serialises prompts: one question on the terminal at a time.
    tty: tokio::sync::Mutex<()>,
}

fn first_line(s: &str, n: usize) -> String {
    s.trim().lines().next().unwrap_or("").chars().take(n).collect()
}

/// Ask on the controlling terminal (not stdin: `-t -` may have consumed it).
fn tty_prompt(text: &str) -> Option<String> {
    let mut tty = std::fs::OpenOptions::new().read(true).write(true).open("/dev/tty").ok()?;
    tty.write_all(text.as_bytes()).ok()?;
    tty.flush().ok()?;
    let mut line = String::new();
    BufReader::new(tty).read_line(&mut line).ok()?;
    Some(line.trim().to_string())
}

impl Headless {
    pub fn new(as_json: bool, ask: bool) -> Self {
        Self { as_json, ask, state: Mutex::new(State::default()), cancel: CancellationToken::new(), tty: tokio::sync::Mutex::new(()) }
    }

    pub fn cancel_token(&self) -> CancellationToken {
        self.cancel.clone()
    }

    pub fn fail(&self, why: &str) {
        let mut st = self.state.lock().expect("state lock");
        st.failed = true;
        st.reasons.push(why.to_string());
    }

    pub fn produced(&self) -> bool {
        let st = self.state.lock().expect("state lock");
        st.wrote || st.tools > 0 || st.failed
    }

    /// Print the result; returns the exit code.
    pub fn finish(&self, extra: &Value) -> u8 {
        let st = self.state.lock().expect("state lock");
        let code = u8::from(st.failed);
        if self.as_json {
            let mut report = json!({"ok": code == 0, "exit_code": code, "reply": st.reply, "reasons": st.reasons, "tool_calls": st.calls});
            if let (Value::Object(r), Value::Object(e)) = (&mut report, extra) {
                r.extend(e.clone());
            }
            println!("{report}");
        } else if st.wrote {
            println!();
        }
        if st.failed {
            eprintln!("headless: incomplete — {}", st.reasons.join("; "));
        }
        code
    }

    fn ask_on_tty(&self, tool: &str, args: &Args, preview: &str) -> Approval {
        let shown = if preview.is_empty() { Value::Object(args.clone()).to_string() } else { preview.to_string() };
        let question = format!("\n── approve {tool} ──\n{}\nAllow? [y] once  [a] always  [t] this session  [n] no  [d] always deny  [s] stop  [c] comment  [o] yes to all: ", shown.trim_end());
        match tty_prompt(&question).as_deref().map(str::to_lowercase).as_deref() {
            Some("y" | "yes") => Approval::Once,
            Some("a") => Approval::Always,
            Some("t") => Approval::SessionAllow,
            Some("d") => Approval::AlwaysDeny,
            Some("o") => Approval::AutoApproveOn,
            Some("s") => Approval::Stop,
            Some("c") => Approval::Steer(tty_prompt("Guidance for the model: ").unwrap_or_default()),
            Some(_) => Approval::Deny(String::new()),
            None => {
                self.fail(&format!("{tool} denied (no terminal to ask on)"));
                Approval::Deny("headless: no terminal to ask the user on".into())
            }
        }
    }
}

#[async_trait]
impl Frontend for Headless {
    fn event(&self, event: AgentEvent) {
        match event {
            AgentEvent::Text { chunk } => {
                let mut st = self.state.lock().expect("state lock");
                st.wrote = true;
                if self.as_json {
                    st.reply.push_str(&chunk);
                } else {
                    let mut out = std::io::stdout().lock();
                    let _ = out.write_all(chunk.as_bytes());
                    let _ = out.flush();
                }
            }
            AgentEvent::Think { .. } | AgentEvent::Usage { .. } => {}
            AgentEvent::ToolStart { name, args } => {
                let mut st = self.state.lock().expect("state lock");
                st.tools += 1;
                let first = args.values().next().map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())).unwrap_or_default();
                st.calls.push(json!({"name": name, "args": args}));
                eprintln!("· {name} {}", first_line(&first, 100));
            }
            AgentEvent::ToolResult { name, output, .. } => {
                {
                    let mut st = self.state.lock().expect("state lock");
                    match st.calls.last_mut() {
                        Some(c) if c.get("result").is_none() && c["name"] == name => {
                            c["result"] = json!(output.chars().take(2000).collect::<String>())
                        }
                        // a call that never started (denied, skipped) is still a call the model made
                        _ => st.calls.push(json!({"name": name, "result": output.chars().take(2000).collect::<String>()})),
                    }
                }
                if output.starts_with("[denied by policy") {
                    self.fail(&format!("{name} denied by policy"));
                }
                let shown = if output.trim().is_empty() { "[no output]".into() } else { first_line(&output, 100) };
                eprintln!("  → {shown}");
            }
            AgentEvent::Notice { message } => {
                eprintln!("· {message}");
                if ["stopped", "interrupted", "provider error"].iter().any(|p| message.starts_with(p)) {
                    self.fail(&message);
                }
            }
        }
    }

    async fn approve(&self, tool: &str, args: &Args, preview: &str) -> Approval {
        if self.ask {
            let _one_at_a_time = self.tty.lock().await;
            let (tool, args, preview) = (tool.to_string(), args.clone(), preview.to_string());
            let me: &Headless = self;
            // the prompt blocks on the terminal; keep it off the async workers
            return tokio::task::block_in_place(|| me.ask_on_tty(&tool, &args, &preview));
        }
        self.fail(&format!("{tool} denied (not on the allowlist)"));
        Approval::Deny("headless: not on the allowlist — ask the user to allow it or run with --yes in a disposable environment".into())
    }

    async fn secret_challenge(&self, context: &str, summary: &str) -> SecretChoice {
        if !self.ask {
            // Python's headless mode: a likely secret ends the run (R337)
            self.fail(&format!("likely secret in {context}"));
            return SecretChoice::Stop;
        }
        let _one_at_a_time = self.tty.lock().await;
        let question = format!("\n── likely secret in {context}: {summary} ──\n[k] keep  [r] redact  [a] always allow these values  [s] stop (default): ");
        let answer = tokio::task::block_in_place(|| tty_prompt(&question));
        match answer.as_deref().map(str::to_lowercase).as_deref() {
            Some("k") => SecretChoice::Keep,
            Some("r") => SecretChoice::Redact,
            Some("a") => SecretChoice::Always,
            _ => {
                self.fail(&format!("likely secret in {context}"));
                SecretChoice::Stop
            }
        }
    }

    async fn ask_continue(&self, iterations: u32) -> Continue {
        self.fail(&format!("step cap reached after {iterations} iterations"));
        Continue::Stop
    }

    fn cancel(&self) -> &CancellationToken {
        &self.cancel
    }
}
