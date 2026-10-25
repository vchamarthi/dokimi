### Fixed

- **`novelty analyze` and `novelty probe`** exit with `cannot read --placebo-text <path>: <reason>`
  when the placebo file is missing, a directory or not UTF-8, instead of a Python traceback.
  The file is now read before any model client is built, so `novelty probe` no longer pays
  for paraphrase generation and then fails on a typo in the path.
