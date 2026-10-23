### Fixed

- **`novelty analyze` and `novelty probe` exit with a message on an unreadable
  `--placebo-text`** — a missing file, a directory or a non-UTF-8 file now stops
  with `cannot read --placebo-text <path>: <reason>` before any model client is
  built, instead of a traceback.
