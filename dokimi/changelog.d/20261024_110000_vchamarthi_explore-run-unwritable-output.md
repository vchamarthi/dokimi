### Fixed

- **`explore run` exits 1 with a message when `--out-json` or `--out-md` cannot be
  written** — a path under a regular file or a directory without write permission now
  prints `Error: cannot write <path>` instead of a Python traceback.
