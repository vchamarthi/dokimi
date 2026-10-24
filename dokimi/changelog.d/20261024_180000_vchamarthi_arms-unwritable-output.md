### Fixed

- **`arms run`, `arms matrix-run` and `arms plugin-delta` exit 1 with a message
  when an output cannot be written** — an `--out-json` or `--out-md` that is
  blocked by a file, or a Markdown path that is a directory, now prints
  `Error: cannot write <path>: ...` instead of a Python traceback.
