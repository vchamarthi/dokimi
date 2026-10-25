### Fixed

- **`tools preference` exits 1 with a message when `--items-out` cannot be
  written** — a path under a regular file, or a directory, now prints
  `Error: cannot write --items-out ...` instead of a Python traceback.
