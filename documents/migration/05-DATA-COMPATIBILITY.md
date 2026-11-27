# Data compatibility

Both versions use the same AURORA_HOME. These formats are fixed. Changing one
needs a requirement and a test on both sides.

## AURORA_HOME

`$AURORA_HOME`, else the path written in `~/.aurora-path`, else `~/.aurora`.
Group and other permissions are removed on use (R450).

## config.yaml

YAML. `${VAR}` is replaced from the environment; an unset variable becomes
empty. `$AURORA_HOME/config.local.yaml` is laid over it: maps merge key by key,
and anything else, lists included, replaces. Empty `providers`, `models`,
`runtime` and `skills` sections become empty containers. `_path` and `_base_dir`
are added.

**Known difference:** PyYAML reads YAML 1.1, where `yes`, `no`, `on` and `off`
are booleans. serde_yaml_ng reads YAML 1.2, where they are strings. Write
`true` and `false`.

## Session JSONL (`sessions/<id>.jsonl`)

One JSON object per line, written as
`json.dumps({"ts": ..., "event": ..., **fields}, ensure_ascii=False, separators=(", ", ": "))`.
The spacing matters: Python's reader looks for the literal `"event": "<name>"`
before parsing. Ids are 12 hex digits. `ts` is `datetime.now(UTC).isoformat()`.
Each append holds an exclusive `flock`. Past 5 MiB a new part `<id>.N.jsonl`
starts.

| Event | Fields |
|---|---|
| `user` | `text`, `model` |
| `assistant` | `text`, `model`, `input_tokens`, `output_tokens`, `billed_input`, `cached_input`, `reasoning_tokens`, `reasoning_chars`, `latency_s`, `degraded`, `extra_body_keys` |
| `tool` | `name`, `args` (strings clipped to 4000), `output` (first 4000), `chars`, `status` (`ok`, `error`, `skipped`) |
| `approval` | `tool`, `decision` (R133c words), `detail` (optional) |

## Keys

The environment wins. Then the OS keyring (service `aurora-agent`; not in Rust
yet). Then `keys.enc`: a Fernet token over a JSON object of name → value, with
the key derived by PBKDF2-HMAC-SHA256, 600 000 rounds, and a 16-byte salt in
`keys.salt`.

## Tool-call arguments in history

The arguments are re-serialised as Python's `json.dumps(args)`: `", "` / `": "`
separators and ASCII escapes. Both versions then send the same bytes.

## Approval rules

`allowlist.yaml` and `denylist.yaml` map a tool name to a list of rules (command
prefixes for `run_command` and `wait_until`, path globs for the file tools,
`scheme://host/*` origins for `web_fetch`, `*` for any other tool). Rust matches
them exactly as `approve.py` does and writes the same rule text for "always".
