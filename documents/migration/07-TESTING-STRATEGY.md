# Testing strategy

1. **Unit tests in each crate** (`cargo test`). The provider is tested against a
   scripted HTTP server that sends chunked SSE, fragmented tool calls, errors,
   429s and cut streams. Tools run against real files and real processes.
2. **Python as the oracle** (`tests/test_rust_compat_*.py`, part of the normal
   pytest run, skipped when cargo is missing).
   - The same scripted model drives `python -m aurora -t` and `aurora-rs -t`.
   - The tests compare the session records, the requests the model received,
     the files changed, stdout and the exit code.
   - Python's own readers open what Rust wrote.
   - Fixtures made by Python's code (such as `keys.enc`) are opened by Rust.
3. **Parity table** (`tests/test_migration_parity_r478.py`): every requirement
   from R1 to the highest has a row, and the table is regenerated whenever the
   statuses change.
4. **Gates before any commit:** the full Python suite, `cargo test`, and
   `cargo clippy --all-targets` with no warnings.

Live checks against real model servers are manual and never run in the suite.
