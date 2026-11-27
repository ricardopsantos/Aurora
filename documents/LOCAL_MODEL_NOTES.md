# Notes on local models

The detail behind the quick start in the [README](../README.md), for anyone
running a local llama.cpp or Ollama server.

- Aurora starts on the last model you used on this machine (saved in
  `AURORA_HOME/state.yaml`). The first time, the terminal starts with no
  model and asks you to pick one with `/model` (R578); piped input, `-t` and
  `--web` use the first entry under `models:`, which in
  `config.yaml.example` is the local model.
- `model: local` means whatever model your local server has loaded right now.
- Every request shows a timed row in the chat (`✻ thinking… Ns`, then
  `thought for Ns`), even for models that don't produce reasoning. When there
  is reasoning, click the row (`▸ thought for Ns — click to read`) to open it
  in place, at any time later. This is TUI only. `/thinking` (or
  `runtime.show_thinking: true`) shows reasoning live as it's generated, and
  `/copy-last` includes it. Reasoning never goes into the history, `/copy` or
  exports.
- To turn off a Qwen model's thinking mode, add
  `extra_body: {chat_template_kwargs: {enable_thinking: false}}` to its model
  entry. Long silent thinking can feel like a hang.
- To check the server, run `/status`. For llama.cpp it shows the gguf that's
  actually loaded and its real context size. For Ollama (`type: ollama`) it
  shows whether the server answers and the configured model's context window
  from `/api/show`. It can't name one loaded model, because Ollama can keep
  several loaded at once.
- A server that unloaded its model to save memory shows as "asleep". Aurora
  tells you before the first reply, which is slow while the model loads.
- A model served with a vision projector (`--mmproj`) can see pictures
  without `vision: true` in the config.
- `base_url` can be one URL or a list. Aurora tries each in order and uses
  the first that answers, so one config can work at home (LAN address) and
  away (VPN name).
- One gateway in front of both the local server and OpenRouter also works,
  with no change to Aurora: one `base_url`, one key, routed by the `model`
  field. Point `openrouter:`'s `base_url` at the gateway and drop the second
  key.
- For settings that apply to one machine only, put them in
  `AURORA_HOME/config.local.yaml`. It's merged over `config.yaml`, so you
  don't have to change the shared file.
