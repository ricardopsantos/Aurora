//! Background jobs (R245, R246, R293): `run_command(background=true)` starts a command detached and returns a
//! job id; `check_command` polls it and `cancel_command` kills its process group. The registry is process-wide,
//! like Python's: a job outlives the tool call (and the turn) that started it, and `kill_all` is the exit hook.

use std::collections::BTreeMap;
use std::process::Stdio;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

use async_trait::async_trait;
use aurora_core::text::{char_len, tail};
use aurora_core::{Args, CancellationToken, Tool, ToolSpec};
use serde_json::json;
use tokio::io::AsyncReadExt;

use crate::command::{build, COMMAND_OUTPUT_CAP};
use crate::sandbox::Policy;
use crate::{arg_int, arg_str};

pub const MAX_BACKGROUND_JOBS: usize = 4;
pub const MAX_FINISHED_JOBS: usize = 8;

#[derive(Default)]
struct State {
    out: Vec<u8>,
    err: Vec<u8>,
    truncated: bool,
    done: bool,
    code: Option<i32>,
    /// R567: a process the job started still holds its output open after the job's shell exited.
    lingering: bool,
}

struct Job {
    n: u64,
    command: String,
    pgid: Option<u32>,
    started: Instant,
    state: Mutex<State>,
}

#[derive(Default)]
struct Registry {
    next: u64,
    jobs: BTreeMap<String, Arc<Job>>,
}

fn registry() -> &'static Mutex<Registry> {
    static R: OnceLock<Mutex<Registry>> = OnceLock::new();
    R.get_or_init(|| Mutex::new(Registry { next: 1, jobs: BTreeMap::new() }))
}

fn kill_group(pgid: Option<u32>) {
    if let Some(g) = pgid {
        // SAFETY: plain syscall; the group may already be gone.
        unsafe { libc::killpg(g as libc::pid_t, libc::SIGKILL) };
    }
}

/// Start `command` detached. `Err` is the `[error: ...]` text the model sees.
pub fn start(command: &str, workdir: Option<&str>, sandbox: Option<&Policy>) -> Result<String, String> {
    let (id, job) = {
        let mut r = registry().lock().expect("jobs lock");
        // R293: drop the oldest FINISHED jobs beyond the cap; a running job is never dropped
        let mut finished: Vec<(u64, String)> =
            r.jobs.iter().filter(|(_, j)| j.state.lock().expect("job lock").done).map(|(k, j)| (j.n, k.clone())).collect();
        finished.sort();
        let excess = finished.len().saturating_sub(MAX_FINISHED_JOBS);
        for (_, k) in finished.into_iter().take(excess) {
            r.jobs.remove(&k);
        }
        let live = r.jobs.values().filter(|j| !j.state.lock().expect("job lock").done).count();
        if live >= MAX_BACKGROUND_JOBS {
            return Err(format!(
                "[error: {MAX_BACKGROUND_JOBS} background jobs already running — check_command() one to completion, or let one finish, before starting another]"
            ));
        }
        let n = r.next;
        r.next += 1;
        (format!("bg{n}"), n)
    };
    let mut cmd = build(command, workdir, sandbox).map_err(|e| format!("[error: cannot run the command: {e}]"))?;
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).process_group(0).kill_on_drop(false);
    let mut child = cmd.spawn().map_err(|e| format!("[error: cannot run the command: {e}]"))?;
    let job = Arc::new(Job { n: job, command: command.to_string(), pgid: child.id(), started: Instant::now(), state: Mutex::default() });
    registry().lock().expect("jobs lock").jobs.insert(id.clone(), job.clone());
    let (mut so, mut se) = (child.stdout.take().expect("piped"), child.stderr.take().expect("piped"));
    tokio::spawn(async move {
        // the job lives on its own task for its whole life, with no deadline: drain both pipes and reap.
        // R567: the job is done when its shell exits, not when its pipes close: `server & echo started` left a
        // child holding them, so the job read "still running" forever and kept one of the MAX_BACKGROUND_JOBS slots.
        // Output still arriving after the exit is kept; a short grace lets the last lines land first.
        let (mut ob, mut eb) = (vec![0u8; 65536], vec![0u8; 65536]);
        let (mut oo, mut eo) = (true, true);
        let mut exit: Option<Option<i32>> = None;
        let mut marked = false;
        let grace = tokio::time::sleep(std::time::Duration::from_secs(86_400 * 365));
        tokio::pin!(grace);
        while oo || eo || !marked {
            if marked && !(oo || eo) {
                break;
            }
            if exit.is_some() && !marked && (!(oo || eo) || grace.is_elapsed()) {
                mark_done(&job, exit.flatten(), oo || eo);
                marked = true;
                continue;
            }
            tokio::select! {
                n = so.read(&mut ob), if oo => match n {
                    Ok(0) | Err(_) => oo = false,
                    Ok(n) => append(&job, true, &ob[..n]),
                },
                n = se.read(&mut eb), if eo => match n {
                    Ok(0) | Err(_) => eo = false,
                    Ok(n) => append(&job, false, &eb[..n]),
                },
                st = child.wait(), if exit.is_none() => {
                    exit = Some(st.ok().map(|s| {
                        use std::os::unix::process::ExitStatusExt;
                        s.code().unwrap_or_else(|| -s.signal().unwrap_or(0))
                    }));
                    grace.as_mut().reset(tokio::time::Instant::now() + EXIT_GRACE);
                },
                _ = &mut grace, if exit.is_some() && !marked => {},
            }
        }
        job.state.lock().expect("job lock").lingering = false;
    });
    Ok(id)
}

/// R567: how long output may still arrive after the job's shell exited before the job reads as done.
const EXIT_GRACE: std::time::Duration = std::time::Duration::from_millis(300);

fn mark_done(job: &Job, code: Option<i32>, lingering: bool) {
    let mut st = job.state.lock().expect("job lock");
    st.done = true;
    st.code = code;
    st.lingering = lingering;
}

fn append(job: &Job, stdout: bool, chunk: &[u8]) {
    let mut st = job.state.lock().expect("job lock");
    let full = if stdout { st.out.len() >= COMMAND_OUTPUT_CAP } else { st.err.len() >= COMMAND_OUTPUT_CAP };
    if full {
        st.truncated = true;
    } else if stdout {
        st.out.extend_from_slice(chunk);
    } else {
        st.err.extend_from_slice(chunk);
    }
}

/// The exit hook: a background job must not outlive the process that started it.
pub fn kill_all() {
    let jobs: Vec<Arc<Job>> = registry().lock().expect("jobs lock").jobs.values().cloned().collect();
    for j in jobs {
        let st = j.state.lock().expect("job lock");
        if !st.done || st.lingering {
            kill_group(j.pgid);
        }
    }
}

fn output(job: &Job) -> String {
    let st = job.state.lock().expect("job lock");
    let mut s = String::from_utf8_lossy(&st.out).into_owned() + &String::from_utf8_lossy(&st.err);
    if st.truncated {
        s.push_str(&format!("\n[output truncated at {COMMAND_OUTPUT_CAP} bytes — the job kept running; redirect to a file if you need all of it]"));
    }
    s
}

fn find(id: &str) -> Option<Arc<Job>> {
    registry().lock().expect("jobs lock").jobs.get(id).cloned()
}

pub struct CheckCommand;

#[async_trait]
impl Tool for CheckCommand {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "check_command".into(),
            description: "Poll a job started by run_command(..., background=True) — its status and recent output. With no job_id, lists every known background job. Read-only, no approval needed.".into(),
            parameters: json!({"type": "object", "properties": {
                "job_id": {"type": "string", "description": "id returned by run_command's background=True (optional — omit to list all jobs)"},
                "tail": {"type": "integer", "description": "max characters of output to return, most-recent-first (default 4000)"}},
                "required": []}),
        }
    }
    fn needs_approval(&self, _: &Args) -> bool {
        false
    }
    async fn run(&self, args: &Args, _c: &CancellationToken) -> String {
        let id = arg_str(args, "job_id");
        if id.is_empty() {
            let jobs: Vec<(String, Arc<Job>)> = registry().lock().expect("jobs lock").jobs.iter().map(|(k, j)| (k.clone(), j.clone())).collect();
            if jobs.is_empty() {
                return "[no background jobs]".into();
            }
            // Python sorts the ids as strings: bg10 comes before bg2
            return jobs
                .iter()
                .map(|(k, j)| {
                    let st = j.state.lock().expect("job lock");
                    let status = if st.done { format!("exit {}", st.code.unwrap_or(0)) } else { format!("running {:.0}s", j.started.elapsed().as_secs_f64()) };
                    format!("{k}: {status} — {}", j.command)
                })
                .collect::<Vec<_>>()
                .join("\n");
        }
        let Some(job) = find(id) else { return format!("[error: no such background job: {id}]") };
        let out = output(&job);
        let limit = match arg_int(args, "tail") {
            Ok(0) | Err(()) => 4000,
            Ok(n) => n.max(200) as usize,
        };
        let out = if char_len(&out) > limit { format!("[…{} earlier bytes omitted…]\n{}", char_len(&out) - limit, tail(&out, limit)) } else { out };
        let st = job.state.lock().expect("job lock");
        let status = if st.done { format!("[exit {}]", st.code.unwrap_or(0)) } else { "[still running]".to_string() };
        let body = out.trim();
        format!("{status}\n{}", if body.is_empty() { "[no output yet]" } else { body })
    }
}

pub struct CancelCommand;

#[async_trait]
impl Tool for CancelCommand {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "cancel_command".into(),
            description: "Kill a job started by run_command(..., background=True) (asks approval). It keeps running until this is called, a check_command call finding it already exited, or the Aurora process itself exits.".into(),
            parameters: json!({"type": "object", "properties": {
                "job_id": {"type": "string", "description": "id returned by run_command's background=True"}},
                "required": ["job_id"]}),
        }
    }
    fn needs_approval(&self, _: &Args) -> bool {
        true
    }
    fn preview(&self, args: &Args) -> String {
        format!("cancel background job {}", arg_str(args, "job_id"))
    }
    async fn run(&self, args: &Args, _c: &CancellationToken) -> String {
        let id = arg_str(args, "job_id");
        let Some(job) = find(id) else { return format!("[error: no such background job: {id}]") };
        {
            let st = job.state.lock().expect("job lock");
            if st.done && !st.lingering {
                return format!("[{id}] already finished (exit {}) — nothing to cancel", st.code.unwrap_or(0));
            }
        }
        kill_group(job.pgid);
        format!("[{id}] kill signal sent")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    // the registry is process-wide, so the tests that count live jobs must not overlap
    static ONE_AT_A_TIME: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

    fn args(v: serde_json::Value) -> Args {
        v.as_object().unwrap().clone()
    }

    async fn check(id: &str) -> String {
        CheckCommand.run(&args(json!({"job_id": id})), &CancellationToken::new()).await
    }

    async fn wait_done(id: &str) -> String {
        for _ in 0..100 {
            let r = check(id).await;
            if !r.starts_with("[still running]") {
                return r;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        panic!("job {id} never finished");
    }

    #[tokio::test]
    async fn a_job_runs_on_after_the_call_returns_and_check_shows_its_output() {
        let _g = ONE_AT_A_TIME.lock().await;
        let id = start("echo out; echo err >&2; sleep 0.3; exit 3", None, None).unwrap();
        assert!(id.starts_with("bg"));
        assert!(check(&id).await.starts_with("[still running]"));
        assert_eq!(wait_done(&id).await, "[exit 3]\nout\nerr");
        let all = CheckCommand.run(&args(json!({})), &CancellationToken::new()).await;
        assert!(all.contains(&format!("{id}: exit 3 — echo out; echo err >&2; sleep 0.3; exit 3")), "{all}");
        assert_eq!(check("bg9999").await, "[error: no such background job: bg9999]");
    }

    #[tokio::test]
    async fn cancel_kills_the_whole_group_and_finished_jobs_say_so() {
        let _g = ONE_AT_A_TIME.lock().await;
        let id = start("sleep 30 & sleep 30", None, None).unwrap();
        let c = CancelCommand.run(&args(json!({"job_id": id})), &CancellationToken::new()).await;
        assert_eq!(c, format!("[{id}] kill signal sent"));
        let done = wait_done(&id).await;
        assert!(done.starts_with("[exit -9]"), "{done}");
        let again = CancelCommand.run(&args(json!({"job_id": id})), &CancellationToken::new()).await;
        assert_eq!(again, format!("[{id}] already finished (exit -9) — nothing to cancel"));
    }

    #[tokio::test]
    async fn tail_keeps_only_the_end_and_the_live_cap_is_enforced() {
        let _g = ONE_AT_A_TIME.lock().await;
        let id = start("seq 1 2000", None, None).unwrap();
        wait_done(&id).await;
        let r = CheckCommand.run(&args(json!({"job_id": id, "tail": 10})), &CancellationToken::new()).await;
        assert!(r.starts_with("[exit 0]\n[…") && r.contains(" earlier bytes omitted…]\n") && r.ends_with("2000"), "{r}");
        let ids: Vec<String> = (0..MAX_BACKGROUND_JOBS).map(|_| start("sleep 30", None, None)).filter_map(Result::ok).collect();
        assert_eq!(ids.len(), MAX_BACKGROUND_JOBS);
        let over = start("true", None, None);
        for i in &ids {
            CancelCommand.run(&args(json!({"job_id": i})), &CancellationToken::new()).await;
        }
        for i in &ids {
            wait_done(i).await; // the slots are free again before the next test counts them
        }
        let e = over.expect_err("a fifth live job must be refused");
        assert!(e.starts_with("[error: 4 background jobs already running"), "{e}");
    }

    #[tokio::test]
    async fn a_job_is_done_when_its_shell_exits_even_if_a_child_keeps_the_pipe() {
        // R567
        let _g = ONE_AT_A_TIME.lock().await;
        let id = start("sleep 20 & echo started", None, None).unwrap();
        assert_eq!(wait_done(&id).await, "[exit 0]\nstarted");
        let c = CancelCommand.run(&args(json!({"job_id": id})), &CancellationToken::new()).await;
        assert_eq!(c, format!("[{id}] kill signal sent"), "the lingering child can still be stopped");
        for _ in 0..100 {
            if !find(&id).unwrap().state.lock().unwrap().lingering {
                return;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        panic!("the child was not killed");
    }
}
