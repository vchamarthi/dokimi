### Fixed

- **`tasks run`** prints `error: cannot write <path>: <reason>` and exits 1 when
  `--out-json` or `--out-md` cannot be written (a parent that is a file, a read-only
  directory), instead of ending in a Python traceback. When only the report fails, the
  message names the saved artifact.
