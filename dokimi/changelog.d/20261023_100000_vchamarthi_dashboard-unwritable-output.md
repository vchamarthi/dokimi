### Fixed

- **`dashboard generate`** exits 1 with
  `Error: cannot write the dashboard to <dir>: ...` when `--output-dir` is a file
  or cannot be written, instead of a Python traceback.
