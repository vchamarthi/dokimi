### Fixed

- **`novelty view` exits 1 with a message on an unreadable `--novelty` or
  `--atoms` file** — a missing path, a file that is not JSON or not UTF-8, a
  `--novelty` file that is not a novelty artifact, or an `--atoms` file that is
  not an atoms record now prints `Error: ...` naming the file instead of a
  Python traceback.
