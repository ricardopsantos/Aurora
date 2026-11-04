# Aurora keys

The full detail behind the quick start in the [README](../README.md).

Store secrets with `aurora key set`. Aurora looks for them in an environment
variable, then the OS keyring, then an encrypted file. It never stores them
on disk in plain text.

| Key | What it unlocks | Where to get it |
|---|---|---|
| `LLAMA_API_KEY` | Your local llama.cpp server, if it needs a key | Wherever you set it up. Leave it unset if your server needs no key. |
| (none) | Ollama (`type: ollama` in `config.yaml`) | No key needed. Leave out `api_key_env`. |
| `OPENROUTER_API_KEY` | OpenRouter models (paid) | [openrouter.ai/keys](https://openrouter.ai/keys) |

```bash
aurora key set                    # LLAMA_API_KEY (default)
aurora key set OPENROUTER_API_KEY
aurora key status                 # is a key set, and where from? (no ENV_VAR = every key this config uses)
aurora key clear [ENV_VAR]        # remove a stored key (--all for every one configured)
aurora wipe                       # delete AURORA_HOME: logs out of every provider, resets sessions/allowlist/state
```

If you'd rather not type a key, the `key_fetch:` block in `config.yaml` lets
`aurora key set` run a command you choose (for example an `ssh` to wherever
the value lives) and store its output. Approve with `y` and it's stored
without copying and pasting. See the commented example in
`config.yaml.example`.

With the encrypted-file store, a wrong passphrase while adding a key stores
nothing. Aurora says `not stored — the existing key store could not be
decrypted` and leaves the store as it was. Run the command again with the
right passphrase. An empty passphrase is refused, and a new one is asked for
twice.

Without an OpenRouter key Aurora still works, but you can't use the paid
remote models; pick your local model with `/model`.
