### Fixed

- **`eval grounding` and `eval judge-validity` exit 1 with a message when
  `--output` cannot be written** — an `--output` below a regular file, or one
  that is a directory, now prints `Error: cannot write <path>: ...` instead of
  a Python traceback.
