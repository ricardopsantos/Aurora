# Deliberate differences: Rust core vs Python Aurora (review note N3)

The differential tests (`tests/test_rust_*.py`) compare the Rust core with Python
for equality. Anything they leave out on purpose is listed here, with the test that
pins it down. A difference that isn't on this list is a bug: the tests compare whole
values (records, request bodies, tool outputs, tool specs) so that such a difference
fails them. A new entry needs an `R<n>` and the test that checks the Rust side.

| # | Where | Python | Rust | Why | Pinned by |
|---|---|---|---|---|---|
| D1 | A skill that is a non-executable `.py` file (R493) | runs with Aurora's own interpreter (`sys.executable`) | runs with `python3` | the Rust binary has no interpreter of its own | `test_rust_skills_r493.py` |
| D2 | Output an extension prints during a call (R495) | goes to Aurora's stdout | goes to stderr (the protocol owns the host's stdout) | `aurora-ext/1` keeps stdout for JSON-RPC | `test_rust_ext_r495.py` (results compared, stray output excluded) |
| D3 | Cancelling a running extension call (R495) | runs in process; whether a call stops depends on the extension | the extension host is stopped; extension tools stay unavailable for the rest of the run | `aurora-ext/1` v1 has no cancel method; a stuck call must not hold the turn | `aurora-tools::ext` (code path), protocol doc "Not in version 1" |
| D4 | The interpreter extensions run in (R495) | Aurora's own | `AURORA_PYTHON`, default `python3` | same as D1 | `test_rust_ext_r495.py` sets it |
| D5 | A YAML 1.1 word (`yes`/`no`/`on`/`off`) written both quoted and unquoted in one config file (R498) | each occurrence by its quoting | all of them stay strings | `serde_yaml_ng` drops the quoting style; a word quoted anywhere is kept a string everywhere, the safe reading | `aurora-storage` `yaml11_words_are_booleans_unless_quoted` |
| D6 | The system prompt text (R480) | names Python's cwd and home | the same facts, same template | compared only by template: every run has its own temp folder | `test_rust_compat_r482.py`, `test_rust_providers_r497.py` |
| D8 | The `e` (explain) answer at a gate with `runtime.core: rust` (R499) | asks the model to explain the call, then asks again | says explanations are not available on the Rust core and asks again | not in aurora-core/1 v1 | `aurora/rustcore.py` `_answer_approve` |
| D9 | A malformed tool call whose one corrective retry then fails with a provider error (R5, R143a) | the `ProviderError` escapes `Engine.send` to the front end | reported like any provider error (the notice), the turn ends | a crash path is not worth copying; history is cleaned the same way on both | `test_core.py::test_an_exception_escaping_run_turn_still_pops_the_user_message` |
| D7 | Interactive commands (`/rewind`, `/undo`, `/compact`, `/name`, …) | in the TUI and the web page | resolved (R499, R500): Python's front ends keep them and drive the Rust loop; Rust's data is in Python's formats | — | `test_rust_rewind_r494.py` (Python's undo on Rust's data) |

Not differences: the sandbox, the denylist, secret scanning and the approval rules
are checked case by case against Python's own functions (`test_rust_policy_r483.py`,
`test_rust_secrets_r484.py`, `test_rust_compat_r482.py`). Where the Rust core lacks
a safety feature it asks or refuses (master plan rule 4); that is a missing feature
listed in `03-FEATURE-PARITY.md`, not an entry here.
