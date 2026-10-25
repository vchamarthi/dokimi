### Fixed

- **A task tagged with a single string, or a product key in mixed case, counts as work in the coverage matrix** —
  `tags = "onednn"` in a task's `[metadata]` was read as the letters `o`, `n`, `e`, ...
  and matched no product, and a registry key such as `oneDAL` never matched the
  lowercased task tags. Both now resolve, so `coverage_status: covered` is no longer
  reported as a missing work half for them.
