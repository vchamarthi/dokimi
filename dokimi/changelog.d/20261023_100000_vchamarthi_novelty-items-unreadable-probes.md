### Fixed

- **`novelty items` exits with a message on an unreadable `--probes` file** — a
  missing file, malformed JSON or bytes that are not UTF-8 now stop with
  `<file>: <reason>` instead of a Python traceback.
