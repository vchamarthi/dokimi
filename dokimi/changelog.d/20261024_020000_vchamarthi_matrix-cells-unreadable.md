### Fixed

- **`arms run` and `arms matrix-run` exit 1 with a message on an unreadable
  `--matrix-cells` file** — a directory or other unreadable path no longer ends
  in a Python traceback, and a file that is not JSON or not UTF-8 now names the
  file in the `Error:` line.
