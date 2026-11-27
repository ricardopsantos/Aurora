# Aurora nightly (N5)

A systemd user timer on the test machine runs `aurora-nightly.sh` every night at 03:30. The script:
1. pulls `aurora-2`;
2. builds `aurora-rs`;
3. runs clippy and the Rust tests;
4. runs the full Python suite on the Rust loop (the default), whose `tests/test_rust_*.py` compare both cores on the same fixtures, then again with `AURORA_CORE=python` so the Python loop stays tested;
5. runs the live local-model tests (R502) when they are enabled.

Each night writes one log to `~/.cache/aurora-nightly/YYYY-MM-DD.log`, which ends `result: PASS` or `result: FAIL`. A failed night also shows up in `systemctl --user --failed`.

Install, or reinstall after a change:

```sh
ln -sf ~/repositories/Aurora/tools/nightly/aurora-nightly.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now aurora-nightly.timer
```

To enable the live tests, create `~/.config/aurora-nightly.env` (mode 600) with:

```sh
AURORA_LIVE=1
AURORA_LIVE_URL=https://<local llama.cpp or Ollama>/v1
LLAMA_API_KEY=...
```

`LLAMA_API_KEY` is only needed if the local server asks for one. Use local models only, never a paid key.
