### Fixed

- **`novelty analyze` and `novelty probe`** print `Error: --placebo-text <path>: <reason>` and
  exit 2 when the placebo file is missing, a directory or not UTF-8, instead of a Python
  traceback. The file is now read before any model client is built, so `novelty probe` no
  longer pays for paraphrase generation and then fails on a typo in the path. The read goes
  through a new `read_text_input` helper next to `read_json_input`.
