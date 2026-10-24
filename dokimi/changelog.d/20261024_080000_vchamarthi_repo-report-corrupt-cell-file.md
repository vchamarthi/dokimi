### Fixed

- **`repo report` and `repo compare` exit 1 with a message on a corrupt batch
  file** — a `result.json`, `unsupported.json` or `_eval/plan.json` that is
  unreadable, not JSON, not UTF-8 or not a JSON object now prints
  `✗ <path> ...` instead of a Python traceback.
