# Aurora — Keys

Full detail behind the [README](../README.md)'s quick-start.

Secrets, all stored via `aurora key set` (env var → OS keyring →
encrypted file — never plaintext on disk):

| Key | What it unlocks | Where to get it |
|---|---|---|
| `LLAMA_API_KEY` | your local llama.cpp server, if it requires one | wherever you configured it (leave unset if your server needs no key) |
| `OPENROUTER_API_KEY` | OpenRouter models (paid) | [openrouter.ai/keys](https://openrouter.ai/keys) |

```bash
aurora key set                    # LLAMA_API_KEY (default)
aurora key set OPENROUTER_API_KEY
aurora key status                 # is a key set, and where from? (no ENV_VAR = every key this config uses)
aurora key clear [ENV_VAR]        # remove a stored key (--all for every one configured)
aurora wipe                       # delete AURORA_HOME — logs out of every provider, resets sessions/allowlist/state
```

If you'd rather not type a key in, `config.yaml`'s `key_fetch:` block lets
`aurora key set` run a command of your choosing (e.g. an `ssh` to wherever
the value lives) and store its output — approve with `y` and it's stored
without copy-pasting. See the commented example in `config.yaml`.

No OpenRouter key? Aurora still works — only the paid remote models are
unusable; pick your local model with `/model`.
