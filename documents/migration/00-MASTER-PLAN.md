# Aurora 2.0: master plan

Aurora 2.0 is the same agent with a Rust core: one fast binary, a smaller
attack surface, and a core other front ends can drive. The Python app keeps
working, unchanged, the whole time. Nothing is switched over until the Rust
version passes the same checks.

## Rules

1. **Python is the oracle.** Every Rust behaviour is checked against the Python
   app on the same input (`tests/test_rust_compat_*.py`). The Rust core can't be
   called done from its own tests alone.
2. **Shared data, no migration step.** Rust reads and writes AURORA_HOME in
   Python's formats (`05-DATA-COMPATIBILITY.md`), so a user can switch back and
   forth at any time.
3. **Same requirement discipline.** Each change has an `R<n>` entry, tests and a
   commit that names it. `03-FEATURE-PARITY.md` tracks every requirement.
4. **Missing features are safe gaps.** Where the Rust core lacks a safety
   feature, it asks or refuses rather than allowing.

## Phases

| Phase | Content | State |
|---|---|---|
| 0 | Baseline (`MIGRATION_BASELINE.md`), parity table, data formats | done (R478) |
| 1 | Workspace, contracts in `aurora-core`, agent loop | done (R478) |
| 2 | Headless milestone: config, keys, sessions, OpenAI-compatible provider, file and command tools, `aurora-rs -t` | done (R479–R482) |
| 3 | Safety parity: allow/deny rule matcher (R483), secret scanning (R484), sandbox on Linux (R485), background jobs (R486) | done (macOS sandbox: N1, done) |
| 4 | Context: bootstrap prompt storage and trust (R488), compaction (R492), resume (R487), skills (R493), checkpoints (R494). Python has no separate notes or memory engine feature. | done (the commands that use them come with the front ends) |
| 5 | Remaining tools: grep, find_files (R489), apply_patch (R490), wait_until (R491); web, MCP and user extensions through the aurora-ext/1 host client (R495) | done |
| 6 | Front ends: Python's front ends drive the Rust loop over aurora-core/1: TUI, REPL, headless (R499) and the web page (R500). Native Rust front ends are not needed for the switch-over: Python keeps the screen, Rust the agent | done (Python front ends on the Rust loop) |
| 7 | Switch-over: the Rust loop is the default behind Python's front ends (R508); `runtime.core: python` keeps the Python loop for a release; the suite and the nightly run on both | done (R508) |

## Decisions so far

- The binary stays `aurora-rs` after phase 7: `aurora` is still the Python
  front end, which starts `aurora-rs --core` for each session.
- R508 checked on both platforms (2026-11-26): the suite passes on each loop
  on Linux and on macOS (macOS: 2489 passed, 36 skipped; see
  `HANDOFF-BUILD-HOST.md`), and `install.sh` builds the Rust core.
- Async runtime: tokio. HTTP: reqwest with rustls. YAML: serde_yaml_ng.
- Extensions stay in Python behind `aurora-ext/1` (R477) instead of being
  rewritten.

## Open review notes (2026-11-25)

- **N1 macOS parity** (done 2026-11-26, macOS 27.2.1): port `sandbox-exec`, and run the workspace and compat suite on macOS during phase 4, not at the end.
- **N2 real providers** (done, R502: opt-in, local models only; set AURORA_LIVE_URL to run): opt-in live tests against OpenRouter, Ollama and llama.cpp (streaming, retries, cancel, fragmented tool calls).
- **N3 deliberate differences** (done, R498: `06-DELIBERATE-DIFFERENCES.md`): list every place the Rust policy differs from Python on purpose; undocumented differences must fail the differential tests.
- **N4 coverage** (done, R496: Compat test column, enforced by `build_parity.py --check`): a Rust compatibility suite per requirement, shown in the parity table; test counts are not parity.
- **N5 nightly** (done, R503: systemd user timer, 03:30): a nightly run of the same fixtures against Python and Rust.
- **Milestone before the UI phase**: config load, Python session resume, multi-step task, all essential tools, approval/deny/cancel, secrets, sandbox, background jobs, MCP and extensions, session records the Python tools can read. Reached on Linux (2026-11-26): every item is done, MCP and extensions through R495; macOS done (N1, 2026-11-26).
