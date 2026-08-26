# Aurora — Local Model Notes

Detail behind the [README](../README.md)'s quick-start, for anyone running
a local llama.cpp server.

- Aurora **starts on the last model you used** (per-machine
  `AURORA_HOME/state.yaml`), falling back to the first `models:` entry —
  the free local model.
- Qwen's *thinking mode* is **disabled by default** for the local model
  (`extra_body: chat_template_kwargs: {enable_thinking: false}` in
  `config.yaml`) — it made replies feel like a silent hang. Remove those two
  lines to get reasoning back.
- Every request shows a timed row in the chat (`✻ thinking… Ns`, then
  `thought for Ns`) — even for models that emit no reasoning. With thinking
  enabled the row is clickable (`▸ thought for Ns — click to read`) and
  expands to the full reasoning **in place, any time later** — TUI only, no
  separate command needed. `/thinking` (or `runtime.show_thinking: true`)
  streams it live/expanded as it's generated; `/copy-last` includes it.
  Reasoning never enters the history, `/copy`, or exports.
- Not sure the server is up? `/status` asks llama-server directly and shows
  the actually-loaded gguf + its real context size. For an Ollama provider
  (`type: ollama`) it reports reachability and the configured model's context
  window from `/api/show`, but not a "currently resident model" — Ollama can
  hold several at once, so there is no single answer to report (R223).
- This repo's committed `config.yaml` keeps **two separate providers**:
  `openrouter:` pointing straight at `https://openrouter.ai/api/v1` with
  `OPENROUTER_API_KEY`, and `local:` with `LLAMA_API_KEY`. So both keys are
  in play — see [KEYS.md](KEYS.md). `local:`'s `base_url` is a **list**
  (Tailscale name first, LAN IP second); Aurora tries each in order and uses
  the first that answers, which is what makes the same config work on and
  off the home network.
- A single gateway fronting both the local server and OpenRouter — one
  `base_url`, one key, routed per-request on the `model` field — also works,
  and Aurora needs no change for it: point `openrouter:`'s `base_url` at the
  gateway and drop the second key. Just don't assume the committed config is
  already set up that way.
