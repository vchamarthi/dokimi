### Fixed

- **`report model-compare` exits with a message on a run file that is not UTF-8 or
  cannot be read** — a directory, a non-UTF-8 file or another read error in
  `--regular-runs` / `--golden-runs` now prints `Error: ...` naming the file
  instead of a Python traceback.
