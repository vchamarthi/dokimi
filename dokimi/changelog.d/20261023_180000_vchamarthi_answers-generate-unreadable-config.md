### Fixed

- **`answers generate`** exits 1 with `Error: cannot read <config>: ...` when the
  runtime config file is not UTF-8 or cannot be read, instead of a Python traceback.
