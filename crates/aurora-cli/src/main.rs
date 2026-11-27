//! `aurora-rs`: the Rust Aurora's command line. Headless mode is Python's
//! `aurora -t` (R311, R337): stdout carries only the assistant's text (or one
//! JSON object with --json), progress goes to stderr, exit 0 = finished
//! cleanly, 1 = incomplete, 2 = usage. `--ask` adds what Python's headless
//! mode does not have: an approval prompt on the terminal.

mod coreserve;
mod headless;

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

use aurora_core::agent::{Agent, AgentConfig};
use aurora_core::ToolBox;
use aurora_providers::OpenAiCompat;
use aurora_storage::approvals::{DenyState, HomePolicy};
use aurora_storage::{config, keystore::Keystore, paths, session::JsonlSession};
use clap::Parser;
use serde_json::{json, Value};

#[derive(Parser)]
#[command(name = "aurora-rs", version, about = "Aurora 2.0 (Rust core, pre-release): one-shot headless runs")]
struct Cli {
    /// The task; `-` reads it from stdin.
    #[arg(short = 't', long = "task")]
    task: Option<String>,
    /// Run gated calls without asking (the denylist still wins).
    #[arg(long, conflicts_with = "ask")]
    yes: bool,
    /// Ask on the terminal before every write and command.
    #[arg(long)]
    ask: bool,
    /// Print one JSON object with the reply, the calls and the outcome.
    #[arg(long)]
    json: bool,
    /// Rounds before stopping (default: runtime.max_iterations).
    #[arg(long = "max-steps", value_parser = clap::value_parser!(u32).range(1..))]
    max_steps: Option<u32>,
    /// A model id from config.yaml's `models:` (default: the last one used).
    #[arg(long)]
    model: Option<String>,
    /// The config file (default: $AURORA_CONFIG, else $AURORA_HOME/config.yaml).
    #[arg(long)]
    config: Option<PathBuf>,
    /// Test hook: read a JSON array of {tool, args, allow, deny} cases on stdin and print what
    /// the approval matcher answers for each, then exit.
    #[arg(long = "policy-oracle", hide = true)]
    policy_oracle: bool,
    /// Test hook: read a JSON array of strings on stdin; print what the secret scanner finds in each.
    #[arg(long = "secrets-oracle", hide = true)]
    secrets_oracle: bool,
    /// Resume the most recent session (R20).
    #[arg(long = "continue")]
    cont: bool,
    /// Resume a specific session by id (R248: an unknown id is an error).
    #[arg(long, value_name = "ID")]
    resume: Option<String>,
    /// Test hook: read a JSON array of ops ("state", "trust") on stdin and print the bootstrap
    /// prompt's state after each, for the current directory.
    #[arg(long = "bootstrap-oracle", hide = true)]
    bootstrap_oracle: bool,
    /// Test hook: read a JSON array of {tool, args} on stdin, run each built-in tool directly
    /// (no approval gate) in the current directory and print the results.
    #[arg(long = "tool-oracle", hide = true)]
    tool_oracle: bool,
    /// Test hook: read {config_base, roots, ops} on stdin; answer listing / load_doc / run like skills.py.
    #[arg(long = "skills-oracle", hide = true)]
    skills_oracle: bool,
    /// R499: serve the agent loop to Python's front ends over stdin/stdout (`aurora-core/1`).
    #[arg(long, hide = true)]
    core: bool,
    /// R500: with --core, serve the web page: the path guard (R450) on, commands boxed unless `sandbox: off` (R475).
    #[arg(long, hide = true, requires = "core")]
    web: bool,
    /// R505: with --core, Python runs each model request (a provider object given in-process) and streams it back.
    #[arg(long = "provider-bridge", hide = true, requires = "core")]
    provider_bridge: bool,
    /// Test hook: read a JSON array of {tool, args} on stdin; print the web path guard's answer for each (R500).
    #[arg(long = "guard-oracle", hide = true)]
    guard_oracle: bool,
    /// Test hook: read {home, aurora_home, network, writable, hide, workspace, darwin_tmp} on stdin; print the
    /// macOS sandbox-exec profile (R501).
    #[arg(long = "macos-profile-oracle", hide = true)]
    macos_profile_oracle: bool,
    /// Print the parsed configuration as JSON and exit.
    #[arg(long = "dump-config")]
    dump_config: bool,
}

fn base_system() -> String {
    let os = match std::env::consts::OS {
        "linux" => "Linux",
        "macos" => "Darwin",
        other => other,
    };
    let arch = match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => "arm64",
        (_, a) => a,
    };
    let cwd = std::env::current_dir().map(|d| d.display().to_string()).unwrap_or_default();
    let home = std::env::var("HOME").unwrap_or_default();
    format!(
        "You are Aurora, a terminal coding agent running directly on the user's machine.\n\
         Environment: {os} ({arch}), cwd: {cwd}, home: {home}.\n\
         Your tools operate on the REAL filesystem — you are NOT sandboxed. You may read any file or directory \
         the user can (reads need no approval); writes, edits and shell commands ask the user first. Use absolute \
         paths or ~ (e.g. ~/Desktop), not guesses relative to the cwd. If a path doesn't exist, list its parent \
         instead of concluding you lack access. In shell commands, ALWAYS double-quote file paths — they often \
         contain spaces or parentheses."
    )
}

/// Which `models:` entry to start on: --model, else state.yaml's last model,
/// else the first whose key is already available, else the first.
fn pick_model(cfg: &Value, wanted: Option<&str>, has_key: &dyn Fn(&str) -> bool) -> Option<Value> {
    let models = cfg["models"].as_array()?;
    let by_name = |name: &str| models.iter().find(|m| m["model"].as_str() == Some(name)).cloned();
    if let Some(w) = wanted {
        return by_name(w);
    }
    let state = config::load_state();
    if let Some(entry) = state.get("last_model").and_then(Value::as_str).and_then(by_name) {
        return Some(entry);
    }
    models.iter().find(|m| m["provider"].as_str().is_some_and(has_key)).or(models.first()).cloned()
}

/// Python `Engine._runtime_number` (R202): a usable number, else the default and a warning.
fn runtime_number(runtime: &Value, key: &str, default: f64, valid: impl Fn(f64) -> bool, hint: &str) -> f64 {
    let raw = &runtime[key];
    let val = match raw {
        Value::Null => return default,
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => None,
    };
    match val {
        None => {
            eprintln!("· config runtime.{key}: {raw} is not a number — using {default}");
            default
        }
        Some(v) if !valid(v) => {
            eprintln!("· config runtime.{key}: {v} must be {hint} — using {default}");
            default
        }
        Some(v) => v,
    }
}

fn die(msg: &str) -> ExitCode {
    eprintln!("aurora-rs: {msg}");
    ExitCode::from(2)
}

#[tokio::main]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    if cli.policy_oracle {
        return policy_oracle();
    }
    if cli.secrets_oracle {
        return secrets_oracle();
    }
    if cli.bootstrap_oracle {
        return bootstrap_oracle();
    }
    if cli.tool_oracle {
        return tool_oracle().await;
    }
    if cli.macos_profile_oracle {
        let mut text = String::new();
        if std::io::stdin().read_to_string(&mut text).is_err() {
            return ExitCode::from(2);
        }
        let Ok(v) = serde_json::from_str::<Value>(&text) else { return ExitCode::from(2) };
        let strs = |k: &str| v[k].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
        let p = aurora_tools::sandbox::Policy {
            home: PathBuf::from(v["home"].as_str().unwrap_or("")),
            aurora_home: PathBuf::from(v["aurora_home"].as_str().unwrap_or("")),
            network: v["network"].as_bool().unwrap_or(true),
            writable: strs("writable"),
            hide: strs("hide"),
        };
        let ws = PathBuf::from(v["workspace"].as_str().unwrap_or("."));
        print!("{}", aurora_tools::sandbox::macos_profile(&p, &ws, Path::new(v["darwin_tmp"].as_str().unwrap_or("/tmp"))));
        return ExitCode::SUCCESS;
    }
    if cli.guard_oracle {
        return guard_oracle();
    }
    if cli.skills_oracle {
        return skills_oracle().await;
    }
    let cfg_path = match config::find_config(cli.config.as_deref()) {
        Ok(p) => p,
        Err(e) => return die(&e.to_string()),
    };
    let cfg = match config::load_config(&cfg_path) {
        Ok(c) => c,
        Err(e) => return die(&format!("config: {e}")),
    };
    if cli.dump_config {
        println!("{}", serde_json::to_string_pretty(&cfg).unwrap_or_default());
        return ExitCode::SUCCESS;
    }
    let task = if cli.core { Some(".".to_string()) } else { cli.task.clone() };
    let Some(task) = task else {
        return die("usage: aurora-rs -t <task|-> [--yes | --ask] [--json] [--max-steps N] [--model ID] [--config FILE]");
    };
    let task = if task == "-" {
        let mut s = String::new();
        if std::io::stdin().read_to_string(&mut s).is_err() {
            return die("could not read the task from stdin");
        }
        s
    } else {
        task
    };
    if task.trim().is_empty() {
        return die("the task is empty");
    }
    let home = match paths::aurora_home() {
        Ok(h) => h,
        Err(e) => return die(&e.to_string()),
    };
    let fe = headless::Headless::new(cli.json, cli.ask);
    let keys = Keystore::new(home.clone(), Box::new(|_| String::new()));
    let key_env = |pkey: &str| cfg["providers"][pkey]["api_key_env"].as_str().unwrap_or_default().to_string();
    let has_key = |pkey: &str| {
        let env = key_env(pkey);
        env.is_empty() || keys.get(&env, false).is_some()
    };
    let Some(entry) = pick_model(&cfg, cli.model.as_deref(), &has_key) else {
        return die(&match &cli.model {
            Some(m) => format!("no model {m:?} in {}", cfg_path.display()),
            None => format!("no models configured in {}", cfg_path.display()),
        });
    };
    let model = entry["model"].as_str().unwrap_or_default().to_string();
    let pkey = entry["provider"].as_str().unwrap_or_default().to_string();
    let pcfg = cfg["providers"][&pkey].clone();
    if !pcfg.is_object() {
        return die(&format!("model {model:?} names provider {pkey:?}, which config.yaml does not define"));
    }
    let env = key_env(&pkey);
    let api_key = if env.is_empty() { String::new() } else { keys.get(&env, false).unwrap_or_default() };
    let runtime = &cfg["runtime"];
    let timeout = runtime["timeout"].as_f64().unwrap_or(300.0).max(1.0);
    let mut provider = OpenAiCompat::new(&pkey, &pcfg, api_key, Duration::from_secs_f64(timeout));
    // R91: remote models cache the system prompt by default; llama.cpp keeps its own prefix cache
    provider.cache_prompt = entry["cache"].as_bool().unwrap_or(runtime["prompt_cache"].as_bool().unwrap_or(true) && model != "local");

    // R475: `runtime.sandbox: all` boxes the model's commands in the terminal too (Python's default boxes only the web page)
    let mode = aurora_tools::sandbox::mode_from(runtime);
    let boxed = if mode == "all" || (cli.web && mode != "off") {
        let user_home = PathBuf::from(std::env::var("HOME").unwrap_or_default());
        let policy = aurora_tools::sandbox::policy_from(runtime, &user_home, &home);
        match aurora_tools::sandbox::check(&policy) {
            Ok(()) => Some(policy),
            Err(why) => {
                eprintln!("· sandbox unavailable ({why}) — commands run unboxed");
                None
            }
        }
    } else {
        None
    };
    let tools = if entry["tools"].as_bool() == Some(false) {
        ToolBox::default()
    } else {
        let builtin = aurora_tools::builtin_boxed(Duration::from_secs(timeout as u64), boxed);
        // R119/R477: Python's bundled extensions and the user's own run in an aurora-ext/1 host
        let python = std::env::var("AURORA_PYTHON").unwrap_or_else(|_| "python3".into());
        let mut paths: Vec<PathBuf> = aurora_tools::ext::bundled_dir(&python).into_iter().collect();
        if home.join("extensions").is_dir() {
            paths.push(home.join("extensions")); // Python skips a missing folder without a word
        }
        let (ext_tools, warnings) = aurora_tools::ext::start(&python, &paths, &cfg);
        // R508: under Python's front end (AURORA_EXT_QUIET) the front end has already said so
        if std::env::var_os("AURORA_EXT_QUIET").is_none() {
            for w in &warnings {
                eprintln!("· {w}");
            }
        }
        let names: Vec<String> = builtin.specs().into_iter().map(|s| s.name).collect();
        let mut all = builtin.into_tools();
        for t in ext_tools {
            let n = t.spec().name;
            if names.contains(&n) {
                eprintln!("· extension tool '{n}' shadows a builtin tool — skipped");
            } else {
                all.push(t);
            }
        }
        ToolBox::new(all)
    };
    let tools = if cli.web {
        aurora_tools::guard::set_web(true);
        aurora_tools::guard::guarded(tools, home.clone())
    } else {
        tools
    };

    let policy = HomePolicy::load(&home);
    if let DenyState::Broken(e) = policy.state() {
        eprintln!("· denylist.yaml is unreadable ({e}) — blocking all gated tool calls until it's fixed");
    }
    let sessions_dir = match paths::sessions_dir() {
        Ok(d) => d,
        Err(e) => return die(&e.to_string()),
    };
    // R20/R248: --resume ID wins over --continue, as in Python
    let mut messages = Vec::new();
    let mut session = JsonlSession::new(sessions_dir.clone(), None);
    let wanted = match (&cli.resume, cli.cont) {
        (Some(id), _) => {
            if !sessions_dir.join(format!("{id}.jsonl")).exists() {
                return die(&format!("no session {id} on this machine (run aurora and use /resume to list them)"));
            }
            Some((id.clone(), "resuming"))
        }
        (None, true) => match aurora_storage::session::latest_session_id(&sessions_dir) {
            Some(id) => Some((id, "continuing")),
            None => {
                eprintln!("· no previous session — starting fresh");
                None
            }
        },
        (None, false) => None,
    };
    if let Some((id, verb)) = wanted {
        let past = JsonlSession::new(sessions_dir.clone(), Some(id.clone()));
        let n = match past.restore() {
            Some((msgs, n)) => {
                messages = msgs;
                session = past;
                n
            }
            None => 0,
        };
        eprintln!("· {verb} session {id} ({n} turns)");
    }
    let mut agent_cfg = AgentConfig {
        no_tools: false,
        model: model.clone(),
        system: base_system(),
        max_iterations: cli.max_steps.unwrap_or_else(|| runtime["max_iterations"].as_u64().unwrap_or(10) as u32),
        auto_approve: cli.yes,
        parallel_tools: runtime["parallel_tools"].as_bool().unwrap_or(true),
        redact_secrets: runtime["redact_secrets"].as_bool().unwrap_or(true),
        secret_allowlist: runtime["secret_allowlist"]
            .as_array()
            .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
            .unwrap_or_default(),
        secret_session_keep: Default::default(),
        compaction: aurora_core::Compaction {
            auto: runtime["auto_compact"].as_bool().unwrap_or(true),
            threshold_pct: runtime_number(runtime, "auto_compact_threshold_pct", 80.0, |v| v > 0.0 && v <= 100.0, ">0 and <=100"),
            keep_recent_tokens: runtime_number(runtime, "compact_keep_recent_tokens", 20_000.0, |v| v.trunc() > 0.0, "a positive number of tokens").trunc() as u64,
            context_limit: aurora_providers::static_context_limit(&model, &pcfg),
            // R90g: a resumed history counts before the first reply measures it
            used: messages.iter().map(|m| aurora_core::compact::content_str(m).chars().count() as u64).sum::<u64>() / 4,
            count: session.compactions() as u32,
        },
        // R47: approved mutations are checkpointed into Python's shadow repo, so /rewind and /undo cover them
        checkpoints: std::env::current_dir()
            .ok()
            .map(|cwd| std::sync::Arc::new(aurora_storage::rewind::Rewind::new(home.clone(), &cwd)) as std::sync::Arc<dyn aurora_core::agent::Checkpointer>),
        turn_prompt: String::new(),
    };

    if cli.core {
        if cli.provider_bridge {
            let bridge = coreserve::BridgeProvider::default();
            return ExitCode::from(coreserve::serve_bridged(&bridge, &tools, &policy, agent_cfg).await);
        }
        return ExitCode::from(coreserve::serve(&provider, &tools, &policy, agent_cfg).await);
    }
    let cancel = fe.cancel_token();
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            cancel.cancel();
        }
    });
    let agent = Agent { provider: &provider, tools: &tools, policy: &policy, frontend: &fe, log: &session };
    let turn = agent.send(&mut agent_cfg, &mut messages, &task).await;
    aurora_tools::jobs::kill_all(); // R245: a background job never outlives the process that started it
    if turn.error.is_none() && !fe.produced() {
        fe.fail("the model produced no response");
    }
    let code = fe.finish(&json!({
        "model": model,
        "session": aurora_core::SessionLog::id(&session),
        "context_tokens": turn.input_tokens,
        "cost_usd": null,
    }));
    ExitCode::from(code)
}

/// Hidden test hook (`--policy-oracle`): the same rules Python's `approve.py` gets, answered by the Rust matcher.
fn policy_oracle() -> ExitCode {
    use aurora_storage::rules;
    let mut text = String::new();
    if std::io::stdin().read_to_string(&mut text).is_err() {
        return ExitCode::from(2);
    }
    let Ok(Value::Array(cases)) = serde_json::from_str::<Value>(&text) else { return ExitCode::from(2) };
    let map = |v: &Value| v.as_object().cloned().unwrap_or_default();
    let out: Vec<Value> = cases
        .iter()
        .map(|c| {
            let (tool, args) = (c["tool"].as_str().unwrap_or(""), map(&c["args"]));
            let (allow, deny) = (map(&c["allow"]), map(&c["deny"]));
            let (rule, scope) = rules::rule_scope(tool, &args);
            json!({
                "allowed": rules::is_allowed(tool, &args, &allow, &rules::Rules::new()),
                "denied": rules::is_denied(tool, &args, &deny),
                "rule": rules::rule_for(tool, &args),
                "scope": [rule, scope],
            })
        })
        .collect();
    println!("{}", Value::Array(out));
    ExitCode::SUCCESS
}

/// Hidden test hook (`--guard-oracle`): `guard.check` with web mode on, case by case.
fn guard_oracle() -> ExitCode {
    let mut text = String::new();
    if std::io::stdin().read_to_string(&mut text).is_err() {
        return ExitCode::from(2);
    }
    let Ok(Value::Array(cases)) = serde_json::from_str::<Value>(&text) else { return ExitCode::from(2) };
    let Ok(home) = paths::aurora_home() else { return ExitCode::from(2) };
    aurora_tools::guard::set_web(true);
    let out: Vec<Value> = cases
        .iter()
        .map(|c| json!(aurora_tools::guard::check(c["tool"].as_str().unwrap_or(""), &c["args"].as_object().cloned().unwrap_or_default(), &home)))
        .collect();
    println!("{}", json!({"checks": out, "grep_excludes": aurora_tools::guard::grep_excludes()}));
    ExitCode::SUCCESS
}

/// Hidden test hook (`--secrets-oracle`): the scanner's answer for each input text.
fn secrets_oracle() -> ExitCode {
    use aurora_core::secrets;
    let mut text = String::new();
    if std::io::stdin().read_to_string(&mut text).is_err() {
        return ExitCode::from(2);
    }
    let Ok(Value::Array(items)) = serde_json::from_str::<Value>(&text) else { return ExitCode::from(2) };
    let none = std::collections::HashSet::new();
    let out: Vec<Value> = items
        .iter()
        .map(|t| {
            let t = t.as_str().unwrap_or("");
            let ms = secrets::scan(t, &none);
            json!({"matches": ms.iter().map(|m| json!([m.kind, m.text])).collect::<Vec<_>>(),
                   "redacted": secrets::redact(t, &ms), "preview": secrets::preview(&ms, 5)})
        })
        .collect();
    println!("{}", Value::Array(out));
    ExitCode::SUCCESS
}

/// Hidden test hook (`--bootstrap-oracle`): what `bootstrap.py` would answer, op by op.
fn bootstrap_oracle() -> ExitCode {
    use aurora_storage::bootstrap::Bootstrap;
    let mut text = String::new();
    if std::io::stdin().read_to_string(&mut text).is_err() {
        return ExitCode::from(2);
    }
    let Ok(Value::Array(ops)) = serde_json::from_str::<Value>(&text) else { return ExitCode::from(2) };
    let (Ok(home), Ok(cwd)) = (paths::aurora_home(), std::env::current_dir()) else { return ExitCode::from(2) };
    let b = Bootstrap::new(home, &cwd);
    let mut out = Vec::new();
    for op in ops {
        if op == "trust" && b.trust().is_err() {
            return ExitCode::from(1);
        }
        let (prompt, source) = b.load().map_or((Value::Null, Value::Null), |(p, s)| (p.into(), s.into()));
        out.push(json!({"prompt": prompt, "source": source, "url": b.source_url(), "needs_trust": b.needs_trust()}));
    }
    println!("{}", Value::Array(out));
    ExitCode::SUCCESS
}

/// Hidden test hook (`--tool-oracle`): each built-in tool's result for the given arguments.
async fn tool_oracle() -> ExitCode {
    let mut text = String::new();
    if std::io::stdin().read_to_string(&mut text).is_err() {
        return ExitCode::from(2);
    }
    let Ok(Value::Array(calls)) = serde_json::from_str::<Value>(&text) else { return ExitCode::from(2) };
    let tools = aurora_tools::builtin(Duration::from_secs(60));
    let cancel = aurora_core::CancellationToken::new();
    let mut out = Vec::new();
    for c in calls {
        let name = c["tool"].as_str().unwrap_or_default();
        let args = c["args"].as_object().cloned().unwrap_or_default();
        let res = match tools.get(name) {
            Some(t) => t.run(&args, &cancel).await,
            None => format!("[error: unknown tool {name}]"),
        };
        out.push(Value::String(res));
    }
    println!("{}", Value::Array(out));
    ExitCode::SUCCESS
}

/// Hidden test hook (`--skills-oracle`).
async fn skills_oracle() -> ExitCode {
    use aurora_tools::skills;
    let mut text = String::new();
    if std::io::stdin().read_to_string(&mut text).is_err() {
        return ExitCode::from(2);
    }
    let Ok(req) = serde_json::from_str::<Value>(&text) else { return ExitCode::from(2) };
    let Ok(home) = paths::aurora_home() else { return ExitCode::from(2) };
    let base = req["config_base"].as_str().map(PathBuf::from);
    let roots: Vec<String> = req["roots"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default();
    let mut out = Vec::new();
    for op in req["ops"].as_array().cloned().unwrap_or_default() {
        let arg = |i: usize| op[i].as_str().unwrap_or_default().to_string();
        out.push(match arg(0).as_str() {
            "listing" => json!(skills::listing(base.as_deref(), &home, &roots)),
            "load_doc" => json!(skills::load_doc(&arg(1), &roots)),
            "run" => json!(skills::run(&arg(1), &arg(2), base.as_deref(), &home, "python3", skills::SKILL_TIMEOUT).await),
            _ => Value::Null,
        });
    }
    println!("{}", Value::Array(out));
    ExitCode::SUCCESS
}
