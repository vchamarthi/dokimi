### Fixed

- **`catalog trigger-set` exits 1 with a message when `--out` cannot be written** —
  an `--out` that is a directory, sits under a file, or is not writable now
  prints `Error: cannot write --out <path>: ...` instead of a Python traceback,
  for every `--format`.
