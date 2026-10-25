### Fixed

- **`report generate` exits 1 with a message when `--output` cannot be
  written** — an output path that is a directory, or whose parent is a file,
  now prints `Error: cannot write <path>: ...` instead of a Python traceback.
