### Fixed

- **`questions generate` exits 1 with a message on an unreadable `--personas` or
  `--topics` file** — a missing or malformed file, or a personas file that is not
  a JSON object with a `personas` list, now prints `Error: ...` naming the file
  instead of ending in a Python traceback.
