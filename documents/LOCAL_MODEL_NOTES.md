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
  the actually-loaded gguf + its real context size.
- This repo's committed `config.yaml` points its `openrouter:` provider at a
  gateway (not `https://openrouter.ai/api/v1` directly) that fronts both the
  local server and real OpenRouter, routing per-request on the `model`
  field — one `base_url` list, one key (`LLAMA_API_KEY`). If your local
  server has no such gateway, split it back into two provider entries and
  set `OPENROUTER_API_KEY` too.
