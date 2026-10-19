### Fixed

- **A judge flag under another name follows its provider** — `eval panel-score
  --provider`, `eval ragas --ragas-provider`, and `answers generate
  --ragas-provider`, with the model flag left out, now pick the default judge
  that runs on that provider (`claude-sonnet-5-5` on `anthropic` and
  `amazon-bedrock`, `gpt-6.1-sol` on `openai`). A provider with no default
  (`google-vertex`, `openrouter`, `openai-codex` on `eval panel-score`) exits 2
  and asks for the model flag, before any judge call. Naming the model yourself
  is unchanged. Before, a left-out model was `claude-sonnet-5-5` on every
  provider, so `--provider openai` sent `openai/claude-sonnet-5-5` and failed
  at the first call.
