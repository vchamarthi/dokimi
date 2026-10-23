### Fixed

- **`novelty probe`**, **`novelty shorten`** and **`novelty view`** exit with
  `Error: cannot read atoms from <file>: ...` when the `--atoms` file is missing, not valid
  JSON, not UTF-8, a directory or not an atom set, instead of a Python traceback.
