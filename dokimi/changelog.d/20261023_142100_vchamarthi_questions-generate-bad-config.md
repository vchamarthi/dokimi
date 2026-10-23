### Fixed

- **`questions generate`** exits 1 with `Error: invalid YAML in <file>` or
  `Error: expected a mapping in <file>` when the runtime products config is not
  valid YAML or not a mapping, as `answers generate` already did, instead of a
  Python traceback.
