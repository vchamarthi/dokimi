### Fixed

- **`run` and `compare` exit 2 on a bad input file** — a missing, malformed or
  invalid `--spec`, or a `compare` snapshot that is missing, not valid JSON or
  UTF-8, or lacks its `summary`, now prints `Error: ...` naming the file and
  exits 2 (bad input; 1 stays the `run --strict` gate verdict) instead of
  ending in a Python traceback.
