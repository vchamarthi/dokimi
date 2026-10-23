### Fixed

- **`repo scan` exits 1 with a message on a `--static` or `--manifest-file` that is not UTF-8** —
  a binary or differently encoded file now prints `✗ ...` and exits 1, like a missing or
  malformed one, instead of ending in a Python traceback.
