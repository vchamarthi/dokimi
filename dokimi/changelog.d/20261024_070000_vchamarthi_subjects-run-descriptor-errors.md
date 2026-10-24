### Fixed

- **`subjects run`** exits 1 with `Error: ...` when the descriptor cannot run
  (`subject.id` or a product resolving to `.`/`..`, products that collide after
  name sanitization, a `suite.questions` count that matches neither one file nor
  one per product, or duplicate arm names), instead of a Python traceback. These
  checks now run before the output directory is created.
